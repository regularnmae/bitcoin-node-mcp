"""Unit tests for the JSON-RPC client.

These drive `BitcoinRPC` directly with a fake `urlopen`, so the whole
transport contract is pinned without a node: a success unwraps `result`, a
JSON-RPC error object raises with its code, an HTTP 401 is named as an auth
failure, a transport failure is named as unreachable, and Basic auth is
attached only when credentials are configured.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error

import pytest

import rpc as rpc_module
from rpc import BitcoinRPC, RPCError

URL = "http://bitcoind:8332"


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _install(monkeypatch, *, body=None, error=None, captured=None, raw=None):
    def fake_urlopen(request, timeout=None):
        if captured is not None:
            captured.append(request)
        if error is not None:
            raise error
        return _Response(raw if raw is not None else json.dumps(body).encode())

    monkeypatch.setattr(rpc_module.urllib.request, "urlopen", fake_urlopen)


def test_success_returns_result(monkeypatch) -> None:
    _install(monkeypatch, body={"result": {"blocks": 5}, "error": None})
    assert BitcoinRPC(URL).call("getblockcount") == {"blocks": 5}


def test_embedded_error_raises_with_code(monkeypatch) -> None:
    _install(
        monkeypatch,
        body={"result": None, "error": {"code": -5, "message": "no such tx"}},
    )
    with pytest.raises(RPCError) as excinfo:
        BitcoinRPC(URL).call("getrawtransaction", ["ab"])
    assert excinfo.value.code == -5
    assert excinfo.value.message == "no such tx"


def test_http_error_with_json_body_keeps_the_code(monkeypatch) -> None:
    err = urllib.error.HTTPError(
        URL,
        500,
        "Internal Server Error",
        email.message.Message(),
        io.BytesIO(
            json.dumps({"result": None, "error": {"code": -1, "message": "pruned"}}).encode()
        ),
    )
    _install(monkeypatch, error=err)
    with pytest.raises(RPCError) as excinfo:
        BitcoinRPC(URL).call("getblock", ["ab"])
    assert excinfo.value.code == -1
    assert excinfo.value.message == "pruned"


def test_http_401_is_named_as_auth_failure(monkeypatch) -> None:
    err = urllib.error.HTTPError(URL, 401, "Unauthorized", email.message.Message(), io.BytesIO(b""))
    _install(monkeypatch, error=err)
    with pytest.raises(RPCError) as excinfo:
        BitcoinRPC(URL, "user", "pass").call("getblockcount")
    assert excinfo.value.code is None
    assert "authentication" in excinfo.value.message


def test_unreachable_node_is_named(monkeypatch) -> None:
    _install(monkeypatch, error=urllib.error.URLError("connection refused"))
    with pytest.raises(RPCError) as excinfo:
        BitcoinRPC(URL).call("getblockcount")
    assert "cannot reach the node" in excinfo.value.message


def test_read_timeout_is_named(monkeypatch) -> None:
    _install(monkeypatch, error=TimeoutError("timed out"))
    with pytest.raises(RPCError) as excinfo:
        BitcoinRPC(URL, timeout=2.5).call("getblockcount")
    assert excinfo.value.code is None
    assert "did not answer within 2.5s" in excinfo.value.message


def test_non_json_body_is_named(monkeypatch) -> None:
    _install(monkeypatch, raw=b"<html>502 Bad Gateway</html>")
    with pytest.raises(RPCError) as excinfo:
        BitcoinRPC(URL).call("getblockcount")
    assert "non-JSON body" in excinfo.value.message


def test_request_ids_increment(monkeypatch) -> None:
    captured: list = []
    _install(monkeypatch, body={"result": 1, "error": None}, captured=captured)
    client = BitcoinRPC(URL)
    for _ in range(3):
        client.call("getblockcount")
    ids = [json.loads(req.data)["id"] for req in captured]
    assert ids == [1, 2, 3]


def test_basic_auth_attached_only_with_credentials(monkeypatch) -> None:
    captured: list = []
    _install(monkeypatch, body={"result": 1, "error": None}, captured=captured)

    BitcoinRPC(URL).call("getblockcount")
    assert captured[-1].get_header("Authorization") is None

    BitcoinRPC(URL, "user", "pass").call("getblockcount")
    assert captured[-1].get_header("Authorization") == "Basic dXNlcjpwYXNz"
