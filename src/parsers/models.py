"""
Data models for test results and summaries.
"""

from dataclasses import dataclass
from typing import Optional
from enum import Enum
from ..utils import remove_duplicate_class_name


class TestStatus(Enum):
    """Test execution status"""
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass
class TestResult:
    """Represents a single test case result"""
    class_name: str
    method_name: str
    status: TestStatus
    duration_seconds: float
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    stack_trace: Optional[str] = None
    platform: Optional[str] = None  # WEB, API, MOBILE
    execution_log: Optional[str] = None  # NEW: Complete execution log from HTML
    description: Optional[str] = None  # English description of what the test case does
    known_failure: Optional[str] = None  # Jira ticket ID if test passed due to known failure
    
    @property
    def full_name(self) -> str:
        """Get fully qualified test name"""
        # CRITICAL: Remove duplicate class names if present
        # Example: "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis.TestDashBusinessesApis"
        # Should become: "Automation.Access.AccountOpening.api.dash.TestDashBusinessesApis"
        cleaned_class_name = remove_duplicate_class_name(self.class_name)
        return f"{cleaned_class_name}.{self.method_name}"
    
    
    @property
    def is_failure(self) -> bool:
        """Check if test failed or errored"""
        return self.status in [TestStatus.FAIL, TestStatus.ERROR]
    
    def __repr__(self) -> str:
        status_icon = "✅" if self.status == TestStatus.PASS else "❌"
        return f"{status_icon} {self.full_name} ({self.status.value})"


@dataclass
class TestSummary:
    """Summary statistics for a test run"""
    total: int
    passed: int
    failed: int
    skipped: int
    errors: int
    duration_seconds: float
    
    @property
    def pass_rate(self) -> float:
        """Calculate pass rate percentage"""
        if self.total == 0:
            return 0.0
        return (self.passed / self.total) * 100
    
    def __repr__(self) -> str:
        return (
            f"TestSummary(total={self.total}, passed={self.passed}, "
            f"failed={self.failed}, pass_rate={self.pass_rate:.1f}%)"
        )


@dataclass
class FailureSummary:
    """Represents a failure from CSV summary"""
    testrail_id: str
    platform: str
    class_name: str
    test_name: str
    failure_reason: str
    maintained_by: str
    status: str
    
    @property
    def full_name(self) -> str:
        """Get fully qualified test name"""
        return f"{self.class_name}.{self.test_name}"
