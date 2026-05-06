"""
Common utility functions used across the codebase.
Consolidates duplicate code to reduce maintenance burden.
"""

import re
from typing import Optional, Tuple
from pathlib import Path


class TestNameNormalizer:
    """
    Centralized test name normalization utility.
    Provides consistent test name matching across the codebase.
    """
    
    @staticmethod
    def normalize(name: str) -> str:
        """
        Normalize test name (remove duplicates, trim whitespace).
        
        Args:
            name: Test name string
            
        Returns:
            Normalized test name
        """
        if not name:
            return ""
        cleaned = remove_duplicate_class_name(name)
        return cleaned.strip()
    
    @staticmethod
    def match(name1: str, name2: str) -> bool:
        """
        Check if two test names match after normalization.
        
        Args:
            name1: First test name
            name2: Second test name
            
        Returns:
            True if names match, False otherwise
        """
        return TestNameNormalizer.normalize(name1) == TestNameNormalizer.normalize(name2)
    
    @staticmethod
    def find_matching_test(test_name: str, test_results: list) -> Optional[object]:
        """
        Find matching test result from a list using consistent matching logic.
        
        Args:
            test_name: Test name to search for
            test_results: List of TestResult objects
            
        Returns:
            Matching TestResult object or None
        """
        normalized_search = TestNameNormalizer.normalize(test_name)
        
        for result in test_results:
            # Try multiple matching strategies
            result_full_name = getattr(result, 'full_name', '')
            result_class_name = getattr(result, 'class_name', '')
            result_method_name = getattr(result, 'method_name', '')
            
            # Strategy 1: Match normalized full_name
            if TestNameNormalizer.match(test_name, result_full_name):
                return result
            
            # Strategy 2: Match normalized test_name directly
            if TestNameNormalizer.normalize(result_full_name) == normalized_search:
                return result
            
            # Strategy 3: Match by class.method if available
            if result_class_name and result_method_name:
                class_method = f"{remove_duplicate_class_name(result_class_name)}.{result_method_name}"
                if TestNameNormalizer.match(test_name, class_method):
                    return result
        
        return None


class FailureClassificationUtils:
    """
    Utilities for handling FailureClassification objects.
    """

    @staticmethod
    def deduplicate(classifications: list) -> list:
        """
        Deduplicate failure classifications by test name.
        Uses a "highest confidence wins" strategy:
        HIGH > MEDIUM > LOW > UNKNOWN

        Args:
            classifications: List of FailureClassification objects

        Returns:
            Deduplicated list of FailureClassification objects
        """
        if not classifications:
            return []

        seen_tests = {}
        deduplicated = []

        confidence_order = {'HIGH': 3, 'MEDIUM': 2, 'LOW': 1, 'UNKNOWN': 0}

        for classification in classifications:
            # Normalize test name for consistent matching
            test_name = classification.test_name
            test_name_normalized = TestNameNormalizer.normalize(test_name)

            if test_name_normalized not in seen_tests:
                seen_tests[test_name_normalized] = classification
                deduplicated.append(classification)
            else:
                existing = seen_tests[test_name_normalized]
                existing_conf = confidence_order.get(existing.confidence, 0)
                new_conf = confidence_order.get(classification.confidence, 0)

                if new_conf > existing_conf:
                    # Replace with higher confidence one
                    index = deduplicated.index(existing)
                    deduplicated[index] = classification
                    seen_tests[test_name_normalized] = classification

        return deduplicated


class ReportUrlBuilder:
    """
    Centralized utility for building report URLs.
    Consolidates logic for extracting project/job names and constructing dashboard links.
    """

    @staticmethod
    def normalize_path(path: str) -> str:
        """
        Normalize file paths to use forward slashes for cross-platform compatibility.
        Handles UNC/Windows backslashes by converting them to POSIX style.
        """
        if not path:
            return path
        return path.replace('\\', '/')
    
    @staticmethod
    def extract_project_name(report_name: str) -> str:
        """Extract project name from report name.
        
        Args:
            report_name: Report name like "Regression-AccountOpening-Tests-420" or "ProdSanity-All-Tests-523"
            
        Returns:
            Project name like "AccountOpening" or "ProdSanity" or empty string if not found
        """
        if not report_name:
            return ""
        
        # Pattern: {Prefix}-{ProjectName}-{Suffix} or {ProjectName}-{Suffix}
        # Examples:
        #   "Regression-Growth-Tests-442" -> "Growth" (2nd segment for Regression-*)
        #   "Regression-AccountOpening-Tests-420" -> "AccountOpening" (2nd segment)
        #   "ProdSanity-All-Tests-523" -> "ProdSanity" (1st segment for non-Regression)
        parts = report_name.split('-')
        if len(parts) >= 2:
            if parts[0] == 'Regression' and len(parts) >= 3:
                # For Regression-*, use 2nd segment
                return parts[1]
            else:
                # For others (like ProdSanity), use 1st segment
                return parts[0]
        else:
            # Fallback if no hyphens
            return report_name

    @staticmethod
    def extract_project_job_from_path(report_dir: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Extract project name and job name from directory structure.
        Expected structure: .../ProjectName/JobName/ReportName (e.g. .../RegressionResults/ProdSanity/ProdSanity-All-Tests-541).
        Uses string parsing for UNC/network paths so project/job are found even when resolve() fails (e.g. share not accessible).
        
        Args:
            report_dir: Path to report directory
            
        Returns:
            Tuple of (project_name, job_name) or (None, None) if extraction fails
        """
        def _parse_from_components(normalized: str) -> Tuple[Optional[str], Optional[str]]:
            # Parse without resolve(): split path and take last 3 parts as report_name, job_name, project_name
            parts = [p for p in normalized.replace('\\', '/').split('/') if p]
            if len(parts) >= 3:
                report_name_part = parts[-1]
                job_name = parts[-2]
                project_name = parts[-3]
                # Skip if any looks like a host (e.g. IP)
                if project_name and job_name and not project_name.replace('.', '').isdigit():
                    return project_name, job_name
            return None, None

        try:
            normalized_dir = ReportUrlBuilder.normalize_path(report_dir)
            report_path_obj = Path(normalized_dir).resolve()

            # Expected structure: .../ProjectName/JobName/ReportName
            job_name = report_path_obj.parent.name
            project_name = report_path_obj.parent.parent.name

            # Validate (ensure not empty and job is not the path anchor, e.g. UNC share)
            if not job_name or not project_name:
                return _parse_from_components(normalized_dir)
            if job_name == report_path_obj.anchor or (report_path_obj.anchor and job_name in report_path_obj.anchor):
                return _parse_from_components(normalized_dir)

            return project_name, job_name

        except Exception:
            # resolve() often fails for UNC paths when share is not accessible; parse from string
            return _parse_from_components(ReportUrlBuilder.normalize_path(report_dir))
            
    @staticmethod
    def build_dashboard_url(base_url: str, report_name: str, html_path: str = "html/index.html", 
                          project_name: str = None, job_name: str = None) -> str:
        """Build dashboard URL for a report.
        
        Args:
            base_url: Base URL of the dashboard
            report_name: Report name like "Regression-AccountOpening-Tests-420" or "ProdSanity-All-Tests-523"
            html_path: Path within the HTML directory (e.g., "html/index.html" or "html/suite1_test1_results.html")
            project_name: Optional explicit project name
            job_name: Optional explicit job name
            
        Returns:
            Full dashboard URL
        """
        if not report_name:
            return html_path
        
        # Use provided project_name or extract it
        if not project_name:
            project_name = ReportUrlBuilder.extract_project_name(report_name)
        
        if not project_name:
            return html_path
        
        # Special case: ProdSanity reports don't have job name in the path
        # Always use report-name-derived project name (not directory-derived) since
        # CI directory structure is .../RegressionResults/ProdSanity/... but dashboard
        # URL must be /Results/ProdSanity/...
        if report_name.startswith('ProdSanity-'):
            prod_project = ReportUrlBuilder.extract_project_name(report_name)
            return f"{base_url}/Results/{prod_project}/{report_name}/{html_path}"
        
        # Use provided job_name or default to configured value (falls back to Access-Jobs)
        if not job_name:
            try:
                from lib.settings import Config
                job_name = Config.DASHBOARD_JOB_NAME
            except Exception:
                job_name = "Access-Jobs"
            
        # Standard pattern: /Results/{project_name}/{job_name}/{report_name}/html/...
        return f"{base_url}/Results/{project_name}/{job_name}/{report_name}/{html_path}"


def remove_duplicate_class_name(class_name: str) -> str:
    """
    Remove duplicate class name segments from a class name string.
    Checks for consecutive duplicate segments anywhere in the string.
    
    Examples:
    - "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.TestDashBusinessesApis.testMethod"
      -> "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.testMethod"
    - "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.TestDashBusinessesApis"
      -> "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis"
    - "TestDashBusinessesApis.TestDashBusinessesApis"
      -> "TestDashBusinessesApis"
    
    Args:
        class_name: Class name string that may contain duplicates
        
    Returns:
        Class name with duplicates removed
    """
    if not class_name or '.' not in class_name:
        return class_name
    
    parts = class_name.split('.')
    if len(parts) < 2:
        return class_name
    
    cleaned_parts = []
    i = 0
    while i < len(parts):
        cleaned_parts.append(parts[i])
        if i + 1 < len(parts) and parts[i] == parts[i + 1]:
            i += 2
        else:
            i += 1
    
    return '.'.join(cleaned_parts)


def normalize_root_cause(root_cause: str) -> str:
    """
    Normalize root cause string to handle dynamic values like URLs, IDs, timestamps, dates, testcase names.
    This allows grouping similar errors even if they contain different dynamic values.
    
    Args:
        root_cause: Original root cause string
        
    Returns:
        Normalized root cause string with dynamic values replaced
    """
    if not root_cause:
        return ""
    
    normalized = root_cause.lower()
    
    # Remove URLs (but keep endpoint patterns)
    normalized = re.sub(r'https?://[^\s\)]+', '[URL]', normalized)
    
    # Remove dates in various formats (e.g., "24 Dec 2025", "2025-12-24", "12/24/2025", "Dec 24, 2025")
    # Remove times (e.g., "22:45:43", "10:30 AM", "14:30:00")
    normalized = re.sub(r'\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}\b', '[DATE]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4}\b', '[DATE]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'\b\d{4}-\d{2}-\d{2}\b', '[DATE]', normalized)
    normalized = re.sub(r'\b\d{1,2}/\d{1,2}/\d{4}\b', '[DATE]', normalized)
    normalized = re.sub(r'\b\d{1,2}-\d{1,2}-\d{4}\b', '[DATE]', normalized)
    
    # Remove times (e.g., "22:45:43", "10:30 AM", "14:30:00")
    normalized = re.sub(r'\b\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?\b', '[TIME]', normalized, flags=re.IGNORECASE)
    
    # Remove timestamps (e.g., "40.431 seconds", "40 seconds", "2025-12-01 22:45:43")
    normalized = re.sub(r'\b\d+\.?\d*\s*(seconds?|minutes?|hours?|ms|milliseconds?)\b', '[DURATION]', normalized, flags=re.IGNORECASE)
    
    # CRITICAL: Normalize missing keys patterns FIRST to group all key mismatch failures together
    # This ensures all "missing keys" failures group together regardless of API name differences
    
    # CRITICAL: Normalize "Actual JSON doesn't contain all expected keys" pattern FIRST
    # This pattern often appears as: "Actual JSON doesn't contain all expected keys. Expected has: '[keys]'"
    # We need to normalize this BEFORE normalizing "Missing Keys" patterns
    normalized = re.sub(r'actual\s+json\s+doesn[\'"]?t\s+contain\s+all\s+expected\s+keys', 'missing keys', normalized, flags=re.IGNORECASE)
    
    # Normalize "Missing Key" vs "Missing Keys" to be consistent
    normalized = re.sub(r'missing\s+keys?', 'missing keys', normalized, flags=re.IGNORECASE)
    
    # Normalize missing keys list - replace the actual key names with placeholder
    # Pattern: "Missing Keys: [rejected_decision_uuid, reason, rejected_decision, ...]" -> "Missing Keys: [keys_list]"
    # Pattern: "Expected has: '[rejected_decision_uuid, ...]'" -> "Missing Keys: [keys_list]"
    # Pattern: "Expected has: [rejected_decision_uuid, ...]" -> "Missing Keys: [keys_list]"
    normalized = re.sub(r'missing\s+keys?:\s*\[[^\]]+\]', 'missing keys: [keys_list]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'expected\s+has:\s*[\'"]?\[[^\]]+\][\'"]?', 'missing keys: [keys_list]', normalized, flags=re.IGNORECASE)
    # Also handle "Expected has:" without quotes (handles HTML entities like &#x27;)
    normalized = re.sub(r'expected\s+has:\s*\[[^\]]+\]', 'missing keys: [keys_list]', normalized, flags=re.IGNORECASE)
    
    # Normalize API endpoints but keep the path structure
    # First normalize UUIDs in paths (e.g., /dashboard/eligibilities/9e89361b-578b-4773-a66b-4d656ee2e98e -> /dashboard/eligibilities/{uuid})
    normalized = re.sub(r'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '/{uuid}', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'/\{?[a-zA-Z0-9_-]+\}?', '/{id}', normalized)
    normalized = re.sub(r'/\d+', '/{id}', normalized)
    
    # Normalize API name patterns - extract endpoint pattern and normalize
    # Pattern: "API Name: GET /dashboard/aml/lnrn-search" -> "api name: [endpoint]"
    # Pattern: "API Name: /dashboard/aml/lnrn-search" -> "api name: [endpoint]"
    # Pattern: "API Name: GetAmlSearchSuccessfulResponse" -> "api name: [api_response]"
    normalized = re.sub(r'api\s+name:\s*(get|post|put|delete|patch)\s+([^\s,\.]+)', r'api name: [endpoint]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'api\s+name:\s*([/][^\s,\.]+)', r'api name: [endpoint]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'api\s+name:\s*([a-z][a-z0-9_]*response)', r'api name: [api_response]', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'api\s+name:\s*([^\s,\.]+)', r'api name: [api_name]', normalized, flags=re.IGNORECASE)
    
    # Replace status codes with placeholder (but preserve error context)
    normalized = re.sub(r'\b(40[0-9]|50[0-9]|30[0-9])\b', '[status_code]', normalized)
    
    # Replace CSS selectors with IDs
    normalized = re.sub(r'#[a-zA-Z0-9_-]+', '#[id]', normalized)
    normalized = re.sub(r'data-cy=[\'"][^\'"]+[\'"]', 'data-cy=[attr]', normalized)
    
    # Replace UUIDs
    normalized = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '[UUID]', normalized, flags=re.IGNORECASE)
    
    # Remove numeric IDs and account numbers (long sequences of digits)
    normalized = re.sub(r'\b\d{6,}\b', '[NUMERIC_ID]', normalized)
    
    # Remove email addresses
    normalized = re.sub(r'\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b', '[EMAIL]', normalized, flags=re.IGNORECASE)
    
    # Normalize whitespace
    normalized = ' '.join(normalized.split())
    
    # SPECIAL CASE: If the normalized string contains any missing keys pattern,
    # normalize to a common pattern that groups all missing keys failures together
    # This ensures all key mismatch failures group together regardless of API name differences
    # Check for various indicators of missing keys failures
    has_missing_keys = (
        'missing keys' in normalized or 
        '[keys_list]' in normalized or
        'expected has' in normalized.lower() or
        'actual json' in normalized.lower()
    )
    
    if has_missing_keys:
        # Normalize to common pattern - extract status code if present
        if '[status_code]' in normalized:
            normalized = 'api name: [api_name], status code: [status_code], missing keys: [keys_list]'
        else:
            normalized = 'api name: [api_name], missing keys: [keys_list]'
    
    # Keep more context - use first 200 chars
    return normalized[:200].strip()


def extract_api_endpoint(root_cause: str) -> Optional[str]:
    """
    Extract API endpoint from root cause text.
    
    Args:
        root_cause: Root cause text that may contain API endpoint
        
    Returns:
        API endpoint string or None if not found
    """
    if not root_cause:
        return None
    
    api_patterns = [
        r'(API Name|Endpoint|api name|api url|url)[:\s]+([^\s,<>\n]+)',
        r'(/api/[^\s,<>\n]+|/dashboard/[^\s,<>\n]+)',
    ]
    
    for pattern in api_patterns:
        match = re.search(pattern, root_cause, re.IGNORECASE)
        if match:
            if len(match.groups()) >= 2:
                api = match.group(2).strip()
            else:
                api = match.group(1).strip()
            
            # Clean up the API endpoint
            api = api.replace('http://', '').replace('https://', '')
            if api.startswith('/'):
                return api.split()[0] if ' ' in api else api
            return api
    
    return None


def normalize_failure_signature(signature: str) -> str:
    """
    Normalize failure signature for grouping.
    - Lowercase
    - Remove trailing punctuation
    - Collapse whitespace
    
    Args:
        signature: Raw failure signature string
    
    Returns:
        Normalized signature string suitable for grouping keys
    """
    if not signature:
        return ""
    # Normalize:
    # 1. Lowercase for case-insensitivity
    # 2. Strip standard whitespace and trailing punctuation
    # 3. Collapse multiple whitespace into single space
    return " ".join(signature.strip().rstrip(".,;:!?").lower().split())


class TestDataCache:
    """
    Centralized cache for test-related data.
    Eliminates redundant execution log fetching and provides consistent data access.
    """
    
    def __init__(self, test_results: Optional[list] = None, html_links: Optional[dict] = None):
        """
        Initialize cache with test results and HTML links.
        
        Args:
            test_results: List of TestResult objects
            html_links: Dictionary mapping test names to HTML file URLs
        """
        self._cache: dict = {}
        self._test_results = test_results or []
        self._html_links = html_links or {}
        self._build_cache()
    
    def _build_cache(self):
        """Build internal cache from test results."""
        for result in self._test_results:
            normalized_name = TestNameNormalizer.normalize(getattr(result, 'full_name', ''))
            if normalized_name:
                # Combine execution log, error message, and stack trace
                execution_log = getattr(result, 'execution_log', '') or ''
                error_message = getattr(result, 'error_message', '') or ''
                stack_trace = getattr(result, 'stack_trace', '') or ''
                
                # Combine all log information
                combined_log = execution_log
                if error_message and error_message not in combined_log:
                    combined_log = f"{combined_log}\n\n{error_message}" if combined_log else error_message
                if stack_trace and stack_trace not in combined_log:
                    combined_log = f"{combined_log}\n\n{stack_trace}" if combined_log else stack_trace
                
                # Get HTML link
                html_link = self._html_links.get(normalized_name) or self._find_html_link(result)
                
                self._cache[normalized_name] = {
                    'test_result': result,
                    'execution_log': execution_log,
                    'error_message': error_message,
                    'stack_trace': stack_trace,
                    'combined_log': combined_log,
                    'html_link': html_link,
                    'class_name': getattr(result, 'class_name', ''),
                    'method_name': getattr(result, 'method_name', ''),
                    'description': getattr(result, 'description', None),
                }
    
    def _find_html_link(self, result) -> Optional[str]:
        """Find HTML link for a test result using multiple strategies."""
        full_name = getattr(result, 'full_name', '')
        normalized = TestNameNormalizer.normalize(full_name)
        
        # Try exact match
        if normalized in self._html_links:
            return self._html_links[normalized]
        
        # Try matching with original name
        if full_name in self._html_links:
            return self._html_links[full_name]
        
        # Try matching by method name only
        method_name = getattr(result, 'method_name', '')
        if method_name:
            for key, value in self._html_links.items():
                if key.endswith(f'.{method_name}'):
                    return value
        
        return None
    
    def get_execution_log(self, test_name: str) -> str:
        """
        Get execution log for a test with consistent matching.
        
        Args:
            test_name: Test name (can be in various formats)
            
        Returns:
            Execution log string (empty string if not found)
        """
        normalized = TestNameNormalizer.normalize(test_name)
        cached = self._cache.get(normalized)
        if cached:
            return cached.get('execution_log', '')
        return ''
    
    def get_combined_log(self, test_name: str) -> str:
        """
        Get combined execution log (execution_log + error_message + stack_trace).
        
        Args:
            test_name: Test name
            
        Returns:
            Combined log string (empty string if not found)
        """
        normalized = TestNameNormalizer.normalize(test_name)
        cached = self._cache.get(normalized)
        if cached:
            return cached.get('combined_log', '')
        return ''
    
    def get_html_link(self, test_name: str) -> Optional[str]:
        """
        Get HTML link for a test.
        
        Args:
            test_name: Test name
            
        Returns:
            HTML link URL or None
        """
        normalized = TestNameNormalizer.normalize(test_name)
        cached = self._cache.get(normalized)
        if cached:
            return cached.get('html_link')
        return None
    
    def get_test_result(self, test_name: str):
        """
        Get TestResult object for a test.
        
        Args:
            test_name: Test name
            
        Returns:
            TestResult object or None
        """
        normalized = TestNameNormalizer.normalize(test_name)
        cached = self._cache.get(normalized)
        if cached:
            return cached.get('test_result')
        return None
    
    def get_all_data(self, test_name: str) -> Optional[dict]:
        """
        Get all cached data for a test.
        
        Args:
            test_name: Test name
            
        Returns:
            Dictionary with all cached data or None
        """
        normalized = TestNameNormalizer.normalize(test_name)
        return self._cache.get(normalized)
    
    def has_test(self, test_name: str) -> bool:
        """
        Check if test exists in cache.
        
        Args:
            test_name: Test name
            
        Returns:
            True if test exists, False otherwise
        """
        normalized = TestNameNormalizer.normalize(test_name)
        return normalized in self._cache


def extract_class_and_method(full_name: str) -> tuple[str, str]:
    """
    Extract class name and method name from a full test name.
    
    Examples:
    - "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.testMethod"
      -> ("TestDashBusinessesApis", "testMethod")
    - "TestDashBusinessesApis.testMethod"
      -> ("TestDashBusinessesApis", "testMethod")
    
    Args:
        full_name: Full test name (may contain package path)
        
    Returns:
        Tuple of (class_name, method_name)
    """
    if not full_name:
        return ("", "")
    
    # Remove duplicate class names first
    cleaned_name = remove_duplicate_class_name(full_name)
    
    # Split by dots
    parts = cleaned_name.split('.')
    
    if len(parts) < 2:
        # No method name found, return as-is for class_name
        return (cleaned_name, "")
    
    # Last part is method name, second-to-last is class name
    method_name = parts[-1]
    class_name = parts[-2]
    
    return (class_name, method_name)
