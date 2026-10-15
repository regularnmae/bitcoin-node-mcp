"""Tool behaviour, driven through the in-memory client against a fake node.

The fake records the RPC call each tool made, so these tests pin both the
parameter shaping (a blockhash appended only when given, include_mempool
forwarded) and the two conditions that matter on this node: txindex is off,
so a confirmed transaction without a blockhash must say so, and blocks
below pruneheight are gone.
"""

from __future__ import annotations

import json

import pytest

from rpc import RPCError


@pytest.mark.anyio
async def test_blockchain_info_is_rendered_as_json(call, fake_rpc) -> None:
    fake_rpc.answers["getblockchaininfo"] = {"chain": "main", "blocks": 971537, "pruned": True}
    text = await call("get_blockchain_info")
    payload = json.loads(text)
    assert payload["chain"] == "main"
    assert payload["pruned"] is True
    assert fake_rpc.calls == [("getblockchaininfo", None)]


@pytest.mark.anyio
async def test_block_hash_returns_bare_string(call, fake_rpc) -> None:
    fake_rpc.answers["getblockhash"] = "0000abcd"
    text = await call("get_block_hash", height=7)
    assert text == "0000abcd"  # a scalar is not JSON-quoted
    assert fake_rpc.calls == [("getblockhash", [7])]


@pytest.mark.anyio
async def test_raw_transaction_without_blockhash_explains_txindex(
    call, fake_rpc
) -> None:
    fake_rpc.answers["getrawtransaction"] = RPCError(
        "No such mempool transaction. Use -txindex or provide a block hash", -5
    )
    text = await call("get_raw_transaction", txid="ab" * 32)
    assert "txindex" in text
    assert "blockhash" in text
    assert fake_rpc.calls == [("getrawtransaction", ["ab" * 32, True])]


@pytest.mark.anyio
async def test_raw_transaction_with_blockhash_is_forwarded(call, fake_rpc) -> None:
    fake_rpc.answers["getrawtransaction"] = {"txid": "ab" * 32}
    await call("get_raw_transaction", txid="ab" * 32, blockhash="cd" * 32, verbose=False)
    assert fake_rpc.calls == [("getrawtransaction", ["ab" * 32, False, "cd" * 32])]


@pytest.mark.anyio
async def test_pruned_block_is_reported_plainly(call, fake_rpc) -> None:
    fake_rpc.answers["getblock"] = RPCError("Block not available (pruned data)", -1)
    text = await call("get_block", blockhash="ef" * 32)
    assert "prune height" in text
    assert "pruneheight" in text


@pytest.mark.anyio
async def test_tx_out_forwards_every_argument(call, fake_rpc) -> None:
    fake_rpc.answers["gettxout"] = {"value": 1.5}
    await call("get_tx_out", txid="ab" * 32, n=2, include_mempool=False)
    assert fake_rpc.calls == [("gettxout", ["ab" * 32, 2, False])]


@pytest.mark.anyio
async def test_unknown_rpc_error_is_surfaced_with_its_code(call, fake_rpc) -> None:
    fake_rpc.answers["getnetworkinfo"] = RPCError("boom", -32601)
    text = await call("get_network_info")
    assert "boom" in text
    assert "-32601" in text


@pytest.mark.anyio
async def test_transport_error_has_no_code(call, fake_rpc) -> None:
    fake_rpc.answers["getmempoolinfo"] = RPCError(
        "cannot reach the node at http://x: connection refused"
    )
    text = await call("get_mempool_info")
    assert "cannot reach the node" in text
