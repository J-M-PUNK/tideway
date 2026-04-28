# Tideway — project instructions

These rules are project-scoped and override anything that conflicts in
the global Claude config. Global rules about writing style, commit
attribution, and git hygiene still apply.

## Release workflow

When the user has a batch of unrelated fixes or features to ship in a
single deploy, the workflow is:

1. **One branch per fix.** Branch off `main`. Naming: `fix/<slug>` for
   bug fixes, `feature/<slug>` for new capability, `chore/<slug>` for
   refactors / tooling. One concern per branch — if the work touches
   two unrelated subsystems, split it.
2. **One PR per branch.** PR targets `main`. Title is what the work
   does, not a position in a queue ("Pause queue advance on track-
   end" beats "Bug 3 of 7"). Body has Summary + Test plan.
3. **Hold approved PRs.** Each PR gets reviewed and approved, but is
   NOT merged on its own. The approval is "this fix is ready", not
   "ship it now."
4. **Integrate on a deploy branch.** When the batch is ready to ship:
   - Create `deploy/v<X.Y.Z>` off the latest `main`.
   - Merge each approved branch into the deploy branch with
     `git merge --no-ff <branch>` so the integration history is
     preserved.
   - Resolve any conflicts here, in the integration branch. The
     individual PR branches stay clean.
   - Add a single release commit on top of the deploy branch:
     - Bump version in any version-pinned file.
     - Write release notes (one paragraph per included PR, in past-
       tense human language).
5. **Test the integrated deploy branch before tagging.** The whole
   point of integrating before shipping is to catch problems that
   only show up when fixes interact. Run `./scripts/preflight.sh`
   on the deploy branch (pytest, tsc, lint, vitest). Then do a
   manual sanity sweep: launch the app from this branch and exercise
   each fix's user-visible path. If any fix regresses something,
   drop it from the branch (git revert the merge commit) and keep
   shipping the rest. Don't tag until the deploy branch is green
   and manually verified — main never had the problem and the
   deploy branch is the only place we can catch it.
6. **Tag the release.** `git tag v<X.Y.Z>` on the deploy branch's
   tip and push the tag.
7. **Run the release pipeline.** GitHub Actions builds artifacts off
   the tag.
8. **Catch main back up.** Fast-forward `main` to the deploy branch:
   `git checkout main && git merge --ff-only deploy/v<X.Y.Z> && git push`.
9. **Clean up.** Delete merged PR branches locally and on GitHub.

The integration-branch step is what differentiates this from the
"merge each PR straight to main" pattern. Reasons:

- **Main stays releasable.** If we merge five PRs straight to main and
  one turns out to break something at integration time, main is stuck
  in a half-broken state until we revert. With the integration branch,
  we can drop a problem PR before tagging.
- **The release diff is one branch.** The deploy branch's diff against
  main IS the release diff. Easier to scan than "all commits since
  v0.4.10."
- **Conflicts land in one place.** Better than resolving the same
  conflict three times across three PRs.

When NOT to use this workflow:

- A single isolated bugfix that needs to ship immediately. Just merge
  the PR to main, tag, release. No integration branch needed.
- Hotfixes off a release tag. Branch off the tag, fix, PR, tag a
  patch release, fast-forward main.

## Claude's behavior in this workflow

- When the user says "let's start fix N" or similar, **create a fresh
  branch off main** with the right naming prefix. Don't pile work on
  whatever branch happens to be checked out.
- When the user says "open a PR" or "make a PR", create the PR
  targeting `main`. Don't pre-emptively assemble a deploy branch.
- When the user explicitly says "let's ship the batch" or "build the
  deploy branch" or names a version number, that's the cue to
  integrate. Until then, individual branches stay separate.
- **Never merge PRs without explicit user instruction.** Approval
  happens on GitHub; the user merges when ready, or directs Claude
  to do it.
- **Never tag, push tags, or run release workflows without explicit
  instruction.** Tagging is a release action.
- **Never fast-forward main without explicit instruction.** Even if
  the deploy branch is ready, main moves only when the user says so.

## Commits

In addition to the global rule (no `Co-Authored-By`, no Claude
attribution):

- One logical change per commit on a fix branch. The branch may have
  several commits if the work has natural stages, but each commit
  should pass tests on its own.
- Commit message subject is imperative present tense, under 70 chars
  ("Fix queue advance on track end", not "Fixed queue advance" or
  "This commit fixes the queue advance bug").
- Body explains WHY, not WHAT. The diff already tells you what; the
  commit message tells future-you why it had to change.

## Tests and lint before PR

Before opening a PR, the branch must be green:

- `pytest tests/` — backend.
- `cd web && npx tsc -b --noEmit` — typecheck.
- `cd web && npm run lint:all` — eslint, stylelint, htmlhint, prettier
  (existing 50 warnings on rules-of-hooks / no-explicit-any are
  acceptable; new errors are not).
- `cd web && npm test` — frontend vitest.

CI runs all four on every PR, but failing CI is annoying for the
reviewer; run them locally first.

## Settings dataclass and Pydantic mirror

When adding a new field to `Settings` in `app/settings.py`, the field
also has to land in **three** other places or the toggle will silently
no-op:

1. `SettingsPayload` Pydantic model in `server.py`. Pydantic drops
   unknown fields from PUT bodies, so missing the mirror means the
   value never reaches the dataclass-construction step.
2. `Settings` interface in `web/src/api/types.ts`.
3. The PUT handler in `server.py` if the field needs side effects on
   change (e.g. flipping a player flag, restarting a listener).

This was a real foot-gun while building cross-device-pause —
specifically the missing `SettingsPayload` field caused the toggle to
revert silently with no error to either side. Worth a checklist or a
test that asserts the dataclass and the Pydantic model stay in sync.

## Logging

The Python `logging` module isn't configured to emit visibly in the
dev console. The existing pattern is bare `print(f"[component] msg",
flush=True)` for things the developer or user should see during
`./run.sh`, with the standard `logging.getLogger(__name__).debug()`
reserved for noise that's only ever read off a captured log file.

If you need a high-signal line (mint failure, takeover received,
state change), use the print pattern. If you need debug detail
(per-frame trace, periodic heartbeat), use the logger.

## Cross-device pause specifics

The realtime listener (`app/tidal_realtime.py`) is **always on** and
spawns at server boot. It's gated under pytest via `_under_pytest()`
in `server.py` so the test suite doesn't open a live WS to Tidal. If
new tests need to exercise the listener, mock it directly — don't
rely on starting it through the boot path.

Diagnostics: `GET /api/realtime/status` returns the listener's runtime
state (running / connected / last_error / counters). Use it before
debugging by log-grep.
