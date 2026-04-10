"""
Failure classification data model used by the test-triaging-agent pipeline.
"""


class FailureClassification:
    """Result of AI failure classification (produced by 03_classify.py)."""

    def __init__(self, test_name: str, classification: str, confidence: str,
                 root_cause: str, recommended_action: str, root_cause_category: str = "OTHER",
                 failure_signature: str = None):
        self.test_name = test_name
        self.classification = classification       # PRODUCT_BUG or AUTOMATION_ISSUE
        self.confidence = confidence               # HIGH, MEDIUM, LOW
        self.root_cause = root_cause
        self.recommended_action = recommended_action
        self.root_cause_category = root_cause_category  # ELEMENT_NOT_FOUND, TIMEOUT, etc.
        self.failure_signature = failure_signature or "Unspecified Failure"

    def is_product_bug(self) -> bool:
        if self.root_cause_category == "CODE_ISSUE":
            return False
        return self.classification == "PRODUCT_BUG"

    def is_automation_issue(self) -> bool:
        if self.root_cause_category == "CODE_ISSUE":
            return True
        return self.classification == "AUTOMATION_ISSUE"

    def __repr__(self) -> str:
        icon = "🐛" if self.is_product_bug() else "🔧"
        return f"{icon} {self.test_name}: {self.classification} ({self.confidence})"
