"""
Auto-Fix Feature Module
Can be enabled/disabled via configuration.

This module provides automated code fixing and PR creation for test failures
classified as "Product Changes" or "Automation Issues".
"""

import sys
from pathlib import Path

# Add parent directory to path to import Config
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.settings import Config

__version__ = "1.0.0"

# Feature toggle based on configuration
AUTO_FIX_ENABLED = Config.AUTO_FIX_ENABLED

if AUTO_FIX_ENABLED:
    # Import all components when feature is enabled
    from .models import FixProposal, FileChange, PRResult, AutoFixResult
    from .manager import AutoFixManager
    from .code_analyzer import CodeAnalyzer
    from .fix_generator import FixGenerator
    from .test_runner import TestRunner
    from .github import GitHubClient, PRCreator
    
    __all__ = [
        'AutoFixManager',
        'CodeAnalyzer',
        'FixGenerator',
        'TestRunner',
        'GitHubClient',
        'PRCreator',
        'FixProposal',
        'FileChange',
        'PRResult',
        'AutoFixResult',
        'AUTO_FIX_ENABLED',
    ]
else:
    # Provide dummy class when feature is disabled
    class AutoFixManager:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(
                "Auto-fix feature is disabled. "
                "Set AUTO_FIX_ENABLED=true in config/.env to enable."
            )
    
    __all__ = ['AUTO_FIX_ENABLED']

