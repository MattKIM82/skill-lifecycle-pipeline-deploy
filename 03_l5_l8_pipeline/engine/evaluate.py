"""Deterministic oracle evaluation + context-separated independent judge."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.registry import OracleRegistry
from .execute import ExecutionEvidence
from .provider import LLMProvider, ProviderRequest


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class EvalReport:
    run_id: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    critical_failures: int
    false_completions: int
    required_pass: bool
    pass_rate: float
    case_verdicts: list[dict[str, Any]] = field(default_factory=list)
    judge_report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "critical_failures": self.critical_failures,
            "false_completions": self.false_completions,
            "required_pass": self.required_pass,
            "pass_rate": self.pass_rate,
            "case_verdicts": self.case_verdicts,
            "judge_report": self.judge_report,
        }


def _read_judge_prompt(prompt_version: str) -> str:
    p = PROMPTS_DIR / f"judge_{prompt_version}.txt"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return (
        "Do not trust the candidate generator's claims. Compare execution evidence against approved oracles "
        "and return verdict, evidence_ref, failure_class, and uncertainty."
    )


def _select_error_oracles(oracle_ids: list[str], fallback: str) -> list[str]:
    matched = [oid for oid in oracle_ids if oid.endswith("_error_contract") or "error" in oid]
    return matched or [fallback or "generic_error_contract"]


def evaluate_execution(
    evidence: ExecutionEvidence,
    oracles: OracleRegistry,
    judge_provider: LLMProvider,
    *,
    prompt_version: str = "v1",
) -> EvalReport:
    """Evaluate tool execution records via L4 deterministic oracles + independent AI judge."""
    verdicts_list: list[dict[str, Any]] = []
    false_completions = 0
    critical_failures = 0
    err_fallback = getattr(evidence, "error_oracle_id", "") or "generic_error_contract"

    for rec in evidence.case_records:
        kind = rec.get("kind", "single")
        if kind == "scenario":
            step_verdicts: list[dict[str, Any]] = []
            case_passed = True
            failure_reasons: list[str] = []
            for st in rec.get("steps", []):
                st_case = {
                    "capability_id": st["capability_id"],
                    "expect": st.get("expect", {"ok": True}),
                    "output_schema": st.get("output_schema"),
                }
                # Only run schema/numeric/artifact oracles when expecting success; always run error_contract.
                oids = list(st.get("oracle_ids", []))
                if not st_case["expect"].get("ok", True):
                    oids = _select_error_oracles(oids, err_fallback)
                ovs = oracles.evaluate(oids, st, st_case)
                for ov in ovs:
                    step_verdicts.append({"step_id": st["step_id"], **ov.to_dict()})
                    if not ov.passed:
                        case_passed = False
                        failure_reasons.append(f"{st['step_id']}:{ov.oracle_id}: {ov.reason}")
            verdicts_list.append(
                {
                    "case_id": rec["case_id"],
                    "split": rec.get("split", "regression"),
                    "severity": rec.get("severity", "major"),
                    "requirement_id": rec.get("requirement_id", ""),
                    "rule_passed": case_passed,
                    "failure_reason": "; ".join(failure_reasons),
                    "suggested_failure_class": "workflow_dag" if not case_passed else None,
                    "evidence_ref": str(evidence.evidence_dir / "execution_evidence.json"),
                    "oracle_verdicts": step_verdicts,
                }
            )
        else:
            ev = rec["evidence"]
            expect = rec.get("expect", {"ok": True})
            oids = list(rec.get("oracle_ids", []))
            if not expect.get("ok", True):
                oids = _select_error_oracles(oids, err_fallback)
            case_ctx = {
                "capability_id": rec["capability_id"],
                "input": rec.get("input"),
                "expect": expect,
                "output_schema": rec.get("output_schema"),
            }
            ovs = oracles.evaluate(oids, ev, case_ctx)
            passed = all(ov.passed for ov in ovs)
            if not expect.get("ok", True) and ev.get("ok"):
                false_completions += 1
            if not passed and rec.get("severity") == "critical":
                critical_failures += 1
            verdicts_list.append(
                {
                    "case_id": rec["case_id"],
                    "split": rec.get("split", "generated"),
                    "severity": rec.get("severity", "major"),
                    "requirement_id": rec.get("requirement_id", ""),
                    "capability_id": rec.get("capability_id"),
                    "rule_passed": passed,
                    "failure_reason": "; ".join(ov.reason for ov in ovs if not ov.passed),
                    "suggested_failure_class": "contract" if not expect.get("ok", True) else "tool",
                    "evidence_ref": str(evidence.evidence_dir / "execution_evidence.json"),
                    "oracle_verdicts": [ov.to_dict() for ov in ovs],
                }
            )

    for sc in evidence.skill_checks:
        ok = bool(sc.get("ok"))
        if not ok and sc.get("severity") == "critical":
            critical_failures += 1
        reasons: list[str] = []
        if not sc.get("rule_active"):
            reasons.append(f"rule {sc.get('requirement_id')} inactive in skill_manifest.json")
        if sc.get("missing_tokens"):
            reasons.append(f"compiled skill missing required guidance: {sc['missing_tokens']}")
        if sc.get("forbidden_tokens"):
            reasons.append(f"compiled skill contains forbidden guidance: {sc['forbidden_tokens']}")
        verdicts_list.append(
            {
                "case_id": sc["case_id"],
                "split": sc.get("split", "regression"),
                "severity": sc.get("severity", "critical"),
                "requirement_id": sc.get("requirement_id", ""),
                "rule_passed": ok,
                "failure_reason": "; ".join(reasons),
                "suggested_failure_class": "skill_rule" if not ok else None,
                "recommended_fix": sc.get("recommended_fix", {}),
                "evidence_ref": str(evidence.evidence_dir / "execution_evidence.json"),
                "oracle_verdicts": [
                    {
                        "oracle_id": "compiled_skill_policy_check",
                        "passed": ok,
                        "reason": "ok" if ok else "; ".join(reasons),
                    }
                ],
            }
        )

    # Context-separated independent judge call (cannot override deterministic failure!)
    judge_schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["pass", "fail"]},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "verdict": {"type": "string"},
                        "failure_class": {"type": "string"},
                        "evidence_ref": {"type": "string"},
                        "reason": {"type": "string"},
                        "uncertainty": {"type": "number"},
                    },
                    "required": ["case_id", "verdict", "failure_class", "reason"],
                },
            },
            "uncertainty": {"type": "number"},
            "summary": {"type": "string"},
        },
        "required": ["verdict", "issues", "summary"],
    }
    judge_records = [
        {
            "case_id": v["case_id"],
            "split": v["split"],
            "requirement_id": v.get("requirement_id"),
            "rule_passed": v["rule_passed"],
            "failure_reason": v.get("failure_reason", ""),
            "suggested_failure_class": v.get("suggested_failure_class"),
            "oracle_verdicts": v.get("oracle_verdicts", []),
        }
        for v in verdicts_list
    ]
    judge_resp = judge_provider.complete_json(
        ProviderRequest(
            role="judge",
            prompt_version=prompt_version,
            system_prompt=_read_judge_prompt(prompt_version),
            payload={
                "run_id": evidence.run_id,
                "records": judge_records,
                "semantic_checks": [],
            },
            output_schema=judge_schema if getattr(judge_provider, "provider_id", "local") in ("antigravity_cli", "external_llm_api") else {},
        )
    )
    judge_doc = judge_resp.content
    judge_failed_ids = {issue["case_id"] for issue in judge_doc.get("issues", []) if issue.get("verdict") == "fail"}
    for v in verdicts_list:
        if v["case_id"] in judge_failed_ids and v["rule_passed"]:
            v["rule_passed"] = False
            v["failure_reason"] = "independent judge flagged semantic violation"

    total = len(verdicts_list)
    passed_count = sum(1 for v in verdicts_list if v["rule_passed"])
    failed_count = total - passed_count
    required_pass = (failed_count == 0) and (false_completions == 0) and (critical_failures == 0)

    report = EvalReport(
        run_id=evidence.run_id,
        total_cases=total,
        passed_cases=passed_count,
        failed_cases=failed_count,
        critical_failures=critical_failures,
        false_completions=false_completions,
        required_pass=required_pass,
        pass_rate=round((passed_count / total) * 100.0, 2) if total else 0.0,
        case_verdicts=verdicts_list,
        judge_report={
            "provider_id": judge_resp.provider_id,
            "model_id": judge_resp.model_id,
            **judge_doc,
        },
    )
    (evidence.evidence_dir / "eval_report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report
