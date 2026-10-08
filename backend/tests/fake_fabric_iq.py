"""An in-process MCP server with Fabric IQ's tool names and parameters, built with the same MCP
SDK, so the real client plumbing (Streamable HTTP, initialize, tools/list, tools/call, headers)
is exercised without a Power BI tenant."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import EmbeddedResource, TextContent, TextResourceContents
from starlette.types import Receive, Scope, Send

ROWS = [
    {"Product[LOB]": "GOLD", "[Net Sales]": 1200.5},
    {"Product[LOB]": "SILVER", "[Net Sales]": 800},
]


@dataclass
class FakeFabricIq:
    """Behaviour knobs for tests."""

    execute_mode: str = "json"  # json | csv | error:<message>
    endpoint_status: int | None = None  # e.g. 401 -> the whole endpoint refuses the caller
    omit_tools: set[str] = field(default_factory=set)
    # A query containing a key fails with that key's message (e.g. an unknown measure).
    dax_errors: dict[str, str] = field(default_factory=dict)
    # Real Fabric IQ (spike S3) reports a failing query as a normal text result, not a tool error.
    dax_errors_as_text: bool = False
    requests: list[dict[str, str]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def build_app(self) -> tuple[MCPServer, Callable[[Scope, Receive, Send], Any]]:
        server = MCPServer("fake-fabric-iq")

        if "ExecuteQuery" not in self.omit_tools:

            @server.tool(name="ExecuteQuery")
            def execute_query(
                artifactId: str,
                daxQueries: list[str],
                maxRows: int = 250,
            ) -> Any:
                self.calls.append(
                    (
                        "ExecuteQuery",
                        {"artifactId": artifactId, "daxQueries": daxQueries, "maxRows": maxRows},
                    )
                )
                for marker, message in self.dax_errors.items():
                    if any(marker in query for query in daxQueries):
                        if self.dax_errors_as_text:
                            return message
                        raise ToolError(message)
                if self.execute_mode.startswith("error:"):
                    raise ToolError(self.execute_mode.removeprefix("error:"))
                if self.execute_mode == "csv":
                    csv_text = "Product[LOB],[Net Sales]\nGOLD,1200.5\nSILVER,800\n"
                    return [
                        TextContent(type="text", text="2 rows (full result attached as CSV)"),
                        EmbeddedResource(
                            type="resource",
                            resource=TextResourceContents(
                                uri="file:///result.csv", mime_type="text/csv", text=csv_text
                            ),
                        ),
                    ]
                return json.dumps({"results": [{"tables": [{"rows": ROWS[:maxRows]}]}]})

        @server.tool(name="GetSemanticModelSchema")
        def get_schema(artifactId: str) -> str:
            self.calls.append(("GetSemanticModelSchema", {"artifactId": artifactId}))
            return json.dumps({"tables": [{"name": "Product", "columns": ["LOB"]}]})

        @server.tool(name="ValueSearch")
        def value_search(artifactId: str, searchTerms: list[str]) -> str:
            self.calls.append(
                ("ValueSearch", {"artifactId": artifactId, "searchTerms": searchTerms})
            )
            return json.dumps([{"column": "Product[LOB]", "value": "GOLD"}])

        app = server.streamable_http_app(
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

        async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                self.requests.append({k.decode(): v.decode() for k, v in scope["headers"]})
                if self.endpoint_status is not None:
                    await send(
                        {
                            "type": "http.response.start",
                            "status": self.endpoint_status,
                            "headers": [],
                        }
                    )
                    await send({"type": "http.response.body", "body": b"refused"})
                    return
            await app(scope, receive, send)

        return server, asgi


@asynccontextmanager
async def running(
    fake: FakeFabricIq,
) -> AsyncIterator[Callable[[dict[str, str], float], httpx2.AsyncClient]]:
    """Starts the fake server; yields an http-client factory for FabricIqMcpGateway."""
    server, asgi = fake.build_app()

    def factory(headers: dict[str, str], timeout: float) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=asgi),
            base_url="http://fabric-iq.test",
            headers=headers,
            timeout=timeout,
        )

    async with server.session_manager.run():
        yield factory
