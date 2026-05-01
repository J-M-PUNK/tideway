# Contributing to Tideway

Thanks for considering a contribution. This document covers how to
report bugs, suggest features, and open pull requests against
Tideway.

By participating in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md). For security vulnerabilities,
please follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.

## Contents

- [Reporting bugs](#reporting-bugs)
- [Suggesting features](#suggesting-features)
- [Setting up a dev environment](#setting-up-a-dev-environment)
- [Pre-PR checks](#pre-pr-checks)
- [Branch naming](#branch-naming)
- [Commits](#commits)
- [Code style](#code-style)
- [Settings additions (foot-gun)](#settings-additions-foot-gun)
- [Logging](#logging)
- [Maintainer release workflow](#maintainer-release-workflow)

## Reporting bugs

Open a [bug report issue](https://github.com/J-M-PUNK/tideway/issues/new?template=bug_report.yml).
The template asks for the version, OS, repro steps, and any
relevant logs. Filling those in up front saves a back-and-forth.

Before filing:

- Update to the [latest release](https://github.com/J-M-PUNK/tideway/releases)
  and confirm the issue still reproduces.
- Search existing issues. There is a good chance someone hit the
  same thing.
- If it is a security issue, do not open a public issue. See
  [SECURITY.md](SECURITY.md).

## Suggesting features

Open a [feature request issue](https://github.com/J-M-PUNK/tideway/issues/new?template=feature_request.yml).
Lead with the user-facing problem, not a proposed implementation.

Skim the [Known limits](README.md#known-limits) section first.
Some asks (Atmos playback, Tidal Connect output, account
impersonation flows) are intentional non-goals and won't be picked
up. The README explains why.

## Setting up a dev environment

```bash
git clone https://github.com/J-M-PUNK/tideway.git
cd tideway
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest httpx        # dev-only test deps
(cd web && npm install)
```

Day-to-day frontend work, with hot module reload:

```bash
./run.sh
```

To exercise the desktop pywebview window (chrome, drag, traffic
lights, global media keys, tray icon), build the frontend then
launch the desktop entry point:

```bash
(cd web && npm run build)
.venv/bin/python desktop.py
```

`desktop.py` serves whatever is in `web/dist/`, so rebuild the
frontend any time you change React code or the desktop window will
show stale UI.

## Pre-PR checks

Run all four locally before opening a PR. CI runs the same four on
every push, but failing CI is annoying for reviewers.

```bash
pytest tests/                       # backend
cd web
npx tsc -b --noEmit                 # typecheck
npm run lint:all                    # eslint + stylelint + htmlhint + prettier
npm test                            # vitest
```

Or just run all of them in sequence:

```bash
./scripts/preflight.sh
```

The 50-or-so pre-existing eslint warnings on
`react-hooks/rules-of-hooks` and `@typescript-eslint/no-explicit-any`
are acceptable. New errors are not.

## Branch naming

One concern per branch. If your work touches two unrelated
subsystems, split it into two branches and two PRs.

- `fix/<slug>` for bug fixes
- `feature/<slug>` for new capability
- `chore/<slug>` for refactors, docs, tests, tooling

Examples: `fix/skip-stale-queue-race`, `feature/aoty-charts`,
`chore/server-split`.

## Commits

- Imperative present-tense subject under 70 chars
  ("Fix queue advance on track end", not "Fixed queue advance" or
  "This commit fixes the queue advance bug")
- Body explains *why*, not *what*. The diff already tells you what.
- One logical change per commit. A branch may have several commits
  if the work has natural stages, but each commit should pass tests
  on its own.
- No `Co-Authored-By` lines, no AI-tool attribution. Commits should
  read as if you wrote them.

## Code style

- **Python**: PEP 8 spacing, type hints where they help readability.
  Aim for clear over clever. The codebase favors small focused
  modules over deep class hierarchies.
- **TypeScript / React**: prettier handles formatting, eslint catches
  the rest. Run `npm run format` to auto-fix style violations
  before committing.
- **No bandaids.** If you find yourself writing a "for now" comment
  or wrapping `try / except` to silence a real error, stop and find
  the actual fix. The project has a strict no-bandaids policy
  documented in [CLAUDE.md](CLAUDE.md) under "No bandaids", and the
  same standard applies to human-authored code.
- **Comments** explain *why* a piece of code is the way it is, not
  *what* it does. The code already says what.

## Settings additions (foot-gun)

Adding a new field to the `Settings` dataclass in `app/settings.py`
requires touching **three** other places or the field will silently
no-op:

1. The matching field in the `SettingsPayload` Pydantic model in
   `server.py`. Pydantic drops unknown fields from PUT bodies, so a
   missing mirror means the value never reaches the dataclass-
   construction step.
2. The matching field on the `Settings` interface in
   `web/src/api/types.ts`.
3. Side-effect handling in the `PUT /api/settings` handler if the
   field needs to do something on change (flip a player flag,
   restart a listener, etc.).

`load_settings()` already filters unknown keys from existing
`settings.json` files, so removing a field doesn't break existing
installs. There is also a sync test (`tests/test_settings_sync.py`)
that fails the build if the dataclass and the Pydantic model drift
out of alignment, so CI will catch the foot-gun for you.

## Logging

The Python `logging` module is not configured to emit in the dev
console. For things developers or users should see during
`./run.sh`, the convention is:

```python
print(f"[component] message", flush=True)
```

For debug detail that only ever reads off a captured log file, use
the standard `logging.getLogger(__name__).debug()`.

## Maintainer release workflow

If you are cutting a release, see
[docs/release-workflow.md](docs/release-workflow.md). Contributors
do not need to read it.
