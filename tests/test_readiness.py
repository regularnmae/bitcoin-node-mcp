"""The `/ready` probe, driven directly against a fake node.

The middleware only routes the path; the probe itself is what decides
between 200 and 503, so it is tested on its own -- including that it uses
the short readiness timeout rather than the endpoint's 30s default.
"""

from __future__ import annotations

import pytest

import server
from rpc import RPCError


@pytest.mark.anyio
async def test_ready_reports_the_chain(fake_rpc) -> None:
    fake_rpc.answers["getblockchaininfo"] = {
        "chain": "main",
        "blocks": 974135,
        "initialblockdownload": False,
    }
    body, status = await server._readiness()
    assert status == 200
    assert body == {
        "status": "ready",
        "chain": "main",
        "blocks": 974135,
        "initialblockdownload": False,
    }
    assert fake_rpc.last_timeout == server.READY_TIMEOUT


@pytest.mark.anyio
async def test_ready_is_503_when_the_node_is_unreachable(fake_rpc) -> None:
    fake_rpc.answers["getblockchaininfo"] = RPCError(
        "cannot reach the node at http://bitcoind:8332: connection refused"
    )
    body, status = await server._readiness()
    assert status == 503
    assert body["status"] == "unready"
    assert "cannot reach the node" in body["error"]


@pytest.mark.anyio
async def test_ready_is_503_on_an_unexpected_body(fake_rpc) -> None:
    fake_rpc.answers["getblockchaininfo"] = []
    body, status = await server._readiness()
    assert status == 503
    assert body["status"] == "unready"
