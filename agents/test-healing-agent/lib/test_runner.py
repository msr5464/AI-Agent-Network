"""Moved to `shared/test_runner.py`.

Invoking a test is not healing-specific: the reproduce step, the fix step, probes
and test-adaptation-agent's verification all have to run a test the *same* way,
or a fix "verified" by a different command than the one that produced the failure
proves nothing.

This shim keeps the old import path working. See failure_clusters.py for why the
path bootstrap is here.
"""

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[3]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from shared.test_runner import *                # noqa: F401,F403
from shared.test_runner import (                # noqa: F401
    _as_properties, _keep_for_capture, _run_streaming, detect_test_command,
    run_test, split_test_name,
)
