"""External ToolAdapter implementations for OSAW (core, MCP stdio, and HTTP Web UI)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from osaw._rrea_runtime import Cancelled, Context, ToolError, invoke as osaw_core_invoke
from osaw.engine import ui_link

from ..registry import AdapterRegistry
from ..schema import RunContext


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MCP_WRAPPER = PROJECT_ROOT / "scripts" / "run_osaw_mcp.sh"
PYTHON_BIN = Path(sys.executable)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_json(url: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"error": body}
        return exc.code, parsed


class OsawCoreAdapter:
    """In-process isolated ToolAdapter calling OSAW's verified dispatcher against a sandbox workspace."""

    adapter_id = "osaw_core"

    def execute(
        self,
        capability_id: str,
        validated_input: dict[str, Any],
        run_context: RunContext,
    ) -> dict[str, Any]:
        run_context.workspace_dir.mkdir(parents=True, exist_ok=True)
        ctx = Context(root=run_context.workspace_dir, cancel_after=run_context.cancel_after)
        t0 = time.perf_counter()
        try:
            out = osaw_core_invoke(capability_id, validated_input, ctx, channel="skill_factory_core")
            return {
                "ok": True,
                "capability_id": capability_id,
                "channel": self.adapter_id,
                "workspace_dir": str(run_context.workspace_dir),
                "output": out,
                "error": None,
                "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
                "invocation_id": ctx.invocation_id,
            }
        except Cancelled as exc:
            return {
                "ok": False,
                "cancelled": True,
                "capability_id": capability_id,
                "channel": self.adapter_id,
                "workspace_dir": str(run_context.workspace_dir),
                "output": None,
                "error": str(exc),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            }
        except (ToolError, Exception) as exc:  # noqa: BLE001
            return {
                "ok": False,
                "cancelled": False,
                "capability_id": capability_id,
                "channel": self.adapter_id,
                "workspace_dir": str(run_context.workspace_dir),
                "output": None,
                "error": str(exc),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            }

    def status(self, handle: dict[str, Any]) -> dict[str, Any]:
        return {"supported": False, "status": "unsupported", "reason": "synchronous core call"}

    def cancel(self, handle: dict[str, Any]) -> dict[str, Any]:
        return {"supported": False, "cancelled": False, "reason": "synchronous core call completed"}


class OsawMcpAdapter:
    """Real MCP stdio ToolAdapter spawning OSAW MCP server against an isolated workspace."""

    adapter_id = "osaw_mcp"

    def execute(
        self,
        capability_id: str,
        validated_input: dict[str, Any],
        run_context: RunContext,
    ) -> dict[str, Any]:
        return asyncio.run(self.execute_batch([(capability_id, validated_input)], run_context))[0]

    async def execute_batch(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        run_context: RunContext,
    ) -> list[dict[str, Any]]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        run_context.workspace_dir.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "OSAW_WORKSPACE_DIR": str(run_context.workspace_dir),
            **run_context.env,
        }
        cmd = str(MCP_WRAPPER) if MCP_WRAPPER.exists() else str(PYTHON_BIN)
        args = [] if MCP_WRAPPER.exists() else ["-m", "osaw.server"]
        params = StdioServerParameters(command=cmd, args=args, env=env)

        results: list[dict[str, Any]] = []
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                for capability_id, validated_input in calls:
                    t0 = time.perf_counter()
                    res = await session.call_tool(capability_id, validated_input)
                    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
                    text = res.content[0].text if res.content else ""
                    if res.isError:
                        results.append(
                            {
                                "ok": False,
                                "capability_id": capability_id,
                                "channel": self.adapter_id,
                                "workspace_dir": str(run_context.workspace_dir),
                                "output": None,
                                "error": text,
                                "duration_ms": duration_ms,
                            }
                        )
                    else:
                        try:
                            parsed = json.loads(text)
                        except json.JSONDecodeError:
                            parsed = {"raw": text}
                        results.append(
                            {
                                "ok": True,
                                "capability_id": capability_id,
                                "channel": self.adapter_id,
                                "workspace_dir": str(run_context.workspace_dir),
                                "output": parsed,
                                "error": None,
                                "duration_ms": duration_ms,
                            }
                        )
        return results

    def status(self, handle: dict[str, Any]) -> dict[str, Any]:
        return {"supported": False, "status": "unsupported"}

    def cancel(self, handle: dict[str, Any]) -> dict[str, Any]:
        return {"supported": False, "cancelled": False}


class OsawUiAdapter:
    """ToolAdapter interacting with OSAW's Web UI launcher (`osaw.ui_launch`) & HTTP `/api/invoke`."""

    adapter_id = "osaw_ui"

    def ensure_ui_running(self, workspace_dir: Path, port: int | None = None) -> dict[str, Any]:
        workspace_dir.mkdir(parents=True, exist_ok=True)
        rec, _ = ui_link.live(workspace_dir)
        if rec is not None:
            return {"status": "running", "pid": rec["pid"], "port": rec["port"], "url": rec["url"], "owned": False}

        chosen_port = port or _find_free_port()
        proc = subprocess.Popen(
            [str(PYTHON_BIN), "-m", "osaw.ui_launch", "--root", str(workspace_dir), "--port", str(chosen_port)],
            cwd=str(workspace_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.time() + 10.0
        while time.time() < deadline:
            rec, _ = ui_link.live(workspace_dir)
            if rec is not None:
                return {
                    "status": "running",
                    "pid": rec["pid"],
                    "port": rec["port"],
                    "url": rec["url"],
                    "owned": True,
                    "_proc": proc,
                }
            if proc.poll() is not None:
                err = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(f"osaw.ui_launch exited early ({proc.returncode}): {err}")
            time.sleep(0.15)
        proc.terminate()
        raise TimeoutError("Timed out waiting for osaw.ui_launch to become ready")

    def execute(
        self,
        capability_id: str,
        validated_input: dict[str, Any],
        run_context: RunContext,
    ) -> dict[str, Any]:
        # ui_open and ui_show execute through core/MCP contract; HTTP UI bindings also allow invoking tools via /api/invoke.
        if capability_id in ("ui_open", "ui_show") and not run_context.metadata.get("via_http"):
            return OsawCoreAdapter().execute(capability_id, validated_input, run_context)

        t0 = time.perf_counter()
        handle = None
        try:
            handle = self.ensure_ui_running(run_context.workspace_dir)
            base_url = handle["url"].rstrip("/")
            status_code, body = _http_json(
                f"{base_url}/api/invoke/{capability_id}",
                payload={"input": validated_input},
                timeout=run_context.timeout_s,
            )
            ok = status_code == 200 and bool(body.get("ok", True)) and "error" not in body
            output = body.get("result", body.get("output", body)) if ok else None
            return {
                "ok": ok,
                "capability_id": capability_id,
                "channel": self.adapter_id,
                "workspace_dir": str(run_context.workspace_dir),
                "ui_url": handle["url"],
                "output": output,
                "error": None if ok else body.get("error", f"HTTP {status_code}"),
                "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
            }
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
        finally:
            if handle and handle.get("owned") and run_context.metadata.get("stop_after", False):
                self.cancel(handle)

    def fetch_bindings(self, workspace_dir: Path) -> dict[str, Any]:
        handle = self.ensure_ui_running(workspace_dir)
        status_code, body = _http_json(f"{handle['url'].rstrip('/')}/api/bindings")
        return {"status_code": status_code, "url": handle["url"], "bindings": body, "handle": handle}

    def status(self, handle: dict[str, Any]) -> dict[str, Any]:
        ws = Path(handle.get("workspace_dir", "."))
        rec, reason = ui_link.live(ws)
        return {
            "supported": True,
            "status": "running" if rec else "stopped",
            "reason": reason,
            "record": rec,
        }

    def cancel(self, handle: dict[str, Any]) -> dict[str, Any]:
        proc = handle.get("_proc")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"supported": True, "cancelled": True}
        pid = handle.get("pid")
        if isinstance(pid, int):
            try:
                os.kill(pid, 15)
                return {"supported": True, "cancelled": True}
            except OSError as exc:
                return {"supported": True, "cancelled": False, "reason": str(exc)}
        return {"supported": False, "cancelled": False, "reason": "no active UI handle"}


def register_osaw_adapters(registry: AdapterRegistry) -> None:
    registry.register(OsawCoreAdapter())
    registry.register(OsawMcpAdapter())
    registry.register(OsawUiAdapter())
