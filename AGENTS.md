# bitcoin-node-mcp — agent notes

Flat three-module Python MCP server (not a package). `server.py` is the app —
config, tools, auth middleware; `rpc.py` is its **stdlib-only** Bitcoin Core
JSON-RPC client; `healthcheck.py` is the container healthcheck probe. Shipped
as a rootless podman/quadlet container, quadlet unit under `deploy/`. Toolchain
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
uv run pytest -q               # 34 tests, ~0.1s, no network
uv run python scripts/smoke.py # boots the real app on a free loopback port: /health, 401s, handshake
uv add <pkg>                   # edits pyproject.toml AND re-locks — commit the uv.lock change
uv lock --check                # CI drift gate
podman build -t localhost/bitcoin-node-mcp:latest .   # multi-stage Containerfile
./deploy.sh                    # builds, restarts bitcoin-node-mcp.service, waits for "(healthy)"
```

## Facts that are easy to get wrong

- **The node is pruned with `txindex=0`.** This is not incidental — it is why
  `get_raw_transaction` takes an optional `blockhash`, why `-5` is turned into
  guidance, why `get_block` special-cases the pruned error, and why there is no
  `get_tx_out_proof` tool. Do not "fix" these by adding txindex: Core forbids
  `-txindex` on a pruned node, and the host has neither the disk nor the chain
  for it. See README.
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
- `.containerignore` excludes `tests/`, `scripts/`, `deploy/`, `*.md`, `.git`.
- The image declares no `HEALTHCHECK`; the quadlet supplies
  `HealthCmd=python /app/healthcheck.py` plus `BITCOIN_MCP_TOKEN`.
- `deploy/install.sh` writes `~/.config/bitcoin-node-mcp/token.env` and
  `upstream.env` (both mode 600) and symlinks the unit. It reads the RPC
  credentials from the **running `bitcoind` container**, so it needs bitcoind
  up on a fresh host. `npm-network.network` is symlinked only if no other repo
  already provided it.
- Publishing: `PublishPort=8101:8000` (host 8101 → container 8000). Open
  `8101/tcp` in firewalld. `/health` is open and answers exactly
  `{"status":"ok"}`.

## Testing quirks

- Tests talk to the server through the SDK's in-memory `Client(mcp)` — no test
  opens a socket. `fake_rpc` (in `tests/conftest.py`) replaces
  `server.rpc.call` with a scripted fake that records calls; use it to pin
  parameter shaping and error handling.
- `pytest` runs with `pythonpath = ["."]`, so `import server` / `import rpc`
  work as top-level modules. Do **not** `from conftest import ...` in a test —
  `tests` is a package and that import fails; use the fixtures.
