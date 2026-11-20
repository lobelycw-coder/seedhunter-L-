"""Signing adapter (AWS KMS).

No key material in the app. EVM uses ECC_SECG_P256K1 + ECDSA_SHA_256 with DER
signatures, Solana uses ECC_ED25519 + ED25519 raw signatures. boto3 is an
optional dependency: without it (or without a key id) signing is reported as
unavailable instead of faked.
"""
from __future__ import annotations

import hashlib

try:                                       # optional
    import boto3
except ImportError:                        # pragma: no cover
    boto3 = None


class SignerError(Exception):
    pass


def available(cfg, network: str) -> tuple[bool, str]:
    if boto3 is None:
        return False, "boto3 not installed"
    key_id = cfg.kms_key_sol if network == "solana" else cfg.kms_key_evm
    if not key_id:
        return False, "no KMS key id configured"
    return True, "ready"


def _client(cfg):
    return boto3.client("kms", region_name=cfg.kms_region)


def der_to_rs(der: bytes) -> tuple[int, int]:
    """Parse a DER ECDSA signature. Raises on malformed input."""
    if not der or der[0] != 0x30:
        raise SignerError("malformed DER signature")
    index = 2 if der[1] < 0x80 else 3
    if der[index] != 0x02:
        raise SignerError("malformed DER r")
    r_len = der[index + 1]
    r = int.from_bytes(der[index + 2:index + 2 + r_len], "big")
    offset = index + 2 + r_len
    if der[offset] != 0x02:
        raise SignerError("malformed DER s")
    s_len = der[offset + 1]
    s = int.from_bytes(der[offset + 2:offset + 2 + s_len], "big")
    return r, s


def sign_evm_digest(cfg, key_id: str, digest: bytes) -> tuple[int, int, int]:
    """Return (v, r, s) with low-s normalisation and recovery id."""
    response = _client(cfg).sign(KeyId=key_id, Message=digest, MessageType="DIGEST",
                                 SigningAlgorithm="ECDSA_SHA_256")
    r, s = der_to_rs(response["Signature"])
    from eth_keys import keys
    order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    if s > order // 2:
        s = order - s
    for v in (0, 1):
        try:
            pub = keys.Signature(vrs=(v, r, s)).recover_public_key_from_msg_hash(digest)
        except Exception:
            continue
        yield_v = v
        return yield_v, r, s, pub.to_address()
    raise SignerError("could not derive recovery id")


def evm_address_of(cfg, key_id: str) -> str:
    response = _client(cfg).get_public_key(KeyId=key_id)
    from eth_keys import keys
    der = response["PublicKey"]
    raw = der[-65:] if len(der) >= 65 else der
    return keys.PublicKey(raw).to_address()


def solana_address_of(cfg, key_id: str) -> str:
    response = _client(cfg).get_public_key(KeyId=key_id)
    der = response["PublicKey"]                      # SPKI, key is the last 32 bytes
    from base58 import b58encode
    return b58encode(der[-32:]).decode()


def sign_solana(cfg, key_id: str, message: bytes) -> bytes:
    response = _client(cfg).sign(KeyId=key_id, Message=message, MessageType="RAW",
                                 SigningAlgorithm="ED25519")
    return response["Signature"]


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()