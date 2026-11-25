"""Tool-agnostic MCP stdio ToolAdapter for standard MCP servers."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from .schema import RunContext


class McpStdioAdapter:
    """Generic ToolAdapter executing capabilities against any stdio MCP server."""

    def __init__(
        self,
        *,
        adapter_id: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})

    def execute(
        self,
        capability_id: str,
        validated_input: dict[str, Any],
        run_context: RunContext,
    ) -> dict[str, Any]:
        return asyncio.run(self._call_once(capability_id, validated_input, run_context))

    async def _call_once(
        self,
        capability_id: str,
        validated_input: dict[str, Any],
        run_context: RunContext,
    ) -> dict[str, Any]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        run_context.workspace_dir.mkdir(parents=True, exist_ok=True)
        merged_env = {**os.environ, **self.env, **run_context.env}
        params = StdioServerParameters(command=self.command, args=self.args, env=merged_env)
        t0 = time.perf_counter()
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    res = await session.call_tool(capability_id, validated_input)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "capability_id": capability_id,
                "channel": self.adapter_id,
                "workspace_dir": str(run_context.workspace_dir),
                "output": None,
                "error": str(exc),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        text = res.content[0].text if res.content else ""
        if res.isError:
            return {
                "ok": False,
                "capability_id": capability_id,
                "channel": self.adapter_id,
                "workspace_dir": str(run_context.workspace_dir),
                "output": None,
                "error": text,
                "duration_ms": duration_ms,
            }
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"raw_text": text}
        return {
            "ok": True,
            "capability_id": capability_id,
            "channel": self.adapter_id,
            "workspace_dir": str(run_context.workspace_dir),
            "output": parsed,
            "error": None,
            "duration_ms": duration_ms,
        }

    def status(self, handle: dict[str, Any]) -> dict[str, Any]:
        return {"supported": False, "status": "unsupported", "reason": "synchronous MCP stdio call"}

    def cancel(self, handle: dict[str, Any]) -> dict[str, Any]:
        return {"supported": False, "cancelled": False, "reason": "synchronous MCP stdio call completed"}
