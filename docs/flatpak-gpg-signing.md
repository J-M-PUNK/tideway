# Flatpak GPG signing — one-time setup

The Linux Flatpak ships through a self-hosted OSTree repo at
`https://j-m-punk.github.io/tideway/`. By default the repo is
served over HTTPS and clients verify the GitHub Pages TLS chain
when they pull, but the OSTree commits themselves are unsigned. A
compromise of the GitHub Pages infrastructure could substitute
malicious bits and clients would have no way to detect it.

Enabling GPG signing closes that gap. Each OSTree commit gets a
detached GPG signature; clients verify it against the embedded
public key before installing. The `.flatpak` bundle attached to the
GitHub Release is independently minisign-signed by the
`scripts/sign-release.sh` step, so direct-bundle installs are
already protected — this doc covers the remote / `flatpak update`
path.

Setup is one-time. After it's done, every tagged release signs its
commit automatically through GitHub Actions.

## Generate the signing key

The signing key is intentionally separate from the minisign
release-signing key (different threat model, different agent on the
client side, different rotation cadence). 5-year expiry matches
typical Flathub guidance.

```sh
gpg --quick-gen-key 'Tideway OSTree Signing <release@tideway>' rsa3072 sign 5y
```

`gpg` will prompt for a passphrase. Choose a strong one and save it
to a password manager — you'll need it in step 3.

Find the fingerprint of the key you just generated:

```sh
gpg --list-keys --fingerprint
```

The long uppercase hex string (no spaces; copy the one printed
below the user ID) is the `key id` for the remaining steps.

## Commit the public half

The public key is what clients use to verify signatures. Export it
in binary form (Flatpak's `GpgKey=` field expects the raw key, not
ASCII-armored) and commit it to the repo:

```sh
gpg --export <fingerprint> > flatpak/tideway-release.pub.gpg
git add flatpak/tideway-release.pub.gpg
git commit -m "Add OSTree signing public key"
git push
```

`.github/workflows/release.yml`'s `publish-flatpak-repo` job
detects this file and embeds it (base64-encoded) into the
`tideway.flatpakrepo` served from GitHub Pages. New users who
`flatpak remote-add` after this lands will have signature
verification turned on automatically; existing users have to
re-add the remote (or pass `--gpg-verify` to `flatpak
remote-modify`) to opt in.

## Stash the private half in GitHub Secrets

Three secrets, all on the repo's Settings → Secrets and variables →
Actions page:

1. **`OSTREE_GPG_PRIVATE_KEY`** — armored private key:
   ```sh
   gpg --export-secret-keys --armor <fingerprint>
   ```
   Paste the entire output (`-----BEGIN PGP PRIVATE KEY BLOCK-----`
   to `-----END PGP PRIVATE KEY BLOCK-----`).

2. **`OSTREE_GPG_KEY_ID`** — the fingerprint, no spaces. CI uses
   the presence of this secret as the on/off switch for signing.

3. **`OSTREE_GPG_PASSPHRASE`** — the passphrase you set in step 1.
   Optional if you skipped the passphrase, but you really shouldn't
   skip it.

## Test it

Push a new tag (a point-release works). Watch the
`build-linux-flatpak` job log for `GPG signing: enabled (key
...)`. The OSTree commit it produces will carry a `.commitmeta`
file with the signature.

On a Linux machine, add the remote freshly (the existing one was
added pre-signing and is sticky to no-gpg-verify mode):

```sh
flatpak remote-delete --user tideway
flatpak remote-add --user tideway \
  https://j-m-punk.github.io/tideway/tideway.flatpakrepo
flatpak install --user tideway com.tidaldownloader.Tideway
```

`flatpak remote-list --user` should show the remote without the
`no-gpg-verify` annotation that the unsigned remote carried before.

## Rotation

When the key gets close to expiry (every 5 years or sooner if
something gets compromised):

1. Generate a new key (same `gpg --quick-gen-key` command).
2. Sign the old key's revocation (`gpg --gen-revoke <old-fingerprint>`)
   and keep the revocation certificate offline in case you need to
   tell users the old key shouldn't be trusted anymore.
3. Replace `flatpak/tideway-release.pub.gpg` with the new public
   key. Commit and push.
4. Update the three `OSTREE_GPG_*` secrets in GitHub Settings.
5. Tag a release. New users get the new key automatically through
   `tideway.flatpakrepo`. Existing users have to re-add the remote
   the same way they would after first enabling signing — the
   `GpgKey=` field is read once at `remote-add` time, not on every
   update.

## If something breaks

Build job logs `GPG signing: disabled (OSTREE_GPG_KEY_ID secret
unset)` means the secret either isn't set or isn't visible to the
running ref (forks don't see secrets from the upstream repo). Check
the Actions secrets page; nothing in the manifest needs to change.

Build job logs `gpg-preset-passphrase failed; signing may prompt`
means the loopback agent couldn't cache the passphrase. The build
might still succeed if your key has no passphrase, but a passphrase
key plus an absent agent will hang the build. The usual cause is a
mistyped `OSTREE_GPG_PASSPHRASE` secret.

Client install logs `GPG signatures found, but none are in
trusted keyring` means the user's local copy of the public key in
`~/.local/share/flatpak/repo/refs/...` is out of date — usually
because the remote was added before signing was enabled. Delete and
re-add the remote per "Test it" above.
