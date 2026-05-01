# Security policy

## Supported versions

Only the latest released version of Tideway receives security fixes.
If you are running an older build, the first step is to update from
the [Releases page](https://github.com/J-M-PUNK/tideway/releases) and
confirm the issue still reproduces.

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**
Tideway holds a Tidal session token on disk, talks to several third-
party services, and ships a packaged desktop app, so a credible
security report deserves a private channel before any disclosure.

Two ways to report, in order of preference:

1. **GitHub Security Advisories (preferred).** Go to the repo's
   [Security tab](https://github.com/J-M-PUNK/tideway/security) and
   click **Report a vulnerability**. This opens a private advisory
   only the maintainers and people you invite can see. Anyone with a
   GitHub account can submit one.
2. **Email.** Send a writeup to **william.pratt.three@gmail.com**
   with `[Tideway security]` in the subject. Include enough detail to
   reproduce.

In your report, please include:

- What the issue is and what an attacker could do with it
- Steps to reproduce, with code or screenshots if relevant
- Tideway version (Settings page, bottom right) and OS
- Whether the issue is already public anywhere

## What to expect

- **Acknowledgement** within 7 days. If you do not hear back in that
  window, please nudge.
- **Triage** within 14 days, with a rough severity assessment and an
  initial fix timeline.
- **Fix and release** as soon as practical. Critical issues get a
  patch release on their own. Lower-severity issues batch with the
  next normal release.
- **Credit** in the release notes if you would like it. Let us know
  whether to use your name, a handle, or anonymous.

Tideway is a hobby project with no SLA, but security reports get
priority over feature work.

## Out of scope

The following are known limitations rather than security issues:

- **Unsigned builds.** macOS Gatekeeper and Windows SmartScreen warn
  on first launch. This is documented in the README. Code signing
  costs money the project does not have.
- **Tidal account risk.** Heavy use can trigger Tidal's anti-abuse
  system. Documented in the README. Using your own Tidal account
  with this app is at your own risk.
- **Anonymous Spotify enrichment.** The app talks to Spotify's
  anonymous public GraphQL endpoint to fetch playcounts. No user
  Spotify credentials are involved unless you opt into the Spotify
  importer.
- **Local-network-only API.** The bundled FastAPI server binds to
  127.0.0.1 only. If you have intentionally exposed it to your LAN
  or the public internet via a reverse proxy, you have opted out of
  the default threat model.
