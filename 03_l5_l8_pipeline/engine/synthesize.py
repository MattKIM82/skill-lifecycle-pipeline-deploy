"""L5-L7 structured synthesis via ProviderAdapter."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.schema import Contract
from .provider import LLMProvider, ProviderRequest


PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class CandidateBundle:
    tool_id: str
    contract_version: str
    knowledge: dict[str, Any]
    policy: dict[str, Any]
    intent_catalog: dict[str, Any]
    workflows: dict[str, Any]
    skill_metadata: dict[str, Any] = field(default_factory=dict)
    disabled_rules: list[str] = field(default_factory=list)
    override_directives: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "contract_version": self.contract_version,
            "knowledge": self.knowledge,
            "policy": self.policy,
            "intent_catalog": self.intent_catalog,
            "workflows": self.workflows,
            "skill_metadata": self.skill_metadata,
            "disabled_rules": self.disabled_rules,
            "override_directives": self.override_directives,
        }


def _load_json_if_exists(path: Path | None) -> dict[str, Any] | None:
    if path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _read_prompt(name: str, fallback: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text(encoding="utf-8") if path.is_file() else fallback


SYNTHESIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "knowledge": {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string"},
                "version": {"type": "string"},
                "rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rule_id": {"type": "string"},
                            "category": {"type": "string"},
                            "applies_to": {"type": "array", "items": {"type": "string"}},
                            "constraint_refs": {"type": "array", "items": {"type": "string"}},
                            "statement": {"type": "string"},
                            "severity": {"type": "string"},
                            "mandatory": {"type": "boolean"},
                            "status": {"type": "string"},
                            "owner": {"type": "string"},
                            "source_ref": {"type": "string"},
                        },
                        "required": ["rule_id", "applies_to", "statement", "mandatory", "status", "owner", "source_ref"],
                    },
                },
            },
            "required": ["tool_id", "rules"],
        },
        "policy": {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string"},
                "mandatory_rule_ids": {"type": "array", "items": {"type": "string"}},
                "forbid_unverified_claims": {"type": "boolean"},
                "forbid_guessing_user_specs": {"type": "boolean"},
            },
            "required": ["tool_id", "mandatory_rule_ids"],
        },
        "intent_catalog": {
            "type": "object",
            "properties": {
                "tool_id": {"type": "string"},
                "intents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "intent_id": {"type": "string"},
                            "description": {"type": "string"},
                            "target_capabilities": {"type": "array", "items": {"type": "string"}},
                            "rule_ids": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["intent_id", "description", "target_capabilities", "rule_ids"],
                    },
                },
            },
            "required": ["tool_id", "intents"],
        },
        "workflows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "description": {"type": "string"},
                    "entry_node": {"type": "string"},
                    "approval_gate": {"type": "boolean"},
                    "error_policy": {"type": "string"},
                    "rule_refs": {"type": "array", "items": {"type": "string"}},
                    "nodes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "node_id": {"type": "string"},
                                "capability_id": {"type": "string"},
                            },
                            "required": ["node_id", "capability_id"],
                        },
                    },
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                            },
                            "required": ["from", "to"],
                        },
                    },
                },
                "required": ["workflow_id", "description", "entry_node", "nodes", "edges"],
            },
        },
    },
    "required": ["knowledge", "policy", "intent_catalog", "workflows"],
}


def _compact_contract_for_ai(contract: Contract) -> dict[str, Any]:
    """Build a token-compact L1-L4 digest preserving capability IDs, constraints, and source_refs."""
    return {
        "tool_id": contract.tool_id,
        "tool_version": contract.tool_version,
        "spec_revision": contract.spec_revision,
        "capabilities": [
            {
                "id": c.id,
                "shape": c.shape,
                "destructive": c.destructive,
                "required_inputs": c.input_schema.get("required", []),
                "input_props": list(c.input_schema.get("properties", {}).keys()),
                "constraints": c.constraints,
                "dependencies": c.dependencies,
                "source_ref": c.source_ref,
            }
            for c in contract.capabilities
        ],
    }


def _load_raw_source_docs(project_root: Path, contract: Contract, max_total_chars: int = 55000) -> dict[str, str]:
    """Read arbitrary tool docs (knowledge/, reference/, docs/, prose .md/.txt) from contract.source_docs_dir."""
    raw: dict[str, str] = {}
    total = 0

    def _add_file(path: Path) -> None:
        nonlocal total
        if not path.is_file() or total >= max_total_chars:
            return
        try:
            key = str(path.relative_to(project_root))
        except ValueError:
            key = path.name
        text = path.read_text(encoding="utf-8", errors="replace")
        remaining = max_total_chars - total
        if len(text) > remaining:
            text = text[:remaining] + "\n... [truncated]"
        raw[key] = text
        total += len(text)

    for rel in contract.skill_config.get("extra_raw_docs", []):
        p = Path(rel)
        _add_file(p if p.is_absolute() else project_root / p)

    doc_roots: list[Path] = []
    if contract.source_docs_dir:
        d = Path(contract.source_docs_dir)
        doc_roots.append(d if d.is_absolute() else project_root / d)
    fallback = project_root / "skill_factory" / "docs" / contract.tool_id
    if fallback.is_dir():
        doc_roots.append(fallback)

    for root in doc_roots:
        if not root.is_dir():
            continue
        # Prioritize top-level guides (SKILL.md, README.md, overview) then subfolders (knowledge/, reference/, docs/, examples/)
        files = sorted(
            (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".txt")),
            key=lambda p: (0 if p.parent == root else 1, len(p.parts), str(p)),
        )
        for f in files:
            _add_file(f)
            if total >= max_total_chars:
                break
    return raw


def generate_candidate(
    contract: Contract,
    provider: LLMProvider,
    *,
    knowledge_dir: Path | None = None,
    workflows_dir: Path | None = None,
    user_requirements: dict[str, Any] | None = None,
    prompt_version: str = "v1",
    from_raw: bool = False,
) -> CandidateBundle:
    """Synthesize L5-L7 candidate bundle grounded strictly in admitted L1-L4 contract."""
    use_raw_ai = from_raw or getattr(provider, "provider_id", "local") in ("antigravity_cli", "external_llm_api")
    existing_k = None if use_raw_ai else (_load_json_if_exists(knowledge_dir / "knowledge.json") if knowledge_dir else None)
    existing_p = None if use_raw_ai else (_load_json_if_exists(knowledge_dir / "policy.json") if knowledge_dir else None)
    existing_i = None if use_raw_ai else (_load_json_if_exists(knowledge_dir / "intent_catalog.json") if knowledge_dir else None)
    existing_w = None if use_raw_ai else (_load_json_if_exists(workflows_dir / "workflow.json") if workflows_dir else None)

    project_root = Path(__file__).resolve().parents[2]
    sys_prompt = _read_prompt(
        f"synthesize_{prompt_version}.txt",
        "Use only verified contract capabilities, mark unclear rules as unknown, and attach source_ref to every rule.",
    )
    if use_raw_ai:
        rule_hints = contract.skill_config.get("rule_id_hints", "unique rule_id (e.g. R-01, R-02)")
        payload = {
            "contract_digest": _compact_contract_for_ai(contract),
            "raw_source_docs": _load_raw_source_docs(project_root, contract),
            "user_requirements": user_requirements or {},
            "instructions": (
                f"Synthesize L5 operational rules ({rule_hints}, constraint_refs linking C-* constraints, and exact source_ref), "
                "L5 policy, intent_catalog, and optional L6 acyclic workflow DAGs (nodes/edges using ONLY valid capability_ids "
                "from contract_digest)."
            ),
        }
    else:
        payload = {
            "contract": contract.to_dict(),
            "user_requirements": user_requirements or {},
            "existing_knowledge": existing_k,
            "existing_policy": existing_p,
            "existing_intents": existing_i,
            "existing_workflows": existing_w,
        }

    resp = provider.complete_json(
        ProviderRequest(
            role="synthesize",
            prompt_version=prompt_version,
            system_prompt=sys_prompt,
            payload=payload,
            output_schema=SYNTHESIZE_SCHEMA if use_raw_ai else {},
        )
    )
    content = resp.content
    workflows_doc = existing_w or {"tool_id": contract.tool_id, "version": "1.0", "workflows": content.get("workflows", [])}
    if "workflows" in content and content["workflows"]:
        workflows_doc = {**workflows_doc, "workflows": content["workflows"]}

    return CandidateBundle(
        tool_id=contract.tool_id,
        contract_version=f"{contract.tool_version}-spec{contract.spec_revision}",
        knowledge=content["knowledge"],
        policy=content["policy"],
        intent_catalog=content["intent_catalog"],
        workflows=workflows_doc,
        skill_metadata={
            "provider_id": resp.provider_id,
            "model_id": resp.model_id,
            "prompt_version": prompt_version,
            "skill_name": f"{contract.tool_id}-compiled",
        },
    )
