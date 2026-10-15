"""What the server advertises in `tools/list`.

The schema is the contract a client validates against, so the parts that
protect the caller -- bounds on every numeric argument, an enum where the
set of valid values is closed -- are asserted here rather than left to the
model's good judgement.

Every tool here is text-only and returns `str`, so every tool must pass
`structured_output=False`: a tool returning `str` that does not would
publish an output schema and send its payload twice, once as text and once
as structured content.
"""

from __future__ import annotations

import anyio
import pytest
from mcp import Client

import server

TOOLS = {
    "get_blockchain_info",
    "get_block_hash",
    "get_block",
    "get_block_header",
    "get_raw_transaction",
    "decode_raw_transaction",
    "get_mempool_info",
    "get_raw_mempool",
    "get_tx_out",
    "get_network_info",
    "get_peer_info",
    "estimate_smart_fee",
    "get_difficulty",
}


@pytest.fixture
def advertised() -> dict:
    """The tools/list result, fetched once per test through a real client."""

    async def _list():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    listing = anyio.run(_list)
    return {tool.name: tool for tool in listing.tools}


def test_every_tool_is_advertised(advertised: dict) -> None:
    assert set(advertised) == TOOLS


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_text_tools_publish_no_output_schema(advertised: dict, name: str) -> None:
    assert advertised[name].output_schema is None, (
        f"{name} publishes an output schema, which means its str return is "
        "serialised twice; pass structured_output=False to @mcp.tool()"
    )


@pytest.mark.parametrize(
    "name,field,minimum,maximum",
    [
        ("get_block_hash", "height", 0, None),
        ("get_tx_out", "n", 0, None),
        ("estimate_smart_fee", "conf_target", 1, 1008),
    ],
)
def test_numeric_arguments_are_bounded(
    advertised: dict, name: str, field: str, minimum: int, maximum: int | None
) -> None:
    schema = advertised[name].input_schema["properties"][field]
    assert schema.get("minimum") == minimum, f"{name}.{field} has no lower bound"
    if maximum is not None:
        assert schema.get("maximum") == maximum, f"{name}.{field} has no upper bound"


def test_block_verbosity_is_an_enum(advertised: dict) -> None:
    schema = advertised["get_block"].input_schema["properties"]["verbosity"]
    assert set(schema["enum"]) == {1, 2}


def test_fee_mode_is_an_enum(advertised: dict) -> None:
    schema = advertised["estimate_smart_fee"].input_schema["properties"]["mode"]
    assert set(schema["enum"]) == {"conservative", "economical", "unset"}


def test_every_tool_declares_itself_read_only(advertised: dict) -> None:
    for name, tool in advertised.items():
        assert tool.annotations is not None, f"{name} publishes no annotations"
        assert tool.annotations.read_only_hint is True, f"{name} is not marked read-only"
        assert tool.annotations.open_world_hint is True, f"{name} is not marked open-world"
