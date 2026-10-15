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
        "Start with get_blockchain_info for chain, height, sync progress "
        "and the prune height. The node is pruned and run with txindex "
        "disabled, so get_raw_transaction needs an explicit blockhash for "
        "a confirmed transaction (use get_block_hash then get_block with "
        "verbosity=2 to find it), and blocks below pruneheight are not on "
        "disk. Nothing here moves funds or changes the chain."
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


def _error_text(err: RPCError) -> str:
    if err.code is None:
        return err.message
    return f"{err.message} (RPC error {err.code})"


def _call(method: str, params: list[Any] | None = None) -> str:
    """Run one RPC and render the result, or a readable error.

    Every tool whose failures need no special-casing goes through here.
    """
    try:
        return _render(rpc.call(method, params))
    except RPCError as err:
        logger.warning("%s failed: %s", method, err.message)
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in %s", method)
        return f"Unexpected error calling {method}: {err}"


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_blockchain_info() -> str:
    """Return the node's chain state.

    Includes the chain name, block and header counts, verification
    progress, difficulty and size on disk, and -- when the node is pruned
    -- `pruned` and `pruneheight`, the lowest height still on disk.
    """
    return _call("getblockchaininfo")


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
    2 each entry is the full transaction, which is how a txid is located
    on a node with txindex disabled. A block below the node's prune height
    has been discarded and cannot be returned.

    Args:
        blockhash: Block hash, as returned by get_block_hash.
        verbosity: 1 for a summary (default), 2 for full transactions.
    """
    try:
        return _render(rpc.call("getblock", [blockhash, verbosity]))
    except RPCError as err:
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
def get_raw_transaction(txid: str, blockhash: str = "", verbose: bool = True) -> str:
    """Return a transaction by id.

    This node runs with txindex disabled, so a confirmed transaction can
    only be fetched when its `blockhash` is supplied; without one only a
    transaction still in the mempool resolves. Use get_block_hash and
    get_block(..., verbosity=2) to find the block first.

    Args:
        txid: Transaction id (hex).
        blockhash: The block the transaction is in. Required for a
            confirmed transaction on this node.
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
            logger.info("getrawtransaction %s has no blockhash and no txindex", txid)
            return (
                "No such transaction in the mempool. This node has txindex "
                "disabled, so a confirmed transaction can only be fetched by "
                "supplying its `blockhash`. Use get_block_hash(height) then "
                "get_block(hash, verbosity=2) to locate the block, then call "
                "get_raw_transaction again with that blockhash."
            )
        return _error_text(err)
    except Exception as err:  # noqa: BLE001 - surfaced as text, never hidden
        logger.exception("unexpected error in getrawtransaction")
        return f"Unexpected error calling getrawtransaction: {err}"


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
    index.

    Args:
        txid: Transaction id (hex).
        n: Output index (vout), 0-based.
        include_mempool: Whether to include the mempool (default True).
    """
    return _call("gettxout", [txid, n, include_mempool])


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
    return _call("estimatesmartfee", [conf_target, mode])


@mcp.tool(structured_output=False, annotations=_NODE_TOOLS)
def get_difficulty() -> str:
    """Return the proof-of-work difficulty as a multiple of the minimum."""
    return _call("getdifficulty")


class BearerAuthMiddleware:
    """Pure-ASGI bearer auth in front of the streamable-HTTP app.

    The endpoint is published on the LAN, so a missing token is a hard
    error rather than a warning: serving every tool unauthenticated for
    as long as the env var is absent is not a failure mode worth keeping.
    Set MCP_ALLOW_ANONYMOUS=1 to run it open on purpose, which is what
    debugging from inside npm-network wants.

    GET /health stays unauthenticated so a poller can say "up" without
    holding the token; it says nothing else.
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
    )
