"""L1-L4 Admission Gate: blocks generation if standard contract or adapters/oracles are incomplete."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..contracts.registry import AdapterRegistry, OracleRegistry
from ..contracts.schema import Contract, validate_contract_meta_schema


@dataclass
class AdmissionResult:
    ok: bool
    contract: Contract | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    admitted_capabilities: list[str] = field(default_factory=list)
    blocked_capabilities: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def verify(
    raw_contract: dict[str, Any] | Contract,
    adapter_registry: AdapterRegistry,
    oracle_registry: OracleRegistry,
) -> AdmissionResult:
    """Validate L1-L4 contract completeness, adapter registrations, and oracle coverage."""
    contract_dict = raw_contract.to_dict() if isinstance(raw_contract, Contract) else raw_contract
    errors = validate_contract_meta_schema(contract_dict)
    if errors:
        return AdmissionResult(
            ok=False,
            contract=None,
            errors=errors,
            evidence={"stage": "admission", "meta_schema_errors": errors},
        )

    contract = raw_contract if isinstance(raw_contract, Contract) else Contract.from_dict(contract_dict)
    warnings: list[str] = []
    admitted_caps: list[str] = []
    blocked_caps: list[str] = []

    if contract.status != "verified":
        errors.append(f"contract status is {contract.status!r}; must be 'verified' to admit")

    if not contract.entrypoints:
        warnings.append("no L1 entrypoints listed in contract")

    for cap in contract.capabilities:
        cap_errors: list[str] = []
        if not adapter_registry.has(cap.adapter_id):
            cap_errors.append(f"unregistered adapter_id={cap.adapter_id!r}")
        for oid in cap.oracle_ids:
            if not oracle_registry.has(oid):
                cap_errors.append(f"unregistered oracle_id={oid!r}")
        if cap.status not in ("verified", "partial"):
            cap_errors.append(f"capability status={cap.status!r} cannot be admitted")
        if not cap.examples:
            warnings.append(f"capability {cap.id!r} has no reference positive example")

        if cap_errors:
            blocked_caps.append(cap.id)
            errors.extend(f"{cap.id}: {e}" for e in cap_errors)
        else:
            admitted_caps.append(cap.id)

    ok = len(errors) == 0 and len(admitted_caps) > 0
    return AdmissionResult(
        ok=ok,
        contract=contract if ok else None,
        errors=errors,
        warnings=warnings,
        admitted_capabilities=admitted_caps,
        blocked_capabilities=blocked_caps,
        evidence={
            "tool_id": contract.tool_id,
            "tool_version": contract.tool_version,
            "spec_revision": contract.spec_revision,
            "admitted_count": len(admitted_caps),
            "blocked_count": len(blocked_caps),
            "errors": errors,
            "warnings": warnings,
        },
    )
