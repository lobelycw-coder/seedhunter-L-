"""Transfer paths: direct → transferFrom → router swap."""
from __future__ import annotations

from eth_utils import keccak

from .chains import NATIVE

TRANSFER = keccak(text="transfer(address,uint256)")[:4].hex()
TRANSFER_FROM = keccak(text="transferFrom(address,address,uint256)")[:4].hex()
SAFE_721 = keccak(text="safeTransferFrom(address,address,uint256)")[:4].hex()
SAFE_1155 = keccak(text="safeTransferFrom(address,address,uint256,uint256,bytes)")[:4].hex()
APPROVE = keccak(text="approve(address,uint256)")[:4].hex()
SWAP = keccak(text="swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)")[:4].hex()


def w(v: int) -> str:
    return f"{v:064x}"


def candidates(asset: dict, owner: str, dest: str, router: str, weth: str) -> list[tuple[str, str]]:
    """Ordered (target, calldata) rescue attempts."""
    t, raw, kind = asset["token"], asset["raw"], asset["kind"]
    out: list[tuple[str, str]] = []
    if kind == "erc721":
        out.append((t, "0x" + SAFE_721 + w(int(owner, 16)) + w(int(dest, 16)) + w(raw)))
        out.append((t, "0x" + SAFE_1155 + w(int(owner, 16)) + w(int(dest, 16))
                    + w(raw) + w(1) + w(160) + w(0)))
        return out
    out.append((t, "0x" + TRANSFER + w(int(dest, 16)) + w(raw)))
    out.append((t, "0x" + TRANSFER_FROM + w(int(owner, 16)) + w(int(dest, 16)) + w(raw)))
    if router and weth:
        out.append((router, "0x" + SWAP + w(raw) + w(0) + w(160) + w(int(dest, 16))
                    + w(9999999999) + w(2) + w(int(t, 16)) + w(int(weth, 16))))
    return out