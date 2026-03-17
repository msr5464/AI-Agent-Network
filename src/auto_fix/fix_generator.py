"""
Generates code fixes using AI for identified issues.
"""

import logging
import re
import json
from typing import Dict, Optional, List

try:
    from langchain_openai import ChatOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    from langchain_ollama import ChatOllama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False

try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# Import from parent package
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.agent.analyzer import FailureClassification
from .models import FixProposal, AdditionalChange

logger = logging.getLogger(__name__)


class FixGenerator:
    """Generates code fixes using AI"""
    
    def __init__(self, llm_provider: str = "openai", openai_api_key: str = None,
                 openai_model: str = "gpt-4o-mini", ollama_model: str = "llama3.2:3b",
                 ollama_base_url: str = "http://localhost:11434",
                 gemini_api_key: str = None, gemini_model: str = "gemini-1.5-flash"):
        """
        Initialize fix generator with LLM.

        Args:
            llm_provider: LLM provider ("openai", "ollama", or "gemini")
            openai_api_key: OpenAI API key (required if provider is "openai")
            openai_model: OpenAI model name
            ollama_model: Ollama model name
            ollama_base_url: Ollama base URL
            gemini_api_key: Gemini API key (required if provider is "gemini")
            gemini_model: Gemini model name
        """
        self.llm_provider = llm_provider.lower()

        if self.llm_provider == "openai":
            if not OPENAI_AVAILABLE:
                raise ImportError("langchain-openai not installed. Run: pip install langchain-openai")
            self.llm = self._init_openai(openai_api_key, openai_model)
        elif self.llm_provider == "gemini":
            if not GEMINI_AVAILABLE:
                raise ImportError("langchain-google-genai not installed. Run: pip install langchain-google-genai")
            self.llm = self._init_gemini(gemini_api_key, gemini_model)
        else:
            if not OLLAMA_AVAILABLE:
                raise ImportError("langchain-ollama not installed. Run: pip install langchain-ollama")
            self.llm = self._init_ollama(ollama_model, ollama_base_url)

        logger.info(f"Fix generator initialized with {self.llm_provider}")
    
    def _init_openai(self, api_key: str, model: str):
        """Initialize OpenAI LLM"""
        if not api_key:
            raise ValueError("OpenAI API key is required")
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            temperature=0.1  # Lower temperature for more deterministic fixes
        )
    
    def _init_ollama(self, model: str, base_url: str):
        """Initialize Ollama LLM"""
        return ChatOllama(
            model=model,
            base_url=base_url,
            temperature=0.1
        )

    def _init_gemini(self, api_key: str, model: str):
        """Initialize Google Gemini LLM"""
        if not api_key:
            raise ValueError("Gemini API key is required")
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=api_key,
            temperature=0.1
        )

    def generate_fix(
        self,
        classification: FailureClassification,
        test_code: str,
        context: Dict
    ) -> Optional[FixProposal]:
        """
        Generate a code fix for the failure.
        
        Args:
            classification: Failure classification with root cause
            test_code: Current test method code
            context: File context (imports, class name, etc.)
            
        Returns:
            FixProposal object or None if fix cannot be generated
        """
        prompt = self._build_fix_prompt(classification, test_code, context)
        
        try:
            logger.info(f"Generating fix for: {classification.test_name}")
            response = self.llm.invoke(prompt)
            
            # Parse response
            fix_proposal = self._parse_fix_response(
                response.content,
                test_code,
                context.get('file_path', '')
            )
            
            if fix_proposal:
                logger.info(f"Generated fix with {fix_proposal.confidence} confidence")
            
            return fix_proposal
            
        except Exception as e:
            logger.error(f"Failed to generate fix: {e}")
            return None
    
    def _build_fix_prompt(
        self,
        classification: FailureClassification,
        test_code: str,
        context: Dict
    ) -> str:
        """Build the prompt for fix generation"""
        
        classification_type = classification.classification
        root_cause = classification.root_cause
        recommended_action = classification.recommended_action
        test_data_refs = context.get('test_data_refs', [])
        test_data_section = ", ".join(test_data_refs) if test_data_refs else "None"
        stack_frames = context.get('stack_frames', [])
        stack_section = self._format_stack_frames(stack_frames)
        stack_file_snippets = context.get('stack_file_snippets', [])
        stack_files_section = self._format_stack_file_snippets(stack_file_snippets)
        page_objects = context.get('page_objects', [])
        page_objects_section = self._format_page_objects(page_objects)
        discovered_locators = context.get('discovered_locators', [])
        discovered_locators_section = self._format_discovered_locators(discovered_locators)
        
        prompt = f"""You are an expert automation test engineer. Your task is to fix a failing test case.

**Classification**: {classification_type}
**Root Cause**: {root_cause}
**Recommended Action**: {recommended_action}

**Current Test Code**:
```java
{test_code}
```

**File Context**:
- Package: {context.get('package', 'N/A')}
- Class: {context.get('class_name', 'N/A')}
- Imports: {', '.join(context.get('imports', [])[:10])}

**Test Data Keys Referenced**: {test_data_section}

**Related Components & Helpers**:
{self._format_related_files(context.get('related_files', []))}

**Stack Trace Context (file:line with snippet)**:
{stack_section}

**Stack File Snippets (from stack frames)**:
{stack_files_section}

**Page Objects (for ELEMENT_NOT_FOUND/TIMEOUT issues)**:
{page_objects_section}

**Discovered Locators (from browser inspection)**:
{discovered_locators_section}

**Instructions**:
1. Analyze the root cause and recommended action
2. If "Discovered Locators" are provided above, these are NEW locators found from browser inspection. Use the HIGH confidence candidates to update the page object locators.
3. Outline a concise `plan_summary` (1-3 bullet points) describing which files will change and why
4. Generate the COMPLETE fixed test method code
5. Ensure the fix addresses the specific issue mentioned
6. Maintain the same method signature and structure
7. Add comments explaining the changes if needed
8. If helper/data/config files also require updates, describe each one in `additional_changes` rather than editing unrelated tests.
9. You MUST include the exact original method signature line unchanged in `fixed_code`. Do not rename the method or alter its parameters/annotations.
10. For ELEMENT_NOT_FOUND/TIMEOUT issues: If discovered locators are provided, update the page object file with the new locator in `additional_changes`.

**Output Format**:
```json
{{
  "plan_summary": [
    "Step explanation 1",
    "Step explanation 2"
  ],
  "fixed_code": "<complete fixed method code>",
  "explanation": "<brief explanation of what was changed and why>",
  "confidence": "<HIGH|MEDIUM|LOW>",
  "additional_changes": [
    {{
      "file_path": "relative/path/to/file.java",
      "original_snippet": "<exact existing code>",
      "updated_snippet": "<new code>"
    }}
  ]
}}
```

Generate the fix now:"""
        
        return prompt

    def _format_stack_frames(self, frames: List[Dict[str, str]]) -> str:
        if not frames:
            return "None"
        parts = []
        for frame in frames[:10]:
            path = frame.get("file_path", "unknown")
            line_no = frame.get("line_no", "N/A")
            snippet = frame.get("snippet", "").strip()
            parts.append(f"{path}:{line_no}\n```java\n{snippet}\n```")
        return "\n\n".join(parts)

    def _format_related_files(self, related_files: List[Dict[str, str]]) -> str:
        if not related_files:
            return "None"
        
        sections = []
        for related in related_files:
            snippet = related.get("snippet", "").strip()
            path = related.get("path", "unknown")
            sections.append(f"File: {path}\n```java\n{snippet}\n```")
        return "\n\n".join(sections[:5])

    def _format_stack_file_snippets(self, stack_files: List[Dict[str, str]]) -> str:
        if not stack_files:
            return "None"
        parts = []
        for item in stack_files[:5]:
            path = item.get("path", "unknown")
            line_no = item.get("line", "N/A")
            snippet = item.get("snippet", "").strip()
            parts.append(f"{path}:{line_no}\n```java\n{snippet}\n```")
        return "\n\n".join(parts)
    
    def _format_page_objects(self, page_objects: List[Dict[str, str]]) -> str:
        """Format page object files for the prompt"""
        if not page_objects:
            return "None"
        
        sections = []
        for po in page_objects:
            path = po.get("path", "unknown")
            matches = po.get("element_matches", [])
            snippet = po.get("snippet", "").strip()
            match_info = f" (matches: {', '.join(matches[:3])})" if matches else ""
            sections.append(f"File: {path}{match_info}\n```java\n{snippet}\n```")
        return "\n\n".join(sections)
    
    def _format_discovered_locators(self, discovered_locators: List[Dict[str, str]]) -> str:
        """Format discovered locators from browser inspection for the prompt"""
        if not discovered_locators:
            return "None (no locators discovered from browser)"
        
        sections = []
        for i, loc in enumerate(discovered_locators[:5], 1):  # Top 5 candidates
            loc_type = loc.get('type', 'unknown')
            loc_value = loc.get('value', '')
            confidence = loc.get('confidence', 'LOW')
            element_text = loc.get('element_text', '')
            
            # Format based on locator type for Java/Selenium
            if loc_type == 'id':
                java_example = f"@FindBy(id = \"{loc_value}\")"
            elif loc_type == 'name':
                java_example = f"@FindBy(name = \"{loc_value}\")"
            elif loc_type == 'css':
                java_example = f"@FindBy(css = \"{loc_value}\")"
            elif loc_type == 'xpath':
                java_example = f"@FindBy(xpath = \"{loc_value}\")"
            else:
                java_example = f"@FindBy({loc_type} = \"{loc_value}\")"
            
            text_info = f" (text: '{element_text}')" if element_text else ""
            sections.append(
                f"Candidate #{i} (confidence: {confidence}){text_info}:\n"
                f"  {java_example}\n"
                f"  private WebElement elementName;"
            )
        
        return "\n\n".join(sections) if sections else "None"
    
    def _parse_fix_response(
        self,
        response: str,
        original_code: str,
        file_path: str
    ) -> Optional[FixProposal]:
        """Parse AI response into FixProposal"""
        json_payload = self._extract_json_payload(response)
        data = self._try_load_json(json_payload) if json_payload else None

        if data is None:
            data = self._try_load_json(response)

        if data is None:
            data = self._parse_relaxed_response(response)

        if data is None:
            logger.error("Failed to parse fix response after all fallbacks")
            return None

        fixed_code = (data.get('fixed_code') or '').strip()
        if not fixed_code:
            logger.error("LLM response missing `fixed_code` field")
            return None

        # Ensure original signature is present; if missing, try to wrap candidate body.
        if not self._contains_original_signature(original_code, fixed_code):
            wrapped = self._wrap_with_original_signature(original_code, fixed_code)
            if wrapped:
                logger.warning("Generated fix missing signature; wrapped with original method signature.")
                fixed_code = wrapped
            else:
                logger.error("Generated fix does not include the original method signature. Rejecting response.")
                return None

        plan_summary = []
        raw_plan = data.get('plan_summary') or data.get('plan')
        if isinstance(raw_plan, list):
            plan_summary = [str(item) for item in raw_plan][:10]
        elif isinstance(raw_plan, str):
            plan_summary = [raw_plan]

        additional_changes = []
        for change in data.get('additional_changes', []):
            if not isinstance(change, dict):
                continue
            file_path = change.get('file_path')
            original_snippet = change.get('original_snippet')
            updated_snippet = change.get('updated_snippet')
            if file_path and original_snippet and updated_snippet:
                additional_changes.append(
                    AdditionalChange(
                        file_path=file_path.strip(),
                        original_snippet=original_snippet,
                        updated_snippet=updated_snippet
                    )
                )

        return FixProposal(
            original_code=original_code,
            fixed_code=fixed_code,
            explanation=data.get('explanation', ''),
            confidence=data.get('confidence', 'MEDIUM'),
            file_path=file_path,
            plan_summary=plan_summary,
            additional_changes=additional_changes
        )
    
    def validate_fix_syntax(self, fix_code: str) -> bool:
        """
        Validate that the fix has valid Java syntax (basic check).
        
        Args:
            fix_code: Fixed code to validate
            
        Returns:
            True if syntax appears valid
        """
        # Basic syntax checks
        checks = [
            '{' in fix_code and '}' in fix_code,  # Has braces
            fix_code.count('{') == fix_code.count('}'),  # Balanced braces
            '@Test' in fix_code or 'test' in fix_code.lower(),  # Looks like a test
        ]
        
        return all(checks)

    def _wrap_with_original_signature(self, original_code: str, candidate_code: str) -> Optional[str]:
        """
        If the candidate code is missing the method signature, wrap its body with
        the original method signature from the source. Best-effort; returns None if
        the signature cannot be found.
        """
        if not original_code or not candidate_code:
            return None

        header_match = re.search(r'([ \t]*(?:public|protected|private)[^{]*\{)', original_code)
        if not header_match:
            return None

        header = header_match.group(1).rstrip()
        base_indent_match = re.match(r'[ \t]*', header)
        base_indent = base_indent_match.group(0) if base_indent_match else ""
        body_indent = base_indent + "    "

        # Strip outer braces if the candidate already includes them
        body = candidate_code.strip()
        if body.startswith("{") and body.endswith("}"):
            body = body[1:-1].strip()

        body_lines = body.splitlines() or [body]
        indented_body = "\n".join(
            f"{body_indent}{line}" if line.strip() else ""
            for line in body_lines
        )

        wrapped = f"{header}\n{indented_body}\n{base_indent}}}"
        return wrapped

    def _extract_json_payload(self, response: str) -> Optional[str]:
        """Extract a JSON block from the LLM response."""
        if not response:
            return None

        fence_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
        if fence_match:
            return fence_match.group(1)

        start = response.find('{')
        end = response.rfind('}')
        if start != -1 and end != -1 and end > start:
            return response[start:end + 1]
        return None

    def _try_load_json(self, payload: Optional[str]) -> Optional[Dict]:
        """Attempt to load JSON from the payload with light sanitization."""
        if not payload:
            return None

        cleaned = payload.strip()
        cleaned = cleaned.strip('`')

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.warning(f"Strict JSON parsing failed: {exc}")
            return None
        except Exception:
            return None

    def _parse_relaxed_response(self, response: str) -> Optional[Dict[str, str]]:
        """
        Heuristic parser for responses that look like JSON but contain
        unescaped quotes or other minor issues.
        """
        if not response:
            return None

        fixed_code = self._extract_section(response, "fixed_code", ["explanation", "confidence"])
        explanation = self._extract_section(response, "explanation", ["confidence"])
        confidence = self._extract_section(response, "confidence", [])

        if not fixed_code and not explanation and not confidence:
            return None

        normalized_confidence = (confidence or "MEDIUM").strip().upper()
        if normalized_confidence not in {"HIGH", "MEDIUM", "LOW"}:
            match = re.search(r'(HIGH|MEDIUM|LOW)', normalized_confidence, re.IGNORECASE)
            normalized_confidence = match.group(1).upper() if match else "MEDIUM"

        return {
            "fixed_code": fixed_code or "",
            "explanation": explanation or "",
            "confidence": normalized_confidence
        }

    def _extract_section(self, text: str, key: str, next_keys: Optional[List[str]]) -> Optional[str]:
        """Extract value between key and the nearest next key marker."""
        pattern = rf'"?{re.escape(key)}"?\s*:\s*'
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            return None

        start = match.end()

        end_positions = []
        for marker in next_keys or []:
            idx = re.search(rf'"?{re.escape(marker)}"?\s*:', text[start:], re.IGNORECASE)
            if idx:
                end_positions.append(start + idx.start())

        end = min(end_positions) if end_positions else len(text)
        raw_value = text[start:end].strip()

        if raw_value.startswith('```'):
            raw_value = raw_value.lstrip('`').rstrip('`')
            if '\n' in raw_value:
                first_line, remainder = raw_value.split('\n', 1)
                if re.match(r'^[A-Za-z]+\s*$', first_line.strip()):
                    raw_value = remainder

        raw_value = raw_value.rstrip(',').strip()
        if raw_value.startswith('"') and raw_value.endswith('"'):
            raw_value = raw_value[1:-1]

        return raw_value or None

    def _contains_original_signature(self, original_code: str, fixed_code: str) -> bool:
        """Ensure the generated snippet still contains the method signature + annotation block."""
        original_block = self._extract_signature_block(original_code)
        candidate_block = self._extract_signature_block(fixed_code)
        if not original_block or not candidate_block:
            return False
        return self._normalize_signature(original_block) == self._normalize_signature(candidate_block)

    def _extract_signature_block(self, code: str) -> Optional[str]:
        if not code:
            return None
        lines = code.strip().splitlines()
        block = []
        for line in lines:
            stripped = line.rstrip()
            if not stripped and not block:
                continue
            block.append(stripped)
            if stripped.endswith('{'):
                break
        return "\n".join(block).strip() if block else None

    def _normalize_signature(self, text: str) -> str:
        return re.sub(r'\s+', ' ', text.strip())
