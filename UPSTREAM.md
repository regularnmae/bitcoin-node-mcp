# What this repo owns, and what it depends on

This repo owns only the **application** — its Python code, tests, CI and
`Containerfile`. It ships no unit and no env file. The runtime is owned by
[`stack`](https://github.com/regularnmae/stack), which holds every quadlet unit
and the `npm-network.network` the MCP servers share, and renders the
`~/.config/<name>/*.env` files. See `stack/deploy/` and
`stack/deploy/examples/bitcoin-node-mcp/`.

It does **not** own `bitcoind` either. The node's unit is owned by `stack`,
together with the rest of the crypto node stack. That is deliberate: several
MCP repos need bitcoind, and one file must have one owner. See that repo to
rebuild the node.

## Upstream contract

The server assumes a bitcoind reachable on the podman network:

| | |
| --- | --- |
| Container / address | `bitcoind:8332` (JSON-RPC, no host publish) |
| Network | `npm-network` |
| Auth | rpcuser/rpcpassword (from bitcoind's own `bitcoin.conf`) |
| Config | **pruned**, **`txindex=0`** |

Two consequences shape the tools:

- **`txindex=0`** — `get_raw_transaction` can only resolve a confirmed
  transaction when given its `blockhash`; without one only mempool transactions
  resolve. There is no `get_tx_out_proof` tool for the same reason.
- **pruned** — `get_block` below the node's `pruneheight` is unavailable.

If you ever point this server at a different node, keep those two properties in
mind (or `aggregate` the tool schemas change).

## Deploy

```sh
cd ~/build/stack
./deploy.sh --only bitcoin-node-mcp   # build the image, restart, wait for "(healthy)"
```

`stack/deploy/install.sh` reads the RPC credentials out of the running
`bitcoind` container, so bitcoind should be up first. Secrets land in
`~/.config/bitcoin-node-mcp/`; `./deploy.sh` opens `8101/tcp` in firewalld.
