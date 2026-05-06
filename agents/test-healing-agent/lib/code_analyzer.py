"""
Analyzes test code to locate files and extract context for fixing.
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)


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
        # Extract class name from test name
        parts = test_name.split('.')
        if len(parts) < 2:
            logger.error(f"Invalid test name format: {test_name}")
            return None
        
        class_name = parts[-2]  # Second to last is the class name
        method_name = parts[-1]  # Last is the method name
        
        logger.info(f"Searching for class: {class_name}, method: {method_name}")
        
        # Search for Java files containing the class
        repo_path = Path(repo_path)
        for java_file in repo_path.rglob("*.java"):
            try:
                content = java_file.read_text(encoding='utf-8')
                # Look for class definition (with optional access modifiers and extends/implements)
                # Pattern: (public|private|protected)? class ClassName (extends|implements)? ... {
                class_pattern = rf'(public|private|protected)?\s*class\s+{class_name}\s*(<[^>]+>)?\s*(extends|implements)?'
                if re.search(class_pattern, content):
                    # Verify method exists
                    if re.search(rf'@Test.*?{method_name}\s*\(', content, re.DOTALL):
                        relative_path = java_file.relative_to(repo_path)
                        logger.info(f"Found test file: {relative_path}")
                        return str(relative_path)
            except Exception as e:
                logger.debug(f"Error reading {java_file}: {e}")
                continue
        
        logger.warning(f"Test file not found for: {test_name}")
        return None
    
    def extract_test_method(self, file_path: str, method_name: str) -> Optional[str]:
        """
        Extract the test method code from the file.
        
        Args:
            file_path: Path to the Java file
            method_name: Name of the test method
            
        Returns:
            Method code as string or None if not found
        """
        try:
            content = Path(file_path).read_text(encoding='utf-8')
            
            # Find method using regex (simplified - may need enhancement for complex cases)
            # Pattern matches: @Test annotation, method signature, and body with nested braces
            pattern = rf'(@Test.*?)\s+(public|private|protected)?\s+\w+\s+{method_name}\s*\([^)]*\)\s*{{([^}}]*(?:{{[^}}]*}}[^}}]*)*)}}'
            match = re.search(pattern, content, re.DOTALL)
            
            if match:
                method_code = match.group(0)
                logger.info(f"Extracted method: {method_name} ({len(method_code)} chars)")
                return method_code
            else:
                logger.warning(f"Method not found: {method_name}")
                return None
                
        except Exception as e:
            logger.error(f"Error extracting method: {e}")
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
        methods = re.findall(r'(?:public|private|protected)?\s+\w+\s+(\w+)\s*\([^)]*\)\s*{', content)
        return methods
    
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
        """
        related: List[Dict[str, str]] = []
        try:
            content = Path(file_path).read_text(encoding='utf-8')
        except Exception:
            return related
        
        imports = self._extract_imports(content)
        repo = Path(repo_path)
        seen_paths = set()
        
        for imp in imports:
            if not imp.startswith("Automation."):
                continue
            
            candidate_rel = Path(*imp.split('.'))
            java_path = candidate_rel.with_suffix('.java')
            search_paths = [
                repo / 'src' / 'test' / 'java' / java_path,
                repo / 'src' / 'main' / 'java' / java_path
            ]
            
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

    def _extract_relevant_block(self, content: str, max_chars: int) -> str:
        if len(content) <= max_chars:
            return content

        # Prefer entire methods
        method_pattern = re.compile(r'(?:public|protected|private)\s+\w[\w<>]*\s+\w+\s*\([^)]*\)\s*\{', re.MULTILINE)
        blocks = []
        for match in method_pattern.finditer(content):
            start = match.start()
            depth = 0
            end = start
            for idx in range(start, len(content)):
                if content[idx] == '{':
                    depth += 1
                elif content[idx] == '}':
                    depth -= 1
                    if depth == 0:
                        end = idx + 1
                        break
            blocks.append(content[start:end])
            if sum(len(b) for b in blocks) >= max_chars:
                break
        if not blocks:
            return content[:max_chars]
        snippet = "\n\n".join(blocks)
        return snippet[:max_chars]

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
        
        # Pattern 3: FindBy annotations in stack traces (e.g., "DashPeopleDetailsPage.blockReasonPopUpHeader")
        pattern3 = re.compile(r"([A-Za-z][\w]*Page)\.([a-z][\w]*)", re.IGNORECASE)
        for match in pattern3.finditer(combined_text):
            page_name = match.group(1)
            field_name = match.group(2)
            element_names.append(f"{page_name}:{field_name}")
            element_names.append(field_name)
        
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
    
    def find_page_objects_for_locators(
        self,
        repo_path: str,
        element_names: List[str],
        max_files: int = 3,
        max_chars_per_file: int = 2000
    ) -> List[Dict[str, str]]:
        """
        Search for page object files containing the specified element names/locators.
        
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
        page_objects = []
        seen_paths = set()
        
        # Common page object directories
        search_dirs = [
            repo / 'src' / 'main' / 'java' / 'Automation' / 'Access',
            repo / 'src' / 'main' / 'java' / 'Automation' / 'Spend',
            repo / 'src' / 'main' / 'java' / 'Automation' / 'Treasury',
        ]
        
        # Search in all Java files under these directories
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            
            for java_file in search_dir.rglob("*.java"):
                if str(java_file) in seen_paths:
                    continue
                
                try:
                    content = java_file.read_text(encoding='utf-8', errors='ignore')
                    matches = []
                    
                    # Check if any element name appears in the file
                    for elem_name in element_names:
                        # Check for field declarations with this name
                        # Pattern: private WebElement elementName; or @FindBy ... elementName
                        field_pattern = rf'(?:private|protected|public)\s+(?:WebElement|MobileElement)\s+{re.escape(elem_name.split(":")[-1])}\s*[;=]'
                        if re.search(field_pattern, content, re.IGNORECASE):
                            matches.append(elem_name)
                            break
                        
                        # Check for @FindBy annotations with element description
                        if ':' in elem_name:
                            page_part, elem_part = elem_name.split(':', 1)
                            # Check if page class name matches
                            if re.search(rf'class\s+{re.escape(page_part)}', content, re.IGNORECASE):
                                # Check for element name in comments or @FindBy
                                if re.search(rf'@FindBy.*?{re.escape(elem_part)}', content, re.IGNORECASE) or \
                                   re.search(rf'//.*?{re.escape(elem_part)}', content, re.IGNORECASE):
                                    matches.append(elem_name)
                                    break
                        
                        # Check if element name appears in comments or strings
                        if elem_name.lower() in content.lower():
                            # More specific: check if it's in a meaningful context
                            if re.search(rf'["\']{re.escape(elem_name)}["\']', content, re.IGNORECASE) or \
                               re.search(rf'//.*?{re.escape(elem_name)}', content, re.IGNORECASE):
                                matches.append(elem_name)
                                break
                    
                    if matches:
                        relative_path = java_file.relative_to(repo)
                        snippet = self._extract_relevant_block(content, max_chars_per_file)
                        page_objects.append({
                            'path': str(relative_path),
                            'element_matches': matches,
                            'snippet': snippet
                        })
                        seen_paths.add(str(java_file))
                        
                        if len(page_objects) >= max_files:
                            break
                            
                except Exception as e:
                    logger.debug(f"Error reading {java_file}: {e}")
                    continue
            
            if len(page_objects) >= max_files:
                break
        
        logger.info(f"Found {len(page_objects)} page object files for locators")
        return page_objects