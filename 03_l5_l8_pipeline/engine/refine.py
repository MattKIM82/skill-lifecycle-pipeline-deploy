"""Scoped closed-loop refinement proposals (forbids relaxing oracles or changing L1-L4 contracts)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .curate import CurationBatch
from .provider import LLMProvider, ProviderRequest
from .synthesize import CandidateBundle


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class PatchProposal:
    applied: bool
    patches: list[dict[str, Any]] = field(default_factory=list)
    diff_summary: list[str] = field(default_factory=list)
    blocked_reason: str | None = None


def _read_refine_prompt(prompt_version: str) -> str:
    p = PROMPTS_DIR / f"refine_{prompt_version}.txt"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return (
        "Propose minimal scoped diffs only on the affected knowledge/workflow/skill layer. "
        "Never alter test cases, holdout splits, or oracle acceptance thresholds."
    )


def propose_scoped_patch(
    candidate: CandidateBundle,
    failures: CurationBatch,
    provider: LLMProvider,
    *,
    prompt_version: str = "v1",
) -> tuple[CandidateBundle, PatchProposal]:
    """Generate and apply scoped patches on L5-L7 only; refuse oracle/test relaxation."""
    if not failures.records:
        return candidate, PatchProposal(applied=False, blocked_reason="no failures to refine")
    if failures.requires_owner:
        return candidate, PatchProposal(
            applied=False,
            blocked_reason="failure requires contract/tool/oracle owner escalation",
        )
    if failures.repeated:
        return candidate, PatchProposal(
            applied=False,
            blocked_reason="identical failure fingerprint repeated across attempts",
        )

    refine_schema = {
        "type": "object",
        "properties": {
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "target_layer": {"type": "string", "enum": ["knowledge", "workflow", "skill"]},
                        "affected_ids": {"type": "array", "items": {"type": "string"}},
                        "action": {"type": "string", "enum": ["enable_rule", "override_statement", "add_rule"]},
                        "rule_patch": {"type": "object"},
                        "evidence_ref": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["case_id", "target_layer", "affected_ids", "action", "reason"],
                },
            }
        },
        "required": ["patches"],
    }
    resp = provider.complete_json(
        ProviderRequest(
            role="refine",
            prompt_version=prompt_version,
            system_prompt=_read_refine_prompt(prompt_version),
            payload={
                "failures": [r.to_dict() for r in failures.records],
                "disabled_rules": candidate.disabled_rules,
                "active_rules": candidate.knowledge.get("rules", []),
            },
            output_schema=refine_schema if getattr(provider, "provider_id", "local") in ("antigravity_cli", "external_llm_api") else {},
        )
    )
    patches = resp.content.get("patches", [])
    if not patches:
        return candidate, PatchProposal(applied=False, blocked_reason="provider produced no valid L5-L7 patches")

    # Guardrail: reject any patch attempting to alter tests, holdout, or oracles
    for patch in patches:
        if patch.get("target_layer") in ("tests", "holdout", "oracle", "contract"):
            return candidate, PatchProposal(
                applied=False,
                blocked_reason=f"forbidden patch target_layer={patch.get('target_layer')!r}",
            )

    updated = copy.deepcopy(candidate)
    diff_summary: list[str] = []

    for patch in patches:
        action = patch.get("action")
        affected = patch.get("affected_ids", [])
        rule_patch = patch.get("rule_patch", {})

        if action == "enable_rule":
            for rid in affected:
                if rid in updated.disabled_rules:
                    updated.disabled_rules.remove(rid)
                    diff_summary.append(f"re-enabled disabled rule {rid}")
        elif action == "override_statement" and affected:
            rid = affected[0]
            new_stmt = rule_patch.get("statement")
            if new_stmt:
                updated.override_directives[rid] = new_stmt
                diff_summary.append(f"updated directive statement for {rid}")
        elif action == "add_rule" and rule_patch.get("rule_id"):
            existing_ids = {r["rule_id"] for r in updated.knowledge.get("rules", [])}
            if rule_patch["rule_id"] not in existing_ids:
                updated.knowledge.setdefault("rules", []).append(rule_patch)
                if rule_patch.get("mandatory"):
                    updated.policy.setdefault("mandatory_rule_ids", []).append(rule_patch["rule_id"])
                diff_summary.append(f"added scoped rule {rule_patch['rule_id']}")
        else:
            # Generic enable for affected rule IDs
            for rid in affected:
                if rid in updated.disabled_rules:
                    updated.disabled_rules.remove(rid)
                    diff_summary.append(f"re-enabled {rid}")

    if not diff_summary:
        return candidate, PatchProposal(applied=False, blocked_reason="patch yielded empty diff")

    return updated, PatchProposal(applied=True, patches=patches, diff_summary=diff_summary)
