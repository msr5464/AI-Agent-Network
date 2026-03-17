import logging
from src.reporters.signal_extractor import FailureSignalExtractor
from src.agent.analyzer import FailureClassification
from src.utils import TestDataCache

# Mock TestDataCache
class MockTestDataCache:
    def get_combined_log(self, test_name):
        return ""

def test_signature_extraction():
    cache = MockTestDataCache()
    extractor = FailureSignalExtractor(cache)
    
    # Create failures with pre-defined AI signatures
    failures = [
        # Group 1: API Errors
        FailureClassification("test1", "PRODUCT_BUG", "HIGH", "Root Cause 1", "Action", "ENVIRONMENT_ISSUE", failure_signature="API Error: POST /login 500"),
        FailureClassification("test2", "PRODUCT_BUG", "HIGH", "Root Cause 2", "Action", "ENVIRONMENT_ISSUE", failure_signature="API Error: POST /login 500"),
        FailureClassification("test3", "PRODUCT_BUG", "HIGH", "Root Cause 3", "Action", "ENVIRONMENT_ISSUE", failure_signature="API Error: GET /user 404"),
        
        # Group 2: Element Issues
        FailureClassification("test4", "AUTOMATION_ISSUE", "HIGH", "Root Cause 4", "Action", "ELEMENT_NOT_FOUND", failure_signature="Element Missing: #submit-btn"),
        FailureClassification("test5", "AUTOMATION_ISSUE", "HIGH", "Root Cause 5", "Action", "ELEMENT_NOT_FOUND", failure_signature="Element Missing: #submit-btn"),
        
        # Group 3: Legacy/Fallback (No signature provided)
        FailureClassification("test6", "AUTOMATION_ISSUE", "LOW", "Root Cause 6", "Action", "OTHER", failure_signature=None),
        
        # Group 4: Cleaning test (trailing dot, lowercase)
        FailureClassification("test7", "PRODUCT_BUG", "HIGH", "Root Cause 7", "Action", "ASSERTION_FAILURE", failure_signature="assertion failure: status code 403."),
    ]
    
    print("\n--- Testing AI Signature Extraction ---")
    
    # Test grouping
    signals = extractor.get_signals("ANY_CATEGORY", failures, limit=10)
    print(f"Signals: {signals}")

    # Expected output:
    # API Error: POST /login 500 (2)
    # Element Missing: #submit-btn (2)
    # API Error: GET /user 404 (1)
    # Unspecified Failure (1)
    # Assertion failure: status code 403 (1) -> Capitalized and stripped

    # Validation
    assert "API Error: POST /login 500 (2)" in signals
    assert "Element Missing: #submit-btn (2)" in signals
    assert "Unspecified Failure (1)" in signals
    assert "Assertion failure: status code 403 (1)" in signals
    
    print("\n✅ Verification Passed!")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_signature_extraction()
