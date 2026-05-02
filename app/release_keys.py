"""Trusted public keys for verifying Tideway release-installer
signatures.

The auto-update flow downloads installer binaries from GitHub and
runs them. Before launching a downloaded installer the update
endpoint verifies its `.minisig` companion against this list. If
verification fails (or no `.minisig` exists), the installer is
deleted and the user gets an error rather than running unverified
code.

Why two keys instead of one
---------------------------

The primary key is what every release is normally signed with. The
cold-backup key exists for one situation: the primary key is lost,
compromised, or rotated, AND we still need to ship an update to
existing users so the signing flow can recover.

If we shipped only one trusted key and that key was lost, every
install on every user's machine would refuse all future updates
forever — there would be no signed path to publish a "rotate to a
new key" build, because the new build would itself be signed by a
key the installed app has never heard of. The cold backup is the
escape hatch: it stays offline (encrypted backup, never on a build
machine), and is used exactly once if the primary is ever lost, to
sign a release whose ONLY change is rotating the trusted-keys list
to a new primary.

Operational rules for the keys
------------------------------

- The primary secret key lives on the maintainer's laptop, encrypted
  with a strong passphrase, never committed, never copied into CI
  secrets. Reason: if both the GitHub publishing credentials AND the
  signing key live in the same place (e.g. GitHub Actions secrets),
  one compromise gets both and the signature check protects nothing.
- The cold-backup secret key lives on offline media (encrypted USB,
  paper backup, etc), never plugged into the build machine except
  during a key-rotation event.
- Both public keys are baked in here from day one of the signing
  rollout. Adding a key after the fact requires shipping a release
  signed with one of the existing keys.

Adding / rotating keys
----------------------

1. `minisign -G -p new.pub -s ~/.tideway-release-key-new` — generate.
2. Copy the second line of `new.pub` (the base64 payload) into the
   `pubkey_b64` field of a new TrustedKey entry below.
3. Ship a release signed with the OLD primary that includes this
   change. Now both old and new are trusted in the wild.
4. Once that release has propagated to users, future releases can be
   signed with the new key and the old one can be retired by removing
   it from the list (in another release signed with the new key).
"""

from __future__ import annotations

from app.release_verify import TrustedKey


# Trusted keys are evaluated in order — the verifier walks the list,
# matches the signature's key id against each one, and runs Ed25519
# verification on the first match. There's no scoring or fallback
# beyond that; either the signature checks out under one of these
# keys or the install is rejected.
#
# Empty list: a build with no trusted keys configured will refuse all
# update installs. That's intentional. A fork that wants to ship its
# own release stream needs to either populate this list with its own
# keys, or disable the auto-updater entirely by leaving
# TIDEWAY_UPDATE_REPO unset.
TRUSTED_RELEASE_PUBKEYS: list[TrustedKey] = [
    # PRIMARY KEY (key id D69CE87FA085D1C8).
    # Secret key lives at ~/.tideway-release-key on the maintainer's
    # laptop, encrypted with a passphrase. Used by
    # scripts/sign-release.sh on every release.
    TrustedKey(
        pubkey_b64="RWTI0YWgf+ic1vZh3Qn6Bu6nlLmwtZcvCxWW2asCXpYsGNqmxtWNk9Yu",
        label="primary",
    ),
    # COLD-BACKUP KEY (key id C3EEFD29E02C96D1).
    # Secret key stored in encrypted backup (Bitwarden Secure Note),
    # never plugged into the build machine in normal operation. Used
    # only if the primary is ever lost or compromised, to sign a
    # single recovery release that rotates the trusted-keys list. See
    # docs/release-signing.md for the full procedure.
    TrustedKey(
        pubkey_b64="RWTRlizgKf3uw0ltDum9ZEcsxKScvYvJNJHjG/DpH8xIXIZp7aQ/G8nc",
        label="cold-backup",
    ),
]
