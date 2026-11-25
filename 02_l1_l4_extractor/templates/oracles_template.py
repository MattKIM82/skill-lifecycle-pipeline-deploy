"""Representative domain Oracle template for verifying tool outputs beyond exit code 0.

Drop your copy at: skill_factory/contracts/<tool_id>/<tool_id>_oracles.py
Generic checks (`generic_schema_valid`, `generic_error_contract`, `generic_numeric_match`) are already
registered globally; add tool-specific file/semantic/invariant checks here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..registry import OracleRegistry
from ..schema import OracleVerdict


def oracle_domain_artifact(evidence: dict[str, Any], case: dict[str, Any]) -> OracleVerdict:
    """Example domain oracle checking expected output artifact existence on disk."""
    if not evidence.get("ok"):
        return OracleVerdict(
            oracle_id="my_tool_artifact_valid",
            passed=False,
            reason=f"execution failed: {evidence.get('error')}",
        )

    output = evidence.get("output") or {}
    rel_path = output.get("report_path") or output.get("output_path")
    if not rel_path:
        return OracleVerdict(
            oracle_id="my_tool_artifact_valid",
            passed=True,
            reason="no file artifact path returned by this capability",
        )

    ws = Path(evidence.get("workspace_dir") or ".")
    target = Path(rel_path) if Path(rel_path).is_absolute() else ws / str(rel_path)
    if not target.is_file() or target.stat().st_size == 0:
        return OracleVerdict(
            oracle_id="my_tool_artifact_valid",
            passed=False,
            reason=f"missing or empty artifact file: {target}",
        )

    return OracleVerdict(
        oracle_id="my_tool_artifact_valid",
        passed=True,
        reason=f"verified artifact ({target.stat().st_size} bytes)",
        details={"artifact_path": str(target), "bytes": target.stat().st_size},
    )


def register_oracles(registry: OracleRegistry) -> None:
    """Required hook name for automatic discovery by default_registries()."""
    registry.register("my_tool_artifact_valid", oracle_domain_artifact)
