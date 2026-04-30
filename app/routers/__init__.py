"""FastAPI routers, one per domain.

Splitting `server.py`'s 200+ route handlers into per-domain routers
is a long, mechanical refactor — this package is the destination for
that work, but each domain moves in its own commit so review stays
tractable. Each router module:

  - Defines `router = APIRouter()` plus its endpoints.
  - Imports auth helpers + singletons from `server` lazily inside
    handlers (top-of-module imports would create a circular import
    because `server.py` registers each router via `include_router`).
  - Uses the same path prefixes the original handlers exposed (no
    breaking changes to URLs).

`server.py` retains:
  - The FastAPI app instance + middleware + exception handlers.
  - Module-level singletons (TidalClient, Downloader, settings, etc.)
    until they migrate to a shared state module.
  - Auth + loopback helpers (`_require_auth`, `_require_local_access`,
    `_ensure_loopback`) that routers can import.
  - The lifespan + startup wiring.
"""
