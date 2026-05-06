"""
Data validation layer for report generation.
Validates data consistency and logs warnings about potential issues.
"""

import logging
from typing import List, Dict, Optional
from ..parsers.models import TestResult
from ..agent.analyzer import FailureClassification
from ..utils import TestDataCache, TestNameNormalizer

logger = logging.getLogger(__name__)


class DataValidator:
    """Validates data consistency before report generation"""
    
    def __init__(self, test_results: List[TestResult], classifications: List[FailureClassification], 
                 cache: TestDataCache, html_links: Optional[Dict[str, str]] = None):
        """
        Initialize validator.
        
        Args:
            test_results: List of test results
            classifications: List of failure classifications
            cache: TestDataCache instance
            html_links: Optional dictionary of HTML links
        """
        self.test_results = test_results
        self.classifications = classifications
        self.cache = cache
        self.html_links = html_links or {}
        self.warnings = []
        self.errors = []
    
    def validate_all(self) -> Dict[str, any]:
        """
        Run all validation checks.
        
        Returns:
            Dictionary with validation results and statistics
        """
        logger.info("🔍 Starting data validation...")
        
        stats = {
            'warnings': [],
            'errors': [],
            'test_name_normalization_issues': 0,
            'missing_execution_logs': 0,
            'missing_html_links': 0,
            'classification_mismatches': 0,
            'duplicate_tests': 0,
        }
        
        # Run all validations
        self._validate_test_name_normalization(stats)
        self._validate_execution_logs(stats)
        self._validate_html_links(stats)
        self._validate_classification_matching(stats)
        self._validate_duplicates(stats)
        
        # Log summary
        if stats['warnings']:
            logger.warning(f"⚠️ Data validation found {len(stats['warnings'])} warnings")
            for warning in stats['warnings'][:10]:  # Log first 10
                logger.warning(f"  - {warning}")
        else:
            logger.info("✅ Data validation passed with no warnings")
        
        if stats['errors']:
            logger.error(f"❌ Data validation found {len(stats['errors'])} errors")
            for error in stats['errors'][:10]:  # Log first 10
                logger.error(f"  - {error}")
        
        return stats
    
    def _validate_test_name_normalization(self, stats: Dict):
        """Validate that test names are normalized consistently"""
        normalized_names = set()
        issues = []
        
        for result in self.test_results:
            normalized = TestNameNormalizer.normalize(result.full_name)
            if normalized in normalized_names:
                issues.append(f"Duplicate normalized name: {normalized}")
            normalized_names.add(normalized)
        
        for classification in self.classifications:
            normalized = TestNameNormalizer.normalize(classification.test_name)
            if normalized not in normalized_names:
                # Check if it matches any test result
                matching = TestNameNormalizer.find_matching_test(classification.test_name, self.test_results)
                if not matching:
                    issues.append(f"Classification test name '{classification.test_name}' doesn't match any test result")
        
        stats['test_name_normalization_issues'] = len(issues)
        if issues:
            stats['warnings'].extend(issues[:5])  # Limit warnings
    
    def _validate_execution_logs(self, stats: Dict):
        """Validate that execution logs are available for failures"""
        missing_logs = []
        
        for classification in self.classifications:
            log = self.cache.get_combined_log(classification.test_name)
            if not log or not log.strip():
                missing_logs.append(f"No execution log for: {classification.test_name}")
        
        stats['missing_execution_logs'] = len(missing_logs)
        if missing_logs:
            stats['warnings'].extend([f"Missing execution logs: {len(missing_logs)} tests"] + missing_logs[:3])
    
    def _validate_html_links(self, stats: Dict):
        """Validate that HTML links are available for tests"""
        missing_links = []
        
        for classification in self.classifications:
            link = self.cache.get_html_link(classification.test_name)
            if not link:
                missing_links.append(f"No HTML link for: {classification.test_name}")
        
        stats['missing_html_links'] = len(missing_links)
        if missing_links:
            stats['warnings'].extend([f"Missing HTML links: {len(missing_links)} tests"] + missing_links[:3])
    
    def _validate_classification_matching(self, stats: Dict):
        """Validate that classifications match test results"""
        mismatches = []
        
        for classification in self.classifications:
            matching_test = TestNameNormalizer.find_matching_test(classification.test_name, self.test_results)
            if not matching_test:
                mismatches.append(f"Classification '{classification.test_name}' has no matching test result")
        
        stats['classification_mismatches'] = len(mismatches)
        if mismatches:
            stats['warnings'].extend(mismatches[:5])
    
    def _validate_duplicates(self, stats: Dict):
        """Validate that there are no duplicate tests in classifications"""
        seen = {}
        duplicates = []
        
        for classification in self.classifications:
            normalized = TestNameNormalizer.normalize(classification.test_name)
            if normalized in seen:
                duplicates.append(f"Duplicate classification: {normalized} (seen {seen[normalized]} times)")
                seen[normalized] = seen.get(normalized, 1) + 1
            else:
                seen[normalized] = 1
        
        stats['duplicate_tests'] = len(duplicates)
        if duplicates:
            stats['warnings'].extend(duplicates[:5])


def validate_report_data(test_results: List[TestResult], classifications: List[FailureClassification],
                         cache: TestDataCache, html_links: Optional[Dict[str, str]] = None) -> Dict[str, any]:
    """
    Convenience function to validate report data.
    
    Args:
        test_results: List of test results
        classifications: List of failure classifications
        cache: TestDataCache instance
        html_links: Optional dictionary of HTML links
        
    Returns:
        Dictionary with validation statistics
    """
    validator = DataValidator(test_results, classifications, cache, html_links)
    return validator.validate_all()


class PostReportValidator:
    """Validates data consistency after report generation"""
    
    def __init__(self, category_counts: Dict[str, int], category_failures: Dict[str, List[FailureClassification]],
                 cache: TestDataCache, total_failures: int):
        """
        Initialize post-report validator.
        
        Args:
            category_counts: Dictionary mapping category to count
            category_failures: Dictionary mapping category to list of failures
            cache: TestDataCache instance
            total_failures: Total number of failures
        """
        self.category_counts = category_counts
        self.category_failures = category_failures
        self.cache = cache
        self.total_failures = total_failures
        self.warnings = []
        self.errors = []
    
    def validate_all(self) -> Dict[str, any]:
        """
        Run all post-report validation checks.
        
        Returns:
            Dictionary with validation results and statistics
        """
        logger.info("🔍 Starting post-report validation...")
        
        stats = {
            'warnings': [],
            'errors': [],
            'tests_without_links': 0,
            'count_inconsistencies': 0,
            'duplicate_tests_in_category': 0,
            'category_sum_mismatch': False,
            'representative_signals_mismatch': 0,
        }
        
        # Run all validations
        self._validate_category_links(stats)
        self._validate_category_counts(stats)
        self._validate_no_duplicates_in_categories(stats)
        self._validate_category_sum(stats)
        self._validate_representative_signals_counts(stats)
        
        # Log summary
        if stats['warnings']:
            logger.warning(f"⚠️ Post-report validation found {len(stats['warnings'])} warnings")
            for warning in stats['warnings'][:10]:  # Log first 10
                logger.warning(f"  - {warning}")
        else:
            logger.info("✅ Post-report validation passed with no warnings")
        
        if stats['errors']:
            logger.error(f"❌ Post-report validation found {len(stats['errors'])} errors")
            for error in stats['errors'][:10]:  # Log first 10
                logger.error(f"  - {error}")
        
        return stats
    
    def _validate_category_links(self, stats: Dict):
        """Validate that all tests in categories have valid HTML links"""
        tests_without_links = []
        
        for category, failures in self.category_failures.items():
            for failure in failures:
                link = self.cache.get_html_link(failure.test_name)
                if not link:
                    tests_without_links.append(f"{category}: {failure.test_name}")
        
        stats['tests_without_links'] = len(tests_without_links)
        if tests_without_links:
            stats['warnings'].extend([
                f"Tests without HTML links: {len(tests_without_links)}",
                *tests_without_links[:5]
            ])
    
    def _validate_category_counts(self, stats: Dict):
        """Validate that counts are consistent across sections"""
        inconsistencies = []
        
        for category, expected_count in self.category_counts.items():
            actual_count = len(self.category_failures.get(category, []))
            if expected_count != actual_count:
                inconsistencies.append(
                    f"Category '{category}': count mismatch - expected {expected_count}, actual {actual_count}"
                )
        
        stats['count_inconsistencies'] = len(inconsistencies)
        if inconsistencies:
            stats['errors'].extend(inconsistencies)
    
    def _validate_no_duplicates_in_categories(self, stats: Dict):
        """Validate that there are no duplicate tests in the same category"""
        duplicates = []
        
        for category, failures in self.category_failures.items():
            seen = set()
            for failure in failures:
                normalized = TestNameNormalizer.normalize(failure.test_name)
                if normalized in seen:
                    duplicates.append(f"Category '{category}': duplicate test '{failure.test_name}'")
                seen.add(normalized)
        
        stats['duplicate_tests_in_category'] = len(duplicates)
        if duplicates:
            stats['errors'].extend(duplicates[:5])
    
    def _validate_category_sum(self, stats: Dict):
        """Validate that all categories sum to total failures"""
        category_sum = sum(self.category_counts.values())
        
        if category_sum != self.total_failures:
            error_msg = (
                f"Category sum mismatch: categories sum to {category_sum}, "
                f"but total failures is {self.total_failures}"
            )
            stats['category_sum_mismatch'] = True
            stats['errors'].append(error_msg)
    
    def _validate_representative_signals_counts(self, stats: Dict):
        """Validate that representative signals counts match test counts"""
        import re
        
        mismatches = []
        
        for category, failures in self.category_failures.items():
            if category == 'TIMEOUT':
                # Extract element patterns and page counts from representative signals logic (matching report_generator.py)
                element_patterns = {}
                page_counts = {}
                matched_count = 0
                
                for failure in failures:
                    root_cause = failure.root_cause or ""
                    exec_log = self.cache.get_combined_log(failure.test_name)
                    search_text = f"{root_cause} {exec_log}"
                    matched = False
                    
                    # Priority 1: Extract element visibility timeout patterns
                    element_match = re.search(
                        r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+visible(?:\s+and\s+clickable)?\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds",
                        search_text,
                        re.IGNORECASE
                    )
                    if element_match:
                        element_pattern = element_match.group(1).strip()
                        element_patterns[element_pattern] = element_patterns.get(element_pattern, 0) + 1
                        matched = True
                    else:
                        # Priority 2: Extract page load timeout patterns
                        page_match = re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", search_text, re.IGNORECASE)
                        if page_match:
                            page_name = page_match.group(1)
                            page_counts[page_name] = page_counts.get(page_name, 0) + 1
                            matched = True
                        else:
                            # Priority 3: Try alternative pattern
                            alt_match = re.search(r"(\w+Page\w*)\s+(?:NOT|not)\s+loaded\s+even\s+after", search_text, re.IGNORECASE)
                            if alt_match:
                                page_name = alt_match.group(1)
                                page_counts[page_name] = page_counts.get(page_name, 0) + 1
                                matched = True
                            else:
                                # Priority 4: Try TimeoutException patterns
                                timeout_exception_match = re.search(
                                    r"TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible).*?['\"]([^'\"]+)['\"]",
                                    search_text,
                                    re.IGNORECASE | re.DOTALL
                                )
                                if timeout_exception_match:
                                    element_desc = timeout_exception_match.group(1).strip()
                                    if element_desc:
                                        element_patterns[element_desc] = element_patterns.get(element_desc, 0) + 1
                                        matched = True
                    
                    if matched:
                        matched_count += 1
                
                # Check if sum matches (only count matched failures, not unmatched)
                signal_sum = sum(element_patterns.values()) + sum(page_counts.values())
                if signal_sum != matched_count:
                    mismatches.append(
                        f"Category '{category}': Representative signals sum ({signal_sum}) "
                        f"doesn't match matched test count ({matched_count} out of {len(failures)})"
                    )
            
            elif category == 'ELEMENT_NOT_FOUND':
                # Extract exception counts
                exception_counts = {}
                for failure in failures:
                    root_cause = failure.root_cause or ""
                    exec_log = self.cache.get_combined_log(failure.test_name)
                    search_text = f"{root_cause} {exec_log}"
                    
                    exception_match = re.search(r'(\w+Exception)(?::|$|\s)', search_text, re.IGNORECASE)
                    if exception_match:
                        exception_type = exception_match.group(1)
                        # Try to get context for NullPointerException
                        if exception_type.lower() == 'nullpointerexception':
                            context_match = re.search(r'Cannot invoke\s+"[^"]*\.(\w+)\(\)"', search_text, re.IGNORECASE)
                            if context_match:
                                context = context_match.group(1)
                                key = f"{exception_type} in {context}"
                            else:
                                key = exception_type
                        else:
                            key = exception_type
                        exception_counts[key] = exception_counts.get(key, 0) + 1
                    else:
                        exception_counts["Unknown exception"] = exception_counts.get("Unknown exception", 0) + 1
                
                signal_sum = sum(exception_counts.values())
                if signal_sum != len(failures):
                    mismatches.append(
                        f"Category '{category}': Representative signals sum ({signal_sum}) "
                        f"doesn't match test count ({len(failures)})"
                    )
            
            elif category == 'ASSERTION_FAILURE':
                # Extract assertion category counts
                assertion_categories = {}
                for failure in failures:
                    root_cause = failure.root_cause or ""
                    exec_log = self.cache.get_combined_log(failure.test_name)
                    search_text = f"{root_cause} {exec_log}".lower()
                    
                    category_type = None
                    if re.search(r"missing\s+key\s*:", search_text, re.IGNORECASE) or \
                       re.search(r"actual\s+json\s+doesn'?t\s+contain\s+all\s+expected\s+keys", search_text, re.IGNORECASE):
                        category_type = "API Keys mismatch"
                    elif re.search(r"classes\s+of\s+actual\s+and\s+expected\s+key", search_text, re.IGNORECASE) or \
                         re.search(r"key\s*/\s*value\s+is\s+null", search_text, re.IGNORECASE):
                        category_type = "Keys formatting mismatch"
                    elif re.search(r"expected\s+['\"]?[^'\"]+['\"]?\s+was\s*[:-]\s*['\"]?[^'\"]+['\"]?\s*\.?\s*but\s+actual\s+is", search_text, re.IGNORECASE):
                        category_type = "Single text not matching"
                    else:
                        category_type = "Assertion failure"
                    
                    assertion_categories[category_type] = assertion_categories.get(category_type, 0) + 1
                
                signal_sum = sum(assertion_categories.values())
                if signal_sum != len(failures):
                    mismatches.append(
                        f"Category '{category}': Representative signals sum ({signal_sum}) "
                        f"doesn't match test count ({len(failures)})"
                    )
        
        stats['representative_signals_mismatch'] = len(mismatches)
        if mismatches:
            stats['warnings'].extend(mismatches)


def validate_post_report(category_counts: Dict[str, int], 
                        category_failures: Dict[str, List[FailureClassification]],
                        cache: TestDataCache, total_failures: int) -> Dict[str, any]:
    """
    Convenience function to validate post-report data.
    
    Args:
        category_counts: Dictionary mapping category to count
        category_failures: Dictionary mapping category to list of failures
        cache: TestDataCache instance
        total_failures: Total number of failures
        
    Returns:
        Dictionary with validation statistics
    """
    validator = PostReportValidator(category_counts, category_failures, cache, total_failures)
    return validator.validate_all()

