"""Tool behaviour, driven through the in-memory client against a fake node.

The fake records the RPC call each tool made, so these tests pin both the
parameter shaping (a blockhash appended only when given, include_mempool
forwarded) and the two error paths worth wording: a -5 from a txindex that
has not caught up must explain the blockhash fallback, and a pruned reply
must be reported plainly rather than leaked raw.
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
async def test_index_info_with_no_arguments_asks_for_every_index(call, fake_rpc) -> None:
    fake_rpc.answers["getindexinfo"] = {"txindex": {"synced": False, "best_block_height": 932000}}
    text = await call("get_index_info")
    payload = json.loads(text)
    assert payload["txindex"]["synced"] is False
    assert fake_rpc.calls == [("getindexinfo", None)]


@pytest.mark.anyio
async def test_index_info_forwards_index_name_and_verbose(call, fake_rpc) -> None:
    fake_rpc.answers["getindexinfo"] = {"txindex": {"synced": True}}
    await call("get_index_info", index_name="txindex", verbose=True)
    assert fake_rpc.calls == [("getindexinfo", ["txindex", True])]


@pytest.mark.anyio
async def test_block_stats_forwards_height(call, fake_rpc) -> None:
    fake_rpc.answers["getblockstats"] = {"height": 974130, "avgfeerate": 8}
    text = await call("get_block_stats", height=974130)
    assert json.loads(text)["avgfeerate"] == 8
    assert fake_rpc.calls == [("getblockstats", [974130])]


@pytest.mark.anyio
async def test_fee_history_summarises_the_default_window(call, fake_rpc) -> None:
    fake_rpc.answers["getblockchaininfo"] = {"blocks": 100}
    fake_rpc.answers["getblockstats"] = lambda params: {
        "height": params[0],
        "avgfeerate": 10,
        "feerate_percentiles": [4, 6, 8, 10, 12],
        "txs": 2000,
    }
    payload = json.loads(await call("get_fee_history"))
    assert payload["blocks"] == 24
    assert (payload["from_height"], payload["to_height"]) == (77, 100)
    assert payload["avg_feerate"] == 10
    assert payload["history"][0]["median_feerate"] == 8


@pytest.mark.anyio
async def test_fee_history_reports_a_block_lookup_failure(call, fake_rpc) -> None:
    fake_rpc.answers["getblockchaininfo"] = {"blocks": 100}
    fake_rpc.answers["getblockstats"] = RPCError("Block height out of range", -8)
    text = await call("get_fee_history", blocks=4)
    assert "out of range" in text
    assert "-8" in text


@pytest.mark.anyio
async def test_mempool_entry_is_forwarded(call, fake_rpc) -> None:
    fake_rpc.answers["getmempoolentry"] = {"vsize": 141, "fees": {"base": 0.00001}}
    text = await call("get_mempool_entry", txid="ab" * 32)
    assert json.loads(text)["vsize"] == 141
    assert fake_rpc.calls == [("getmempoolentry", ["ab" * 32])]


@pytest.mark.anyio
async def test_mempool_entry_missing_points_at_the_confirmed_lookup(call, fake_rpc) -> None:
    fake_rpc.answers["getmempoolentry"] = RPCError("Transaction not in mempool", -5)
    text = await call("get_mempool_entry", txid="ab" * 32)
    assert "mempool" in text
    assert "get_raw_transaction" in text


@pytest.mark.anyio
async def test_txindex_still_backfilling_is_reported_with_its_height(call, fake_rpc) -> None:
    fake_rpc.answers["getrawtransaction"] = RPCError(
        "No such mempool transaction. Use -txindex or provide a block hash", -5
    )
    fake_rpc.answers["getindexinfo"] = {
        "txindex": {"synced": False, "best_block_height": 938967}
    }
    fake_rpc.answers["getblockchaininfo"] = {"blocks": 974136}
    text = await call("get_raw_transaction", txid="ab" * 32)
    assert "938,967 of 974,136" in text
    assert "blockhash" in text
    assert fake_rpc.calls[0] == ("getrawtransaction", ["ab" * 32, True])


@pytest.mark.anyio
async def test_txindex_absent_is_named_as_the_cause(call, fake_rpc) -> None:
    fake_rpc.answers["getrawtransaction"] = RPCError("No such transaction", -5)
    fake_rpc.answers["getindexinfo"] = {}
    text = await call("get_raw_transaction", txid="ab" * 32)
    assert "no txindex" in text
    assert "blockhash" in text


@pytest.mark.anyio
async def test_synced_txindex_means_the_transaction_is_unknown(call, fake_rpc) -> None:
    fake_rpc.answers["getrawtransaction"] = RPCError("No such transaction", -5)
    fake_rpc.answers["getindexinfo"] = {"txindex": {"synced": True}}
    text = await call("get_raw_transaction", txid="ab" * 32)
    assert "neither the chain nor the mempool" in text
    assert "blockhash" not in text


@pytest.mark.anyio
async def test_failed_diagnosis_falls_back_to_the_ambiguous_wording(call, fake_rpc) -> None:
    # A broken getindexinfo must not replace the error the caller actually hit.
    fake_rpc.answers["getrawtransaction"] = RPCError("No such transaction", -5)
    fake_rpc.answers["getindexinfo"] = RPCError("boom", -32601)
    text = await call("get_raw_transaction", txid="ab" * 32)
    assert "backfilling or disabled" in text
    assert "blockhash" in text


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
async def test_spent_tx_out_is_explained_not_null(call, fake_rpc) -> None:
    # A callable, because the fake treats an `answers` value of None as
    # "no script for this method" rather than a null result.
    fake_rpc.answers["gettxout"] = lambda _params: None
    text = await call("get_tx_out", txid="ab" * 32, n=0)
    assert text != "null"
    assert "spent" in text
    assert "mempool" in text


@pytest.mark.anyio
async def test_fee_estimate_without_data_becomes_a_sentence(call, fake_rpc) -> None:
    fake_rpc.answers["estimatesmartfee"] = {
        "errors": ["Insufficient data or no feerate found"],
        "blocks": 6,
    }
    text = await call("estimate_smart_fee")
    assert "no fee estimate" in text
    assert "Insufficient data" in text
    assert "get_fee_history" in text


@pytest.mark.anyio
async def test_fee_estimate_with_a_rate_is_still_json(call, fake_rpc) -> None:
    fake_rpc.answers["estimatesmartfee"] = {"feerate": 0.00002, "blocks": 6}
    text = await call("estimate_smart_fee")
    assert json.loads(text)["feerate"] == 0.00002


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
