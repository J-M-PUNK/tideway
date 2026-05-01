<!--
Thanks for opening a PR. Fill in the sections below. The checklist
at the bottom mirrors what CI runs, so ticking it off as you go
makes the review faster.
-->

## Summary

<!-- 1 to 3 bullets describing what changed and why. -->

-

## Test plan

<!--
Steps a reviewer can follow to verify the change. Be concrete:
"Open Settings, toggle X, observe Y" beats "tested locally". If the
change is hard to verify by hand, link to the test that covers it.
-->

- [ ]

## Screenshots / video (optional)

<!-- Drop in before/after images for UI changes. -->

## Related issues

<!-- "Closes #123" / "Refs #456" -->

## Pre-PR checklist

- [ ] Branch name follows `fix/<slug>` / `feature/<slug>` / `chore/<slug>`
- [ ] `pytest tests/` passes locally
- [ ] `cd web && npx tsc -b --noEmit` passes locally
- [ ] `cd web && npm run lint:all` passes locally (no NEW errors; the existing baseline of 50-ish warnings is fine)
- [ ] `cd web && npm test` passes locally
- [ ] Commit messages are imperative, under 70 chars, with no AI attribution
- [ ] If this PR adds a `Settings` field, the matching `SettingsPayload` (server.py) and `Settings` interface (web/src/api/types.ts) entries are also added (see CONTRIBUTING.md)
