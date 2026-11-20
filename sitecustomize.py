"""Auto-imported by CPython when the repo root is on sys.path.

No-op unless SH_ATTACH=1. Wrapped so it can never break a hunt run.
"""
try:
    from rpc_service import attach
    attach.install()
except Exception:
    pass
