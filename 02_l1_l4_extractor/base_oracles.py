"""Tool-agnostic L4/L8 deterministic oracle builders and generic defaults."""

from __future__ import annotations

import math
from typing import Any, Callable

import jsonschema
from referencing import Registry

from .schema import OracleVerdict


def is_clean_json_numbers(obj: Any) -> tuple[bool, str | None]:
    """Ensure floats contain no NaN or Inf."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return False, f"non-finite float: {obj!r}"
        return True, None
    if isinstance(obj, dict):
        for k, v in obj.items():
            ok, msg = is_clean_json_numbers(v)
            if not ok:
                return False, f"{k}.{msg}"
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            ok, msg = is_clean_json_numbers(item)
            if not ok:
                return False, f"[{idx}].{msg}"
    return True, None


def partial_match(expected: Any, actual: Any, rel_tol: float = 1e-6, path: str = "$") -> tuple[bool, str]:
    """Recursively verify that every key/item in expected matches actual (with numeric tolerance)."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False, f"{path}: expected dict, got {type(actual).__name__}"
        for k, v in expected.items():
            if k not in actual:
                return False, f"{path}: missing key {k!r}"
            ok, msg = partial_match(v, actual[k], rel_tol=rel_tol, path=f"{path}.{k}")
            if not ok:
                return False, msg
        return True, "ok"
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False, f"{path}: expected list len>={len(expected)}"
        for idx, item in enumerate(expected):
            ok, msg = partial_match(item, actual[idx], rel_tol=rel_tol, path=f"{path}[{idx}]")
            if not ok:
                return False, msg
        return True, "ok"
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            return False, f"{path}: expected number {expected!r}, got {actual!r}"
        if not math.isclose(float(expected), float(actual), rel_tol=rel_tol, abs_tol=1e-9):
            return False, f"{path}: expected {expected}, got {actual}"
        return True, "ok"
    if expected != actual:
        return False, f"{path}: expected {expected!r}, got {actual!r}"
    return True, "ok"


def make_schema_oracle(oracle_id: str) -> Callable[[dict[str, Any], dict[str, Any]], OracleVerdict]:
    """Create a schema-validation oracle bound to oracle_id."""

    def _oracle(evidence: dict[str, Any], case: dict[str, Any]) -> OracleVerdict:
        if not evidence.get("ok"):
            return OracleVerdict(oracle_id=oracle_id, passed=False, reason=f"execution failed: {evidence.get('error')}")
        output = evidence.get("output")
        clean_ok, clean_msg = is_clean_json_numbers(output)
        if not clean_ok:
            return OracleVerdict(oracle_id=oracle_id, passed=False, reason=f"non-clean float in output: {clean_msg}")
        schema = case.get("output_schema")
        if schema:
            validator = jsonschema.Draft202012Validator(schema, registry=Registry())
            errors = sorted(validator.iter_errors(output), key=lambda e: list(e.path))
            if errors:
                return OracleVerdict(oracle_id=oracle_id, passed=False, reason=f"output schema violation: {errors[0].message}")
        return OracleVerdict(oracle_id=oracle_id, passed=True, reason="output matches schema")

    return _oracle


def make_error_oracle(oracle_id: str) -> Callable[[dict[str, Any], dict[str, Any]], OracleVerdict]:
    """Create an error-contract oracle verifying negative/positive expectations."""

    def _oracle(evidence: dict[str, Any], case: dict[str, Any]) -> OracleVerdict:
        expect = case.get("expect", {})
        expect_ok = expect.get("ok", True)
        actual_ok = bool(evidence.get("ok"))
        if expect_ok:
            if not actual_ok:
                return OracleVerdict(oracle_id=oracle_id, passed=False, reason=f"expected success, got error: {evidence.get('error')}")
            return OracleVerdict(oracle_id=oracle_id, passed=True, reason="succeeded as expected")
        if actual_ok:
            return OracleVerdict(oracle_id=oracle_id, passed=False, reason="expected failure per error contract, but tool succeeded")
        err_msg = str(evidence.get("error") or "")
        expected_sub = expect.get("error_contains")
        if expected_sub and expected_sub.lower() not in err_msg.lower():
            return OracleVerdict(
                oracle_id=oracle_id,
                passed=False,
                reason=f"error {err_msg!r} did not contain expected substring {expected_sub!r}",
            )
        return OracleVerdict(oracle_id=oracle_id, passed=True, reason=f"failed cleanly as contracted: {err_msg}")

    return _oracle


def make_match_oracle(oracle_id: str) -> Callable[[dict[str, Any], dict[str, Any]], OracleVerdict]:
    """Create a structured partial-match / numeric tolerance oracle."""

    def _oracle(evidence: dict[str, Any], case: dict[str, Any]) -> OracleVerdict:
        if not evidence.get("ok"):
            return OracleVerdict(oracle_id=oracle_id, passed=False, reason=f"execution failed: {evidence.get('error')}")
        expected = case.get("expect", {}).get("output_match")
        if not expected:
            return OracleVerdict(oracle_id=oracle_id, passed=True, reason="no numeric reference constraint specified")
        rel_tol = float(case.get("expect", {}).get("rel_tol", 1e-6))
        ok, msg = partial_match(expected, evidence.get("output"), rel_tol=rel_tol)
        return OracleVerdict(oracle_id=oracle_id, passed=ok, reason="golden numeric match verified" if ok else f"mismatch: {msg}")

    return _oracle


def register_generic_oracles(registry: Any) -> None:
    """Register tool-agnostic schema, error-contract, and structured-match oracles."""
    registry.register("generic_schema_valid", make_schema_oracle("generic_schema_valid"))
    registry.register("generic_error_contract", make_error_oracle("generic_error_contract"))
    registry.register("generic_numeric_match", make_match_oracle("generic_numeric_match"))
