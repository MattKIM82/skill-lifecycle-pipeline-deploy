"""Isolated sandbox execution adapter for L8 test suites."""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.registry import AdapterRegistry
from ..contracts.schema import Contract, RunContext
from .compile import CompileResult


@dataclass
class ExecutionEvidence:
    run_id: str
    evidence_dir: Path
    case_records: list[dict[str, Any]] = field(default_factory=list)
    skill_checks: list[dict[str, Any]] = field(default_factory=list)
    error_oracle_id: str = "generic_error_contract"


def _error_oracles_for(oracle_ids: list[str], contract: Contract) -> list[str]:
    matched = [oid for oid in oracle_ids if oid.endswith("_error_contract") or "error" in oid]
    if matched:
        return matched
    return [contract.error_oracle_id or "generic_error_contract"]


def _resolve_bindings(value: Any, step_outputs: dict[str, Any]) -> Any:
    """Substitute ${step_id.field.subfield} references from previous scenario step outputs."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        expr = value[2:-1]
        parts = expr.split(".")
        cur: Any = step_outputs
        for part in parts:
            if isinstance(cur, dict):
                cur = cur[part]
            elif isinstance(cur, list):
                cur = cur[int(part)]
            else:
                raise KeyError(f"cannot resolve binding {value!r} at {part!r}")
        return cur
    if isinstance(value, dict):
        return {k: _resolve_bindings(v, step_outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_bindings(item, step_outputs) for item in value]
    return value


def _execute_single_case(
    case: dict[str, Any],
    contract: Contract,
    adapters: AdapterRegistry,
    ws_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    cap_map = contract.capability_map()
    kind = case.get("kind", "single")

    if kind == "scenario":
        step_outputs: dict[str, Any] = {}
        step_results: list[dict[str, Any]] = []
        overall_ok = True
        for idx, step in enumerate(case.get("steps", []), start=1):
            step_id = step.get("step_id", f"step{idx}")
            cap_id = step["capability_id"]
            cap = cap_map[cap_id]
            adapter_id = step.get("adapter_id") or case.get("adapter_id") or cap.adapter_id
            adapter = adapters.resolve(adapter_id)
            resolved_input = _resolve_bindings(step.get("input", {}), step_outputs)
            ctx = RunContext(
                workspace_dir=ws_dir,
                run_id=run_id,
                channel=adapter_id,
                timeout_s=cap.execution_control.timeout_s,
                cancel_after=step.get("cancel_after"),
                metadata=step.get("metadata", {}),
            )
            ev = adapter.execute(cap_id, resolved_input, ctx)
            ev["step_id"] = step_id
            ev["expect"] = step.get("expect", {"ok": True})
            ev["oracle_ids"] = step.get("oracle_ids") or cap.oracle_ids
            ev["output_schema"] = cap.output_schema
            step_results.append(ev)
            if ev.get("ok") and ev.get("output") is not None:
                step_outputs[step_id] = ev["output"]
            if bool(ev.get("ok")) != bool(ev["expect"].get("ok", True)):
                overall_ok = False
        return {
            "case_id": case["case_id"],
            "split": case.get("split", "regression"),
            "severity": case.get("severity", "major"),
            "kind": "scenario",
            "requirement_id": case.get("requirement_id", ""),
            "ok": overall_ok,
            "steps": step_results,
            "workspace_dir": str(ws_dir),
            "expect": case.get("expect", {"ok": True}),
        }

    # Single capability call (with optional setup steps executed first)
    step_outputs = {}
    for s_idx, setup in enumerate(case.get("setup", []), start=1):
        s_cap = cap_map[setup["capability_id"]]
        s_adapter = adapters.resolve(setup.get("adapter_id") or s_cap.adapter_id)
        s_in = _resolve_bindings(setup.get("input", {}), step_outputs)
        s_res = s_adapter.execute(
            setup["capability_id"],
            s_in,
            RunContext(workspace_dir=ws_dir, run_id=run_id, channel=s_adapter.adapter_id),
        )
        if not s_res.get("ok"):
            return {
                "case_id": case["case_id"],
                "split": case.get("split", "generated"),
                "severity": case.get("severity", "major"),
                "kind": kind,
                "requirement_id": case.get("requirement_id", ""),
                "capability_id": case["capability_id"],
                "oracle_ids": _error_oracles_for(case.get("oracle_ids") or s_cap.oracle_ids, contract),
                "expect": case.get("expect", {"ok": True}),
                "evidence": {
                    "ok": False,
                    "error": f"setup step {s_idx} failed: {s_res.get('error')}",
                    "workspace_dir": str(ws_dir),
                },
            }
        step_outputs[setup.get("step_id", f"setup{s_idx}")] = s_res.get("output")

    cap_id = case["capability_id"]
    cap = cap_map[cap_id]
    adapter_id = case.get("adapter_id") or cap.adapter_id
    adapter = adapters.resolve(adapter_id)
    resolved_in = _resolve_bindings(case.get("input", {}), step_outputs)
    ctx = RunContext(
        workspace_dir=ws_dir,
        run_id=run_id,
        channel=adapter_id,
        timeout_s=cap.execution_control.timeout_s,
        cancel_after=case.get("cancel_after"),
        metadata=case.get("metadata", {}),
    )
    ev = adapter.execute(cap_id, resolved_in, ctx)
    return {
        "case_id": case["case_id"],
        "split": case.get("split", "generated"),
        "severity": case.get("severity", "major"),
        "kind": kind,
        "requirement_id": case.get("requirement_id", ""),
        "capability_id": cap_id,
        "oracle_ids": case.get("oracle_ids") or cap.oracle_ids,
        "output_schema": cap.output_schema,
        "input": resolved_in,
        "expect": case.get("expect", {"ok": True}),
        "evidence": ev,
    }


def _run_skill_checks(
    skill_cases: list[dict[str, Any]],
    compiled: CompileResult,
) -> list[dict[str, Any]]:
    """Verify compiled SKILL.md / GEMINI.md / manifest actually enforce required rules & references."""
    if not compiled.output_dir or not compiled.output_dir.is_dir():
        return []
    skill_text = (compiled.output_dir / "SKILL.md").read_text(encoding="utf-8")
    gemini_text = (compiled.output_dir / "GEMINI.md").read_text(encoding="utf-8")
    manifest = json.loads((compiled.output_dir / "skill_manifest.json").read_text(encoding="utf-8"))
    combined = f"{skill_text}\n{gemini_text}"

    results: list[dict[str, Any]] = []
    for case in skill_cases:
        req_rule = case.get("requirement_id", "")
        must_contain = case.get("must_contain", [])
        must_not_contain = case.get("must_not_contain", [])
        missing = [tok for tok in must_contain if tok not in combined]
        forbidden_found = [tok for tok in must_not_contain if tok in combined]
        rule_active = (not req_rule) or (req_rule in manifest.get("active_rule_ids", []))
        ok = rule_active and not missing and not forbidden_found
        results.append(
            {
                "case_id": case["case_id"],
                "split": case.get("split", "regression"),
                "severity": case.get("severity", "critical"),
                "requirement_id": req_rule,
                "kind": "skill_check",
                "rule_active": rule_active,
                "missing_tokens": missing,
                "forbidden_tokens": forbidden_found,
                "ok": ok,
                "recommended_fix": case.get("recommended_fix", {}),
            }
        )
    return results


def _snapshot_case_workspace(case_ws: Path, snapshot_root: Path) -> str:
    """Copy generated workspace artifacts to immutable evidence snapshot and return snapshot root."""
    ws_src = case_ws / "workspace"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    if ws_src.is_dir():
        ws_dst = snapshot_root / "workspace"
        if ws_dst.exists():
            shutil.rmtree(ws_dst)
        shutil.copytree(ws_src, ws_dst, ignore=shutil.ignore_patterns("ui.log", "*.lock"))
    return str(snapshot_root)


def run_isolated_suite(
    cases: list[dict[str, Any]],
    contract: Contract,
    adapters: AdapterRegistry,
    compiled: CompileResult,
    *,
    run_id: str,
    evidence_dir: Path,
    keep_workspaces: bool = False,
) -> ExecutionEvidence:
    """Execute test cases in isolated temp workspaces and persist immutable evidence."""
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    sandbox_root = Path(tempfile.mkdtemp(prefix=f"{contract.tool_id}_sandbox_{run_id}_"))
    snapshots_dir = evidence_dir / "snapshots"

    tool_cases = [c for c in cases if c.get("kind") != "skill_check"]
    skill_cases = [c for c in cases if c.get("kind") == "skill_check"]

    records: list[dict[str, Any]] = []
    try:
        for idx, case in enumerate(tool_cases, start=1):
            case_ws = sandbox_root / f"case_{idx:03d}_{case['case_id']}"
            case_ws.mkdir(parents=True, exist_ok=True)
            rec = _execute_single_case(case, contract, adapters, case_ws, run_id)
            snap_dir = _snapshot_case_workspace(case_ws, snapshots_dir / f"case_{idx:03d}_{case['case_id']}")
            if rec.get("kind") == "scenario":
                rec["workspace_dir"] = snap_dir
                for st in rec.get("steps", []):
                    st["workspace_dir"] = snap_dir
            elif "evidence" in rec:
                rec["evidence"]["workspace_dir"] = snap_dir
            records.append(rec)
    finally:
        if not keep_workspaces:
            shutil.rmtree(sandbox_root, ignore_errors=True)

    skill_checks = _run_skill_checks(skill_cases, compiled)
    out = ExecutionEvidence(
        run_id=run_id,
        evidence_dir=evidence_dir,
        case_records=records,
        skill_checks=skill_checks,
        error_oracle_id=contract.error_oracle_id or "generic_error_contract",
    )
    (evidence_dir / "execution_evidence.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "tool_cases_count": len(records),
                "skill_checks_count": len(skill_checks),
                "case_records": records,
                "skill_checks": skill_checks,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out
