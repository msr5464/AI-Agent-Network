"""
Category classification rule engine.
Provides a clean, maintainable way to reclassify test failures into root cause categories.
"""

import re
import logging
from typing import Optional
from ..agent.analyzer import FailureClassification
from ..utils import TestDataCache

logger = logging.getLogger(__name__)


class CategoryRule:
    """Base class for category classification rules"""
    
    priority: int = 0  # Higher = checked first
    category: str = "OTHER"
    
    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        """
        Check if this rule matches the failure.
        
        Args:
            failure: FailureClassification object
            cache: TestDataCache for accessing execution logs
            
        Returns:
            True if rule matches, False otherwise
        """
        raise NotImplementedError("Subclasses must implement matches()")
    
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(priority={self.priority}, category={self.category})"


class ElementClickInterceptedRule(CategoryRule):
    """Rule for ElementClickInterceptedException -> ELEMENT_NOT_FOUND"""
    priority = 12  # ELEMENT_NOT_FOUND priority (after TIMEOUT)
    category = 'ELEMENT_NOT_FOUND'
    
    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        root_cause = failure.root_cause or ""
        combined_text = f"{root_cause} {failure.recommended_action or ''}".lower()
        execution_log = cache.get_combined_log(failure.test_name).lower()
        
        return (
            "ElementClickInterceptedException" in root_cause or
            "elementclickinterceptedexception" in combined_text or
            "ElementClickInterceptedException" in cache.get_combined_log(failure.test_name) or
            "elementclickinterceptedexception" in execution_log
        )


class PageLoadTimeoutRule(CategoryRule):
    """Rule for page load timeout patterns -> TIMEOUT"""
    priority = 15  # Highest priority: Page Load Issues
    category = 'TIMEOUT'
    
    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        root_cause = failure.root_cause or ""
        execution_log = cache.get_combined_log(failure.test_name)
        root_cause_lower = root_cause.lower()
        execution_log_lower = execution_log.lower()
        
        # Pattern 1: "'PageName' NOT loaded even after :- X seconds"
        is_page_load_timeout_pattern = (
            # Pattern with single quotes: 'DashReviewPage' NOT loaded even after :- 40.071 seconds
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+NOT\s+loaded\s+even\s+after\s*[:-]\s*\d+\.?\d*\s+seconds", root_cause, re.IGNORECASE) or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+NOT\s+loaded\s+even\s+after\s*[:-]\s*\d+\.?\d*\s+seconds", execution_log, re.IGNORECASE) or
            # Pattern with lowercase "not": 'PageName' not loaded even after
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+not\s+loaded\s+even\s+after", root_cause_lower) or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+not\s+loaded\s+even\s+after", execution_log_lower) or
            # More flexible pattern: any page name followed by "NOT loaded" or "not loaded" with "even after"
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", root_cause, re.IGNORECASE) or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", execution_log, re.IGNORECASE)
        )
        
        # Pattern 2: Element visibility/hidden timeout - "Element 'PageName:element' is NOT visible even after waiting for X seconds"
        # Generalized to handle "is NOT hidden", "is NOT shown", etc.
        is_element_visibility_timeout = (
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", root_cause, re.IGNORECASE) or
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", execution_log, re.IGNORECASE) or
            # Flexible pattern: "<abc> is NOT <xyz> even after waiting for <N> seconds"
            re.search(r"['\"]?([^'\"]+)['\"]?\s+is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", root_cause, re.IGNORECASE) or
            re.search(r"['\"]?([^'\"]+)['\"]?\s+is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", execution_log, re.IGNORECASE) or
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+visible\s+and\s+clickable\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", root_cause, re.IGNORECASE) or
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+visible\s+and\s+clickable\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", execution_log, re.IGNORECASE)
        )
        
        # Pattern 3: TimeoutException for element clickable/visible
        is_timeout_exception_for_element = (
            re.search(r"TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", root_cause, re.IGNORECASE) or
            re.search(r"TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", execution_log, re.IGNORECASE) or
            re.search(r"org\.openqa\.selenium\.TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", root_cause, re.IGNORECASE) or
            re.search(r"org\.openqa\.selenium\.TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", execution_log, re.IGNORECASE) or
            re.search(r"TimeoutException.*Expected\s+condition\s+failed.*waiting\s+for\s+element\s+to\s+be\s+clickable", root_cause, re.IGNORECASE) or
            re.search(r"TimeoutException.*Expected\s+condition\s+failed.*waiting\s+for\s+element\s+to\s+be\s+clickable", execution_log, re.IGNORECASE)
        )
        
        return bool(is_page_load_timeout_pattern or is_element_visibility_timeout or is_timeout_exception_for_element)


class ElementLocatorExceptionRule(CategoryRule):
    """Rule for element locator related exceptions -> ELEMENT_NOT_FOUND"""
    priority = 11  # ELEMENT_NOT_FOUND priority (after TIMEOUT)
    category = 'ELEMENT_NOT_FOUND'
    
    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        root_cause = failure.root_cause or ""
        recommended_action = failure.recommended_action or ""
        combined_text = f"{root_cause} {recommended_action}".lower()
        execution_log = cache.get_combined_log(failure.test_name).lower()
        
        is_element_locator_issue = (
            "staleelementreferenceexception" in combined_text or
            "staleelementreferenceexception" in execution_log or
            (("nullpointerexception" in combined_text) and (
                "webelement" in combined_text or 
                "getpageelement" in combined_text or 
                "gettext()" in combined_text or 
                ".gettext()" in combined_text
            )) or
            (("nullpointerexception" in execution_log) and (
                "webelement" in execution_log or 
                "getpageelement" in execution_log or 
                "gettext()" in execution_log or 
                ".gettext()" in execution_log
            )) or
            (("indexoutofboundsexception" in combined_text) and (
                "length 0" in combined_text or 
                "index 0" in combined_text or 
                "out of bounds for length 0" in combined_text
            )) or
            (("indexoutofboundsexception" in execution_log) and (
                "length 0" in execution_log or 
                "index 0" in execution_log or 
                "out of bounds for length 0" in execution_log
            )) or
            # StringIndexOutOfBoundsException should be categorized as ELEMENT_NOT_FOUND
            "stringindexoutofboundsexception" in combined_text or
            "stringindexoutofboundsexception" in execution_log or
            "StringIndexOutOfBoundsException" in root_cause or
            "StringIndexOutOfBoundsException" in cache.get_combined_log(failure.test_name) or
            # DefaultElementLocator / Proxy element patterns (e.g. "Expected condition failed:
            # waiting for element to be clickable: Proxy element for: DefaultElementLocator ...")
            # These are element locator issues, not timeouts.
            ("defaultelementlocator" in combined_text) or
            ("defaultelementlocator" in execution_log) or
            ("proxy element" in combined_text and "waiting for element to be clickable" in combined_text) or
            ("proxy element" in execution_log and "waiting for element to be clickable" in execution_log)
        )
        
        return bool(is_element_locator_issue)


class IllegalArgumentExceptionRule(CategoryRule):
    """Rule for IllegalArgumentException -> ELEMENT_NOT_FOUND"""
    priority = 10  # ELEMENT_NOT_FOUND priority (after TIMEOUT)
    category = 'ELEMENT_NOT_FOUND'
    
    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        root_cause = failure.root_cause or ""
        combined_text = f"{root_cause} {failure.recommended_action or ''}".lower()
        execution_log = cache.get_combined_log(failure.test_name).lower()
        
        return (
            "illegalargumentexception" in combined_text or
            "illegalargumentexception" in execution_log or
            "IllegalArgumentException" in root_cause or
            "IllegalArgumentException" in cache.get_combined_log(failure.test_name)
        )


class NonPageLoadTimeoutFilterRule(CategoryRule):
    """Filter rule: Move non-page-load timeouts from TIMEOUT to OTHER"""
    priority = 6
    category = 'OTHER'
    
    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        # Only apply if current category is TIMEOUT
        current_category = getattr(failure, 'root_cause_category', 'OTHER')
        if current_category != 'TIMEOUT':
            return False
        
        root_cause = failure.root_cause or ""
        root_cause_lower = root_cause.lower()
        execution_log = cache.get_combined_log(failure.test_name)
        execution_log_lower = execution_log.lower()
        
        # Check if it's a valid timeout pattern (page load, element visibility, or element clickable timeout)
        is_valid_timeout = (
            # Page load timeout patterns
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", root_cause, re.IGNORECASE) or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", execution_log, re.IGNORECASE) or
            "not loaded even after" in root_cause_lower or
            ("not loaded" in root_cause_lower and ("seconds" in root_cause_lower or "timeout" in root_cause_lower)) or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+not\s+loaded", root_cause_lower, re.IGNORECASE) or
            # Element visibility timeout patterns
            re.search(r"is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", root_cause, re.IGNORECASE) or
            re.search(r"is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", execution_log, re.IGNORECASE) or
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+visible\s+and\s+clickable\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", root_cause, re.IGNORECASE) or
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+visible\s+and\s+clickable\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", execution_log, re.IGNORECASE) or
            # TimeoutException for element clickable/visible
            re.search(r"TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", root_cause, re.IGNORECASE) or
            re.search(r"TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", execution_log, re.IGNORECASE) or
            re.search(r"org\.openqa\.selenium\.TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", root_cause, re.IGNORECASE) or
            re.search(r"org\.openqa\.selenium\.TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", execution_log, re.IGNORECASE) or
            re.search(r"TimeoutException.*Expected\s+condition\s+failed.*waiting\s+for\s+element\s+to\s+be\s+clickable", root_cause, re.IGNORECASE) or
            re.search(r"TimeoutException.*Expected\s+condition\s+failed.*waiting\s+for\s+element\s+to\s+be\s+clickable", execution_log, re.IGNORECASE)
        )
        
        # If it's NOT a valid timeout pattern, move to OTHER
        return not bool(is_valid_timeout)


class AssertionFailureFilterRule(CategoryRule):
    """Filter rule: Ensure ASSERTION_FAILURE only contains actual assertion failures"""
    priority = 5
    category = 'OTHER'
    
    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        # Only apply if current category is ASSERTION_FAILURE
        current_category = getattr(failure, 'root_cause_category', 'OTHER')
        if current_category != 'ASSERTION_FAILURE':
            return False
        
        root_cause = failure.root_cause or ""
        recommended_action = failure.recommended_action or ""
        execution_log = cache.get_combined_log(failure.test_name)
        
        assertion_text = f"{root_cause} {recommended_action} {execution_log}".lower()
        
        # Check if it's clearly NOT an assertion
        is_clearly_not_assertion = (
            "nosuchelementexception" in assertion_text or
            "elementclickinterceptedexception" in assertion_text or
            "staleelementreferenceexception" in assertion_text or
            "timeoutexception" in assertion_text or
            "webdriverexception" in assertion_text or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", root_cause, re.IGNORECASE) or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", execution_log, re.IGNORECASE) or
            "not loaded even after" in assertion_text or
            "not loaded even after" in execution_log.lower()
        )
        
        if is_clearly_not_assertion:
            return True
        
        # Check for valid assertion patterns
        is_valid_assertion = (
            # Pattern: "Expected 'X' was :-'Y'. But actual is 'Z'"
            re.search(r"expected\s+['\"]?[^'\"]+['\"]?\s+was\s*[:-]\s*['\"]?[^'\"]+['\"]?\s*\.?\s*but\s+actual\s+is", assertion_text, re.IGNORECASE) or
            # Pattern: "Expected 'X' but actual"
            re.search(r"expected\s+[^.]*but\s+actual", assertion_text, re.IGNORECASE) or
            # Pattern: "Classes of actual and expected key"
            re.search(r"classes\s+of\s+actual\s+and\s+expected\s+key", assertion_text, re.IGNORECASE) or
            # Pattern: "Missing Key:"
            re.search(r"missing\s+(?:key|field)\s*:", assertion_text, re.IGNORECASE) or
            # Pattern: "Actual JSON doesn't contain all expected keys"
            re.search(r"actual\s+json\s+doesn'?t\s+contain\s+all\s+expected\s+keys", assertion_text, re.IGNORECASE) or
            # Pattern: "Key/Value is null"
            re.search(r"key\s*/\s*value\s+is\s+null", assertion_text, re.IGNORECASE) or
            # Pattern: "The following asserts failed"
            re.search(r"the\s+following\s+asserts\s+failed", assertion_text, re.IGNORECASE) or
            # Pattern: Contains both "expected" and "actual" keywords
            (re.search(r"\bexpected\b", assertion_text, re.IGNORECASE) and re.search(r"\bactual\b", assertion_text, re.IGNORECASE))
        )
        
        # If it doesn't match any assertion patterns, move to OTHER
        return not bool(is_valid_assertion)


class AssertionFailurePriorityRule(CategoryRule):
    """
    High priority rule to catch clear assertion failures that might be misclassified 
    as ELEMENT_NOT_FOUND or other categories because they contain element-related text.
    """
    priority = 20  # Very High Priority (checks before everything else)
    category = 'ASSERTION_FAILURE'

    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        root_cause = (failure.root_cause or "").lower()
        execution_log = (cache.get_combined_log(failure.test_name) or "").lower()
        text = f"{root_cause} {execution_log}"

        # Strong assertion patterns that should override other categories
        is_strong_assertion = (
            # Pattern: "Expected 'X' was :-'Y'. But actual is 'Z'"
            re.search(r"expected\s+['\"]?[^'\"]+['\"]?\s+was\s*[:-]\s*['\"]?[^'\"]+['\"]?\s*\.?\s*but\s+actual\s+is", text, re.IGNORECASE) or
            # Pattern: "Expected ... but found ..."
            re.search(r"expected\s+.*but\s+found", text, re.IGNORECASE) or
            # Pattern: "Expected ... but actual ..."
            re.search(r"expected\s+.*but\s+actual", text, re.IGNORECASE) or
            # Pattern: "Missing key/field"
            re.search(r"missing\s+(?:key|field)\s*:", text, re.IGNORECASE) or
             # Pattern: "Actual JSON doesn't contain all expected keys"
            re.search(r"actual\s+json\s+doesn'?t\s+contain\s+all\s+expected\s+keys", text, re.IGNORECASE) or
            # Pattern: "The following asserts failed"
            re.search(r"the\s+following\s+asserts\s+failed", text, re.IGNORECASE) or
            # Pattern: toast message mismatch "Expected toast ... but found"
            ("toast message" in text and "expected" in text and ("found" in text or "actual" in text)) or
            # Pattern: Status code mismatch
            re.search(r"expected\s+status\s+code\s+\d+\s+but\s+found\s+\d+", text, re.IGNORECASE) or
             # Pattern: "Value mismatch" explicit mention
            "value mismatch" in text
        )

        return bool(is_strong_assertion)


class OtherToElementNotFoundRule(CategoryRule):
    """When AI returned OTHER, reclassify to ELEMENT_NOT_FOUND if we see element/locator exception patterns."""
    priority = 4
    category = 'ELEMENT_NOT_FOUND'

    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        current = getattr(failure, 'root_cause_category', 'OTHER')
        if current not in ('OTHER', 'ASSERTION_FAILURE'):
            return False
        root_cause = (failure.root_cause or "").lower()
        execution_log = cache.get_combined_log(failure.test_name).lower()
        text = f"{root_cause} {execution_log}"
        return (
            "nosuchelementexception" in text or
            "staleelementreferenceexception" in text or
            "elementclickinterceptedexception" in text or
            ("nullpointerexception" in text and ("getpageelement" in text or "gettext()" in text or "webelement" in text)) or
            ("indexoutofboundsexception" in text and ("length 0" in text or "out of bounds for length")) or
            ("illegalargumentexception" in text and ("bound must be positive" in text or "element" in text or "locator" in text)) or
            "element" in root_cause and "not found" in root_cause or
            "locator" in root_cause and "not found" in root_cause or
            "is not clickable" in text or
            "waiting for element to be clickable" in text or
            "proxyelement" in text or
            "defaultelementlocator" in text
        )


class OtherToTimeoutRule(CategoryRule):
    """When AI returned OTHER, reclassify to TIMEOUT if we see timeout patterns."""
    priority = 4
    category = 'TIMEOUT'

    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        current = getattr(failure, 'root_cause_category', 'OTHER')
        if current not in ('OTHER', 'ASSERTION_FAILURE'):
            return False
        root_cause = failure.root_cause or ""
        execution_log = cache.get_combined_log(failure.test_name)
        return bool(
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", root_cause, re.IGNORECASE) or
            re.search(r"['\"]([^'\"]+Page[^'\"]*)['\"]\s+(?:NOT|not)\s+loaded\s+even\s+after", execution_log, re.IGNORECASE) or
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+visible\s+even\s+after\s+waiting", root_cause, re.IGNORECASE) or
            re.search(r"Element\s+['\"]([^'\"]+)['\"]\s+is\s+(?:NOT|not)\s+visible\s+even\s+after\s+waiting", execution_log, re.IGNORECASE) or
            # Generalized to handle "is NOT hidden", "is NOT shown", etc.
            re.search(r"is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", root_cause, re.IGNORECASE) or
            re.search(r"is\s+(?:NOT|not)\s+(\w+)\s+even\s+after\s+waiting\s+for\s+\d+\s+seconds", execution_log, re.IGNORECASE) or
            re.search(r"TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", root_cause, re.IGNORECASE) or
            re.search(r"TimeoutException.*waiting\s+for\s+element\s+to\s+be\s+(?:clickable|visible)", execution_log, re.IGNORECASE) or
            ("not loaded even after" in root_cause.lower()) or
            ("not loaded even after" in (execution_log or "").lower())
        )


class OtherToAssertionRule(CategoryRule):
    """When AI returned OTHER, reclassify to ASSERTION_FAILURE if we see assertion patterns."""
    priority = 4
    category = 'ASSERTION_FAILURE'

    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        current = getattr(failure, 'root_cause_category', 'OTHER')
        if current not in ('OTHER', 'ASSERTION_FAILURE'):
            return False
        root_cause = (failure.root_cause or "").lower()
        execution_log = (cache.get_combined_log(failure.test_name) or "").lower()
        text = f"{root_cause} {execution_log}"
        return (
            (re.search(r"expected\s+.*but\s+actual", text, re.IGNORECASE) or re.search(r"actual\s+.*expected", text, re.IGNORECASE)) or
            re.search(r"missing\s+(?:key|field)\s*:", text, re.IGNORECASE) or
            re.search(r"classes\s+of\s+actual\s+and\s+expected\s+key", text, re.IGNORECASE) or
            re.search(r"actual\s+json\s+doesn'?t\s+contain\s+all\s+expected\s+keys", text, re.IGNORECASE) or
            re.search(r"key\s*/\s*value\s+is\s+null", text, re.IGNORECASE) or
            re.search(r"the\s+following\s+asserts\s+failed", text, re.IGNORECASE) or
            ("assertion" in text and ("failed" in text or "error" in text)) or
            ("expected" in text and "actual" in text) or
            # Failure reason patterns for StringIndexOutOfBoundsException
            re.search(r"failure\s+reason\s*:\s*begin\s+\d+\s*,\s*end\s+[-\d]+\s*,\s*length\s+\d+", text, re.IGNORECASE) or
            re.search(r"StringIndexOutOfBoundsException", text, re.IGNORECASE)
        )


class OtherToEnvironmentRule(CategoryRule):
    """When AI returned OTHER, reclassify to ENVIRONMENT_ISSUE if we see environment/network patterns."""
    priority = 4
    category = 'ENVIRONMENT_ISSUE'

    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        current = getattr(failure, 'root_cause_category', 'OTHER')
        if current not in ('OTHER', 'ASSERTION_FAILURE', 'ENVIRONMENT_ISSUE'):
            return False
        root_cause = (failure.root_cause or "").lower()
        execution_log = (cache.get_combined_log(failure.test_name) or "").lower()
        text = f"{root_cause} {execution_log}"
        return (
            "503" in text or "502" in text or "504" in text or
            "connection refused" in text or "connection reset" in text or
            "service unavailable" in text or "bad gateway" in text or "gateway timeout" in text or
            "unknown host" in text or "dns" in text and "error" in text or
            "connection timed out" in text or "connect timed out" in text
        )

class OtherToCodeIssueRule(CategoryRule):
    """When AI returned OTHER or ENVIRONMENT, reclassify to CODE_ISSUE if we see non-Selenium NPE patterns."""
    priority = 5  # Higher than OtherToEnvironmentRule
    category = 'CODE_ISSUE'

    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        current = getattr(failure, 'root_cause_category', 'OTHER')
        if current not in ('OTHER', 'ENVIRONMENT_ISSUE', 'ASSERTION_FAILURE'):
            return False
            
        root_cause = (failure.root_cause or "").lower()
        execution_log = (cache.get_combined_log(failure.test_name) or "").lower()
        text = f"{root_cause} {execution_log}"
        
        # Check for modern Java NullPointerException messages that aren't about Selenium elements
        npe_match = re.search(r"cannot invoke\s+['\"]?[^'\"]+['\"]?\s+because\s+['\"]?[^'\"]+['\"]?\s+is\s+null", text, re.IGNORECASE)
        
        if npe_match:
            # Filter out Selenium-related NPEs which belong to ELEMENT_NOT_FOUND
            selenium_patterns = ["webelement", "getpageelement", "gettext()", "selenium", "locator", "org.openqa.selenium"]
            if any(p in text.lower() for p in selenium_patterns):
                return False
            return True
            
        return False


class OtpIsProductBugRule(CategoryRule):
    """OTP-related failures (e.g. matching OTP not found, OTP delivery) are product/test-flow issues, not environment."""
    priority = 8  # Override ENVIRONMENT_ISSUE when OTP pattern detected
    category = 'ASSERTION_FAILURE'

    def matches(self, failure: FailureClassification, cache: TestDataCache) -> bool:
        current = getattr(failure, 'root_cause_category', 'OTHER')
        # Allow matching even if current category is ASSERTION_FAILURE to prevent downgrade by other rules
        if current not in ('ENVIRONMENT_ISSUE', 'OTHER', 'ASSERTION_FAILURE'):
            return False
        root_cause = (failure.root_cause or "").lower()
        execution_log = (cache.get_combined_log(failure.test_name) or "").lower()
        text = f"{root_cause} {execution_log}"
        if "otp" not in text:
            return False
        return (
            "matching otp not found" in text or "otp not found" in text or
            "otp not received" in text or "otp delivery" in text or
            "otp generation" in text or "otp validation" in text or
            "invalid otp" in text or "wrong otp" in text
        )


class CategoryRuleEngine:
    """Engine to apply category classification rules"""
    
    def __init__(self):
        """Initialize with all rules, sorted by priority (highest first)"""
        self.rules = [
            ElementClickInterceptedRule(),
            PageLoadTimeoutRule(),
            AssertionFailurePriorityRule(),  # High priority check
            ElementLocatorExceptionRule(),
            IllegalArgumentExceptionRule(),
            OtpIsProductBugRule(),  # OTP-related → product (ASSERTION_FAILURE), not environment
            NonPageLoadTimeoutFilterRule(),
            AssertionFailureFilterRule(),
            # When AI returned OTHER, try to upgrade to a specific category from text patterns
            OtherToElementNotFoundRule(),
            OtherToTimeoutRule(),
            OtherToAssertionRule(),
            OtherToCodeIssueRule(),
            OtherToEnvironmentRule(),
        ]
        # Sort by priority (highest first)
        self.rules.sort(key=lambda r: r.priority, reverse=True)
    
    def classify(self, failure: FailureClassification, cache: TestDataCache) -> str:
        """
        Apply rules to classify a failure into a category.
        
        Args:
            failure: FailureClassification object
            cache: TestDataCache for accessing execution logs
            
        Returns:
            Category string (e.g., 'ELEMENT_NOT_FOUND', 'TIMEOUT', 'ASSERTION_FAILURE', 'OTHER')
        """
        # Start with the AI's initial classification
        category = getattr(failure, 'root_cause_category', 'OTHER')
        
        # Merge NETWORK_ISSUE and ENVIRONMENT_FAILURE into ENVIRONMENT_ISSUE
        if category == 'NETWORK_ISSUE' or category == 'ENVIRONMENT_FAILURE':
            category = 'ENVIRONMENT_ISSUE'
        
        # Define category priority mapping (higher number = higher priority)
        # Order: TIMEOUT > ELEMENT_NOT_FOUND > ASSERTION_FAILURE > ENVIRONMENT_ISSUE > OTHER
        category_priority = {
            'TIMEOUT': 15,
            'ELEMENT_NOT_FOUND': 12,
            'CODE_ISSUE': 10,
            'ASSERTION_FAILURE': 20, # Increased priority to respect AssertionFailurePriorityRule
            'ENVIRONMENT_ISSUE': 6,
            'OTHER': 1
        }
        
        # Find all matching rules (instead of breaking on first match)
        # This ensures we select the highest priority category when multiple failures exist
        matching_rules = []
        for rule in self.rules:
            if rule.matches(failure, cache):
                matching_rules.append(rule)
                logger.debug(f"Rule {rule.__class__.__name__} matched for {failure.test_name}: {rule.category}")
        
        # If multiple rules match, select the one with highest priority category
        if matching_rules:
            # Sort by category priority (highest first), then by rule priority
            matching_rules.sort(key=lambda r: (
                category_priority.get(r.category, 0),
                r.priority
            ), reverse=True)
            category = matching_rules[0].category
            logger.debug(f"Selected highest priority category for {failure.test_name}: {category} (from {len(matching_rules)} matching rules)")
        
        # Update the failure object with the final category
        failure.root_cause_category = category
        
        return category

