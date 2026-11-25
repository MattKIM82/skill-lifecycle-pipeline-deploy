"""Standard L1-L4 Meta-Schema definitions and validation (tool-agnostic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


VALID_CONTRACT_STATUSES = {"verified", "partial", "unverified", "blocked"}


@dataclass(frozen=True)
class ExecutionControl:
    """L3 execution control contract for a capability or tool."""

    timeout_s: float = 60.0
    supports_async_job: bool = False
    supports_status: bool = False
    supports_cancel: bool = False
    supports_resume: bool = False
    idempotent: bool = False
    concurrency_limit: int = 1
    retry_policy: str = "never_on_side_effect"

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_s": self.timeout_s,
            "supports_async_job": self.supports_async_job,
            "supports_status": self.supports_status,
            "supports_cancel": self.supports_cancel,
            "supports_resume": self.supports_resume,
            "idempotent": self.idempotent,
            "concurrency_limit": self.concurrency_limit,
            "retry_policy": self.retry_policy,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionControl":
        return cls(
            timeout_s=float(data.get("timeout_s", 60.0)),
            supports_async_job=bool(data.get("supports_async_job", False)),
            supports_status=bool(data.get("supports_status", False)),
            supports_cancel=bool(data.get("supports_cancel", False)),
            supports_resume=bool(data.get("supports_resume", False)),
            idempotent=bool(data.get("idempotent", False)),
            concurrency_limit=int(data.get("concurrency_limit", 1)),
            retry_policy=str(data.get("retry_policy", "never_on_side_effect")),
        )


@dataclass
class CapabilitySpec:
    """Normalized L1-L4 metadata for one capability."""

    id: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    adapter_id: str
    oracle_ids: list[str]
    source_ref: str
    status: str = "verified"
    shape: str = "unknown"
    effects: list[str] = field(default_factory=list)
    destructive: bool = False
    constraints: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    execution_control: ExecutionControl = field(default_factory=ExecutionControl)
    examples: list[dict[str, Any]] = field(default_factory=list)
    failure_examples: list[dict[str, Any]] = field(default_factory=list)
    ui_binding: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "adapter_id": self.adapter_id,
            "oracle_ids": self.oracle_ids,
            "source_ref": self.source_ref,
            "status": self.status,
            "shape": self.shape,
            "effects": self.effects,
            "destructive": self.destructive,
            "constraints": self.constraints,
            "dependencies": self.dependencies,
            "execution_control": self.execution_control.to_dict(),
            "examples": self.examples,
            "failure_examples": self.failure_examples,
            "ui_binding": self.ui_binding,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapabilitySpec":
        return cls(
            id=data["id"],
            description=data.get("description", ""),
            input_schema=data["input_schema"],
            output_schema=data["output_schema"],
            adapter_id=data["adapter_id"],
            oracle_ids=list(data.get("oracle_ids", [])),
            source_ref=data.get("source_ref", ""),
            status=data.get("status", "unverified"),
            shape=data.get("shape", "unknown"),
            effects=list(data.get("effects", [])),
            destructive=bool(data.get("destructive", False)),
            constraints=list(data.get("constraints", [])),
            dependencies=list(data.get("dependencies", [])),
            execution_control=ExecutionControl.from_dict(data.get("execution_control", {})),
            examples=list(data.get("examples", [])),
            failure_examples=list(data.get("failure_examples", [])),
            ui_binding=data.get("ui_binding"),
        )


@dataclass
class Contract:
    """Normalized L1-L4 tool contract following skill_architecture_pipeline.html Section 10."""

    schema_version: str
    tool_id: str
    tool_version: str
    spec_revision: int | str
    source_ref: str
    status: str
    default_adapter_id: str
    capabilities: list[CapabilitySpec]
    entrypoints: list[dict[str, Any]] = field(default_factory=list)
    auth_requirements: dict[str, Any] = field(default_factory=dict)
    environment_checks: list[str] = field(default_factory=list)
    golden_scenarios: list[dict[str, Any]] = field(default_factory=list)
    source_docs_dir: str = ""
    error_oracle_id: str = ""
    skill_config: dict[str, Any] = field(default_factory=dict)

    def capability_map(self) -> dict[str, CapabilitySpec]:
        return {cap.id: cap for cap in self.capabilities}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "spec_revision": self.spec_revision,
            "source_ref": self.source_ref,
            "status": self.status,
            "default_adapter_id": self.default_adapter_id,
            "entrypoints": self.entrypoints,
            "auth_requirements": self.auth_requirements,
            "environment_checks": self.environment_checks,
            "capabilities": [cap.to_dict() for cap in self.capabilities],
            "golden_scenarios": self.golden_scenarios,
            "source_docs_dir": self.source_docs_dir,
            "error_oracle_id": self.error_oracle_id,
            "skill_config": self.skill_config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contract":
        return cls(
            schema_version=str(data.get("schema_version", "")),
            tool_id=str(data.get("tool_id", "")),
            tool_version=str(data.get("tool_version", "")),
            spec_revision=data.get("spec_revision", ""),
            source_ref=str(data.get("source_ref", "")),
            status=str(data.get("status", "unverified")),
            default_adapter_id=str(data.get("default_adapter_id", "")),
            entrypoints=list(data.get("entrypoints", [])),
            auth_requirements=dict(data.get("auth_requirements", {})),
            environment_checks=list(data.get("environment_checks", [])),
            capabilities=[CapabilitySpec.from_dict(c) for c in data.get("capabilities", [])],
            golden_scenarios=list(data.get("golden_scenarios", [])),
            source_docs_dir=str(data.get("source_docs_dir", "")),
            error_oracle_id=str(data.get("error_oracle_id", "")),
            skill_config=dict(data.get("skill_config", {})),
        )


@dataclass
class RunContext:
    """Isolated runtime context passed to ToolAdapter implementations."""

    workspace_dir: Path
    run_id: str
    channel: str = "core"
    timeout_s: float = 60.0
    env: dict[str, str] = field(default_factory=dict)
    cancel_after: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OracleVerdict:
    """Deterministic or judge verdict emitted by an L4/L8 oracle."""

    oracle_id: str
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "oracle_id": self.oracle_id,
            "passed": self.passed,
            "reason": self.reason,
            "details": self.details,
        }


@runtime_checkable
class ToolAdapter(Protocol):
    """Tool execution boundary defined in skill_architecture_pipeline.html Section 10."""

    adapter_id: str

    def execute(self, capability_id: str, validated_input: dict[str, Any], run_context: RunContext) -> dict[str, Any]:
        ...

    def status(self, handle: dict[str, Any]) -> dict[str, Any]:
        ...

    def cancel(self, handle: dict[str, Any]) -> dict[str, Any]:
        ...


def validate_contract_meta_schema(data: dict[str, Any]) -> list[str]:
    """Validate raw contract dict against the L1-L4 standard meta-schema requirements."""
    errors: list[str] = []
    for req_key in ("schema_version", "tool_id", "tool_version", "source_ref", "status", "capabilities"):
        if not data.get(req_key):
            errors.append(f"missing required contract field: {req_key}")

    if data.get("status") not in VALID_CONTRACT_STATUSES:
        errors.append(f"invalid contract status: {data.get('status')!r}")

    caps = data.get("capabilities")
    if not isinstance(caps, list) or not caps:
        errors.append("capabilities must be a non-empty list")
        return errors

    seen_ids: set[str] = set()
    for idx, cap in enumerate(caps):
        if not isinstance(cap, dict):
            errors.append(f"capability[{idx}] must be an object")
            continue
        cid = cap.get("id")
        if not cid or not isinstance(cid, str):
            errors.append(f"capability[{idx}] missing string id")
            continue
        if cid in seen_ids:
            errors.append(f"duplicate capability id: {cid}")
        seen_ids.add(cid)

        for field_name in ("input_schema", "output_schema", "adapter_id", "oracle_ids", "source_ref"):
            val = cap.get(field_name)
            if val is None or (isinstance(val, (str, list)) and len(val) == 0):
                errors.append(f"capability '{cid}' missing required field: {field_name}")

        if not isinstance(cap.get("input_schema"), dict) or cap["input_schema"].get("type") != "object":
            errors.append(f"capability '{cid}' input_schema must be an object JSON schema")

        ec = cap.get("execution_control")
        if not isinstance(ec, dict) or "timeout_s" not in ec:
            errors.append(f"capability '{cid}' missing L3 execution_control.timeout_s")

    return errors
