# bitcoin-node-mcp — agent notes

Flat three-module Python MCP server (not a package). `server.py` is the app —
config, tools, auth middleware; `rpc.py` is its **stdlib-only** Bitcoin Core
JSON-RPC client; `healthcheck.py` is the container healthcheck probe. Shipped
as a rootless podman/quadlet container; the runtime — its quadlet unit, the
`npm-network` it shares and the env wiring — is owned by the `stack` repo
(https://github.com/regularnmae/stack), not here. Toolchain
is uv only — no pip in CI, in the build, or in the image (CI asserts it).
`mcp` is the 2.x line: the app object is `MCPServer` (the v2 rename of
`FastMCP`).

This is one of four sibling crypto MCP servers (`monero-node-mcp`,
`monero-wallet-mcp`, `lightning-mcp`), modelled on the pre-existing
`searxng-mcp`. Follow that repo's structure and conventions. Env vars, tool
list and the security model are documented in `README.md`; don't re-derive
them from the code.

## Commands

```sh
uv run ruff check .            # lint (CI gate). No `ruff format`; line-length 100, rules E,F,I,UP,B,SIM
uv run pyright                 # typecheck (CI gate); include list in [tool.pyright]
uv run pytest -q               # 52 tests, ~0.3s, no network
uv run python scripts/smoke.py # boots the real app on loopback: /health, /ready, 401s, handshake
uv add <pkg>                   # edits pyproject.toml AND re-locks — commit the uv.lock change
uv lock --check                # CI drift gate
podman build -t localhost/bitcoin-node-mcp:latest .   # single-stage Containerfile
```

Deployment is owned by the `stack` repo: `cd ~/build/stack && ./deploy.sh --only bitcoin-node-mcp`
rebuilds the image, restarts `bitcoin-node-mcp.service` and waits for
"(healthy)"; `./deploy.sh` deploys the whole stack. The unit and `npm-network`
live there, not here.

## Facts that are easy to get wrong

- **The node is a full node with `txindex=1`.** It was pruned with `txindex=0`
  when the tools were written, and that history is still visible in them.
  `get_raw_transaction` keeps its optional `blockhash` and turns `-5` into
  guidance because a newly enabled txindex backfills in the background and
  cannot resolve recent transactions until it catches up; `get_block` keeps the
  pruned-error branch as defence for a repointed node. Do not describe the node
  as pruned in docs or tool text. See README.
- `rpc.py` is **stdlib-only on purpose** (urllib, not httpx2). It raises one
  exception type, `RPCError`, for a JSON-RPC error, an HTTP status and a
  transport failure alike; `code` is Core's numeric code or None. Tests fake
  `urllib.request.urlopen` rather than a transport.
- Tool bodies are synchronous (`def`, not `async def`); anyio runs them on
  worker threads. Keep new tools sync.
- Every `@mcp.tool` passes `structured_output=False` and
  `annotations=_NODE_TOOLS`; `tests/test_schemas.py` asserts this and the exact
  set of tool names — adding a tool means updating `TOOLS` there.
- Numeric args are bounded with `Annotated[int, Field(ge=…, le=…)]`, closed
  sets use `Literal`. The docstring `Args:` section is the model-visible
  contract.
- A new top-level module must be added to the `Containerfile`'s `COPY` lines
  **and** pyright's `include`, or it won't ship or typecheck.
- `.containerignore` excludes `tests/`, `scripts/`, `*.md`, `.git`.
- The image declares no `HEALTHCHECK`; the `stack` unit supplies
  `HealthCmd=python /app/healthcheck.py`. `stack` also reads the RPC credentials
  out of the running `bitcoind` container and writes this server's env file(s)
  (shape: `stack/deploy/examples/bitcoin-node-mcp/`); see that repo for the
  unit, the env-file location and the host port.
- The server binds `0.0.0.0:8000` and serves `/mcp` (token-gated); `/health` is
  open and answers exactly `{"status":"ok"}`. `/ready` is also open and probes
  the node (`200` with chain state, `503` when it does not answer, 2s timeout);
  the podman healthcheck must stay on `/health`, or a syncing node restart-loops
  the container.

## Testing quirks

- Tests talk to the server through the SDK's in-memory `Client(mcp)` — no test
  opens a socket. `fake_rpc` (in `tests/conftest.py`) replaces
  `server.rpc.call` with a scripted fake that records calls; use it to pin
  parameter shaping and error handling.
- `pytest` runs with `pythonpath = ["."]`, so `import server` / `import rpc`
  work as top-level modules. Do **not** `from conftest import ...` in a test —
  `tests` is a package and that import fails; use the fixtures.
