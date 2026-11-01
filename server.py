import asyncio
import hmac
import json
import logging
import os
from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from rpc import BitcoinRPC, RPCError

logger = logging.getLogger("bitcoin-node-mcp")

# The SDK types log_level as a closed set, so an unrecognised LOG_LEVEL
# has to be resolved to one of its levels here -- the raw env string is
# not assignable, and an unknown level falling back to INFO beats the
# import-time raise that the SDK would otherwise produce.
_LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: tuple[_LogLevel, ...] = get_args(_LogLevel)


def _log_level() -> _LogLevel:
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    return requested if requested in _LEVELS else "INFO"


def _env_float(name: str, default: float) -> float:
    """A float env var, or the default when unset or unparseable.

    A typo'd timeout should not stop the server from starting, so a bad
    value warns and falls back rather than raising at import.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ignoring non-numeric %s=%r; using %s", name, raw, default)
        return default


# Initialize MCPServer (v2 API; FastMCP was renamed in mcp 2.x).
# `instructions` is the one place to tell the model how these tools chain
# together, and every MCP client surfaces it before the first call.
mcp = MCPServer(
    "Bitcoin Node",
    instructions=(
        "Read-only access to this host's Bitcoin Core node over JSON-RPC. "
        "Start with get_blockchain_info for chain, height and sync progress. "
        "This is a full node with txindex enabled, so get_raw_transaction "
        "resolves a confirmed transaction by txid alone. If it answers -5 the "
        "txindex has not reached that transaction yet (it backfills in the "
        "background): get_index_info reports how far it has got, and passing "
        "the blockhash instead works now -- find it by walking from "
        "get_block_hash(height) to get_block(hash, verbosity=2). For an "
        "unconfirmed transaction, get_mempool_entry answers whether this node "
        "has seen it. Nothing here moves funds or changes the chain."
    ),
    log_level=_log_level(),
)

# Every tool here only reads. Saying so lets a well-behaved client skip a
# confirmation prompt before running one of them. A node is open-world:
# its answers depend on the state of the network it is following.
_NODE_TOOLS = ToolAnnotations(read_only_hint=True, open_world_hint=True)

# Reached over the podman npm-network (container name resolves without any
# host publishing); BITCOIN_RPC_URL overrides.
BITCOIN_RPC_URL = os.environ.get("BITCOIN_RPC_URL", "http://bitcoind:8332")
BITCOIN_RPC_USER = os.environ.get("BITCOIN_RPC_USER", "")
BITCOIN_RPC_PASSWORD = os.environ.get("BITCOIN_RPC_PASSWORD", "")
RPC_TIMEOUT = _env_float("BITCOIN_RPC_TIMEOUT", 30.0)

# Set to "1" to serve the endpoint with no token at all. Without it a
# missing token is a startup error, not a warning.
MCP_ALLOW_ANONYMOUS = os.environ.get("MCP_ALLOW_ANONYMOUS", "") == "1"

# Unauthenticated liveness path, for pollers that hold no token.
HEALTH_PATH = "/health"

# Readiness path: /health says the process is up, this says the node answers.
# Deliberately separate -- a node outage must not fail a liveness probe, or
# the container restarts in a loop while the node catches up.
READY_PATH = "/ready"
READY_TIMEOUT = 2.0

# The env var the bearer token is read from (also the name in the quadlet
# unit's EnvironmentFile and in ~/.config/opencode/opencode.json).
TOKEN_ENV = "BITCOIN_MCP_TOKEN"

rpc = BitcoinRPC(BITCOIN_RPC_URL, BITCOIN_RPC_USER, BITCOIN_RPC_PASSWORD, timeout=RPC_TIMEOUT)


def _render(result: Any) -> str:
    """A tool result as text: a bare string as-is, anything else as JSON.

    A hash or a difficulty is a scalar and reads better unquoted; an
    object is pretty-printed so the model can read it directly.
    """
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


_TXINDEX_MISS = (
    "The node has no mempool transaction with that id, and its txindex did not "
    "resolve it either: the index is either still backfilling or disabled. "
    "Supply the transaction's `blockhash` -- find it with get_block_hash(height) "
    "then get_block(hash, verbosity=2), then call again with it."
)


def _txindex_miss_text() -> str:
    """Explain a -5 from getrawtransaction using the node's own index state.

    Best-effort: any failure here falls back to the wording that covers both
    causes, so a diagnostic call can never replace the error the caller hit.
    """
    try:
        info = rpc.call("getindexinfo", ["txindex"])
    except Exception as err:  # noqa: BLE001 - diagnostics, not the failure
        logger.info("getindexinfo unavailable for the -5 message: %s", err)
        return _TXINDEX_MISS
    index = info.get("txindex") if isinstance(info, dict) else None
    if not isinstance(index, dict):
        return (
            "This node has no txindex, so a confirmed transaction can only be "
            "read with its blockhash: find it with get_block_hash(height) then "
            "get_block(hash, verbosity=2), and call again with it."
        )
    if index.get("synced"):
        return (
            "The txindex is synced, so that transaction is in neither the chain "
            "nor the mempool. Check the txid; if it is recent, this node has "
            "simply not seen it."
        )
    height = index.get("best_block_height")
    if not isinstance(height, int):
        return _TXINDEX_MISS
    tip = ""
    try:
        chain = rpc.call("getblockchaininfo")
    except Exception as err:  # noqa: BLE001 - the tip height is a nicety
        logger.info("getblockchaininfo unavailable for the -5 message: %s", err)
    else:
        blocks = chain.get("blocks") if isinstance(chain, dict) else None
        if isinstance(blocks, int):
            tip = f" of {blocks:,}"
    return (
        f"The txindex is still backfilling: it has reached block {height:,}{tip}, "
        "so a transaction newer than that has no index entry yet. Find its block "
        "with get_block_hash(height) then get_block(hash, verbosity=2), and pass "
        "the blockhash."
    )


def _error_text(err: RPCError) -> str:
    if err.code is None:
        return err.message
    return f"{err.message} (RPC error {err.code})"


def _call(method: str, params: list[Any] | None = None, *, null_text: str = "") -> str:
    """Run one RPC and render the result, or a readable error.

    Every tool whose failures need no special-casing goes through here.
    `null_text`, when set, replaces a bare `null` result with a sentence --
    for the RPCs where null is a real answer rather than a failure.
    """
    try:
        result = rpc.call(method, params)
    except RPCError as err:
        logger.warning("%s failed: %s", method, err.message)
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", method)
        return f"Unexpected error calling {method}: {err}"
    if result is None and null_text:
        return null_text
    return _render(result)


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_blockchain_info() -> str:
    """Return the node's chain state.

    Includes the chain name, block and header counts, verification
    progress, difficulty and size on disk, and -- when the node is pruned
    -- `pruned` and `pruneheight`, the lowest height still on disk.
    """
    return _call("getblockchaininfo")


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_index_info(index_name: str = "", verbose: bool = False) -> str:
    """Return the node's index build state, txindex included.

    Each index carries `synced` and `best_block_height`. That is what
    answers the question a -5 from get_raw_transaction leaves open: how far
    the txindex has backfilled, and whether it is still running.

    Args:
        index_name: One index to report, e.g. "txindex". Empty (default)
            reports every index the node has.
        verbose: True for extra per-index detail, e.g. the recorded best
            block hash; only meaningful alongside index_name.
    """
    # Core's positional signature is (index_name, verbose); sending neither
    # is the "every index" form.
    params: list[Any] | None = [index_name, verbose] if index_name or verbose else None
    return _call("getindexinfo", params)


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_block_hash(height: Annotated[int, Field(ge=0)]) -> str:
    """Return the block hash at a height.

    Use this to turn a height into the `blockhash` that get_block and
    get_raw_transaction need.

    Args:
        height: Block height, 0 for the genesis block.
    """
    return _call("getblockhash", [height])


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_block(blockhash: str, verbosity: Literal[1, 2] = 1) -> str:
    """Return a block by hash.

    At verbosity 1 the block's `tx` field is a list of txids; at verbosity
    2 each entry is the full transaction. This node keeps every block, so
    any hash from get_block_hash is available; verbosity 2 on a busy block
    returns several megabytes.

    Args:
        blockhash: Block hash, as returned by get_block_hash.
        verbosity: 1 for a summary (default), 2 for full transactions.
    """
    try:
        return _render(rpc.call("getblock", [blockhash, verbosity]))
    except RPCError as err:
        # Retained as defence: the target node is unpruned, but a repointed
        # server may not be, and a plain "Block not available" reads badly.
        if err.code == -1 and "pruned" in err.message.lower():
            logger.warning("getblock %s below prune height", blockhash)
            return (
                "That block is below this node's prune height, so its data has "
                "been discarded. Only recent blocks are on disk; call "
                "get_blockchain_info to read the current pruneheight."
            )
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in getblock")
        return f"Unexpected error calling getblock: {err}"


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_block_header(blockhash: str, verbose: bool = True) -> str:
    """Return a block header by block hash.

    Args:
        blockhash: Block hash, as returned by get_block_hash.
        verbose: True (default) for the decoded header, False for the
            serialised hex.
    """
    return _call("getblockheader", [blockhash, verbose])


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_block_stats(height: Annotated[int, Field(ge=0)]) -> str:
    """Return fee and transaction statistics for one block.

    The context for judging a fee rate: min/max/median/mean feerate, total
    fees, transaction count, size and weight, and the UTXO-set deltas. Where
    estimate_smart_fee reports the node's model, this reports what a block
    actually paid.

    Args:
        height: Block height. From a block hash, get_block returns the
            height in its `height` field.
    """
    return _call("getblockstats", [height])


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_fee_history(blocks: Annotated[int, Field(ge=1, le=144)] = 24) -> str:
    """Return per-block feerate history for the most recent blocks.

    What blocks actually paid, block by block, with the window's average and
    range: the trend behind estimate_smart_fee's single number. Costs one
    node call per block in the window. Feerates are in sat/vB.

    Args:
        blocks: Window size in blocks, 1 to 144 (default 24).
    """
    try:
        info = rpc.call("getblockchaininfo")
        tip = info.get("blocks") if isinstance(info, dict) else None
        if not isinstance(tip, int):
            return "Unexpected response from getblockchaininfo: no block height."
        heights = range(max(0, tip - blocks + 1), tip + 1)
        stats = [rpc.call("getblockstats", [height]) for height in heights]
    except RPCError as err:
        logger.warning("getblockstats failed: %s", err.message)
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in getblockstats")
        return f"Unexpected error calling getblockstats: {err}"

    history: list[dict[str, Any]] = []
    for stat in stats:
        if not isinstance(stat, dict):
            continue
        percentiles = stat.get("feerate_percentiles")
        median = percentiles[2] if isinstance(percentiles, list) and len(percentiles) > 2 else None
        history.append(
            {
                "height": stat.get("height"),
                "avg_feerate": stat.get("avgfeerate"),
                "median_feerate": median,
                "txs": stat.get("txs"),
            }
        )
    rates = [
        row["avg_feerate"]
        for row in history
        if isinstance(row["avg_feerate"], (int, float))
    ]
    return _render(
        {
            "from_height": history[0]["height"] if history else None,
            "to_height": history[-1]["height"] if history else None,
            "blocks": len(history),
            "avg_feerate": round(sum(rates) / len(rates), 2) if rates else None,
            "min_avg_feerate": min(rates) if rates else None,
            "max_avg_feerate": max(rates) if rates else None,
            "history": history,
        }
    )


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_raw_transaction(txid: str, blockhash: str = "", verbose: bool = True) -> str:
    """Return a transaction by id.

    This node has txindex enabled, so a confirmed transaction resolves by
    txid alone. Pass `blockhash` only as a fallback: while the index is
    still backfilling it answers -5, and the block is then found with
    get_block_hash(height) and get_block(..., verbosity=2).

    Args:
        txid: Transaction id (hex).
        blockhash: The block the transaction is in. Only needed while the
            txindex is backfilling or disabled; the call then answers -5.
        verbose: True (default) for the decoded transaction, False for the
            serialised hex.
    """
    params: list[Any] = [txid, verbose]
    if blockhash:
        params.append(blockhash)
    try:
        return _render(rpc.call("getrawtransaction", params))
    except RPCError as err:
        if err.code == -5 and not blockhash:
            logger.info("getrawtransaction %s did not resolve", txid)
            return _txindex_miss_text()
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in getrawtransaction")
        return f"Unexpected error calling getrawtransaction: {err}"


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_mempool_entry(txid: str) -> str:
    """Return one unconfirmed transaction's mempool entry.

    Answers whether an unconfirmed transaction has reached this node, and
    what it pays: fee, vsize, ancestor and descendant counts and fees (the
    figure a CPFP child would need to pay against), and the replacement
    flag. A confirmed transaction is not in the mempool -- get_raw_transaction
    resolves those.

    Args:
        txid: Transaction id (hex).
    """
    try:
        return _render(rpc.call("getmempoolentry", [txid]))
    except RPCError as err:
        if err.code == -5:
            logger.info("getmempoolentry %s is not in the mempool", txid)
            return (
                "That transaction is not in this node's mempool: it has not "
                "been seen here, has already been mined, or was evicted. Call "
                "get_raw_transaction(txid) to check whether it is confirmed."
            )
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in getmempoolentry")
        return f"Unexpected error calling getmempoolentry: {err}"


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def decode_raw_transaction(hexstring: str) -> str:
    """Decode a serialised (hex) transaction into a JSON object.

    Args:
        hexstring: The transaction as hex, e.g. from get_raw_transaction
            with verbose=False.
    """
    return _call("decoderawtransaction", [hexstring])


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_mempool_info() -> str:
    """Return statistics about the node's transaction mempool."""
    return _call("getmempoolinfo")


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_raw_mempool(verbose: bool = False) -> str:
    """List the transaction ids currently in the mempool.

    Args:
        verbose: False (default) for a list of txids, True for a map of
            txid to details.
    """
    return _call("getrawmempool", [verbose])


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_tx_out(
    txid: str, n: Annotated[int, Field(ge=0)], include_mempool: bool = True
) -> str:
    """Return details of an unspent transaction output (the UTXO set).

    Works without txindex: this reads the UTXO set, not the transaction
    index. A null answer means the output is spent (or never existed), or
    -- with include_mempool=False -- that it is only in the mempool.

    Args:
        txid: Transaction id (hex).
        n: Output index (vout), 0-based.
        include_mempool: Whether to include the mempool (default True).
    """
    return _call(
        "gettxout",
        [txid, n, include_mempool],
        null_text=(
            "No unspent output with that transaction id and index: it has "
            "been spent, never existed, or -- because include_mempool was "
            "False -- is currently only in the mempool."
        ),
    )


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_network_info() -> str:
    """Return the node's network state: version, connections, relay fee."""
    return _call("getnetworkinfo")


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_peer_info() -> str:
    """Return one entry per connected peer."""
    return _call("getpeerinfo")


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def estimate_smart_fee(
    conf_target: Annotated[int, Field(ge=1, le=1008)] = 6,
    mode: Literal["conservative", "economical", "unset"] = "conservative",
) -> str:
    """Estimate the fee rate needed to confirm within a number of blocks.

    Args:
        conf_target: Confirmation target in blocks, 1 to 1008
            (default 6).
        mode: Estimation mode: "conservative", "economical", or "unset"
            for the node's default.
    """
    try:
        result = rpc.call("estimatesmartfee", [conf_target, mode])
    except RPCError as err:
        logger.warning("estimatesmartfee failed: %s", err.message)
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in estimatesmartfee")
        return f"Unexpected error calling estimatesmartfee: {err}"
    if isinstance(result, dict) and "feerate" not in result and result.get("errors"):
        return (
            f"The node has no fee estimate for {conf_target} blocks: "
            + "; ".join(str(error) for error in result["errors"])
            + ". It needs a fuller set of recent blocks to model one; call "
            "get_fee_history to see what recent blocks actually paid."
        )
    return _render(result)


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_difficulty() -> str:
    """Return the proof-of-work difficulty as a multiple of the minimum."""
    return _call("getdifficulty")


async def _readiness() -> tuple[dict[str, Any], int]:
    """Probe the node for `/ready`, off the event loop with a short timeout.

    Returns the JSON body and status code: 200 with the chain state when the
    node answers, 503 with the reason when it does not.
    """
    try:
        info = await asyncio.to_thread(rpc.call, "getblockchaininfo", None, READY_TIMEOUT)
    except RPCError as err:
        logger.warning("/ready: the node is not answering: %s", err.message)
        return {"status": "unready", "error": err.message}, 503
    except Exception as err:  # noqa: BLE001 - reported, never raised at a probe
        logger.exception("/ready: unexpected error")
        return {"status": "unready", "error": str(err)}, 503
    if not isinstance(info, dict):
        return {"status": "unready", "error": "the node returned an unexpected body"}, 503
    return {
        "status": "ready",
        "chain": info.get("chain"),
        "blocks": info.get("blocks"),
        "initialblockdownload": info.get("initialblockdownload"),
    }, 200


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth in front of the streamable-HTTP app.

    The endpoint is published on the LAN, so a missing token is a hard
    error rather than a warning: serving every tool unauthenticated for
    as long as the env var is absent is not a failure mode worth keeping.
    Set MCP_ALLOW_ANONYMOUS=1 to run it open on purpose, which is what
    debugging from inside npm-network wants.

    GET /health stays unauthenticated so a poller can say "up" without
    holding the token; it says nothing else. GET /ready does the same and
    adds whether the node answers, on a short timeout, for the same reason.
    """

    def __init__(self, app: Any, token: str | None) -> None:
        self.app = app
        self.token = token or ""
        if not self.token:
            if not MCP_ALLOW_ANONYMOUS:
                raise RuntimeError(
                    f"{TOKEN_ENV} is not set, and this endpoint is published "
                    "on the LAN. Set the token, or set MCP_ALLOW_ANONYMOUS=1 "
                    "to serve without authentication."
                )
            logger.warning(
                "%s not set and MCP_ALLOW_ANONYMOUS=1: endpoint is UNAUTHENTICATED",
                TOKEN_ENV,
            )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == HEALTH_PATH:
            from starlette.responses import JSONResponse

            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
            return

        if scope.get("path") == READY_PATH:
            from starlette.responses import JSONResponse

            body, status_code = await _readiness()
            response = JSONResponse(body, status_code=status_code)
            await response(scope, receive, send)
            return

        if self.token and not self._authorised(scope):
            from starlette.responses import JSONResponse

            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _authorised(self, scope: Any) -> bool:
        """Compare the presented credential in constant time."""
        headers = dict(scope.get("headers") or [])
        header = headers.get(b"authorization", b"").decode("latin-1").strip()
        # RFC 7235 makes the scheme case-insensitive; a client sending
        # "bearer <token>" is not a typo worth a 401, and neither is a
        # stray space inside the header.
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer":
            return False
        return hmac.compare_digest(presented.strip().encode(), self.token.encode())


if __name__ == "__main__":
    import uvicorn

    # Accept any Host header: DNS-rebinding protection is disabled
    # because the bearer token gates every request.
    app = BearerAuthMiddleware(
        mcp.streamable_http_app(
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            )
        ),
        os.environ.get(TOKEN_ENV),
    )

    # 0.0.0.0 is what the container publishes; loopback is what running it
    # for a smoke test wants, hence the env overrides.
    uvicorn.run(
        app,
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8000")),
        # Same floor as the SDK above: INFO would log every request
        # (the transport's own access lines included) forever.
        log_level=_log_level().lower(),
    )
