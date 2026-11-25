"""Representative ToolAdapter template for onboarding any CLI, API, or library tool.

Drop your copy at: skill_factory/contracts/<tool_id>/<tool_id>_adapter.py
It will be auto-discovered by skill_factory.contracts.registry.default_registries().
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

from ..registry import AdapterRegistry
from ..schema import RunContext


class MyToolAdapter:
    """Example subprocess/library ToolAdapter implementing L1-L3 boundaries."""

    adapter_id = "my_tool_cli"

    def execute(
        self,
        capability_id: str,
        validated_input: dict[str, Any],
        run_context: RunContext,
    ) -> dict[str, Any]:
        """Run one capability inside run_context.workspace_dir and return a normalized evidence dict."""
        run_context.workspace_dir.mkdir(parents=True, exist_ok=True)
        t0 = time.perf_counter()

        # Map capability_id + validated_input to your target CLI / Python API / REST endpoint.
        cmd = ["my-tool-cli", capability_id, "--json", json.dumps(validated_input)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=run_context.workspace_dir,
                capture_output=True,
                text=True,
                timeout=run_context.timeout_s,
            )
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
        if proc.returncode != 0:
            return {
                "ok": False,
                "capability_id": capability_id,
                "channel": self.adapter_id,
                "workspace_dir": str(run_context.workspace_dir),
                "output": None,
                "error": (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip(),
                "duration_ms": duration_ms,
            }

        try:
            parsed = json.loads(proc.stdout) if proc.stdout.strip() else {}
        except json.JSONDecodeError:
            parsed = {"raw_stdout": proc.stdout}

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
        return {"supported": False, "status": "unsupported"}

    def cancel(self, handle: dict[str, Any]) -> dict[str, Any]:
        return {"supported": False, "cancelled": False}


def register_adapters(registry: AdapterRegistry) -> None:
    """Required hook name for automatic discovery by default_registries()."""
    registry.register(MyToolAdapter())
