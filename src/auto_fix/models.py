"""
Data models for the auto-fix feature.
Defines all data structures used by auto-fix components.
"""

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class AdditionalChange:
    """Represents an extra file edit besides the main test method"""
    file_path: str
    original_snippet: str
    updated_snippet: str


@dataclass
class FixProposal:
    """Represents a proposed code fix"""
    original_code: str
    fixed_code: str
    explanation: str
    confidence: str  # HIGH, MEDIUM, LOW
    file_path: str
    plan_summary: List[str] = field(default_factory=list)
    additional_changes: List[AdditionalChange] = field(default_factory=list)


@dataclass
class FileChange:
    """Represents a file change to be applied"""
    file_path: str
    new_content: str
    change_type: str = "modify"  # "modify", "create", "delete"


@dataclass
class PRResult:
    """Result of PR creation"""
    success: bool
    pr_url: Optional[str] = None
    error: Optional[str] = None


@dataclass
class AutoFixResult:
    """Result of auto-fix attempt"""
    test_name: str
    success: bool
    pr_url: Optional[str] = None
    error: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
