"""Moved to `shared/failure_clusters.py`.

Clustering is not healing-specific — test-adaptation-agent groups work the same
way — so it lives in `shared/` now. This shim keeps the old import path working
for anything that loads it by file path (tests/unit/test_failure_clusters.py
does exactly that) rather than through the package.

The sys.path bootstrap is deliberate: a module executed via
`spec_from_file_location` has no package context, so `shared` is only importable
if the repo root is on the path, and the loader is not obliged to have put it there.
"""

import sys as _sys
from pathlib import Path as _Path

_ROOT = _Path(__file__).resolve().parents[3]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from shared.failure_clusters import *          # noqa: F401,F403
from shared.failure_clusters import (           # noqa: F401
    Cluster, _element_key, _normalize, build_clusters, cluster_key, evidence_rank,
)
