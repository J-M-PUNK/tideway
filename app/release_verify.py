"""Verify minisign-format signatures on downloaded release artifacts.

The auto-update flow downloads installer binaries from GitHub Releases
and runs them. Signature verification is what protects users from a
compromised GitHub publishing account: even if an attacker can upload
a release, they can't sign it with the private key kept off the
publishing channel, and verification here will refuse to launch the
installer.

We accept the minisign signature format (Ed25519 over BLAKE2b-512 of
the file content), specifically the prehashed variant produced by
`minisign -S -H`. Raw-message minisign signatures ("Ed" algorithm) are
rejected — every Tideway release goes through prehashed mode, so the
narrower acceptance keeps the verifier surface small.

The implementation is pure Python on top of `hashlib`. There's no
secret material involved on the verification side, so the usual
"don't roll your own crypto" caveats about side channels and timing
attacks don't apply: we're checking a public-key signature with a
public key. The algorithm itself is the RFC 8032 reference Ed25519,
which has been independently re-implemented and cross-validated many
times.

Avoiding a native crypto dependency (libsodium / pynacl, OpenSSL via
cryptography) keeps the PyInstaller bundle simpler. Each platform
spec would otherwise need its own datas / binaries entries, and a
silently-missing native lib would degrade to "no verification" — the
exact failure mode this module exists to prevent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


class SignatureError(Exception):
    """Raised when a release artifact fails signature verification.

    Message text is intentionally suitable for surfacing in a user-
    facing error toast: it names which check failed in plain language
    without leaking cryptographic internals the user can't act on.
    """


# ---------------------------------------------------------------------------
# Minisign format constants
# ---------------------------------------------------------------------------

# Public-key algorithm marker. Minisign writes the ASCII bytes "Ed"
# (0x45 0x64) at the start of the base64 payload to identify Ed25519
# keys. There is no other key type in minisign as of v0.12.
_PUBKEY_ALG = b"Ed"

# Signature algorithm marker for the BLAKE2b-prehashed variant
# (minisign -S -H). The legacy raw-message variant uses "Ed" (0x45
# 0x64); we deliberately reject it. Tideway's signing script always
# uses -H so prehashed is the only mode in use, and rejecting the
# legacy mode means a downgrade to raw-message Ed25519 (which would
# require streaming the entire artifact through SHA-512 a second
# time inside Ed25519's internal hash) can never sneak through.
_SIG_ALG_PREHASHED = b"ED"

# Lengths in bytes for the parsed binary blobs.
_KEY_ID_LEN = 8
_PUBKEY_LEN = 32
_SIG_LEN = 64
_PUBKEY_BLOB_LEN = 2 + _KEY_ID_LEN + _PUBKEY_LEN  # 42
_SIG_BLOB_LEN = 2 + _KEY_ID_LEN + _SIG_LEN        # 74
_GLOBAL_SIG_BLOB_LEN = _SIG_LEN                    # 64


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedKey:
    """One public key Tideway will accept release signatures from.

    `pubkey_b64` is the second line of a minisign.pub file (the base64
    payload). The leading two-byte algorithm marker, 8-byte key id, and
    32-byte Ed25519 public key are parsed out of that payload by the
    verifier. Callers don't supply them separately.

    `label` is a human-readable name shown in logs when a verification
    succeeds, so we can tell which key (primary vs cold backup) was
    actually used.
    """

    pubkey_b64: str
    label: str = ""


@dataclass(frozen=True)
class _ParsedPubkey:
    key_id: bytes
    pubkey: bytes


@dataclass(frozen=True)
class _ParsedSig:
    key_id: bytes
    signature: bytes
    trusted_comment: str
    global_signature: bytes


# ---------------------------------------------------------------------------
# Format parsing
# ---------------------------------------------------------------------------


def _parse_pubkey_payload(payload_b64: str) -> _ParsedPubkey:
    try:
        blob = base64.b64decode(payload_b64.strip(), validate=True)
    except Exception as exc:
        raise SignatureError(f"Trusted public key isn't valid base64: {exc}")
    if len(blob) != _PUBKEY_BLOB_LEN:
        raise SignatureError(
            f"Trusted public key has wrong length ({len(blob)} bytes, "
            f"expected {_PUBKEY_BLOB_LEN})"
        )
    if blob[:2] != _PUBKEY_ALG:
        raise SignatureError(
            "Trusted public key isn't an Ed25519 minisign key "
            f"(algorithm marker {blob[:2]!r}, expected b'Ed')"
        )
    return _ParsedPubkey(
        key_id=blob[2 : 2 + _KEY_ID_LEN],
        pubkey=blob[2 + _KEY_ID_LEN :],
    )


def _parse_signature_text(sig_text: str) -> _ParsedSig:
    """Parse a `.minisig` file's contents into its 4 logical fields.

    Layout is fixed — minisign always writes exactly 4 lines:
      1. untrusted comment (free text, NEVER trusted)
      2. base64 signature blob
      3. trusted comment (verified by the global signature)
      4. base64 global signature blob (sig over line 2's bytes + line 3's text)
    """
    lines = [ln for ln in sig_text.splitlines() if ln.strip()]
    if len(lines) != 4:
        raise SignatureError(
            f"Signature file has {len(lines)} non-empty lines, expected 4"
        )
    if not lines[0].lower().startswith("untrusted comment:"):
        raise SignatureError("Signature file is missing the untrusted comment header")
    trusted_prefix = "trusted comment:"
    if not lines[2].lower().startswith(trusted_prefix):
        raise SignatureError("Signature file is missing the trusted comment header")

    try:
        sig_blob = base64.b64decode(lines[1].strip(), validate=True)
    except Exception as exc:
        raise SignatureError(f"Signature payload isn't valid base64: {exc}")
    try:
        global_blob = base64.b64decode(lines[3].strip(), validate=True)
    except Exception as exc:
        raise SignatureError(f"Global signature payload isn't valid base64: {exc}")

    if len(sig_blob) != _SIG_BLOB_LEN:
        raise SignatureError(
            f"Signature blob has wrong length ({len(sig_blob)} bytes, "
            f"expected {_SIG_BLOB_LEN})"
        )
    if sig_blob[:2] != _SIG_ALG_PREHASHED:
        raise SignatureError(
            "Signature isn't a prehashed Ed25519 signature "
            f"(algorithm marker {sig_blob[:2]!r}, expected b'ED'). "
            "Re-sign with `minisign -S -H ...`."
        )
    if len(global_blob) != _GLOBAL_SIG_BLOB_LEN:
        raise SignatureError(
            f"Global signature has wrong length ({len(global_blob)} bytes, "
            f"expected {_GLOBAL_SIG_BLOB_LEN})"
        )

    # Trusted comment text is everything after "trusted comment:"
    # with leading whitespace trimmed (minisign writes a single space
    # after the colon). The bytes used in the global-signature input
    # are the raw text without a trailing newline.
    trusted_comment = lines[2][len(trusted_prefix) :].lstrip()

    return _ParsedSig(
        key_id=sig_blob[2 : 2 + _KEY_ID_LEN],
        signature=sig_blob[2 + _KEY_ID_LEN :],
        trusted_comment=trusted_comment,
        global_signature=global_blob,
    )


# ---------------------------------------------------------------------------
# Verification entry points
# ---------------------------------------------------------------------------


def verify_artifact(
    artifact_path: Path | str,
    signature_text: str,
    trusted_keys: Iterable[TrustedKey],
) -> TrustedKey:
    """Verify `artifact_path` against `signature_text` using one of
    `trusted_keys`. Returns the key that produced the matching
    signature. Raises SignatureError if no key validates.

    The verification is two-step:
      1. The file's BLAKE2b-512 hash is verified against the signature
         payload (line 2 of the .minisig file).
      2. The trusted comment line is verified against the global
         signature (line 4). This binds the comment text to the key,
         so we can safely log "verified release X by key Y" without
         the attacker being able to lie about either via the sig file.

    The key id encoded in the signature is matched against trusted
    pubkeys' key ids before attempting Ed25519 verification — this is
    purely a fast-path to avoid running the curve math against keys
    that obviously won't match. The actual security guarantee comes
    from the Ed25519 verification, not from the key-id match.
    """
    parsed = _parse_signature_text(signature_text)
    parsed_keys = [
        (tk, _parse_pubkey_payload(tk.pubkey_b64)) for tk in trusted_keys
    ]
    if not parsed_keys:
        raise SignatureError(
            "No trusted release-signing keys are configured in this build"
        )

    candidates = [
        (tk, pk) for tk, pk in parsed_keys if hmac.compare_digest(pk.key_id, parsed.key_id)
    ]
    if not candidates:
        # Surface the encountered key id so a misconfiguration (e.g.
        # primary key was rotated and the cold backup wasn't yet baked
        # into the new build) is debuggable without re-running with
        # extra logging.
        #
        # Byte-reversed for display to match minisign's convention:
        # minisign's binary format stores the 8-byte key id
        # little-endian but its `untrusted comment: minisign public
        # key XXXXXXXX` line prints big-endian hex. Showing the bytes
        # in raw on-disk order would make support requests confusing
        # ("the verifier says key 6DE6... but my .pub file says
        # CFF9..."), so we reverse here to match what the user sees
        # everywhere else.
        seen_id = parsed.key_id[::-1].hex().upper()
        known_ids = ", ".join(
            pk.key_id[::-1].hex().upper() for _, pk in parsed_keys
        )
        raise SignatureError(
            f"Signature was made by key {seen_id}, which isn't in this "
            f"build's trusted set ({known_ids})"
        )

    artifact = Path(artifact_path)
    file_hash = _blake2b_file(artifact)

    last_error: Optional[str] = None
    for tk, pk in candidates:
        if not _ed25519_verify(pk.pubkey, file_hash, parsed.signature):
            last_error = "file signature didn't verify under the matching key"
            continue
        # File sig is good. Now verify the trusted comment is bound to
        # this key too. The minisign global-signature input is the
        # raw signature bytes (64) followed by the trusted comment
        # text (no trailing newline, no length prefix).
        global_input = parsed.signature + parsed.trusted_comment.encode("utf-8")
        if not _ed25519_verify(pk.pubkey, global_input, parsed.global_signature):
            last_error = (
                "file signature verified, but trusted-comment signature "
                "didn't — sig file may have been tampered with"
            )
            continue
        return tk

    raise SignatureError(last_error or "signature didn't verify")


def _blake2b_file(path: Path) -> bytes:
    """Streaming BLAKE2b-512 of a file. Matches what `minisign -H`
    feeds into Ed25519 sign / verify on the signing side.

    Streamed in 1 MiB chunks so the 100+ MB installer doesn't get
    pulled into RAM in one shot.
    """
    h = hashlib.blake2b(digest_size=64)
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.digest()


# ---------------------------------------------------------------------------
# Ed25519 verification (RFC 8032 reference algorithm, verify-only)
# ---------------------------------------------------------------------------
#
# This is the standard Edwards-curve verification, ported from the
# pseudocode in RFC 8032 section 6 and the reference Python sample in
# appendix A. We only implement verify (no signing, no key gen),
# because the only role this module has is checking signatures made
# by the publisher.
#
# Performance note: a full verify on this implementation runs in well
# under a second on modern hardware. The download it gates is on the
# order of 30+ seconds for a 100 MB installer, so the verification
# overhead is invisible.

_p = 2**255 - 19
_q = 2**252 + 27742317777372353535851937790883648493  # group order

# d = -121665 / 121666 mod p (curve constant)
_d = (-121665 * pow(121666, -1, _p)) % _p

# sqrt(-1) mod p, used by recover_x when the trial root needs correcting.
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _recover_x(y: int, sign: int) -> Optional[int]:
    """Recover x from y on the Edwards curve, picking the root whose
    low bit matches `sign`. Returns None if no valid x exists (i.e.
    the input y doesn't lie on the curve).
    """
    if y >= _p:
        return None
    x2 = ((y * y - 1) * pow(_d * y * y + 1, -1, _p)) % _p
    if x2 == 0:
        if sign:
            return None
        return 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = (x * _sqrt_m1) % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


# Base point B from RFC 8032 Section 5.1: y = 4/5 mod p, x = positive
# (even) root via _recover_x. Stored in extended Edwards coordinates
# (X, Y, Z, T) for use by the doubling-and-add multiplier.
def _make_basepoint() -> tuple[int, int, int, int]:
    by = (4 * pow(5, -1, _p)) % _p
    bx = _recover_x(by, 0)
    assert bx is not None  # base point is on the curve by construction
    return (bx, by, 1, (bx * by) % _p)


_BASEPOINT = _make_basepoint()


def _point_add(
    P: tuple[int, int, int, int], Q: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    A = ((P[1] - P[0]) * (Q[1] - Q[0])) % _p
    B = ((P[1] + P[0]) * (Q[1] + Q[0])) % _p
    C = (2 * P[3] * Q[3] * _d) % _p
    D = (2 * P[2] * Q[2]) % _p
    E = B - A
    F = D - C
    G = D + C
    H = B + A
    return ((E * F) % _p, (G * H) % _p, (F * G) % _p, (E * H) % _p)


def _point_mul(s: int, P: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    Q = (0, 1, 1, 0)  # neutral element
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P)
        P = _point_add(P, P)
        s >>= 1
    return Q


def _point_equal(
    P: tuple[int, int, int, int], Q: tuple[int, int, int, int]
) -> bool:
    # x1/z1 == x2/z2  iff  x1*z2 == x2*z1
    if (P[0] * Q[2] - Q[0] * P[2]) % _p != 0:
        return False
    if (P[1] * Q[2] - Q[1] * P[2]) % _p != 0:
        return False
    return True


def _point_decompress(s: bytes) -> Optional[tuple[int, int, int, int]]:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, (x * y) % _p)


def _sha512_modq(b: bytes) -> int:
    return int.from_bytes(hashlib.sha512(b).digest(), "little") % _q


def _ed25519_verify(pubkey: bytes, message: bytes, signature: bytes) -> bool:
    """Strict Ed25519 verification per RFC 8032: SB == R + hA mod L
    (no cofactor multiplication). Returns False on any malformed
    input rather than raising — the caller's job is to translate
    that into a user-facing SignatureError with context.
    """
    if len(pubkey) != 32 or len(signature) != 64:
        return False
    A = _point_decompress(pubkey)
    if A is None:
        return False
    R = _point_decompress(signature[:32])
    if R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _q:
        return False
    h = _sha512_modq(signature[:32] + pubkey + message)
    sB = _point_mul(s, _BASEPOINT)
    hA = _point_mul(h, A)
    return _point_equal(sB, _point_add(R, hA))
