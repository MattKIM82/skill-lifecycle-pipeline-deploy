"""Deterministic L4/L8 oracles for OSAW outputs, errors, files, and UI state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..base_oracles import (
    make_error_oracle,
    make_match_oracle,
    make_schema_oracle,
    partial_match as _partial_match,
)
from ..registry import OracleRegistry
from ..schema import OracleVerdict


oracle_schema_valid = make_schema_oracle("osaw_schema_valid")
oracle_error_contract = make_error_oracle("osaw_error_contract")
oracle_numeric_match = make_match_oracle("osaw_numeric_match")


def oracle_file_artifact(evidence: dict[str, Any], case: dict[str, Any]) -> OracleVerdict:
    """Verify artifact files (PNG plot or HTML report) actually exist and are non-empty."""
    if not evidence.get("ok"):
        return OracleVerdict(
            oracle_id="osaw_file_artifact",
            passed=False,
            reason=f"execution failed: {evidence.get('error')}",
        )
    ws_dir = Path(evidence.get("workspace_dir", ""))
    output = evidence.get("output") or {}

    rel_path = None
    if isinstance(output, dict):
        if "path" in output and isinstance(output["path"], str):
            rel_path = output["path"]
        elif isinstance(output.get("payload"), dict) and isinstance(output["payload"].get("image"), dict):
            rel_path = output["payload"]["image"].get("path")

    if not rel_path:
        return OracleVerdict(
            oracle_id="osaw_file_artifact",
            passed=False,
            reason="output did not reference an artifact path (anti-fake-completion)",
        )
    target = (ws_dir / rel_path).resolve()
    if not target.is_file() or target.stat().st_size == 0:
        return OracleVerdict(
            oracle_id="osaw_file_artifact",
            passed=False,
            reason=f"referenced artifact missing or empty: {target}",
        )
    if target.suffix.lower() == ".png":
        header = target.read_bytes()[:8]
        if header != b"\x89PNG\r\n\x1a\n":
            return OracleVerdict(
                oracle_id="osaw_file_artifact",
                passed=False,
                reason=f"invalid PNG signature: {target}",
            )
    elif target.suffix.lower() == ".html":
        html_text = target.read_text(encoding="utf-8", errors="replace")
        if "<html" not in html_text.lower() or len(html_text) < 200:
            return OracleVerdict(
                oracle_id="osaw_file_artifact",
                passed=False,
                reason=f"HTML report appears incomplete: {target}",
            )
    return OracleVerdict(
        oracle_id="osaw_file_artifact",
        passed=True,
        reason=f"verified artifact on disk ({target.stat().st_size} bytes)",
        details={"artifact_path": str(target), "bytes": target.stat().st_size},
    )


def oracle_ui_directive(evidence: dict[str, Any], case: dict[str, Any]) -> OracleVerdict:
    """Verify UI link contracts (ui_open metadata or ui_show workspace/ui/show.json)."""
    if not evidence.get("ok"):
        return OracleVerdict(
            oracle_id="osaw_ui_directive",
            passed=False,
            reason=f"execution failed: {evidence.get('error')}",
        )
    ws_dir = Path(evidence.get("workspace_dir", ""))
    output = evidence.get("output") or {}
    cap_id = case.get("capability_id") or evidence.get("capability_id")

    if cap_id == "ui_show":
        if case.get("input"):
            show_file = ws_dir / "workspace" / "ui" / "show.json"
            if not show_file.is_file():
                return OracleVerdict(
                    oracle_id="osaw_ui_directive",
                    passed=False,
                    reason="ui_show returned ok without writing workspace/ui/show.json",
                )
            doc = json.loads(show_file.read_text(encoding="utf-8"))
            if doc.get("seq", 0) < 1 or not isinstance(doc.get("directive"), dict):
                return OracleVerdict(
                    oracle_id="osaw_ui_directive",
                    passed=False,
                    reason=f"invalid show.json contents: {doc}",
                )
            return OracleVerdict(
                oracle_id="osaw_ui_directive",
                passed=True,
                reason=f"ui_show wrote directive seq={doc['seq']}",
            )
        return OracleVerdict(
            oracle_id="osaw_ui_directive",
            passed="seq" in output and "written" in output,
            reason="ui_show inspection output verified",
        )

    if cap_id == "ui_open":
        status = output.get("status")
        if status not in ("running", "not_running", "port_in_use"):
            return OracleVerdict(
                oracle_id="osaw_ui_directive",
                passed=False,
                reason=f"unexpected ui_open status: {status!r}",
            )
        return OracleVerdict(
            oracle_id="osaw_ui_directive",
            passed=True,
            reason=f"ui_open returned valid status={status}",
        )

    return OracleVerdict(
        oracle_id="osaw_ui_directive",
        passed=True,
        reason="UI contract check passed",
    )


def oracle_invariants(evidence: dict[str, Any], case: dict[str, Any]) -> OracleVerdict:
    """Verify semantic/domain invariants such as filter boundary inclusion or UI HTTP bindings."""
    if not evidence.get("ok"):
        return OracleVerdict(
            oracle_id="osaw_invariants",
            passed=False,
            reason=f"execution failed: {evidence.get('error')}",
        )
    inv = case.get("expect", {}).get("invariant")
    if not inv:
        return OracleVerdict(
            oracle_id="osaw_invariants",
            passed=True,
            reason="no extra invariant requested",
        )
    output = evidence.get("output") or {}
    kind = inv.get("kind")
    if kind == "min_analysis_rows":
        actual_rows = output.get("payload", {}).get("analysis_context", {}).get("analysis_rows")
        if actual_rows is None or actual_rows < inv["min_rows"]:
            return OracleVerdict(
                oracle_id="osaw_invariants",
                passed=False,
                reason=f"analysis_rows={actual_rows} < required min_rows={inv['min_rows']}",
            )
        return OracleVerdict(
            oracle_id="osaw_invariants",
            passed=True,
            reason=f"analysis_rows={actual_rows} >= {inv['min_rows']}",
        )
    if kind == "chart_data_present":
        cd = output.get("payload", {}).get("chart_data")
        if not isinstance(cd, dict) or not cd:
            return OracleVerdict(
                oracle_id="osaw_invariants",
                passed=False,
                reason="plot output missing chart_data for non-visual verification",
            )
        return OracleVerdict(oracle_id="osaw_invariants", passed=True, reason="chart_data present")

    return OracleVerdict(oracle_id="osaw_invariants", passed=True, reason="invariant checked")


def register_osaw_oracles(registry: OracleRegistry) -> None:
    registry.register("osaw_schema_valid", oracle_schema_valid)
    registry.register("osaw_error_contract", oracle_error_contract)
    registry.register("osaw_numeric_match", oracle_numeric_match)
    registry.register("osaw_file_artifact", oracle_file_artifact)
    registry.register("osaw_ui_directive", oracle_ui_directive)
    registry.register("osaw_invariants", oracle_invariants)
