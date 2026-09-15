"""Moved to `shared/code_analyzer.py`.

Source scanning (page objects, test files, brace-aware member splitting) is used
by test-healing-agent, `shared/test_catalog.py` and now test-adaptation-agent, so
it lives in `shared/`. `shared/test_catalog.py` used to reach *into* this agent's
lib/ via a sys.path insert to get at it; that hack is gone.

This shim keeps the old import path working. See failure_clusters.py for why the
path bootstrap is here.
"""

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[3]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from shared.code_analyzer import *              # noqa: F401,F403
from shared.code_analyzer import (              # noqa: F401
    CodeAnalyzer, _class_body_span, _classify_member, _is_within, _iter_source_files,
    _match_brace, _skip_annotation, _skip_noise, _strip_annotations, invalidate_file,
    read_source, reset_caches, source_roots, split_class_members,
)
