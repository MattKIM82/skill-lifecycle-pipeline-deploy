"""Reproducible failure curation, hash deduplication, and owner triage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .evaluate import EvalReport


OWNER_ESCALATION_CLASSES = {"contract", "tool", "eval_oracle"}


@dataclass
class FailureRecord:
    case_id: str
    run_id: str
    failure_class: str
    evidence_ref: str
    affected_ids: list[str]
    severity: str
    reproducible: bool
    status: str
    fingerprint: str
    summary: str
    recommended_fix: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "failure_class": self.failure_class,
            "evidence_ref": self.evidence_ref,
            "affected_ids": self.affected_ids,
            "severity": self.severity,
            "reproducible": self.reproducible,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "summary": self.summary,
            "recommended_fix": self.recommended_fix,
        }


@dataclass
class CurationBatch:
    records: list[FailureRecord]
    requires_owner: bool
    repeated: bool
    corpus_path: Path


def curate_failures(
    report: EvalReport,
    corpus_path: Path,
    *,
    previous_fingerprints: set[str] | None = None,
) -> CurationBatch:
    """Collect, classify, and deduplicate failed test cases into failure_cases.jsonl."""
    corpus_path = Path(corpus_path)
    corpus_path.parent.mkdir(parents=True, exist_ok=True)

    existing_fps: set[str] = set(previous_fingerprints or set())
    if corpus_path.is_file():
        for line in corpus_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    existing_fps.add(json.loads(line).get("fingerprint", ""))
                except json.JSONDecodeError:
                    continue

    curated: list[FailureRecord] = []
    requires_owner = False
    repeated = False
    seen_in_run: set[str] = set()

    for v in report.case_verdicts:
        if v.get("rule_passed"):
            continue
        fclass = v.get("suggested_failure_class") or "skill_rule"
        reason = v.get("failure_reason") or "unknown failure"
        fp = hashlib.sha256(f"{v['case_id']}|{fclass}|{reason}".encode("utf-8")).hexdigest()[:16]
        if fp in seen_in_run:
            continue
        seen_in_run.add(fp)
        if fp in existing_fps and previous_fingerprints is not None:
            repeated = True
        if fclass in OWNER_ESCALATION_CLASSES:
            requires_owner = True

        affected = [x for x in (v.get("requirement_id"), v.get("capability_id")) if x]
        rec = FailureRecord(
            case_id=v["case_id"],
            run_id=report.run_id,
            failure_class=fclass,
            evidence_ref=v.get("evidence_ref", ""),
            affected_ids=affected,
            severity=v.get("severity", "major"),
            reproducible=True,
            status="escalated_to_owner" if fclass in OWNER_ESCALATION_CLASSES else "triaged",
            fingerprint=fp,
            summary=reason,
            recommended_fix=v.get("recommended_fix", {}),
        )
        curated.append(rec)

    if curated:
        with corpus_path.open("a", encoding="utf-8") as fh:
            for rec in curated:
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    return CurationBatch(
        records=curated,
        requires_owner=requires_owner,
        repeated=repeated,
        corpus_path=corpus_path,
    )
