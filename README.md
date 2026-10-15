# bitcoin-node-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives
a client read-only access to a Bitcoin Core node (`bitcoind`) over JSON-RPC.
No keys, no wallet, no chain writes: nothing here can move funds or change
the chain. It shares the podman `npm-network` with the node and reaches it by
container name.

Thirteen tools, all read-only:

| Tool | What it does |
| --- | --- |
| `get_blockchain_info` | Chain, height, sync progress, difficulty, and — when pruned — `pruneheight` |
| `get_block_hash` | Block hash at a height |
| `get_block` | A block by hash; verbosity 1 lists txids, 2 returns full transactions |
| `get_block_header` | A block header by block hash |
| `get_raw_transaction` | A transaction by id; needs a `blockhash` for confirmed txs (see below) |
| `decode_raw_transaction` | Decode a serialised (hex) transaction |
| `get_mempool_info` | Mempool statistics |
| `get_raw_mempool` | Transaction ids currently in the mempool |
| `get_tx_out` | An unspent output (UTXO-set read; works without txindex) |
| `get_network_info` | Version, connections, relay fee |
| `get_peer_info` | One entry per connected peer |
| `estimate_smart_fee` | Fee rate for a confirmation target |
| `get_difficulty` | Proof-of-work difficulty |

Each tool returns text — a scalar as-is, an object as pretty-printed JSON.
They are all marked read-only and open-world, so a well-behaved client skips
the confirmation prompt.

## The pruned, txindex-less node

This server is written for a node that is **pruned** and runs with **txindex
disabled**, which is the configuration on the target host. That shapes two
tools and is worth understanding before you use them:

- **`get_raw_transaction`** can only resolve a confirmed transaction when you
  pass its `blockhash`. Without one, only a transaction still in the mempool
  resolves; for a confirmed txid the node answers `-5`, and the tool turns
  that into a message telling you to supply the block. Find the block with
  `get_block_hash(height)` then `get_block(hash, verbosity=2)`, which returns
  each transaction in the block.
- **`get_block`** cannot return a block below the node's `pruneheight`; its
  data has been discarded. The tool reports that plainly instead of leaking
  `-1 Block not available (pruned data)`. Read the current floor from
  `get_blockchain_info`.

`get_tx_out` is unaffected: it reads the UTXO set, not the transaction index.

> Bitcoin Core will not run `-txindex` on a pruned node (`-help`: *"incompatible
> with -txindex ... reverting this setting requires re-downloading the entire
> blockchain"*), so enabling it here would mean a new disk and a full chain
> re-download. The tools are shaped to the node instead.

## Running it

```sh
uv sync                 # installs from uv.lock
BITCOIN_MCP_TOKEN=<a long random token> \
BITCOIN_RPC_USER=btcuser BITCOIN_RPC_PASSWORD=<pw> \
uv run python server.py
```

Or, inside the container, with no uv anywhere:

```sh
podman build -t localhost/bitcoin-node-mcp:latest .
podman run -e BITCOIN_MCP_TOKEN=<token> \
  -e BITCOIN_RPC_USER=btcuser -e BITCOIN_RPC_PASSWORD=<pw> \
  -p 8101:8000 localhost/bitcoin-node-mcp:latest
```

It listens on `0.0.0.0:8000` and serves streamable HTTP at `/mcp`. `GET
/health` is unauthenticated and answers `{"status":"ok"}` — nothing more, so a
poller can say "up" without holding the token.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `BITCOIN_RPC_URL` | `http://bitcoind:8332` | The node's JSON-RPC endpoint |
| `BITCOIN_RPC_USER` / `BITCOIN_RPC_PASSWORD` | – | RPC credentials; also settable in `upstream.env` |
| `BITCOIN_RPC_TIMEOUT` | `30` | RPC timeout in seconds |
| `BITCOIN_MCP_TOKEN` | – | Bearer token. **Required** unless `MCP_ALLOW_ANONYMOUS=1` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | Listen address |
| `MCP_ALLOW_ANONYMOUS` | – | Set to `1` to serve with no token (loud warning) |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `BITCOIN_MCP_URL` | `http://127.0.0.1:8000/mcp` | Where `healthcheck.py` probes |

Without `BITCOIN_MCP_TOKEN` the server refuses to start rather than serving
unauthenticated — the port is published on the LAN.

## Security model

This container shares `npm-network` with `bitcoind`, `monerod`,
`monero-wallet-rpc`, `lightningd` and the rest of the stack, and publishes
`8101` on the LAN. It is deliberately the least dangerous member of that
stack: it speaks only to bitcoind's RPC port, and only read methods, so a
prompt-injected client cannot reach a wallet or a spending RPC through it.

The bearer token gates every request except `/health`; the comparison is
constant-time. Startup fails without a token unless `MCP_ALLOW_ANONYMOUS=1`.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/) from
`pyproject.toml`; `uv.lock` is the exact closure (versions *and* hashes).

```sh
uv run pytest -q                # 34 tests, ~0.1s, no network
uv run ruff check .             # lint
uv run pyright                  # typecheck
uv run python scripts/smoke.py  # boots the real app on loopback
uv add <package>                # edits pyproject.toml, then re-locks
```

`rpc.py` is stdlib-only on purpose (no `httpx`/`httpx2`), so the dependency
closure is just `mcp` + `uvicorn` — less to audit for a server on the node's
network. `tests/test_rpc.py` drives it with a fake `urlopen`; `tests/test_tools.py`
drives each tool against a scripted fake node.

### The image

A build stage turns `uv.lock` into a hash-pinned requirements file, the
runtime stage installs it with `uv pip install --system --require-hashes`, and
both build tools — uv and pip — are deleted in the same layer.

### Adding a tool

```python
@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def my_tool(height: Annotated[int, Field(ge=0)]) -> str:
    """What the model sees. Args: ..."""
    return _call("getblockhash", [height])
```

`structured_output=False` keeps a text result from being sent twice;
`_NODE_TOOLS` marks it read-only so clients skip the confirmation prompt; and
`_call` renders the result or a readable error. Add the name to `TOOLS` in
`tests/test_schemas.py` — that test pins the exact tool set.

## Deploying

The runtime for this server — its quadlet unit and env wiring — is owned by the
separate [`stack`](https://github.com/regularnmae/stack) repo, not here. `stack`
supplies the bearer token, reads the node's RPC credentials into this server's
env file, publishes the host port and provides the healthcheck; see that repo
for the unit and env-file locations.

```sh
cd ~/build/stack
./deploy.sh --only bitcoin-node-mcp   # build the image, restart, wait for "(healthy)"
./deploy.sh                           # the whole stack
```

No secret is committed here; the env-file shape is shown in
`stack/deploy/examples/bitcoin-node-mcp/`.
