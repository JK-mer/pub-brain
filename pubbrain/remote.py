"""Remote exposure: app-level auth and what stays behind (#48).

Cloudflare Access and connector OAuth are the front door; this is the second
lock. One Cloudflare misconfiguration should not be the difference between
private and a fully open write surface, so the workbench and the MCP server
each check a token of their own when one is configured.

**Off unless configured.** With no token set nothing changes — the workbench
answers on localhost and the LAN exactly as before (#51). That keeps the
common case (the workstation) free of a login it does not need.

**`PUBBRAIN_REMOTE=1` is the tunnel-facing mode**, and it does two things:
it requires the tokens rather than merely honouring them, and it drops the
upcoming layer (#56) entirely — routes, endpoint and nav. The owner's default
on #48 is that internal notes about unpublished work do not cross the tunnel,
so the safe state is the one that needs no configuration to be safe: a remote
deployment that forgets to think about it serves only what merics.org has
already published.
"""

import hmac
import os

WEB_TOKEN_ENV = "PUBBRAIN_WEB_TOKEN"
MCP_TOKEN_ENV = "PUBBRAIN_MCP_TOKEN"
REMOTE_ENV = "PUBBRAIN_REMOTE"
COOKIE = "pubbrain_token"


def is_remote() -> bool:
    return os.environ.get(REMOTE_ENV, "").strip().lower() in ("1", "true", "yes")


def web_token():
    return os.environ.get(WEB_TOKEN_ENV, "").strip() or None


def mcp_token():
    return os.environ.get(MCP_TOKEN_ENV, "").strip() or None


def serves_upcoming() -> bool:
    """The upcoming layer is local-only by default (#48, #56)."""
    return not is_remote()


def check_config() -> list:
    """Problems that must not start a remote service. Returned rather than
    raised so the caller decides — the CLI refuses, a test inspects."""
    if not is_remote():
        return []
    missing = [env for env, value in ((WEB_TOKEN_ENV, web_token()),
                                      (MCP_TOKEN_ENV, mcp_token())) if not value]
    return [f"{REMOTE_ENV} is set but ${env} is empty — a remote service "
            f"without its own token has only Cloudflare between it and the "
            f"internet" for env in missing]


def token_ok(presented, expected) -> bool:
    """Constant-time compare. `hmac.compare_digest` rather than `==` because a
    token check that leaks its own timing is not a token check."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(str(presented), str(expected))


def bearer_from(header):
    """The token out of an `Authorization: Bearer …` header, or None."""
    if not header:
        return None
    scheme, _, value = str(header).partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None
