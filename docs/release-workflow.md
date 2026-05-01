# Release workflow (maintainer)

Tideway batches unrelated bug fixes and small features into a single
deploy with a sequence of focused PRs and an integration branch.
This is the maintainer-facing flow. Contributors do not need to read
this — see [CONTRIBUTING.md](../CONTRIBUTING.md) for the contributor
side.

## Why an integration branch

- **Main stays releasable.** If a PR turns out to break something at
  integration time, drop it from the deploy branch before tagging.
  No revert dance on `main`.
- **The release diff is one branch.** `git diff main..deploy/v0.X.Y`
  IS the release diff.
- **Conflicts land in one place.** Better than resolving the same
  conflict across three PRs.

## When NOT to use this workflow

- A single isolated bugfix that needs to ship immediately. Merge the
  PR to main, tag, release.
- Hotfixes off a release tag. Branch off the tag, fix, PR, tag a
  patch release.

## Steps

### 1. Hold approved PRs

PRs are reviewed and approved as they come in, but **not merged on
their own**. Approval means "this fix is ready", shipping happens on
a release schedule, not per-PR.

### 2. Build the deploy branch

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

# Resolve any conflicts here, in the integration branch. The
# individual PR branches stay clean.
```

### 3. Bump version + write release notes

```bash
# Bump VERSION at the repo root.
echo "0.X.Y" > VERSION

# Write the release notes commit. The body becomes the GitHub
# release body — the workflow extracts it via
# `git log -1 --format=%b`. Anything you'd want a user to read on
# the Releases page goes here. Subject stays a one-liner.
git add VERSION
git commit -m "Release 0.X.Y: <one-line summary>

## <Section heading>

Paragraph explaining the change in plain language.

## <Another section>

..."
```

### 4. Test the integrated deploy branch BEFORE tagging

```bash
./scripts/preflight.sh
```

Then a manual smoke pass: launch the app from this branch and
exercise each fix's user-visible path. Fixes that look fine in
isolation can still interact badly once merged together. The
deploy branch is the only place to catch that. If you find a
regression, revert the offending merge commit and keep shipping the
rest. **Do not tag** until the deploy branch is green and manually
verified.

For the desktop pywebview window specifically (chrome, drag,
traffic lights, global media keys, tray), you have to build the
frontend and launch via `desktop.py` rather than `./run.sh`:

```bash
(cd web && npm run build)
.venv/bin/python desktop.py
```

### 5. Tag and push

```bash
git tag v0.X.Y
git push -u origin deploy/v0.X.Y
git push origin v0.X.Y
```

### 6. Wait for the Release workflow

GitHub Actions picks up the tag and runs `.github/workflows/release.yml`,
which builds the platform installers (mac DMG, Windows x64 EXE,
Windows ARM64 EXE, Linux AppImage) and creates a **draft** release on
the Releases page with:

- Title and tag binding set to `v0.X.Y`
- Body populated from your release commit's message body
- All installers attached as assets

### 7. Publish the draft

The draft sits there for a final human review:

1. Open <https://github.com/J-M-PUNK/tideway/releases>. There will be
   a "Draft" badge on `v0.X.Y` at the top of the list.
2. Skim the auto-populated body. Edit on the GitHub UI if you want
   to tweak wording, reorder sections, etc. Permanent improvements
   should also land back in the release commit so `git log` and the
   Releases page stay in sync.
3. Click **Publish release**.

Publishing is what actually ships. The auto-updater on user installs
hits `GET /repos/.../releases/latest`, which excludes drafts, so a
release in draft state is invisible to users and the auto-update
notification never fires. If you tag, walk away, and forget to
publish, no one gets the update.

### 8. Catch main back up

After the release is built, verified, and published:

```bash
git checkout main
git merge --ff-only deploy/v0.X.Y
git push
```

Then delete the merged PR branches locally and on GitHub.
