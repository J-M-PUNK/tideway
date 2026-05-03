# private/ — local-only notes

This directory is for working notes that shouldn't ship to the
public repository: future-feature scoping, roadmap thinking,
personal todos, draft docs that aren't ready for review,
research summaries you want next to the code but don't want
indexed by GitHub search.

The directory itself is committed (so the convention is visible
to anyone cloning the repo); its contents are not. The local
[`.gitignore`](.gitignore) here ignores everything except itself
and this README.

## Suggested layout

Once you have more than a handful of files, organise into
subdirectories. The contents of `private/` are gitignored, so
these exist only on your machine — recreate them as needed on
fresh clones:

- `features/` — bigger plans for new functionality
- `bugs/` — bug investigations, repro notes, fix sketches
- `improvements/` — smaller enhancements, refactors, polish
- `research/` — research notes, competitor analysis, surveys
- `archive/` — shipped or shelved plans, kept for reasoning history

None of these directories are mandatory; pick what fits. A
single flat directory is fine for fewer files.

## What goes here

Good fits:
- Feature scope docs you're not ready to publish yet (e.g. the
  Tidal Connect receiver-mode doc currently on the
  `docs/tidal-connect-receiver-scope` branch could live here
  instead until you decide to ship it).
- Long-running research notes (audiophile feature lists,
  competitor comparisons, packet-capture plans).
- Personal weekly/monthly planning lists.
- Draft release notes before they're polished.
- Findings from spikes that didn't pan out and you don't want
  to forget the reasoning behind.

Bad fits:
- Anything contributors need to see → `docs/` instead.
- Anything that should outlive this machine → see "Sync across
  machines" below.
- Secrets / credentials → don't keep these in any repo, even
  ignored. Use a password manager.

## Sync across machines

`.gitignore` keeps these files off GitHub. If you want history or
multi-machine access, two patterns work:

1. **Private GitHub repo cloned inside `private/`.** Make a
   second private repo (e.g. `tideway-private`), `git clone` it
   into this directory. The outer `.gitignore` already ignores
   everything here, so the inner clone won't leak. You get
   normal git history for your notes without polluting the
   public repo.
2. **A synced folder (Dropbox / iCloud / Syncthing).** Symlink
   `private/` to a folder in your synced storage. Easier; no
   git history.

Either is fine; pick based on whether you want history.

## Why this pattern

A separate private repo would also work but adds a second
remote to manage. A `.gitignore`d directory keeps everything
co-located with the code that motivates the notes — clicking
through file paths in scope docs lands in the actual codebase
without juggling two checkouts.
