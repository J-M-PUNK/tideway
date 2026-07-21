Thanks for the detailed review. Commit `f8c05e7c` addresses all the merge blockers:

- **#1 (Cast regression):** `do_GET` now gates Phase 1/2/head-only on `server.dlna`. Cast path uses `_force=True` always, skipping head cache and `set_ring_active` — same supersede-and-serve-live semantics as before.
- **#2 (overflow accounting):** Both drop paths (`block=False` and `block=True` timeout) now add `overflow` to `_total_read`.
- **#3a (race):** `passthrough_active = True` set before `flush()`.
- **#3b (lock scope):** `_head` filling and all `flush()` state mutations moved inside `with self._cv:`.
- **#4 (id collision):** Guard now keys on `tuple(source)` instead of `id(source)`. PT comment translated.
- **#6a, #6b, #6d:** All addressed.

Bonus: this commit also adds stale `?ts=` request handling (UAPP Range requests to previous track URLs now serve from head cache only, without stealing the ring lock), a 30s socket write timeout, and `_track_id` consistency between buffer and URL.

Still open (non-blocking, follow-up commits on this branch):
- **#5a (seek):** Not yet implemented. Will document as known limitation in PR description and open a follow-up issue.
- **#5b (last-track source_done):** Not yet implemented. Will do as follow-up commit.
- **#6c (bare except):** Only `_build_load_pipeline` was fixed; other `start_passthrough` call sites still swallow exceptions silently. Will audit and fix in follow-up commit.
