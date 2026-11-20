"""Fast BIP32 derivation with cached account node."""
from __future__ import annotations

import hashlib
import hmac

from coincurve import PrivateKey
from eth_utils import keccak
from mnemonic import Mnemonic

_MN = Mnemonic("english")
WORDLIST = _MN.wordlist
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
H = 0x80000000
EVM_ACCOUNT = (44 + H, 60 + H, 0 + H, 0)


def _ckd(k: int, c: bytes, i: int) -> tuple[int, bytes]:
    if i >= H:
        data = b"\x00" + k.to_bytes(32, "big") + i.to_bytes(4, "big")
    else:
        data = PrivateKey(k.to_bytes(32, "big")).public_key.format(True) + i.to_bytes(4, "big")
    out = hmac.new(c, data, hashlib.sha512).digest()
    return (int.from_bytes(out[:32], "big") + k) % N, out[32:]


def _master(seed: bytes) -> tuple[int, bytes]:
    out = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return int.from_bytes(out[:32], "big"), out[32:]


class Account:
    __slots__ = ("seed", "_k", "_c")

    def __init__(self, seed: str):
        self.seed = seed
        k, c = _master(_MN.to_seed(seed))
        for i in EVM_ACCOUNT:
            k, c = _ckd(k, c, i)
        self._k, self._c = k, c

    def child(self, index: int) -> tuple[int, str]:
        k, _ = _ckd(self._k, self._c, index)
        pub = PrivateKey(k.to_bytes(32, "big")).public_key.format(False)
        return k, "0x" + keccak(pub[1:])[-20:].hex()

    def batch(self, n: int) -> list[tuple[int, int, str]]:
        return [(i, *self.child(i)) for i in range(n)]


def derive_batch(payload: tuple[str, int]) -> list[tuple[int, int, str]]:
    seed, n = payload
    return Account(seed).batch(n)


def sol_keys(seed: str, n: int) -> list[tuple[int, object, str]]:
    try:
        from solders.keypair import Keypair
    except ImportError:
        return []
    raw = _MN.to_seed(seed)
    out = []
    for i in range(n):
        try:
            kp = Keypair.from_seed_and_derivation_path(raw, f"m/44'/501'/{i}'/0'")
            out.append((i, kp, str(kp.pubkey())))
        except Exception:
            continue
    return out


def valid(seed: str) -> bool:
    return _MN.check(seed)