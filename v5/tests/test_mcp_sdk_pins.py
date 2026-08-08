"""Pins for the mcp SDK internals that carry caller identity to the tools.

In ``AUTH_MODE=oidc`` the ASGI layer validates the bearer token once and
writes the resulting principal into the ASGI ``scope``; ``call_tool`` reads
it back out. That works because the Streamable HTTP transport wraps *the
same* scope dict in a Starlette ``Request`` and hands it to the low-level
server as request metadata:

    handle_request(scope, ...) -> Request(scope, receive)
      -> ServerMessageMetadata(request_context=request)
        -> RequestContext(..., request=request)
          -> Server.request_context.request.scope

None of those hops is a documented public API — ``ServerMessageMetadata``
is SDK-internal and the unpacking branch is marked ``# pragma: no cover``.
If an SDK upgrade breaks the chain, identity silently stops arriving and
every caller looks anonymous. Fail-closed handling in the dispatcher turns
that into refusals rather than a mailbox leak, but these pins are what make
the breakage *visible*, and at upgrade time rather than in production.

Verified live against mcp 1.29.0.
"""

import json

import anyio
import pytest
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.context import RequestContext
from mcp.shared.message import ServerMessageMetadata
from mcp.types import TextContent, Tool

SENTINEL = "principal-sentinel"
SCOPE_KEY = "ewsmcp.principal"


# --------------------------------------------------------------- static pins

def test_server_message_metadata_carries_request_context():
    """The slot the transport puts the Request into."""
    assert "request_context" in ServerMessageMetadata.__dataclass_fields__


def test_request_context_exposes_request():
    """The slot the low-level server unpacks it back out of."""
    fields = getattr(RequestContext, "__dataclass_fields__", None)
    names = fields.keys() if fields else RequestContext.model_fields.keys()
    assert "request" in names


def test_server_exposes_request_context_property():
    """What a handler calls to reach the current request."""
    assert isinstance(getattr(Server, "request_context", None), property)


# ------------------------------------------------------- end-to-end identity

def _build_server(seen: dict) -> Server:
    server = Server("pin-probe")

    @server.list_tools()
    async def _list_tools():
        return [Tool(name="echo", description="pin probe",
                     inputSchema={"type": "object"})]

    @server.call_tool()
    async def _call_tool(name, arguments):
        request = getattr(server.request_context, "request", None)
        seen["request"] = request
        seen["principal"] = request.scope.get(SCOPE_KEY) if request is not None else None
        return [TextContent(type="text", text="ok")]

    return server


async def _drive_tools_call(json_response: bool) -> dict:
    """POST a tools/call through the real transport, with the principal
    injected into the scope exactly as ``http.build_app`` will inject it."""
    seen: dict = {}
    manager = StreamableHTTPSessionManager(
        app=_build_server(seen), json_response=json_response, stateless=True
    )
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "echo", "arguments": {}},
    }).encode()
    scope = {
        "type": "http", "method": "POST", "path": "/mcp",
        "headers": [
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
        ],
        SCOPE_KEY: SENTINEL,
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async with manager.run():
        with anyio.move_on_after(10):
            await manager.handle_request(scope, receive, send)
    seen["status"] = next(
        (m["status"] for m in sent if m["type"] == "http.response.start"), None
    )
    return seen


@pytest.mark.parametrize("json_response", [False, True], ids=["sse", "json"])
def test_scope_injection_reaches_call_tool(json_response):
    """THE pin: what the ASGI layer writes into the scope is what the tool
    layer reads. ``json_response=False`` is the production configuration
    (http.serve_http); the JSON variant is pinned too so a change to either
    code path is caught."""
    seen = anyio.run(_drive_tools_call, json_response)
    assert seen["status"] == 200
    assert seen["request"] is not None, (
        "request_context.request was None — the SDK stopped attaching the "
        "ASGI request, so no caller identity can reach the tools"
    )
    assert seen["principal"] == SENTINEL
