# deploy/ — what this repo owns, and what it depends on

This repo owns exactly **one** unit — `bitcoin-node-mcp.container` — plus a
shared network file:

- `bitcoin-node-mcp.container` — the MCP server, `PublishPort=8101:8000`.
- `npm-network.network` — the podman network it shares with the daemons.
  `install.sh` links it **only if no other repo has already provided it**, so
  deploying this repo cannot fight another one over the same file.

It does **not** own `bitcoind`. The node's unit is owned by
[`crypto-nodes`](https://github.com/regularnmae/crypto-nodes), together with the
rest of the crypto node stack. That is deliberate: several MCP repos need
bitcoind, and one file must have one owner. See that repo to rebuild the node.

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
./deploy/install.sh     # token.env + upstream.env, symlink the unit, reload
./deploy.sh             # install + build + restart + wait healthy
sudo firewall-cmd --permanent --add-port=8101/tcp && sudo firewall-cmd --reload
```

`install.sh` reads the RPC credentials out of the running `bitcoind` container,
so bitcoind should be up first. Secrets land in `~/.config/bitcoin-node-mcp/`.
