# Contributing to Tideway

## Release workflow

Tideway batches unrelated bug fixes and small features into a single
deploy with a sequence of focused PRs and an integration branch.

### One branch per fix

Branch off `main`. Naming:

- `fix/<slug>` for bug fixes
- `feature/<slug>` for new capability
- `chore/<slug>` for refactors and tooling

One concern per branch. If the work touches two unrelated subsystems,
split it.

### One PR per branch

Each branch opens its own PR against `main`. Title is what the work
does, not a position in a queue. Body has:

- **Summary**: 1 to 3 bullets describing the change.
- **Test plan**: a checklist of how to verify the fix.

### Hold approved PRs

PRs are reviewed and approved as they come in, but **not merged on
their own**. Approval means "this fix is ready" — shipping happens
on a release schedule, not per-PR.

### Integrate on a deploy branch

When the batch is ready to ship:

```bash
git checkout main
git pull
git checkout -b deploy/v0.X.Y

# Merge each approved branch with --no-ff so integration history
# is preserved.
git merge --no-ff fix/branch-one
git merge --no-ff fix/branch-two
git merge --no-ff feature/branch-three
# ...

# Resolve any conflicts here, in the integration branch.
# The individual PR branches stay clean.

# Bump the VERSION file and write the release notes commit. The
# notes go into the commit message body — the release workflow
# pulls them out of `git log -1 --format=%b` and uses them as the
# GitHub release body, so anything you'd want a user to read on
# the Releases page goes here. Subject line stays a one-liner.
git commit -m "Release 0.X.Y: <one-line summary>

## <Section heading>

### <What changed, user-facing>

Paragraph explaining the change in plain language — same shape
as the v0.4.10 / v0.4.11 release notes.

## <Another section>
..."

# Test the integrated branch BEFORE tagging.
./scripts/preflight.sh
# Plus a manual smoke pass: launch the app from this branch and
# exercise each fix's user-visible path. Fixes that look fine in
# isolation can still interact badly once merged together — the
# deploy branch is the only place to catch that. If you find a
# regression, revert the offending merge commit and keep shipping
# the rest. Do NOT tag until the deploy branch is green and
# manually verified.

# Tag the release commit.
git tag v0.X.Y

# Push the branch and tag.
git push -u origin deploy/v0.X.Y
git push origin v0.X.Y
```

### Publish the draft release

GitHub Actions picks up the tag and runs the Release workflow,
which builds the three platform installers (mac DMG + Windows
x64 EXE + Windows ARM64 EXE) and creates a **draft** release on
the Releases page with:

- Title and tag binding set to `v0.X.Y`
- Body populated from your release commit's message body
- All three installers attached as assets

The draft sits there for a final human review:

1. Open https://github.com/J-M-PUNK/tideway/releases — there will
   be a "Draft" badge on `v0.X.Y` at the top of the list.
2. Skim the auto-populated body. Edit on the GitHub UI if you
   want to tweak wording, reorder sections, etc. (Permanent
   improvements should also land back in the release commit so
   `git log` and the Releases page stay in sync.)
3. Click **Publish release**.

Publishing is what actually ships. The auto-updater on user
installs hits `GET /repos/.../releases/latest`, which excludes
drafts — so a release in draft state is invisible to users and
the auto-update notification never fires. If you tag, walk away,
and forget to publish, no one gets the update.

### Catch main back up

After the release is built and verified:

```bash
git checkout main
git merge --ff-only deploy/v0.X.Y
git push
```

Then delete the merged PR branches locally and on GitHub.

### Why an integration branch

- **Main stays releasable.** If a PR turns out to break something at
  integration time, drop it from the deploy branch before tagging.
  No revert dance on `main`.
- **The release diff is one branch.** `git diff main..deploy/v0.X.Y`
  IS the release diff.
- **Conflicts land in one place.** Better than resolving the same
  conflict across three PRs.

### When NOT to use this workflow

- A single isolated bugfix that needs to ship immediately. Merge the
  PR to main, tag, release.
- Hotfixes off a release tag. Branch off the tag, fix, PR, tag a
  patch release.

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

The 50 pre-existing eslint warnings on `react-hooks/rules-of-hooks`
and `@typescript-eslint/no-explicit-any` are acceptable. New errors
are not.

## Commits

- Imperative present-tense subject under 70 chars.
- Body explains why, not what.
- One logical change per commit. A branch may have several commits
  if the work has natural stages, but each commit should pass tests
  on its own.
- No `Co-Authored-By` lines, no AI-tool attribution.

## Settings additions

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
   restart a listener, etc).

`load_settings()` already filters unknown keys from existing
`settings.json` files, so removing a field doesn't break existing
installs.

## Logging

The Python `logging` module isn't configured to emit in the dev
console. For things developers or users should see during
`./run.sh`, the convention is:

```python
print(f"[component] message", flush=True)
```

For debug detail that only ever reads off a captured log file, use
the standard `logging.getLogger(__name__).debug()`.
