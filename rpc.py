"""Minimal JSON-RPC client for Bitcoin Core's HTTP RPC.

Stdlib-only on purpose. This server does nothing but POST a JSON envelope
to one daemon that lives on the same podman network, so it needs neither a
redirect-following client nor a TLS stack, and the smaller dependency
closure is less to audit for a server that shares that network with the
node.

The client raises a single exception type for every failure -- a JSON-RPC
error object, an HTTP status, or a transport error -- so a tool body has
one thing to catch. `RPCError.code` carries Core's numeric code when the
daemon supplied one (-5 "no such transaction", -1 "block not available
(pruned data)"), which is what lets a tool turn a known condition into a
message that tells the model what to do instead.
"""

import base64
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 30.0


class RPCError(Exception):
    """A failed RPC call.

    `message` is Core's human-readable text; `code` is Core's numeric error
    code, or None for a transport failure (which has no daemon-side code).
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _error_code(error: dict[str, Any]) -> int | None:
    """Core's numeric code, when it is actually an int."""
    code = error.get("code")
    return code if isinstance(code, int) else None


class BitcoinRPC:
    """A JSON-RPC client bound to one bitcoind endpoint."""

    def __init__(
        self,
        url: str,
        user: str = "",
        password: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._url = url
        self._timeout = timeout
        # Basic auth is only attached when credentials are configured, so
        # an unauthenticated cookie-free node still works.
        if user or password:
            raw = f"{user}:{password}".encode()
            self._auth = "Basic " + base64.b64encode(raw).decode()
        else:
            self._auth = ""
        self._id = 0

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Invoke `method`, returning `result` or raising `RPCError`."""
        self._id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self._auth:
            headers["Authorization"] = self._auth
        request = urllib.request.Request(
            self._url, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as err:
            # Core answers application errors with a JSON body even when the
            # status is non-2xx, so read it before falling back to the code.
            parsed = _decode(err.read())
            if parsed is not None and parsed.get("error"):
                raise RPCError(
                    str(parsed["error"].get("message", "rpc error")),
                    _error_code(parsed["error"]),
                ) from err
            if err.code == 401:
                raise RPCError(
                    "authentication to the node failed "
                    "(check BITCOIN_RPC_USER / BITCOIN_RPC_PASSWORD)"
                ) from err
            raise RPCError(f"HTTP {err.code} from the node") from err
        except urllib.error.URLError as err:
            raise RPCError(f"cannot reach the node at {self._url}: {err.reason}") from err

        if body.get("error"):
            raise RPCError(
                str(body["error"].get("message", "rpc error")),
                _error_code(body["error"]),
            )
        return body.get("result")


def _decode(raw: bytes) -> dict[str, Any] | None:
    """Parse a JSON object from a response body, or None if it is not one."""
    try:
        parsed = json.loads(raw.decode(errors="replace"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
