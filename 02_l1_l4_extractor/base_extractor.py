"""Tool-agnostic L1-L4 contract extraction helpers, generic MCP introspector, and AI extractor."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .schema import CapabilitySpec, Contract, ExecutionControl, validate_contract_meta_schema


def derive_schema_constraints(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract generic L2 constraints directly from any JSON Schema without tool-specific code."""
    constraints: list[dict[str, Any]] = []
    if input_schema.get("additionalProperties") is False:
        allowed = sorted(input_schema.get("properties", {}).keys())
        constraints.append(
            {
                "id": "C-SCHEMA-NO-EXTRA-PROPS",
                "rule": f"Reject unknown top-level keys; only {allowed} are permitted.",
            }
        )

    req = input_schema.get("required", [])
    if req:
        constraints.append(
            {
                "id": "C-SCHEMA-REQUIRED",
                "rule": f"Required parameters must be provided: {req}.",
            }
        )

    for prop_name, prop in input_schema.get("properties", {}).items():
        if not isinstance(prop, dict):
            continue
        if "enum" in prop:
            constraints.append(
                {
                    "id": f"C-ENUM-{prop_name.upper()}",
                    "rule": f"Parameter '{prop_name}' must be one of {prop['enum']}.",
                }
            )
        bounds = []
        if "minimum" in prop:
            bounds.append(f"min={prop['minimum']}")
        if "maximum" in prop:
            bounds.append(f"max={prop['maximum']}")
        if "pattern" in prop:
            bounds.append(f"pattern={prop['pattern']}")
        if bounds:
            constraints.append(
                {
                    "id": f"C-BOUNDS-{prop_name.upper()}",
                    "rule": f"Parameter '{prop_name}' constraints: {', '.join(bounds)}.",
                }
            )
    return constraints


def build_standard_contract(
    *,
    tool_id: str,
    tool_version: str,
    spec_revision: int | str,
    source_ref: str,
    default_adapter_id: str,
    capabilities: list[CapabilitySpec],
    entrypoints: list[dict[str, Any]] | None = None,
    auth_requirements: dict[str, Any] | None = None,
    environment_checks: list[str] | None = None,
    golden_scenarios: list[dict[str, Any]] | None = None,
    source_docs_dir: str = "",
    error_oracle_id: str = "",
    skill_config: dict[str, Any] | None = None,
    status: str = "verified",
    output_path: Path | None = None,
) -> Contract:
    """Assemble and meta-schema validate a normalized L1-L4 Contract."""
    contract = Contract(
        schema_version="1.0",
        tool_id=tool_id,
        tool_version=tool_version,
        spec_revision=spec_revision,
        source_ref=source_ref,
        status=status,
        default_adapter_id=default_adapter_id,
        entrypoints=entrypoints or [],
        auth_requirements=auth_requirements or {},
        environment_checks=environment_checks or [],
        capabilities=capabilities,
        golden_scenarios=golden_scenarios or [],
        source_docs_dir=source_docs_dir,
        error_oracle_id=error_oracle_id or "generic_error_contract",
        skill_config=skill_config or {},
    )
    errors = validate_contract_meta_schema(contract.to_dict())
    if errors:
        raise ValueError(f"Generated contract for {tool_id!r} violates L1-L4 meta-schema: {errors}")

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(contract.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return contract


def extract_from_mcp_stdio(
    *,
    tool_id: str,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    adapter_id: str | None = None,
    tool_version: str = "0.1.0",
    output_path: Path | None = None,
) -> Contract:
    """Generic L1/L2 extractor for ANY stdio MCP server via `list_tools()` introspection."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _introspect() -> list[Any]:
        params = StdioServerParameters(command=command, args=args or [], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                listed = await session.list_tools()
                return list(listed.tools)

    raw_tools = asyncio.run(_introspect())
    chosen_adapter = adapter_id or f"{tool_id}_mcp"
    capabilities: list[CapabilitySpec] = []

    for tool in raw_tools:
        input_schema = dict(getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}})
        if "type" not in input_schema:
            input_schema["type"] = "object"
        ann = getattr(tool, "annotations", None)
        destructive = bool(getattr(ann, "destructiveHint", False)) if ann else False
        idempotent = bool(getattr(ann, "idempotentHint", False)) if ann else False
        read_only = bool(getattr(ann, "readOnlyHint", False)) if ann else False

        capabilities.append(
            CapabilitySpec(
                id=tool.name,
                description=(tool.description or tool.name).strip(),
                input_schema=input_schema,
                output_schema={"type": "object"},  # MCP list_tools rarely exposes outputSchema; refine in L4
                adapter_id=chosen_adapter,
                oracle_ids=["generic_schema_valid", "generic_error_contract"],
                source_ref=f"mcp_stdio:{command}#{tool.name}",
                status="partial",  # Requires L4 oracle registration & verification before Admission!
                shape="inspect" if read_only else ("mutate" if destructive else "workflow"),
                effects=["read-only"] if read_only else ["side-effect-possible"],
                destructive=destructive,
                constraints=derive_schema_constraints(input_schema),
                execution_control=ExecutionControl(
                    timeout_s=60.0,
                    supports_cancel=False,
                    idempotent=idempotent or read_only,
                ),
            )
        )

    return build_standard_contract(
        tool_id=tool_id,
        tool_version=tool_version,
        spec_revision=1,
        source_ref=f"mcp_stdio:{command}",
        default_adapter_id=chosen_adapter,
        capabilities=capabilities,
        entrypoints=[{"name": tool_id, "kind": "mcp-stdio", "command": command, "args": args or []}],
        status="partial",
        output_path=output_path,
    )


def extract_from_docs_via_ai(
    *,
    tool_id: str,
    tool_version: str,
    source_docs: dict[str, str],
    provider: Any,
    adapter_id: str | None = None,
    source_docs_dir: str = "",
    output_path: Path | None = None,
) -> Contract:
    """Use an LLMProvider (e.g., AntigravityCLIProvider) + extract_contract_v1.txt to draft L1-L4 contract."""
    from ..engine.provider import ProviderRequest

    prompt_path = Path(__file__).resolve().parent / "prompts" / "extract_contract_v1.txt"
    sys_prompt = prompt_path.read_text(encoding="utf-8")

    schema = {
        "type": "object",
        "properties": {
            "capabilities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "shape": {"type": "string"},
                        "destructive": {"type": "boolean"},
                        "input_schema": {"type": "object"},
                        "output_schema": {"type": "object"},
                        "constraints": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"id": {"type": "string"}, "rule": {"type": "string"}},
                                "required": ["id", "rule"],
                            },
                        },
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "source_ref": {"type": "string"},
                    },
                    "required": ["id", "description", "input_schema", "output_schema", "source_ref"],
                },
            }
        },
        "required": ["capabilities"],
    }

    resp = provider.complete_json(
        ProviderRequest(
            role="extract_contract",
            prompt_version="v1",
            system_prompt=sys_prompt,
            payload={"tool_id": tool_id, "tool_version": tool_version, "source_docs": source_docs},
            output_schema=schema,
        )
    )
    chosen_adapter = adapter_id or f"{tool_id}_adapter"
    caps = [
        CapabilitySpec(
            id=c["id"],
            description=c.get("description", c["id"]),
            input_schema=c["input_schema"],
            output_schema=c.get("output_schema", {"type": "object"}),
            adapter_id=chosen_adapter,
            oracle_ids=["generic_schema_valid", "generic_error_contract"],
            source_ref=c["source_ref"],
            status="partial",
            shape=c.get("shape", "unknown"),
            destructive=bool(c.get("destructive", False)),
            constraints=c.get("constraints", []) + derive_schema_constraints(c["input_schema"]),
            dependencies=c.get("dependencies", []),
            execution_control=ExecutionControl(),
        )
        for c in resp.content.get("capabilities", [])
    ]
    return build_standard_contract(
        tool_id=tool_id,
        tool_version=tool_version,
        spec_revision=1,
        source_ref=f"ai_extracted:{tool_id}",
        default_adapter_id=chosen_adapter,
        capabilities=caps,
        source_docs_dir=source_docs_dir,
        error_oracle_id="generic_error_contract",
        status="partial",
        output_path=output_path,
    )
