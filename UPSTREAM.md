# What this repo owns, and what it depends on

This repo owns only the **application** — its Python code, tests, CI and
`Containerfile`. It ships no unit and no env file. The runtime is owned by
[`stack`](https://github.com/regularnmae/stack), which holds every quadlet unit
and the `npm-network.network` the MCP servers share, writes this server's env
file(s), and publishes the host port. See that repo for the unit, the env-file
locations and `stack/deploy/examples/bitcoin-node-mcp/`.

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
| Config | **full node**, **`txindex=1`** |

One property still shapes the tools:

- **`txindex` backfills once enabled** — no `-reindex` is needed, but until the
  index catches up `get_raw_transaction` answers `-5` for recent transactions
  and needs their `blockhash`. The tool keeps that guidance for exactly this
  window, and `get_block` keeps a pruned-error branch as defence in case this
  server is ever repointed at a pruned node.

Address history stays out of reach either way: bitcoind has no address index,
so an Electrum server (Fulcrum) is the thing to add next to it, not a tool.

## Deploy

```sh
cd ~/build/stack
./deploy.sh --only bitcoin-node-mcp   # build the image, restart, wait for "(healthy)"
```

`stack` reads the RPC credentials out of the running `bitcoind` container, so
bitcoind should be up first. See that repo for the env-file location and the
host port it publishes.
