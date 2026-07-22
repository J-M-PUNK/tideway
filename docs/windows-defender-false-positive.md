# Windows Defender false positives (maintainer)

Windows Defender's machine-learning heuristic flags Tideway's Windows
installer as malware — most recently `Trojan:Win32/Wacatac.C!ml` on
1.21.2 ([#293](https://github.com/J-M-PUNK/tideway/issues/293)). This
document is the maintainer-side reference for clearing it. End users do
not need to read this.

Note this is unrelated to the minisign signing in
[`release-signing.md`](release-signing.md). Those signatures are
Tideway's own auto-update integrity check; Defender has no knowledge of
them and they do nothing for its reputation scoring.

## Why it happens

The `!ml` suffix marks a detection from Defender's ML model rather than
a signature match. Two properties of our installer drive the score, and
neither is something we can code around:

- **It's a PyInstaller bundle inside an Inno Setup package.** A
  self-extracting stub that writes a Python interpreter and compiled
  modules to a temp directory and then launches them is behaviourally
  indistinguishable from a dropper. `Tideway-win.spec` already uses
  onedir rather than onefile partly for this reason (see the comment at
  the top of that file) — onefile scores worse still — but onedir
  doesn't eliminate it.
- **The binary is unsigned and has no reputation.** Every release
  publishes a brand-new unsigned executable that Defender has never seen
  and that no measurable number of users have run. Low prevalence plus no
  Authenticode signature is most of the score on its own.

Because reputation is per-binary, **clearing one release does not
protect the next one.** Expect this to recur on every release until an
Authenticode certificate is in place.

## The recurring fix: submit as a false positive

Free, and clears the flagged build within a few days.

Portal: <https://www.microsoft.com/en-us/wdsi/filesubmission>

Sign in with a Microsoft account, choose **Submit a file for analysis**,
then **Software developer** — the developer queue is the one that
handles false positives on distributed binaries; the home-customer queue
will not action it.

Submit **both** Windows artifacts. They are separate binaries and are
scored separately:

- `Tideway-setup-<version>.exe`
- `Tideway-setup-<version>-arm64.exe`

Both are at
`https://github.com/J-M-PUNK/tideway/releases/download/v<version>/<filename>`.

### Form fields

| Field | Value |
| --- | --- |
| Submission type | Software developer |
| Detection name | the exact string Defender reported, e.g. `Trojan:Win32/Wacatac.C!ml` |
| Product | Microsoft Defender Antivirus |
| Do you believe this is a false positive? | Yes |

### Justification text

Paste this, updating the version:

> Tideway is an open-source desktop music client for TIDAL, distributed
> under the MIT licence. Source: https://github.com/J-M-PUNK/tideway
>
> Defender's ML heuristic flags our Windows installer. This is a false
> positive. The installer is an Inno Setup package wrapping a
> PyInstaller onedir bundle of a Python application; the packing
> behaviour that pattern produces (a self-extracting stub writing an
> interpreter and compiled modules to a temp directory, then launching
> them) is what we believe drives the generic ML score. The binary is
> not obfuscated or packed beyond standard PyInstaller output.
>
> The installer is built entirely in public on GitHub-hosted runners —
> no local build step, no manual artifact handling. The workflow, the
> PyInstaller spec, and the Inno Setup script are all in the
> repository:
>
>   Build workflow: .github/workflows/release.yml (job: build-windows)
>   PyInstaller spec: Tideway-win.spec
>   Installer script: scripts/Tideway.iss
>
> Every release artifact is also signed with minisign and published
> with a .minisig sidecar, and the application verifies those
> signatures before applying an auto-update.
>
> The application makes outbound HTTPS connections to TIDAL's API for
> streaming and to GitHub Releases for update checks. It writes to the
> user's configured download directory and to the per-user application
> data directory. It does not modify system settings, install services
> or drivers, or write outside the user profile.
>
> We are not currently Authenticode-signed, which we understand
> contributes to the low reputation score.

## The permanent fix: Authenticode

An Authenticode certificate is the only thing that ends the recurrence.

- **OV (organisation validation)**, roughly $100–300/year. Stops most ML
  heuristics immediately and accumulates SmartScreen reputation over a
  few weeks of download volume. Requires business-identity validation.
- **EV (extended validation)**, roughly $300–600/year, issued on a
  hardware token or via a cloud HSM. Clears SmartScreen from the first
  signed build with no reputation-building period. The token makes CI
  signing harder — either sign locally alongside
  `scripts/sign-release.sh`, or use a cloud signing service that exposes
  an API.

Wiring it into CI means adding an `signtool sign /fd SHA256 /tr <rfc3161
timestamp url> /td SHA256` step to the `build-windows` and
`build-windows-arm64` jobs in `.github/workflows/release.yml`, after
Inno Setup produces the installer and before the artifact upload. Gate
it on the certificate secret being present so forks and unsigned local
builds keep working.

## What to tell affected users

The detection is a false positive, but "ignore your antivirus" is not
advice to give casually — a user cannot distinguish our reassurance from
what actual malware would say. Point them at evidence instead:

- The build is fully public and reproducible from the workflow linked
  above; nothing is uploaded from a maintainer's machine.
- A VirusTotal scan shows which engines flag it — typically only the
  ML-heuristic ones — which is a stronger signal than our word.
- Anyone uncomfortable can build from source, or wait for the
  submission above to clear.
