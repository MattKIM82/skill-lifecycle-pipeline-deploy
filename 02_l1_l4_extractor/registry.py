"""Tool-agnostic adapter and oracle registries."""

from __future__ import annotations

from typing import Any, Callable

from .schema import OracleVerdict, ToolAdapter


OracleFn = Callable[[dict[str, Any], dict[str, Any]], OracleVerdict]


class AdapterRegistry:
    """Registry mapping approved adapter_id strings to ToolAdapter implementations."""

    def __init__(self) -> None:
        self._adapters: dict[str, ToolAdapter] = {}

    def register(self, adapter: ToolAdapter) -> None:
        self._adapters[adapter.adapter_id] = adapter

    def has(self, adapter_id: str) -> bool:
        return adapter_id in self._adapters

    def resolve(self, adapter_id: str) -> ToolAdapter:
        if adapter_id not in self._adapters:
            raise KeyError(f"unregistered adapter_id: {adapter_id}")
        return self._adapters[adapter_id]

    def list_ids(self) -> list[str]:
        return sorted(self._adapters)


class OracleRegistry:
    """Registry mapping oracle_id strings to deterministic evaluation callables."""

    def __init__(self) -> None:
        self._oracles: dict[str, OracleFn] = {}

    def register(self, oracle_id: str, fn: OracleFn) -> None:
        self._oracles[oracle_id] = fn

    def has(self, oracle_id: str) -> bool:
        return oracle_id in self._oracles

    def evaluate(
        self,
        oracle_ids: list[str],
        evidence: dict[str, Any],
        case: dict[str, Any],
    ) -> list[OracleVerdict]:
        verdicts: list[OracleVerdict] = []
        for oid in oracle_ids:
            if oid not in self._oracles:
                verdicts.append(
                    OracleVerdict(
                        oracle_id=oid,
                        passed=False,
                        reason=f"unregistered oracle_id: {oid}",
                    )
                )
            else:
                verdicts.append(self._oracles[oid](evidence, case))
        return verdicts

    def list_ids(self) -> list[str]:
        return sorted(self._oracles)


def default_registries() -> tuple[AdapterRegistry, OracleRegistry]:
    """Build registries with generic oracles, auto-discovered tool modules, and MCP contracts."""
    import importlib
    import json
    from pathlib import Path

    from .base_adapter import McpStdioAdapter
    from .base_oracles import register_generic_oracles

    adapters = AdapterRegistry()
    oracles = OracleRegistry()
    register_generic_oracles(oracles)

    from .. import WORKSPACE_ROOT

    search_roots = [WORKSPACE_ROOT / "contracts", Path(__file__).resolve().parent]
    skip_dirs = {"templates", "prompts", "examples", "__pycache__"}
    seen_pkgs: set[str] = set()
    for contracts_root in search_roots:
        if not contracts_root.is_dir():
            continue
        for tool_dir in sorted(contracts_root.iterdir()):
            if not tool_dir.is_dir() or tool_dir.name.startswith("_") or tool_dir.name in skip_dirs:
                continue
            pkg = tool_dir.name
            if pkg in seen_pkgs:
                continue
            seen_pkgs.add(pkg)

            for mod_path in sorted(tool_dir.glob("*_adapter.py")):
                mod = importlib.import_module(f"skill_factory.contracts.{pkg}.{mod_path.stem}")
                for fn_name in ("register_adapters", f"register_{pkg}_adapters"):
                    fn = getattr(mod, fn_name, None)
                    if callable(fn):
                        fn(adapters)

            for mod_path in sorted(tool_dir.glob("*_oracles.py")):
                mod = importlib.import_module(f"skill_factory.contracts.{pkg}.{mod_path.stem}")
                for fn_name in ("register_oracles", f"register_{pkg}_oracles"):
                    fn = getattr(mod, fn_name, None)
                    if callable(fn):
                        fn(oracles)

            # Auto-register standard MCP stdio adapters declared in contract.json entrypoints
            cpath = tool_dir / "contract.json"
            if cpath.is_file():
                try:
                    cdata = json.loads(cpath.read_text(encoding="utf-8"))
                    default_aid = cdata.get("default_adapter_id", f"{pkg}_mcp")
                    for ep in cdata.get("entrypoints", []):
                        if ep.get("kind") == "mcp-stdio" and ep.get("command") and not adapters.has(default_aid):
                            adapters.register(
                                McpStdioAdapter(
                                    adapter_id=default_aid,
                                    command=str(ep["command"]),
                                    args=list(ep.get("args", [])),
                                    env=dict(ep.get("env", {})),
                                )
                            )
                except (OSError, json.JSONDecodeError):
                    pass

    return adapters, oracles
