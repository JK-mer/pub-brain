# Server deployment and remote exposure (#48)

The code half is done and tested. The steps below are the half that needs a
browser, a tunnel provider account and a shell on the deployment server —
they cannot be done from a build session.

Precedent to copy throughout: the calendar service's deployment, which
already runs a tunnel daemon on the same box with an AI-assistant connector
authenticated against a self-hosted OIDC provider.

## What the code already does

| Concern | Mechanism |
|---|---|
| Key delivery on a headless box | `llm.api_key` reads `$PUBBRAIN_LLM_API_KEY` before falling back to `secret-tool`. Unblocks the weekly job (#8) too. |
| App-level auth (second lock) | `PUBBRAIN_WEB_TOKEN`, `PUBBRAIN_MCP_TOKEN`. Off entirely when unset, so the workstation and LAN (#51) are unchanged. |
| Upcoming layer stays home (#56) | `PUBBRAIN_REMOTE=1` unregisters the `/upcoming` blueprint and 404s the Insights endpoint. Not a 403 — the routes do not exist. |
| Fail closed | A service started with `PUBBRAIN_REMOTE=1` and no token **exits** rather than serving. |
| MCP over HTTP | `pubbrain mcp --transport streamable-http`, bearer-checked by ASGI middleware in front of the transport. Verified: 401 without, 200 with. |

**Bind loopback only.** Both units do. The tunnel daemon reaches them from
the same host; binding the LAN would put the bearer token in clear on every
request.

## Steps that need a human

1. **Clone and install** on the server: clone the repo to `~/pub-brain`,
   then install `flask`, `mcp` and `uvicorn` (distro packages or a venv).
   Copy `data/catalog.db` across — the catalog is not in git.
2. **Secrets**: `cp deploy/server/env.example deploy/server/.env`, fill it,
   `chmod 600`. Generate tokens with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. **Services**: copy both `.service` files to `~/.config/systemd/user/`,
   `loginctl enable-linger <user>`, then `systemctl --user enable --now
   pubbrain-web pubbrain-mcp`.
4. **Tunnel**: authenticate the tunnel daemon (browser), create the tunnel,
   and route two hostnames at the loopback ports — one for the workbench
   (8901), one for MCP (8902).
5. **Access policy** on the workbench hostname only: email OTP pinned to your
   addresses, short session. The MCP hostname is authenticated by connector
   OAuth instead — an access wall in front of it would break the connector.
6. **Connector OAuth**: create a static confidential client on the OIDC
   provider, with its resource URL set to the MCP hostname + `/mcp`, then add
   the OAuth proxy. **This needs `fastmcp` 2.x**, which pub-brain does not
   currently depend on — see the open question below.
7. **Verify**: the workbench from a second machine through the tunnel; the
   connector from the assistant; and that nothing answers on 8901/8902 from
   off-box (`curl` from another machine should fail, not 401).

## The one open question

The connector's OAuth needs `fastmcp` 2.x (`OAuthProxy` + `JWTVerifier`),
which the bundled `mcp` SDK does not provide. pub-brain uses the SDK's own
`FastMCP`, so wiring OAuth means taking that dependency and porting
`create_server`. The calendar service already does exactly this and works,
so it is a known quantity rather than research — but it is a dependency
decision, so it is left for the owner rather than made in passing.

Until then the MCP hostname is **bearer-only**: usable from a client that can
set a header, not from a connector UI.

## What is writable over the tunnel

Unresolved, and worth a decision before step 4. MCP exposes exactly one write
(`flag_summary`). The workbench exposes every mutation there is — credits,
body text, summaries, project membership, chapters. Behind an access policy
that is the owner's own hands, but "every write form on the internet" is a
different posture from "read the catalog from a work PC".
