
import logging
from src.reporters.signal_extractor import FailureSignalExtractor
from src.agent.analyzer import FailureClassification
from src.utils import TestDataCache

# Mock TestDataCache
class MockTestDataCache:
    def get_combined_log(self, test_name):
        return ""

def test_signal_extraction():
    cache = MockTestDataCache()
    extractor = FailureSignalExtractor(cache)
    
    # Test cases mimicking the user report
    failures = [
        # Case 1: "The Test (3)" - likely causing naive fallback
        FailureClassification("test1", "PRODUCT_BUG", "HIGH", "The test validates that after approving a pending transfer...", "Action"),
        FailureClassification("test2", "PRODUCT_BUG", "HIGH", "The test failed without a specific error message...", "Action"),
        FailureClassification("test3", "PRODUCT_BUG", "HIGH", "The test scenario for approval flow failed...", "Action"),
        
        # Case 2: "Element 'Cardpage:Card (2)" - Regex capture issue
        FailureClassification("test4", "AUTOMATION_ISSUE", "HIGH", "Element 'Cardpage:Card' is NOT visible", "Action"),
        FailureClassification("test5", "AUTOMATION_ISSUE", "HIGH", "Element 'Cardpage:Card' is NOT visible", "Action"),
        
        # New cases as per instruction
        FailureClassification("test9", "AUTOMATION_ISSUE", "HIGH", "Otp Form Did not load properly", "Action"),
        FailureClassification("test10", "PRODUCT_BUG", "HIGH", "00' timestamp error", "Action"),
        
         # Case 3: "Cannot Invoke (1)" - Naive fallback
        FailureClassification("test6", "AUTOMATION_ISSUE", "HIGH", "Cannot invoke method because object is null", "Action"),

        # Case 4: "Expected 'Transfer (1)" - Regex capture issue
        FailureClassification("test7", "PRODUCT_BUG", "HIGH", "Expected 'Transfer' was :-'true'. But actual is 'false'", "Action"),
        
        # Case 5: "Verified 'Past (1)" - Naive fallback
        FailureClassification("test8", "PRODUCT_BUG", "HIGH", "Verified 'Past' tab presence failed", "Action"),

        # Round 3 Cases
        FailureClassification("test11", "AUTOMATION_ISSUE", "HIGH", "Toast message 'Success' not displayed", "Action"),
        FailureClassification("test12", "PRODUCT_BUG", "HIGH", "Expected status code 200 but found 404", "Action"),
        FailureClassification("test13", "ENVIRONMENT_ISSUE", "HIGH", "API Error: Internal Server Error", "Action"),
        FailureClassification("test14", "PRODUCT_BUG", "HIGH", "Login failed", "Action"), 
    ]
    
    print("\n--- Testing Signal Extraction ---")
    
    # Test PRODUCT_BUG grouping
    product_bugs = [f for f in failures if f.is_product_bug()]
    signals_pb = extractor.get_signals("ASSERTION_FAILURE", product_bugs)
    print(f"ASSERTION_FAILURE Signals: {signals_pb}")
    
    # Test AUTOMATION_ISSUE grouping
    automation_issues = [f for f in failures if f.is_automation_issue()]
    signals_ai = extractor.get_signals("TIMEOUT", automation_issues) # Using TIMEOUT as a bucket for some
    print(f"TIMEOUT/Element Signals: {signals_ai}")
    
    # Test Mixed grouping (Simulating what might happen in "Other" or specific categories)
    # Let's test specific categories directly
    
    # Test Case 1: "The Test" issue in default/other category
    other_failures = [f for f in failures[:3]]
    signals_other = extractor.get_signals("OTHER", other_failures)
    print(f"OTHER (The Test) Signals: {signals_other}")

    # Test Round 3 Specifics
    round3_failures = [f for f in failures if f.test_name in ["test11", "test12", "test13", "test14"]]
    signals_r3 = extractor.get_signals("OTHER", round3_failures)
    print(f"Round 3 (Toast/Status/API) Signals: {signals_r3}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_signal_extraction()
