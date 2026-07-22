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

## You cannot reproduce this locally

The maintainer's Windows machine runs a third-party antivirus
(Surfshark), and Windows stands Defender's real-time protection down
whenever another AV registers itself — `Get-Service WinDefend` reports
`Stopped`. The engine that emits these detections is therefore not
running here at all. Every release installer back to 1.14.0, including
the exact 1.21.2 file reported in #293, sits intact in the maintainer's
Downloads folder.

So a clean download on the maintainer's machine is not evidence of
anything, and neither is a clean install. To actually check a build:

- Upload the installer to VirusTotal and read which engines flag it.
  Typically only the ML-heuristic ones do. This is also the artifact
  worth linking when replying to a user, because it's independent
  evidence rather than our own reassurance.
- Or run it on a Windows VM with Defender enabled and cloud-delivered
  protection on.

Bear in mind detection is not even consistent across Defender users:
`!ml` verdicts shift with definition versions and cloud-protection
settings, so the same file can pass one day and be quarantined the
next.

## The permanent fix: Authenticode

Getting the installer Authenticode-signed is the only thing that ends
the recurrence. The ground rules changed recently and older advice
(including an earlier draft of this document) is misleading:

- Since **June 2023**, code-signing private keys must live on a
  hardware token or an HSM. You can no longer drop a `.pfx` into a CI
  secret and call `signtool`.
- Since **23 February 2026**, maximum certificate lifetime is 459 days
  (~15 months), and multi-year purchases ship a new hardware device
  each year.

That makes a CA-shipped USB token awkward for a GitHub Actions build,
and pushes the practical options toward managed signing services:

- **SignPath Foundation** (<https://signpath.org/>) — free for open
  source, with CI-native GitHub connectors. Their model is verifying
  that the binary was built from the public repository, which Tideway
  already satisfies: `release.yml` builds entirely on GitHub-hosted
  runners with no local build step and no manual artifact handling.
  Requires an application and approval. Worth stating plainly in the
  application that Tideway downloads audio from TIDAL, rather than
  having it discovered later.
- **Azure Trusted Signing** — cloud-based, roughly $10/month, designed
  for CI so there is no token to manage. Public trust is limited to
  organisations in the US/Canada/EU/UK and to individual developers in
  the US and Canada only, so eligibility depends on where the
  maintainer is.
- **A traditional OV/IV certificate** — a distant third now. Individual
  Validation certificates exist for solo developers without a
  registered company, but you are managing shipped hardware plus annual
  device replacement.

Verify current pricing and eligibility directly with the provider; this
area has been changing yearly.

### Where signing has to happen in the release flow

**Authenticode signing must happen inside the build job, before the
artifact is uploaded to the draft release.**

`scripts/sign-release.sh` downloads whatever is attached to the release
and minisigns those exact bytes. Authenticode signing rewrites the PE —
it appends to the certificate table — which changes the file hash. Sign
in the wrong order and every `.minisig` is silently invalid, which
breaks "Install now" for every existing install: precisely the failure
the minisign scheme exists to prevent.

The correct order is:

```
build-windows / build-windows-arm64
  → Inno Setup produces the installer
  → Authenticode signing (SignPath / Azure / signtool)
  → upload artifact to the draft release
  → maintainer runs scripts/sign-release.sh (minisign over signed bytes)
  → publish
```

Gate the signing step on its credential being present, so forks and
unsigned local builds keep working.

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
