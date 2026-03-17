"""
Builds rich context packs for the fix generator, similar to how IDEs surface
related helpers, page objects, and data files.
"""

import re
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

from .code_analyzer import CodeAnalyzer

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Collects repository context for a target test method."""

    def __init__(self, code_analyzer: CodeAnalyzer):
        self.code_analyzer = code_analyzer

    def collect(
        self,
        repo_path: str,
        test_file: str,
        method_name: str,
        stack_frames: List[Dict[str, Any]] = None,
        root_cause: str = "",
        execution_log: str = "",
        root_cause_category: str = ""
    ) -> Dict[str, Any]:
        file_path = Path(repo_path) / test_file
        file_context = self.code_analyzer.get_file_context(str(file_path), method_name)
        related_files = self.code_analyzer.get_related_files(repo_path, str(file_path), max_chars=4000)
        test_data_refs = self._extract_test_data_refs(file_path.read_text(encoding="utf-8"))
        helper_methods = self._extract_helper_methods(file_path.read_text(encoding="utf-8"), method_name)

        file_context["related_files"] = related_files
        file_context["test_data_refs"] = test_data_refs
        file_context["helper_methods"] = helper_methods
        if stack_frames:
            file_context["stack_frames"] = stack_frames[:10]
        
        # Locator-based page object lookup for ELEMENT_NOT_FOUND/TIMEOUT
        if root_cause_category in ['ELEMENT_NOT_FOUND', 'TIMEOUT']:
            element_names = self.code_analyzer.extract_element_names(
                root_cause, execution_log, root_cause_category
            )
            if element_names:
                page_objects = self.code_analyzer.find_page_objects_for_locators(
                    repo_path, element_names, max_files=3, max_chars_per_file=2000
                )
                if page_objects:
                    file_context["page_objects"] = page_objects
                    logger.info(f"Found {len(page_objects)} page object files for locators: {[po['path'] for po in page_objects]}")
        
        return file_context

    def _extract_test_data_refs(self, content: str) -> List[str]:
        """Grab keys referenced via testConfig.testData or runtime properties."""
        refs = set()
        patterns = [
            r'testData\.get\("([^"]+)"\)',
            r'putRunTimeProperty\("([^"]+)"',
            r'getRunTimeProperty\("([^"]+)"'
        ]
        for pattern in patterns:
            for match in re.findall(pattern, content):
                refs.add(match)
        return sorted(refs)[:20]

    def _extract_helper_methods(self, content: str, target_method: str) -> List[str]:
        """Return signatures of nearby helper methods for context."""
        method_pattern = re.compile(
            r'(?:public|protected|private)\s+\w[\w<>]*\s+(\w+)\s*\([^)]*\)\s*\{',
            re.MULTILINE
        )
        helpers: List[str] = []
        for match in method_pattern.finditer(content):
            name = match.group(1)
            if name == target_method:
                continue
            signature = content[match.start():content.find("{", match.start())].strip()
            helpers.append(signature)
        return helpers[:10]

