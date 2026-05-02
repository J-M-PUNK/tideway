# Release signing (maintainer)

Tideway's auto-updater verifies a minisign signature on every
installer it downloads before launching it. This document is the
maintainer-side reference for generating and using the signing key.
End users do not need to read this.

## Why signing exists

The auto-updater downloads installer binaries from GitHub Releases
and runs them. Without a signature check, anyone who could publish a
release on the GitHub repo could push arbitrary code to every user
who clicks "Install now." That includes:

- A leaked GitHub PAT or OAuth token with `contents: write`.
- A phished or compromised maintainer account.
- A malicious GitHub Action that runs in the release workflow.

The defense is a signature made with a private key that lives
**only on the maintainer's local machine**, never in CI, never in
GitHub Actions secrets. An attacker who compromises the publishing
channel still doesn't have the signing key, and the in-app verifier
will refuse to launch any binary it can't verify.

The trusted public keys are baked into the app at build time. See
[`app/release_keys.py`](../app/release_keys.py) for the actual list
and [`app/release_verify.py`](../app/release_verify.py) for the
verifier.

## First-time setup

You'll do this once. After that, every release picks up the same
key and the only step per-release is running the signing script.

### 1. Install minisign

```bash
brew install minisign
```

(or your distro's equivalent — minisign is in most package managers)

### 2. Generate the primary keypair

```bash
minisign -G \
    -p tideway-release.pub \
    -s ~/.tideway-release-key
```

You'll be prompted for a passphrase. **Use a strong one and don't
lose it.** The passphrase is the second factor — if malware ever
copies the secret-key file off your machine, it still can't sign
without the passphrase.

The two files this produces:

- `~/.tideway-release-key` — the encrypted secret. Keep this on your
  laptop, encrypted with FileVault. Don't commit it. Don't put it
  in CI. Don't sync it through Dropbox.
- `tideway-release.pub` — the public key. Safe to share.

### 3. Generate the cold-backup keypair

```bash
minisign -G \
    -p tideway-cold-backup.pub \
    -s tideway-cold-backup-key
```

This second key exists for one situation: the primary is lost or
compromised, and we still need a signed path to push a "rotate the
trusted-key list" build to existing users. Without it, losing the
primary means every installed copy refuses every future update
forever — there's no signed path to teach them to trust a new key.

The cold-backup secret key gets stored offline, never on the build
machine in normal operation:

- Burn it to a USB stick (encrypted) and put it in a drawer.
- Or write the base64 onto paper and put it in a safe.
- Or both.

The key gets used exactly once if the primary is ever compromised.

### 4. Bake the public keys into the app

Open [`app/release_keys.py`](../app/release_keys.py) and uncomment
both `TrustedKey(...)` blocks, pasting in the second line of each
`.pub` file (the base64 payload, not the comment line above it):

```python
TRUSTED_RELEASE_PUBKEYS: list[TrustedKey] = [
    TrustedKey(
        pubkey_b64="RWQ...",  # paste from tideway-release.pub
        label="primary",
    ),
    TrustedKey(
        pubkey_b64="RWQ...",  # paste from tideway-cold-backup.pub
        label="cold-backup",
    ),
]
```

Commit this change. It needs to ship in a release before the next
release can be signature-required, since older installs need to be
running a build that knows about both keys.

### 5. Back up the secret keys

The primary secret key file (`~/.tideway-release-key`) needs a
backup. If you lose it AND the cold backup, the recovery path is
"every existing user reinstalls Tideway by hand from a fresh
download." Avoid that.

A reasonable backup setup:

- Encrypt `~/.tideway-release-key` with `gpg` or `age` using a
  passphrase that's NOT the minisign passphrase (so a single
  compromise doesn't unlock both layers).
- Store the encrypted blob in two places: 1Password / Bitwarden, and
  an offline USB stick.

The cold-backup secret should already be offline by virtue of step
3 — its backup story is "you wrote it down on paper or burned it
to a USB stick that lives in a drawer."

## Per-release: signing the installers

The release workflow ([release-workflow.md](release-workflow.md))
covers everything else. The signing step slots in between "GitHub
Actions built the draft release" and "publish the draft."

```bash
scripts/sign-release.sh v1.3.0
```

What this does, in order:

1. `gh release download v1.3.0` pulls the installer artifacts that
   CI just attached to the draft release.
2. For each artifact, runs `minisign -S -H -m <artifact>` —
   prehashed Ed25519 signatures, the only mode the in-app verifier
   accepts. You'll be prompted for the passphrase once per file.
3. `gh release upload v1.3.0 *.minisig` uploads the four sidecar
   `.minisig` files back to the same draft release.

Now the draft release has both the installers and their signatures.
Open the Releases page and click Publish.

If you skip the signing step and publish a release with no
`.minisig` files, the auto-updater on every user's installed copy
will refuse to install the update. The user will see an error toast
naming the missing signature. No silent failures, but also no
silent installs of unverified binaries.

## Key rotation

Two scenarios:

### Routine rotation (e.g. annual)

1. Generate a new keypair (`tideway-release-2027.pub`, etc).
2. Add the new public key to `TRUSTED_RELEASE_PUBKEYS` AS WELL AS
   the existing one. Do NOT remove the old key yet.
3. Ship a release signed with the OLD key that includes this change.
   After this release propagates, every install trusts both keys.
4. Subsequent releases sign with the new key.
5. Once you're confident the rotated-trust release has propagated
   (give it a few months — there are always users on stale builds),
   ship a release signed with the NEW key that removes the old key
   from the trusted list.

### Emergency rotation (key compromised)

If the primary secret key is leaked, an attacker can sign anything
they want and existing installs will accept it. You need to:

1. Revoke / disable the GitHub publishing channel temporarily so
   the attacker can't push a malicious release ahead of you.
2. Pull out the cold-backup secret key.
3. Generate a brand-new primary keypair.
4. Edit `app/release_keys.py` to list ONLY the new primary key and
   the cold backup. The compromised old primary gets removed.
5. Build a release.
6. Sign it with the cold-backup key (since the new primary isn't
   trusted by existing installs, but the cold backup is).
7. Publish.

After existing installs take the cold-backup-signed update, they'll
trust the new primary. From the next release onward you sign with
the new primary and the cold backup goes back into the drawer.

If you lose BOTH the primary AND the cold backup before getting an
update out, the only recovery is to ship a brand-new build with a
new keypair and tell every existing user to reinstall by hand. The
auto-updater on their existing install can't help — it has no key
in its trusted list that matches anything you can sign.

## Verifying a release as an end user

The auto-updater does this automatically. If you want to verify by
hand (e.g. you downloaded the installer from a release page rather
than via the in-app updater):

```bash
# Tideway's primary release-signing public key.
PUBKEY=RWQ...                          # see app/release_keys.py

minisign -V \
    -P "$PUBKEY" \
    -m Tideway-1.3.0.dmg
```

A "Signature and comment signature verified" line means the file
is genuine. Anything else: don't run it.
