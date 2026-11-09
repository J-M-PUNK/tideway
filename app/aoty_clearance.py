"""Cloudflare clearance for the AOTY scraper.

Background (2026-09-17): albumoftheyear.org escalated from
fingerprint-based bot filtering to a site-wide Cloudflare **managed
challenge** (`cf-mitigated: challenge`, `cType: 'managed'`). Every
path except `/robots.txt` serves the "Just a moment..." interstitial.

This is categorically different from the two earlier outages, which
were both profile staleness and were fixed by moving the
`impersonate=` target. A managed challenge is solved by *executing
Cloudflare's JavaScript* and taking the `cf_clearance` cookie it
issues. No TLS fingerprint passes it — a live sweep of all 21
impersonation profiles in curl_cffi 0.16.2 (chrome150 down to
chrome99, edge, firefox147, safari2601, the mobile variants, and
tor145) drew a challenge on every single one, 8/8 attempts over 30
seconds. Bumping `_CFFI_IMPERSONATE` again cannot fix this.

What does work: Tideway already ships a browser engine. The desktop
shell renders the whole UI in pywebview — WebView2 (Chromium) on
Windows, WKWebView on macOS, WebKitGTK on Linux. That engine executes
the challenge the same way any browser does. So the shell opens a
hidden child window at an AOTY URL, waits for the interstitial to
clear, and hands back the `cf_clearance` cookie plus the User-Agent
that earned it. `app/aoty.py` then attaches both to its ordinary
curl_cffi requests, which go back to returning 200 in ~0.3s.

Measured on Windows/WebView2: the solve costs one hidden-window
round trip (~2-5s warm, up to ~40s on a cold WebView2 start), and
every fetch afterwards is full speed. The prewarm thread in
`server.py` already runs at startup off the critical path, so the
solve lands there rather than in front of a user-visible request.

**The User-Agent override is load-bearing.** `cf_clearance` is bound
to the UA that obtained it; replaying the cookie under curl_cffi's
own impersonated UA is rejected outright:

    chrome + WebView2 UA -> 200, 97417 bytes
    chrome alone         -> 403, cf-mitigated: challenge

This directly inverts the "send NO custom headers" rule documented on
`_CFFI_IMPERSONATE` in `aoty.py`. That rule still holds for
unauthenticated requests, where a header inconsistent with the TLS
hello reads as automation. Once we carry a clearance cookie the
constraint flips: the UA *must* match the one in the cookie's
fingerprint, so `aoty.py` sends it deliberately. Both paths are
correct; they just answer to different checks.

No cookie is persisted here. pywebview runs with `private_mode=False`
and a stable `storage_path` (see `desktop.py`), so the webview's own
profile already carries `cf_clearance` across restarts — on a second
launch the hidden window is served the page directly and the harvest
is fast. Duplicating that into a second on-disk cache would give us
two sources of truth that can disagree.

When no solver is registered — dev runs under `run.sh`, the
`--browser` fallback, any headless server — there is no browser
engine to drive and no clearance is possible. `aoty.py` then behaves
exactly as it did before: the fetch fails, the block flag is set, and
the Home page renders its "AOTY is blocking us" notice. Degraded, but
honest.

Only background work may solve. A solve runs for up to 90 seconds, and
since November 2026 AOTY's challenge wants a human click and then
rejects the embedded browser anyway, so a Home request that waited on
one sat for most of that time to show the "unavailable" notice. The
startup prewarm runs inside `solving_allowed()`; everything else uses
a clearance the prewarm already earned, or fails straight away.
"""
from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

# The solver takes the URL to load and the `cf_clearance` value that
# was just rejected (None on a first solve), and returns the cookie jar
# plus the User-Agent string from the window that earned it — or None if
# the challenge never cleared.
#
# The rejected token is what makes a re-solve meaningful. pywebview runs
# with a persistent profile, so a solve normally finds a cookie already
# in the jar and returns immediately. That's the fast path and it's
# usually right, but when the cookie we just had was refused, returning
# that same value again would be useless. Passing it down lets the
# solver wait for Cloudflare to issue a genuinely new one.
SolverResult = Optional[tuple[dict, str]]
Solver = Callable[[str, Optional[str]], SolverResult]

# There is deliberately no expiry clock on a clearance. Only Cloudflare
# knows when a `cf_clearance` stops working, and the cheapest way to ask
# is to use it — a 403 on a request we thought was cleared is the one
# authoritative signal, and `_fetch` already turns that into a refresh.
#
# There used to be a 25 minute ceiling here, and it was a real bug
# rather than a safety net. `get()` returned None once a clearance
# passed that age, so a cookie that was still perfectly good went
# unused: the next request went out bare, drew the managed challenge,
# and paid a full solve — 30 to 90 seconds with a hidden window — every
# 25 minutes of normal use. From the outside the AOTY rows simply hung
# and timed out at random intervals.
#
# The asymmetry is the whole argument. Trying a dead cookie costs one
# cheap 403 and the refresh we would have done anyway; declining to try
# a live one costs a minute of solving.


@dataclass(frozen=True)
class Clearance:
    """A Cloudflare clearance cookie and the UA it is bound to."""

    cookies: dict
    user_agent: str
    obtained_at: float

    # `obtained_at` is provenance for logs and debugging only. Nothing
    # branches on it: see the note above on why age is not an expiry
    # signal here.


_solver: Optional[Solver] = None
_solver_lock = threading.Lock()
# Set when a solver registers. The AOTY prewarm thread starts during
# FastAPI's lifespan startup, which under the packaged app runs several
# seconds before `webview.start()` — so the prewarm would otherwise ask
# for a clearance before there is any GUI loop to open a window on, and
# spend its one shot failing. It waits on this instead.
_solver_ready = threading.Event()

_state_lock = threading.Lock()
_clearance: Optional[Clearance] = None

# Single-flight. Opening two challenge windows at once would race for
# the same cookie and show the user two hidden webviews' worth of work
# for one result; the prewarm thread and a user-triggered drill-down
# landing together is the realistic case. Holders of this lock may
# block for tens of seconds, which is why it is separate from the
# short `_state_lock` that guards the cached value.
_solve_lock = threading.Lock()

# How long a failed solve stands before anyone opens another window.
# A solve that fails has run its full 90 seconds, and every request
# that drew a challenge meanwhile is queued on `_solve_lock` behind it.
# Without this, each of them then ran its own 90 second solve against
# the same wall, so the AOTY rows on Home hung for minutes and often
# never loaded. Matches `_BLOCK_WINDOW_SEC` in aoty.py, the time the
# Home page shows its "unavailable" notice, so the rows stay quiet for
# exactly as long as the notice says they will.
_FAILED_SOLVE_HOLD_SEC = 600.0
# Monotonic time of the last solve that produced no clearance, or None.
# Guarded by `_state_lock`.
_failed_at: Optional[float] = None

# Whether the code running now may start, or wait on, a solve. Off by
# default, so a request serving the UI never does; the startup prewarm
# turns it on with `solving_allowed()`. A context variable rather than
# a parameter because the solve is reached through every AOTY listing
# function, and only the prewarm's calls differ.
_may_solve: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "aoty_may_solve", default=False
)


@contextmanager
def solving_allowed() -> Iterator[None]:
    """Let AOTY fetches inside this block solve the challenge.

    For background work only (the startup prewarm). A request a user is
    waiting on must never sit through a solve; see the module docstring.
    """
    token = _may_solve.set(True)
    try:
        yield
    finally:
        _may_solve.reset(token)


def register_solver(fn: Solver) -> None:
    """Install the challenge solver. Called once by `desktop.py` after
    `webview.start()` is running, since only the shell owns a GUI loop
    that can open a window."""
    global _solver
    with _solver_lock:
        _solver = fn
    _solver_ready.set()


def solver_available() -> bool:
    """True when a browser engine is around to solve challenges."""
    with _solver_lock:
        return _solver is not None


def wait_for_solver(timeout_sec: float) -> bool:
    """Block until a solver registers, up to `timeout_sec`.

    For background warmers that start before the desktop shell has a
    GUI loop. Returns False on timeout, which is the normal outcome
    under `run.sh` or `--browser` where no solver ever arrives — the
    caller should carry on and let the fetch fail honestly rather than
    treat this as an error.
    """
    return _solver_ready.wait(timeout_sec)


def get() -> Optional[Clearance]:
    """Whatever clearance we hold, or None if we hold none.

    Deliberately does not screen on age. Only Cloudflare knows when a
    `cf_clearance` stops working, and the cheapest way to ask is to use
    it: a cookie that still works saves a solve, and one that doesn't
    costs a single 403 that `_fetch` turns straight into a `refresh()`.
    Screening here instead threw away working cookies and paid a
    minute-long solve for each one.

    Never solves — callers that want a solve ask for `refresh()`.
    """
    with _state_lock:
        return _clearance


def refresh(stale: Optional[Clearance] = None, *, probe_url: str) -> Optional[Clearance]:
    """Solve the challenge and return a new clearance.

    `stale` is the clearance the caller just had rejected. If another
    thread already replaced it while this one waited on the solve lock,
    that newer value is returned without solving again — which is what
    keeps a burst of 403s from queueing up a burst of windows.

    For the same reason in the other direction, it returns None without
    solving while a recent solve has failed (`_FAILED_SOLVE_HOLD_SEC`):
    the requests queued behind a failed solve must not each go on to
    open a window of their own.

    Outside `solving_allowed()` it never solves or waits: it returns a
    clearance the background solve has already replaced `stale` with,
    or None.
    """
    global _clearance, _failed_at

    with _solver_lock:
        solver = _solver
    if solver is None:
        return None

    if not _may_solve.get():
        with _state_lock:
            current = _clearance
        if current is not None and current is not stale:
            return current
        return None

    with _solve_lock:
        # Re-check under the lock: whoever held it before us may have
        # already done the work this caller is waiting for.
        with _state_lock:
            current = _clearance
        # Identity, not age: the only thing that disqualifies what we
        # hold is that it is the exact value the caller just had
        # refused. Anything else means another thread solved while this
        # one waited, and that result is what this caller wanted.
        if current is not None and current is not stale:
            return current
        with _state_lock:
            failed_at = _failed_at
        if failed_at is not None and time.monotonic() - failed_at < _FAILED_SOLVE_HOLD_SEC:
            return None

        rejected = stale.cookies.get("cf_clearance") if stale else None
        try:
            result = solver(probe_url, rejected)
        except Exception as exc:
            # A solver failure is a real signal, not something to
            # swallow: it means the shell could not open or drive a
            # window. Surface it and let the caller fall through to the
            # block notice.
            print(
                f"[aoty] clearance solver raised {exc!r} — AOTY rows will "
                f"stay empty until the next attempt",
                flush=True,
            )
            result = None

        cookies, user_agent = result or ({}, "")
        if not cookies or not cookies.get("cf_clearance") or not user_agent:
            # No clearance came back: the challenge never cleared, the
            # solver raised, or the window loaded something that wasn't
            # a cleared page. Record it so the requests queued behind
            # this one give up rather than solving again.
            with _state_lock:
                _failed_at = time.monotonic()
            return None

        fresh = Clearance(
            cookies=dict(cookies),
            user_agent=user_agent,
            obtained_at=time.time(),
        )
        with _state_lock:
            _clearance = fresh
            _failed_at = None
        print(
            f"[aoty] obtained Cloudflare clearance via the app's webview "
            f"({len(cookies)} cookies)",
            flush=True,
        )
        return fresh


def reset_for_tests() -> None:
    """Drop solver + cached clearance. Test-support only."""
    global _solver, _clearance, _failed_at
    with _solver_lock:
        _solver = None
    _solver_ready.clear()
    with _state_lock:
        _clearance = None
        _failed_at = None
