"""Per-user cache location for imported AutoEQ profiles.

Tideway used to bundle a manifest fetcher / catalog updater here
that pulled the full ~5,000-profile AutoEQ catalog from GitHub on
demand and supported per-source search. That whole flow was
removed once we shifted to user-imported profiles only — the
maintenance burden of keeping an in-app catalog browser in sync
with autoeq.app's evolving export formats wasn't worth it when
users can just generate the profile they want on autoeq.app and
import the file.

Only `cache_dir()` survives. It's where `/api/eq/import-profile`
writes new profiles, and what the index walker treats as a
secondary root alongside the (now-empty) bundled-data dir. Layout
inside it mirrors AutoEQ's own:

    user_data_dir/autoeq_cache/results/<source>/<headphone>/
        <Brand Model> ParametricEQ.txt

For user imports `<source>` is always "User imported"; see the
constant in `server.py`.
"""
from __future__ import annotations

from pathlib import Path

from app.paths import user_data_dir


def cache_dir() -> Path:
    """Where imported AutoEQ profiles live. Created on first write."""
    return user_data_dir() / "autoeq_cache" / "results"
