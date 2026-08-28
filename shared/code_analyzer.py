"""
Analyzes test code to locate files and extract context for fixing.

The prompt built from this module is the only thing the fix step shows Claude, so
the guiding rule here is: never drop a locator declaration. Page objects are
trimmed to fit a character budget, but fields, annotations and constructors —
where locators actually live — are kept in full and only *methods* are dropped.
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Build output and vendor trees: never source worth reading, and rglob over them
# on a large repo is the difference between a 200ms and a 20s context build.
_SKIP_DIRS = {".git", "node_modules", "target", "build", "out", "bin",
              ".gradle", ".idea", "test-output", "allure-results", "__pycache__"}

# Path fragments that suggest a file declares page objects / locators.
_PAGE_HINTS = ("page", "web", "screen", "locator", "element", "component")

_CLASS_DECL_RE = re.compile(r'\b(?:class|interface|enum|record)\s+(\w+)')

# Roots to search for page objects when PAGE_OBJECT_DIRS is not set. Ordered
# most- to least-specific; the first that exists wins.
_DEFAULT_SOURCE_ROOTS = (
    "src/main/java", "src/main/kotlin", "src/test/java", "src/test/kotlin",
    "src", "tests", "test", "e2e",
)


# ── Per-run caches ────────────────────────────────────────────────────────────
#
# A build with 30 failures used to walk and re-read the entire source tree 30
# times over — once per test, for the test-file search and again for the page
# object search. The tree does not change while contexts are being built, so it
# is walked once and read once. `invalidate_file` is called after a fix is
# applied so later work sees the edited file, not the cached original.

_FILE_TEXT_CACHE: Dict[str, str] = {}
_SOURCE_FILES_CACHE: Dict[str, List[Path]] = {}
_TEST_FILE_CACHE: Dict[str, Optional[str]] = {}
_MAX_CACHED_FILE_BYTES = 1_000_000


def invalidate_file(path) -> None:
    """Drop one file from the read cache after it has been modified on disk."""
    _FILE_TEXT_CACHE.pop(str(Path(path).resolve()), None)


def without_comments(text: str) -> str:
    """Drop comments, keeping string literals intact.

    `// auto-wire retry on every @Test` must not read as a test method — but
    blanking literals as well would empty `description = "..."`, which is the
    single most useful thing the picker shows. So strings are stepped over
    rather than removed: a `//` inside one is still not a comment.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            i = n if j == -1 else j
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            out.append(" ")
            i = n if j == -1 else j + 2
            continue
        if ch in '"\'':
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(text[i:j])       # literal preserved verbatim
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def reset_caches() -> None:
    """Clear every per-run cache. Call between workspaces, not between tests."""
    _FILE_TEXT_CACHE.clear()
    _SOURCE_FILES_CACHE.clear()
    _TEST_FILE_CACHE.clear()


def read_source(path: Path) -> str:
    """Read a source file, caching the text for the rest of the run."""
    key = str(Path(path).resolve())
    cached = _FILE_TEXT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        if path.stat().st_size > _MAX_CACHED_FILE_BYTES:
            return path.read_text(encoding="utf-8", errors="ignore")
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.debug(f"Error reading {path}: {e}")
        return ""
    _FILE_TEXT_CACHE[key] = text
    return text


# ── Source tree helpers ───────────────────────────────────────────────────────

def _iter_source_files(root: Path, suffixes=(".java", ".kt")):
    """Yield source files under root, skipping build and vendor directories."""
    key = f"{root}|{','.join(suffixes)}"
    cached = _SOURCE_FILES_CACHE.get(key)
    if cached is not None:
        yield from cached
        return

    found: List[Path] = []
    for suffix in suffixes:
        for path in root.rglob(f"*{suffix}"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.is_file():
                found.append(path)
    _SOURCE_FILES_CACHE[key] = found
    yield from found


def source_roots(repo_path: str) -> List[Path]:
    """Resolve the directories to search for source files in this repo.

    Honours PAGE_OBJECT_DIRS (comma-separated, relative to the repo root) so a
    project with an unusual layout can point the agent at the right place
    without a code change. Falls back to the conventional Maven/Gradle roots,
    then to the repo root itself.
    """
    repo = Path(repo_path)
    configured = os.environ.get("PAGE_OBJECT_DIRS", "")
    if configured:
        roots = [repo / p.strip() for p in configured.split(",") if p.strip()]
        roots = [r for r in roots if r.exists()]
        if roots:
            return roots
        logger.warning("PAGE_OBJECT_DIRS set but none of those paths exist under %s", repo)

    roots = [repo / rel for rel in _DEFAULT_SOURCE_ROOTS]
    roots = [r for r in roots if r.exists()]
    if not roots:
        return [repo]

    # Keep only the most specific roots: src/main/java makes a bare src
    # redundant in both directions, and walking both visits every file twice.
    deduped: List[Path] = []
    for root in roots:
        if any(_is_within(root, kept) or _is_within(kept, root) for kept in deduped):
            continue
        deduped.append(root)
    return deduped


def _is_within(child: Path, parent: Path) -> bool:
    """True when child is parent or sits underneath it."""
    return child == parent or str(child).startswith(str(parent) + os.sep)


# ── Brace-aware Java scanning ─────────────────────────────────────────────────
#
# Regex cannot match balanced braces, and the previous single-level-nesting
# pattern silently truncated any method containing a nested block. These helpers
# walk the text instead, skipping comments and string/char literals so a brace
# inside "}" or // } never unbalances the count.

def _skip_noise(content: str, i: int, n: int) -> Optional[int]:
    """If content[i] starts a comment or literal, return the index just past it."""
    ch = content[i]
    if ch == '/' and i + 1 < n:
        if content[i + 1] == '/':
            j = content.find('\n', i)
            return n if j == -1 else j + 1
        if content[i + 1] == '*':
            j = content.find('*/', i + 2)
            return n if j == -1 else j + 2
    if ch in '"\'':
        # Java text blocks (\"\"\") are rare in page objects; treated as an
        # empty string here, which at worst keeps a little extra text.
        quote = ch
        j = i + 1
        while j < n:
            if content[j] == '\\':
                j += 2
                continue
            if content[j] == quote:
                return j + 1
            j += 1
        return n
    return None


def _match_brace(content: str, open_index: int) -> int:
    """Index just past the brace closing content[open_index] ('{'), or len()."""
    depth = 0
    i, n = open_index, len(content)
    while i < n:
        skipped = _skip_noise(content, i, n)
        if skipped is not None:
            i = skipped
            continue
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _class_body_span(content: str) -> Optional[Tuple[int, int, str]]:
    """Return (body_start, body_end, class_name) for the outermost type."""
    match = _CLASS_DECL_RE.search(content)
    if not match:
        return None
    brace = content.find('{', match.end())
    if brace == -1:
        return None
    return brace + 1, _match_brace(content, brace) - 1, match.group(1)


def _strip_annotations(text: str) -> str:
    """Remove annotations and their arguments, keeping the declaration itself."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == '@':
            i = _skip_annotation(text, i, n)
            out.append(' ')
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _skip_annotation(content: str, at_index: int, end: int) -> int:
    """Index just past an annotation, including any parenthesised arguments.

    Only a '(' immediately following the annotation name opens arguments — a
    bare "@Override\n    public void x()" must not swallow the method signature.
    """
    i = at_index + 1
    while i < end and (content[i].isalnum() or content[i] in "_.$"):
        i += 1

    j = i
    while j < end and content[j] in " \t\n\r":
        j += 1
    if j >= end or content[j] != '(':
        return i

    depth = 0
    while j < end:
        skipped = _skip_noise(content, j, end)
        if skipped is not None:
            j = skipped
            continue
        if content[j] == '(':
            depth += 1
        elif content[j] == ')':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def _classify_member(text: str, class_name: str) -> Dict[str, str]:
    """Label one class member as field / constructor / method."""
    stripped = text.strip()
    # Strip annotations first: their arguments can contain both '{' and ')',
    # so splitting on those before removing them reads the wrong identifier —
    # @FindBy(id = "user") private WebElement usernameField;  →  "user".
    without_annotations = _strip_annotations(stripped)
    head = without_annotations.split('{', 1)[0]
    head_clean = head.strip()
    has_body = '{' in without_annotations

    call = re.search(r'(\w+)\s*\([^()]*\)\s*$', head_clean.rstrip(';').strip())
    if call:
        name = call.group(1)
        if name == class_name:
            kind = "constructor"
        else:
            kind = "method" if has_body else "method_decl"
        return {"kind": kind, "name": name, "text": stripped}

    if has_body and not head_clean:
        return {"kind": "initializer", "name": "", "text": stripped}

    field = re.search(r'(\w+)\s*(?:\[\s*\])?\s*(?:=[^;]*)?;?\s*$',
                      head_clean.rstrip(';').strip() + ";", re.DOTALL)
    return {"kind": "field", "name": field.group(1) if field else "", "text": stripped}


def split_class_members(content: str) -> List[Dict[str, str]]:
    """Split a Java/Kotlin type body into its members, annotations attached.

    A member runs from just after the previous member to either a ';' at class
    level (a field) or a balanced '{...}' (a method, constructor or
    initializer). Leading annotations, javadoc and comments stay attached to the
    member that follows them.
    """
    span = _class_body_span(content)
    if not span:
        return []
    start, end, class_name = span

    members: List[Dict[str, str]] = []
    i = buf_start = start
    while i < end:
        skipped = _skip_noise(content, i, end)
        if skipped is not None:
            i = skipped
            continue
        ch = content[i]
        if ch == '@':
            # An annotation belongs to the member that follows it, and its
            # argument list can contain braces — @Test(groups = {A, B}),
            # @FindAll({@FindBy(..), @FindBy(..)}). Scanning through it naively
            # mistakes that '{' for a member body and tears the annotation off
            # the declaration it describes.
            i = _skip_annotation(content, i, end)
            continue
        if ch == ';':
            members.append(_classify_member(content[buf_start:i + 1], class_name))
            i += 1
            buf_start = i
            continue
        if ch == '{':
            close = min(_match_brace(content, i), end)
            members.append(_classify_member(content[buf_start:close], class_name))
            i = buf_start = close
            continue
        i += 1

    return [m for m in members if m["text"].strip()]


class CodeAnalyzer:
    """Analyzes test code structure and locates test files"""

    def find_test_file(self, test_name: str, repo_path: str) -> Optional[str]:
        """
        Find the test file in the repository based on test name.

        Args:
            test_name: Full test name (e.g., "Automation.Access.AccountOpening.api.TestDashApis.testMethod")
            repo_path: Path to the repository

        Returns:
            Relative path to test file or None if not found
        """
        parts = test_name.split('.')
        if len(parts) < 2:
            logger.error(f"Invalid test name format: {test_name}")
            return None

        class_name = parts[-2]   # Second to last is the class name
        method_name = parts[-1]  # Last is the method name

        cache_key = f"{repo_path}|{test_name}"
        if cache_key in _TEST_FILE_CACHE:
            return _TEST_FILE_CACHE[cache_key]

        logger.info(f"Searching for class: {class_name}, method: {method_name}")

        repo = Path(repo_path)
        class_pattern = re.compile(
            rf'(public|private|protected)?\s*class\s+{re.escape(class_name)}\s*(<[^>]+>)?\s*(extends|implements)?'
        )
        method_pattern = re.compile(rf'@Test.*?{re.escape(method_name)}\s*\(', re.DOTALL)

        for source_file in _iter_source_files(repo):
            content = read_source(source_file)
            if not content:
                continue
            if class_pattern.search(content) and method_pattern.search(content):
                relative_path = source_file.relative_to(repo)
                logger.info(f"Found test file: {relative_path}")
                _TEST_FILE_CACHE[cache_key] = str(relative_path)
                return str(relative_path)

        logger.warning(f"Test file not found for: {test_name}")
        _TEST_FILE_CACHE[cache_key] = None
        return None

    def extract_test_method(self, file_path: str, method_name: str) -> Optional[str]:
        """
        Extract the test method code from the file, including its annotations.

        Uses brace matching rather than a regex, so a method containing nested
        blocks (try/catch inside an if, a lambda, an anonymous class) is
        returned whole instead of being truncated at the first inner '}'.
        """
        try:
            content = Path(file_path).read_text(encoding='utf-8')
        except Exception as e:
            logger.error(f"Error extracting method: {e}")
            return None

        for member in split_class_members(content):
            if member["name"] == method_name and member["kind"] in ("method", "constructor"):
                logger.info(f"Extracted method: {method_name} ({len(member['text'])} chars)")
                return member["text"]

        logger.warning(f"Method not found: {method_name}")
        return None

    def get_file_context(self, file_path: str, method_name: str) -> Dict:
        """
        Get context about the file and method.

        Args:
            file_path: Path to the Java file
            method_name: Name of the test method

        Returns:
            Dictionary with context information
        """
        try:
            content = Path(file_path).read_text(encoding='utf-8')

            context = {
                'file_path': file_path,
                'method_name': method_name,
                'imports': self._extract_imports(content),
                'class_name': self._extract_class_name(content),
                'package': self._extract_package(content),
                'other_methods': self._extract_method_names(content),
                'file_content': content
            }

            return context

        except Exception as e:
            logger.error(f"Error getting file context: {e}")
            return {}

    def _extract_imports(self, content: str) -> List[str]:
        """Extract import statements"""
        imports = re.findall(r'import\s+([^;]+);', content)
        return imports

    def _extract_class_name(self, content: str) -> Optional[str]:
        """Extract class name"""
        match = re.search(r'class\s+(\w+)', content)
        return match.group(1) if match else None

    def _extract_package(self, content: str) -> Optional[str]:
        """Extract package name"""
        match = re.search(r'package\s+([^;]+);', content)
        return match.group(1) if match else None

    def _extract_method_names(self, content: str) -> List[str]:
        """Extract all method names in the file"""
        return [m["name"] for m in split_class_members(content)
                if m["kind"] in ("method", "method_decl") and m["name"]]

    def get_related_files(
        self,
        repo_path: str,
        file_path: str,
        max_files: int = 3,
        max_chars: int = 1200
    ) -> List[Dict[str, str]]:
        """
        Collect related files referenced by imports in the target file.
        Helps the fix generator understand broader repo context.

        Only first-party imports are followed. "First-party" is derived from the
        importing file's own package root rather than hardcoded, so this works
        on any repo layout.
        """
        related: List[Dict[str, str]] = []
        try:
            content = Path(file_path).read_text(encoding='utf-8')
        except Exception:
            return related

        own_package = self._extract_package(content) or ""
        package_root = own_package.split('.')[0] if own_package else ""

        imports = self._extract_imports(content)
        repo = Path(repo_path)
        roots = source_roots(repo_path)
        seen_paths = set()

        for imp in imports:
            imp = imp.strip()
            if not package_root or not imp.startswith(package_root + "."):
                continue

            java_path = Path(*imp.split('.')).with_suffix('.java')
            kt_path = Path(*imp.split('.')).with_suffix('.kt')
            search_paths = [root / p for root in roots for p in (java_path, kt_path)]

            target_file = next((p for p in search_paths if p.exists()), None)
            if not target_file or str(target_file) in seen_paths:
                continue

            try:
                full_text = target_file.read_text(encoding='utf-8')
                snippet = self._extract_relevant_block(full_text, max_chars)
                related.append({
                    "import": imp,
                    "path": str(target_file.relative_to(repo)),
                    "snippet": snippet
                })
                seen_paths.add(str(target_file))
            except Exception:
                continue

            if len(related) >= max_files:
                break

        return related

    def _extract_relevant_block(self, content: str, max_chars: int,
                                focus: Optional[List[str]] = None) -> str:
        """Trim a source file to max_chars without losing its declarations.

        Fields, annotations and constructors are always kept in full — on a page
        object those ARE the locators, and dropping them leaves Claude guessing
        at the very thing it is being asked to fix. Only method bodies are
        dropped to make room, least-relevant first.
        """
        if len(content) <= max_chars:
            return content

        members = split_class_members(content)
        if not members:
            return content[:max_chars]

        header_parts = []
        package_match = re.search(r'^\s*package\s+[^;]+;', content, re.MULTILINE)
        if package_match:
            header_parts.append(package_match.group(0).strip())
        decl_match = _CLASS_DECL_RE.search(content)
        if decl_match:
            line_start = content.rfind('\n', 0, decl_match.start()) + 1
            brace = content.find('{', decl_match.end())
            if brace != -1:
                header_parts.append(content[line_start:brace + 1].strip())
        header = "\n\n".join(header_parts)

        declarations = [m for m in members if m["kind"] in ("field", "constructor", "initializer")]
        methods = [m for m in members if m["kind"] in ("method", "method_decl")]

        # Prefer methods that mention the element we are chasing.
        focus_terms = [term.split(":")[-1].strip().lower()
                       for term in (focus or []) if term and term.strip()]
        if focus_terms:
            methods.sort(
                key=lambda m: sum(1 for t in focus_terms if t in m["text"].lower()),
                reverse=True,
            )

        chunks: List[str] = [header] if header else []
        used = len(header)
        for member in declarations:
            chunks.append(member["text"])
            used += len(member["text"]) + 2

        omitted = 0
        for member in methods:
            if used + len(member["text"]) + 2 > max_chars:
                omitted += 1
                continue
            chunks.append(member["text"])
            used += len(member["text"]) + 2

        if omitted:
            chunks.append(f"// … {omitted} method(s) omitted to fit the context budget. "
                          f"All field/locator declarations above are complete.")

        snippet = "\n\n".join(chunks)
        # Declarations are allowed to overrun max_chars — they are the payload —
        # but guard against a pathological file with thousands of fields.
        hard_cap = max(max_chars * 4, max_chars)
        if len(snippet) > hard_cap:
            snippet = snippet[:hard_cap] + "\n// … truncated"
        return snippet

    def get_fully_qualified_name(self, file_path: str, class_name: str, method_name: str) -> Optional[str]:
        """
        Get fully qualified name for a test method.

        Args:
            file_path: Path to the Java file
            class_name: Name of the class
            method_name: Name of the method

        Returns:
            Fully qualified name (e.g., "com.package.Class.method")
        """
        try:
            content = Path(file_path).read_text(encoding='utf-8')
            package = self._extract_package(content)

            if package:
                return f"{package}.{class_name}.{method_name}"
            else:
                return f"{class_name}.{method_name}"

        except Exception as e:
            logger.error(f"Error getting FQCN: {e}")
            return None

    def extract_element_names(self, root_cause: str, execution_log: str = "", category: str = "") -> List[str]:
        """
        Extract element names/locators from error messages for ELEMENT_NOT_FOUND/TIMEOUT cases.

        Args:
            root_cause: Root cause text from classification
            execution_log: Full execution log
            category: Root cause category (ELEMENT_NOT_FOUND, TIMEOUT, etc.)

        Returns:
            List of extracted element names/locators
        """
        element_names = []
        if category not in ['ELEMENT_NOT_FOUND', 'TIMEOUT']:
            return element_names

        combined_text = f"{root_cause}\n{execution_log}"

        # Pattern 1: "PageName:ElementName" format (most common)
        # Example: "Element 'DashPeopleDetailsPage:Block Reason PopUp Header' is NOT visible"
        pattern1 = re.compile(r"['\"]([A-Za-z][\w]*Page):([A-Za-z][\w\s]+)['\"]", re.IGNORECASE)
        for match in pattern1.finditer(combined_text):
            page_name = match.group(1)
            element_name = match.group(2).strip()
            element_names.append(f"{page_name}:{element_name}")
            element_names.append(element_name)  # Also add just the element name

        # Pattern 2: "Element 'elementName' is NOT visible/clickable"
        pattern2 = re.compile(r"Element\s+['\"]([A-Za-z][\w\s]+)['\"]\s+is\s+NOT", re.IGNORECASE)
        for match in pattern2.finditer(combined_text):
            element_name = match.group(1).strip()
            element_names.append(element_name)

        # Pattern 2b: the Playwright framework's own WaitHelper wording, which
        # carries the element name unquoted:
        #   "Element not visible after timeout: Block Reason PopUp Header"
        pattern2b = re.compile(
            r"Element\s+(?:not|is not)\s+(?:visible|clickable|displayed|present|interactable)"
            r"[^:]*:\s*([A-Za-z][\w \-]{2,60})",
            re.IGNORECASE)
        for match in pattern2b.finditer(combined_text):
            element_names.append(match.group(1).strip())

        # Pattern 3: page-object field references (e.g. "DashPeopleDetailsPage.blockReasonPopUpHeader").
        # A stack trace is full of "ProductsPage.java:19", so source-file suffixes
        # and Java keywords are excluded — otherwise every failure reports an
        # element helpfully named "java", which then matches no page object at all.
        _NOT_FIELDS = {"java", "kt", "class", "this", "new", "init", "super"}
        pattern3 = re.compile(r"([A-Za-z][\w]*Page)\.([a-z][\w]*)", re.IGNORECASE)
        for match in pattern3.finditer(combined_text):
            page_name = match.group(1)
            field_name = match.group(2)
            if field_name.lower() in _NOT_FIELDS:
                continue
            element_names.append(f"{page_name}:{field_name}")
            element_names.append(field_name)

        # Pattern 3b: the framework prints the locator itself
        #   "Failed to load Element Locator@.product-page-heading in ProductsPage"
        pattern3b = re.compile(
            r"Locator@(\S+?)\s+in\s+([A-Za-z][\w]*)|"
            r"Failed to load Element\s+(\S+?)\s+in\s+([A-Za-z][\w]*)")
        for match in pattern3b.finditer(combined_text):
            selector = match.group(1) or match.group(3)
            owner = match.group(2) or match.group(4)
            if selector:
                # Playwright's Locator.toString() is "Locator@<selector>"; the bare
                # selector is what appears in the page object source.
                selector = selector.strip()
                if selector.startswith("Locator@"):
                    selector = selector[len("Locator@"):]
                element_names.append(selector)
            if owner and selector:
                element_names.append(f"{owner}:{selector.strip()}")

        # Pattern 4: Extract from NoSuchElementException or TimeoutException messages
        pattern4 = re.compile(r"(?:NoSuchElementException|TimeoutException).*?['\"]([^'\"]+)['\"]", re.IGNORECASE)
        for match in pattern4.finditer(combined_text):
            locator = match.group(1).strip()
            if len(locator) > 3 and len(locator) < 100:  # Reasonable length
                element_names.append(locator)

        # Deduplicate and return
        unique_elements = []
        seen = set()
        for elem in element_names:
            normalized = elem.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique_elements.append(normalized)

        logger.info(f"Extracted {len(unique_elements)} element names: {unique_elements[:5]}...")
        return unique_elements[:10]  # Limit to top 10

    def _score_element_matches(self, content: str, element_names: List[str]) -> List[str]:
        """Return every element name this file plausibly declares.

        Unlike a first-match search, this collects all of them so callers can
        rank candidate files by how much of the failure they explain.
        """
        matches: List[str] = []
        for elem_name in element_names:
            bare = elem_name.split(":")[-1].strip()
            if not bare:
                continue

            # A declaration of the element: `private WebElement fooHeader;`,
            # `private final Locator fooHeader;`, `Locator fooHeader =`, etc.
            declaration = rf'(?:WebElement|MobileElement|Locator|By|Element)\s+{re.escape(bare)}\s*[;=)]'
            if re.search(declaration, content):
                matches.append(elem_name)
                continue

            if ':' in elem_name:
                page_part, elem_part = elem_name.split(':', 1)
                if re.search(rf'\bclass\s+{re.escape(page_part)}\b', content, re.IGNORECASE):
                    if re.search(rf'@FindBy[^;]*{re.escape(elem_part)}', content, re.IGNORECASE) or \
                       re.search(rf'//.*?{re.escape(elem_part)}', content, re.IGNORECASE):
                        matches.append(elem_name)
                        continue

            # Quoted string or comment mention — weakest signal, still useful.
            if re.search(rf'["\']{re.escape(elem_name)}["\']', content, re.IGNORECASE) or \
               re.search(rf'//.*?{re.escape(elem_name)}', content, re.IGNORECASE):
                matches.append(elem_name)

        return matches

    def find_page_objects_for_locators(
        self,
        repo_path: str,
        element_names: List[str],
        max_files: int = 3,
        max_chars_per_file: int = 2000
    ) -> List[Dict[str, str]]:
        """
        Search for page object files containing the specified element names/locators.

        Search roots are derived from the repo layout (or PAGE_OBJECT_DIRS) rather
        than hardcoded, and every candidate file is scored by how many of the
        failing element names it declares — the best matches are returned first,
        so the file most likely to hold the broken locator is the one Claude sees.

        Args:
            repo_path: Path to repository
            element_names: List of element names to search for
            max_files: Maximum number of page object files to return
            max_chars_per_file: Maximum characters per file snippet

        Returns:
            List of dicts with 'path', 'element_matches', and 'snippet' keys
        """
        if not element_names:
            return []

        repo = Path(repo_path)
        scored: List[Tuple[int, int, Path, List[str], str]] = []
        seen_paths = set()

        for root in source_roots(repo_path):
            for source_file in _iter_source_files(root):
                key = str(source_file.resolve())
                if key in seen_paths:
                    continue
                seen_paths.add(key)

                content = read_source(source_file)
                if not content:
                    continue

                matches = self._score_element_matches(content, element_names)
                if not matches:
                    continue

                # Break ties toward files that look like page objects.
                path_bonus = 1 if any(hint in str(source_file).lower() for hint in _PAGE_HINTS) else 0
                scored.append((len(matches), path_bonus, source_file, matches, content))

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)

        page_objects = []
        for _, _, source_file, matches, content in scored[:max_files]:
            page_objects.append({
                'path': str(source_file.relative_to(repo)),
                'element_matches': matches,
                'snippet': self._extract_relevant_block(content, max_chars_per_file,
                                                        focus=element_names),
            })

        if not page_objects:
            logger.warning(
                "No page object files matched %s under %s — the fix prompt will have no "
                "locator declarations to work from",
                element_names[:3], [str(r) for r in source_roots(repo_path)],
            )
        logger.info(f"Found {len(page_objects)} page object files for locators")
        return page_objects
