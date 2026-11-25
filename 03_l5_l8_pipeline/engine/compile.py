"""Deterministic L5-L7 validation and Antigravity skill package compiler."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.schema import Contract
from .state import sha256_json
from .synthesize import CandidateBundle


@dataclass
class CompileResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    output_dir: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    rendered_files: list[str] = field(default_factory=list)


def _validate_knowledge(candidate: CandidateBundle, contract: Contract) -> tuple[list[str], list[str], dict[str, dict[str, Any]]]:
    errors: list[str] = []
    warnings: list[str] = []
    cap_ids = {c.id for c in contract.capabilities}
    rules_by_id: dict[str, dict[str, Any]] = {}

    rules = candidate.knowledge.get("rules", [])
    mandatory_ids = set(candidate.policy.get("mandatory_rule_ids", []))
    disabled = set(candidate.disabled_rules)

    # Track explicit supersession so upgraded rules cleanly replace older ones without conflict.
    superseded_by: dict[str, str] = {}
    for r in rules:
        rid = r.get("rule_id")
        if rid and rid not in disabled:
            for old_id in r.get("supersedes", []):
                superseded_by[old_id] = rid

    for r in rules:
        rid = r.get("rule_id")
        if not rid:
            errors.append("L5 rule missing rule_id")
            continue
        if rid in rules_by_id:
            errors.append(f"L5 duplicate rule_id: {rid}")
            continue
        if rid in disabled:
            warnings.append(f"L5 rule {rid} is disabled in candidate")
            continue
        if rid in superseded_by:
            warnings.append(f"L5 rule {rid} superseded by {superseded_by[rid]}")
            continue

        if not r.get("source_ref"):
            errors.append(f"L5 rule {rid} missing source_ref")
        if not r.get("owner"):
            errors.append(f"L5 rule {rid} missing owner")
        if r.get("status") == "unknown" and rid in mandatory_ids:
            errors.append(f"L5 mandatory rule {rid} has status='unknown'")

        for target_cap in r.get("applies_to", []):
            if target_cap != "*" and target_cap not in cap_ids:
                errors.append(f"L5 rule {rid} references unknown capability {target_cap!r}")

        rules_by_id[rid] = r

    # Detect explicit rule conflicts (same target + same conflict_group with mutually exclusive directives)
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rules_by_id.values():
        cg = r.get("conflict_group")
        if cg:
            by_group[cg].append(r)
    for cg, grp in by_group.items():
        if len(grp) > 1:
            modes = {g.get("directive_mode") for g in grp if g.get("directive_mode")}
            if len(modes) > 1:
                ids = [g["rule_id"] for g in grp]
                errors.append(f"L5/L7 rule conflict in group {cg!r}: {ids} have incompatible modes {sorted(modes)}")

    for m_id in mandatory_ids:
        if m_id not in rules_by_id and m_id not in disabled and m_id not in superseded_by:
            errors.append(f"L5 policy references missing mandatory rule_id {m_id!r}")

    return errors, warnings, rules_by_id


def _validate_workflows(candidate: CandidateBundle, contract: Contract) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    cap_map = contract.capability_map()
    workflows = candidate.workflows.get("workflows", [])
    seen_wids: set[str] = set()

    for wf in workflows:
        wid = wf.get("workflow_id")
        if not wid:
            errors.append("L6 workflow missing workflow_id")
            continue
        if wid in seen_wids:
            errors.append(f"L6 duplicate workflow_id: {wid}")
            continue
        seen_wids.add(wid)

        nodes = wf.get("nodes", [])
        edges = wf.get("edges", [])
        if not nodes:
            errors.append(f"L6 workflow {wid} has no nodes")
            continue

        node_map: dict[str, dict[str, Any]] = {}
        for n in nodes:
            nid = n.get("node_id")
            if not nid:
                errors.append(f"L6 workflow {wid} node missing node_id")
                continue
            if nid in node_map:
                errors.append(f"L6 workflow {wid} duplicate node_id {nid}")
                continue
            cap_id = n.get("capability_id")
            if cap_id not in cap_map:
                errors.append(f"L6 workflow {wid} node {nid} references unknown capability {cap_id!r}")
            node_map[nid] = n

        indegree: dict[str, int] = {nid: 0 for nid in node_map}
        adj: dict[str, list[str]] = {nid: [] for nid in node_map}

        for e in edges:
            src = e.get("from")
            dst = e.get("to")
            if src not in node_map or dst not in node_map:
                errors.append(f"L6 workflow {wid} edge {src!r}->{dst!r} references missing node")
                continue
            adj[src].append(dst)
            indegree[dst] += 1

            # Type/role compatibility check across edge data bindings
            for binding in e.get("data_bindings", []):
                src_type = binding.get("source_type")
                dst_type = binding.get("target_type")
                if src_type and dst_type and src_type != dst_type:
                    errors.append(
                        f"L6 workflow {wid} edge {src}->{dst} incompatible binding type {src_type} != {dst_type}"
                    )

        # Kahn's topological sort to catch cycles
        queue = deque([nid for nid, deg in indegree.items() if deg == 0])
        visited = 0
        reachable: set[str] = set()
        entry_id = wf.get("entry_node") or (nodes[0].get("node_id") if nodes else None)
        if entry_id and entry_id in node_map:
            bfs = deque([entry_id])
            while bfs:
                cur = bfs.popleft()
                if cur in reachable:
                    continue
                reachable.add(cur)
                bfs.extend(adj.get(cur, []))
        else:
            errors.append(f"L6 workflow {wid} missing valid entry_node")

        while queue:
            cur = queue.popleft()
            visited += 1
            for nxt in adj[cur]:
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)

        if visited != len(node_map):
            errors.append(f"L6 workflow {wid} contains a cycle (visited {visited}/{len(node_map)} nodes)")

        unreachable = sorted(set(node_map) - reachable)
        if unreachable:
            errors.append(f"L6 workflow {wid} has unreachable nodes from {entry_id}: {unreachable}")

        if not wf.get("error_policy"):
            warnings.append(f"L6 workflow {wid} has no explicit error_policy")

    return errors, warnings


def detect_upgraded_impact(
    previous_manifest: dict[str, Any] | None,
    new_contract: Contract,
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Identify changed capability contracts on tool upgrade and rules needing conflict review."""
    prev_hashes = (previous_manifest or {}).get("capability_hashes", {})
    changed_caps: list[str] = []
    added_caps: list[str] = []
    for cap in new_contract.capabilities:
        cur_hash = sha256_json(
            {
                "input_schema": cap.input_schema,
                "output_schema": cap.output_schema,
                "constraints": cap.constraints,
            }
        )
        if cap.id not in prev_hashes:
            added_caps.append(cap.id)
        elif prev_hashes[cap.id] != cur_hash:
            changed_caps.append(cap.id)

    impacted_rules = [
        r["rule_id"]
        for r in rules
        if any(cid in changed_caps for cid in r.get("applies_to", []))
    ]
    return {
        "changed_capabilities": changed_caps,
        "added_capabilities": added_caps,
        "impacted_rule_ids": impacted_rules,
    }


def _copy_source_docs(contract: Contract, output_dir: Path) -> list[str]:
    """Preserve arbitrary original doc folders (knowledge/, reference/, docs/, examples/, prose .md) untouched."""
    if not contract.source_docs_dir:
        return []
    project_root = Path(__file__).resolve().parents[2]
    src_root = Path(contract.source_docs_dir)
    if not src_root.is_absolute():
        src_root = project_root / src_root
    if not src_root.is_dir():
        return []

    copied: list[str] = []
    for item in sorted(src_root.rglob("*")):
        if not item.is_file() or item.name in (".DS_Store",) or "__pycache__" in item.parts:
            continue
        rel = item.relative_to(src_root)
        if rel == Path("SKILL.md"):
            dest = output_dir / "references" / "original_skill_notes.md"
            rel_out = Path("references/original_skill_notes.md")
        else:
            dest = output_dir / rel
            rel_out = rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, dest)
        copied.append(str(rel_out).replace("\\", "/"))
    return copied


def _render_examples_docs(
    candidate: CandidateBundle,
    contract: Contract,
    output_dir: Path,
    scenario_cases: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Render examples/verified_scenarios.md and examples/tool_examples.md alongside preserved examples."""
    examples_dir = output_dir / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    generated: list[str] = []

    scen_lines = [
        f"# Verified Scenarios & Workflows ({contract.tool_id} v{contract.tool_version})",
        "",
        "Concrete step-by-step usage patterns and expected checks.",
        "",
    ]
    all_scenarios = list(contract.golden_scenarios) + [
        c for c in (scenario_cases or []) if c.get("kind") == "scenario"
    ]
    if all_scenarios:
        for sc in all_scenarios:
            cid = sc.get("case_id") or sc.get("id") or sc.get("title") or "scenario"
            scen_lines.append(f"## Scenario: `{cid}`")
            if sc.get("requirement_id"):
                scen_lines.append(f"- Enforced Rule: `{sc['requirement_id']}`")
            for idx, st in enumerate(sc.get("steps", []), start=1):
                cap_id = st.get("capability_id", "")
                scen_lines.append(f"### Step {idx}: `{cap_id}`")
                if "input" in st:
                    scen_lines.append("Input:")
                    scen_lines.append("```json")
                    scen_lines.append(json.dumps(st["input"], ensure_ascii=False, indent=2))
                    scen_lines.append("```")
                if st.get("expect"):
                    scen_lines.append("Expected:")
                    scen_lines.append("```json")
                    scen_lines.append(json.dumps(st["expect"], ensure_ascii=False, indent=2))
                    scen_lines.append("```")
            scen_lines.append("")
    else:
        for wf in candidate.workflows.get("workflows", []):
            scen_lines.append(f"## Pattern: `{wf['workflow_id']}`")
            if wf.get("description"):
                scen_lines.append(wf["description"])
            chain = " -> ".join(f"`{n['capability_id']}`" for n in wf.get("nodes", []))
            scen_lines.append(f"- Sequence: {chain}")
            scen_lines.append("")

    (examples_dir / "verified_scenarios.md").write_text("\n".join(scen_lines), encoding="utf-8")
    generated.append("examples/verified_scenarios.md")

    trap_lines = [f"# Tool Call & Failure Examples ({contract.tool_id} v{contract.tool_version})", ""]
    has_cap_examples = False
    for cap in contract.capabilities:
        if not cap.examples and not cap.failure_examples:
            continue
        has_cap_examples = True
        trap_lines.append(f"## `{cap.id}`")
        if cap.examples:
            ex = cap.examples[0]
            trap_lines.append("- Succeeding call:")
            trap_lines.append("```json")
            trap_lines.append(json.dumps(ex.get("input", {}), ensure_ascii=False, indent=2))
            trap_lines.append("```")
            if ex.get("expected_contains"):
                trap_lines.append("- Result contains:")
                trap_lines.append("```json")
                trap_lines.append(json.dumps(ex["expected_contains"], ensure_ascii=False, indent=2))
                trap_lines.append("```")
        if cap.failure_examples:
            trap_lines.append("- Known failing inputs:")
            for fe in cap.failure_examples:
                err_sub = fe.get("error_contains")
                msg = f" -> `{err_sub}`" if err_sub else ""
                trap_lines.append(f"  - Input `{json.dumps(fe.get('input', {}), ensure_ascii=False)}` fails{msg}")
        trap_lines.append("")

    if has_cap_examples:
        (examples_dir / "tool_examples.md").write_text("\n".join(trap_lines), encoding="utf-8")
        generated.append("examples/tool_examples.md")

    return generated


def export_verified_execution_example(
    output_dir: Path,
    contract: Contract,
    main_evidence: dict[str, Any] | None,
    holdout_evidence: dict[str, Any] | None = None,
) -> Path | None:
    """Write examples/verified_execution.md summarizing actual sandbox-verified runs."""
    if not output_dir or not output_dir.is_dir() or not main_evidence:
        return None
    examples_dir = output_dir / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Verified Execution Examples ({contract.tool_id} v{contract.tool_version})",
        "",
        "Recorded automatically from isolated L8 sandbox runs (never from exit code 0 alone).",
        "",
    ]
    for label, ev in (("Main Suite", main_evidence), ("Locked Holdout", holdout_evidence)):
        if not ev:
            continue
        lines.append(f"## {label}")
        for rec in ev.get("case_records", []):
            cid = rec.get("case_id", "")
            if rec.get("kind") == "scenario":
                steps = [f"`{st.get('step_id')}`({'ok' if st.get('ok') else 'err'})" for st in rec.get("steps", [])]
                lines.append(f"- Scenario `{cid}` (`ok={rec.get('ok')}`): {' -> '.join(steps)}")
            else:
                res = rec.get("evidence", {})
                expect_ok = rec.get("expect", {}).get("ok", True)
                actual_ok = bool(res.get("ok"))
                status_tag = "PASS" if actual_ok == expect_ok else "FAIL"
                lines.append(f"- Case `{cid}` (`{rec.get('capability_id')}`): `{status_tag}` (expected ok=`{expect_ok}`, actual ok=`{actual_ok}`)")
        lines.append("")

    dest = examples_dir / "verified_execution.md"
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def _summarize_doc_topics(path: Path, max_topics: int = 10) -> str:
    """Extract concise headings and distinct technical/env terms across any preserved doc."""
    if not path.is_file() or path.suffix.lower() not in (".md", ".txt"):
        return ""
    import re

    text = path.read_text(encoding="utf-8", errors="replace")
    headings: list[str] = []
    terms: list[str] = []
    lines_list = text.splitlines()
    for line in lines_list:
        s = line.strip()
        if s.startswith(("# ", "## ")):
            heading = re.sub(r"\(not verified\)", "", s.lstrip("#"), flags=re.I).strip()
            if heading and heading not in headings:
                headings.append(heading[:48])

    # Interleave bottom-up and top-down lines so late environment/caveat notes are also surfaced.
    interleaved: list[str] = []
    for i in range(len(lines_list)):
        if i % 2 == 0:
            interleaved.append(lines_list[i // 2])
        else:
            interleaved.append(lines_list[-(i // 2) - 1])
    for line in interleaved:
        s = line.strip()
        if s.startswith("|") or s.startswith("## Tools"):
            continue
        for pat in (r"\b([A-Z][A-Z0-9_]{5,})\b", r"`([A-Za-z0-9_./=-]{3,36})`", r"\b([a-z_]+_[a-z_]{3,})\b"):
            for tok in re.findall(pat, s):
                if tok not in terms and tok not in headings and not tok.startswith("reference/"):
                    terms.append(tok)
    combined = headings[:3] + terms
    return ", ".join(combined[:max_topics])


def _render_skill_md(
    candidate: CandidateBundle,
    contract: Contract,
    rules_by_id: dict[str, dict[str, Any]],
    preserved_files: list[str],
    generated_examples: list[str],
    output_dir: Path | None = None,
) -> str:
    """Compile tool-agnostic SKILL.md with rules, full tool index, examples, and doc map."""
    skill_name = candidate.skill_metadata.get("skill_name", f"{contract.tool_id}-compiled")
    rules = list(rules_by_id.values())
    workflows = candidate.workflows.get("workflows", [])
    mandatory_rules = [r for r in rules if r.get("mandatory")]
    desc = contract.skill_config.get(
        "description",
        f"Compiled skill package for {contract.tool_id} (v{contract.tool_version}, spec {contract.spec_revision}).",
    )
    efficiency_notes = contract.skill_config.get(
        "execution_notes",
        [
            "Input schemas define required arguments and enums; consult preserved docs (knowledge/, reference/, docs/, examples/) for domain prose, output shapes, and failure traps.",
            "Follow all mandatory rules (`R-*`) below regardless of which analysis/task path the user requests.",
        ],
    )

    lines: list[str] = [
        "---",
        f"name: {skill_name}",
        f"description: >-\n  {desc}",
        "---",
        "",
        f"# {skill_name} (Compiled L5-L7 Skill Package)",
        "",
        f"> Source Contract: `{contract.tool_id}` v`{contract.tool_version}` (spec `{contract.spec_revision}`) | Capabilities: `{len(contract.capabilities)}`",
        "",
        "## Execution Guidelines",
        "",
    ]
    for note in efficiency_notes:
        lines.append(f"- {note}")

    lines.extend(["", "## 1. Mandatory Rules (Invariants by `rule_id`)", ""])

    for r in mandatory_rules:
        rid = r["rule_id"]
        stmt = candidate.override_directives.get(rid, r["statement"])
        targets = ", ".join(r.get("applies_to", ["*"]))
        lines.append(f"- **[{rid}]** (`{targets}`): {stmt}")

    lines.extend(
        [
            "",
            "## 2. Complete Capability Index",
            "",
            "| Capability | Shape | Summary |",
            "|---|---|---|",
        ]
    )
    for cap in contract.capabilities:
        summary = cap.description.replace("\n", " ").strip()
        if len(summary) > 120:
            summary = summary[:117] + "..."
        lines.append(f"| `{cap.id}` | `{cap.shape}` | {summary} |")

    if workflows:
        lines.extend(["", "## 3. Reference Workflow Patterns (Adapt to User Goal)", ""])
        for wf in workflows:
            node_chain = " -> ".join(f"`{n['capability_id']}`" for n in wf.get("nodes", []))
            rule_tags = ", ".join(wf.get("rule_refs", []))
            gate = " (requires user spec/approval)" if wf.get("approval_gate") else ""
            lines.append(f"- **{wf['workflow_id']}**{gate}: {node_chain}")
            if wf.get("description"):
                lines.append(f"  - Purpose: {wf['description']}")
            if rule_tags:
                lines.append(f"  - Enforced Rules: `{rule_tags}`")

    lines.extend(["", "## 4. Examples, Preserved Docs & Contract Reference", ""])
    all_docs = sorted(dict.fromkeys(["references/capabilities.md", *preserved_files, *generated_examples]))
    for rel in all_docs:
        topic_hint = _summarize_doc_topics(output_dir / rel) if output_dir else ""
        suffix = f" — topics: {topic_hint}" if topic_hint else ""
        lines.append(f"- [{rel}]({rel}){suffix}")
    lines.append("")
    return "\n".join(lines)


def _render_gemini_rules(
    candidate: CandidateBundle,
    contract: Contract,
    rules_by_id: dict[str, dict[str, Any]],
) -> str:
    """Render concise workspace rule file (< 24 KB cap)."""
    lines = [
        f"# Compiled Rules for {contract.tool_id} (v{contract.tool_version})",
        "",
        f"Follow these compiled operational rules whenever using `{contract.tool_id}`:",
        "",
    ]
    for r in rules_by_id.values():
        if r.get("mandatory"):
            stmt = candidate.override_directives.get(r["rule_id"], r["statement"])
            lines.append(f"- [{r['rule_id']}] {stmt}")
    lines.append("")
    return "\n".join(lines)


def _render_capability_reference(contract: Contract, rules_by_id: dict[str, dict[str, Any]]) -> str:
    """Render lossless tool-agnostic capabilities reference grouped by generic shape."""
    by_shape: dict[str, list[Any]] = defaultdict(list)
    for cap in contract.capabilities:
        by_shape[cap.shape or "unknown"].append(cap)

    lines = [
        f"# {contract.tool_id.upper()} Contract Capabilities Reference (v{contract.tool_version})",
        "",
        "Auto-compiled from L1-L4 contract (preserving schemas, outputs, examples, and known failure inputs).",
        "",
    ]
    for shape in sorted(by_shape):
        lines.extend([f"## Shape: `{shape}`", ""])
        for cap in by_shape[shape]:
            lines.append(f"### `{cap.id}`")
            lines.append(cap.description)
            req = cap.input_schema.get("required", [])
            props = list(cap.input_schema.get("properties", {}).keys())
            lines.append(f"- Destructive: `{cap.destructive}` | Adapter: `{cap.adapter_id}` | Required: `{req}` | Inputs: `{props}`")
            for c in cap.constraints:
                lines.append(f"- Constraint ({c.get('id', 'C')}): {c['rule']}")
            for r in rules_by_id.values():
                if cap.id in r.get("applies_to", []):
                    lines.append(f"- Rule [{r['rule_id']}]: {r['statement']}")
            if cap.output_schema and cap.output_schema != {"type": "object"}:
                lines.append("- Output schema:")
                lines.append("```json")
                lines.append(json.dumps(cap.output_schema, ensure_ascii=False, indent=2))
                lines.append("```")
            if cap.examples:
                ex = cap.examples[0]
                lines.append("- Verified call example:")
                lines.append("```json")
                lines.append(json.dumps(ex.get("input", {}), ensure_ascii=False, indent=2))
                lines.append("```")
                if ex.get("expected_contains"):
                    lines.append("- Result contains:")
                    lines.append("```json")
                    lines.append(json.dumps(ex["expected_contains"], ensure_ascii=False, indent=2))
                    lines.append("```")
            if cap.failure_examples:
                lines.append("- Known failing inputs:")
                for fe in cap.failure_examples:
                    err_sub = fe.get("error_contains")
                    suffix = f" (message contains `{err_sub}`)" if err_sub else ""
                    lines.append(f"  - `{json.dumps(fe.get('input', {}), ensure_ascii=False)}`{suffix}")
            lines.append("")
    return "\n".join(lines)


def validate_and_compile(
    candidate: CandidateBundle,
    contract: Contract,
    output_dir: Path,
    *,
    prompt_version: str = "v1",
    scenario_cases: list[dict[str, Any]] | None = None,
) -> CompileResult:
    """Run deterministic L5-L7 validation checks and compile tool-agnostic skill package."""
    k_errors, k_warnings, rules_by_id = _validate_knowledge(candidate, contract)
    w_errors, w_warnings = _validate_workflows(candidate, contract)
    errors = [*k_errors, *w_errors]
    warnings = [*k_warnings, *w_warnings]

    # Verify intent routing map references valid rules & capabilities
    cap_ids = {c.id for c in contract.capabilities}
    superseded_ids = {
        old_id
        for r in candidate.knowledge.get("rules", [])
        if r.get("rule_id") and r.get("rule_id") not in set(candidate.disabled_rules)
        for old_id in r.get("supersedes", [])
    }
    for intent in candidate.intent_catalog.get("intents", []):
        for tcap in intent.get("target_capabilities", []):
            if tcap not in cap_ids:
                errors.append(f"L7 intent {intent.get('intent_id')} targets unknown capability {tcap!r}")
        for rid in intent.get("rule_ids", []):
            if rid not in rules_by_id and rid not in set(candidate.disabled_rules) and rid not in superseded_ids:
                errors.append(f"L7 intent {intent.get('intent_id')} references unknown rule {rid!r}")

    if errors:
        return CompileResult(ok=False, errors=errors, warnings=warnings)

    output_dir = Path(output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    refs_dir = output_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)

    gemini_md = _render_gemini_rules(candidate, contract, rules_by_id)
    if len(gemini_md.encode("utf-8")) > 24000:
        return CompileResult(
            ok=False,
            errors=["compiled GEMINI.md exceeds 24,000 byte rule cap"],
            warnings=warnings,
        )

    preserved_files = _copy_source_docs(contract, output_dir)
    (refs_dir / "capabilities.md").write_text(
        _render_capability_reference(contract, rules_by_id), encoding="utf-8"
    )
    generated_examples = _render_examples_docs(
        candidate, contract, output_dir, scenario_cases=scenario_cases
    )

    skill_md = _render_skill_md(
        candidate, contract, rules_by_id, preserved_files, generated_examples, output_dir=output_dir
    )
    skill_path = output_dir / "SKILL.md"
    gemini_path = output_dir / "GEMINI.md"
    skill_path.write_text(skill_md, encoding="utf-8")
    gemini_path.write_text(gemini_md, encoding="utf-8")

    all_rel_files = sorted(
        str(p.relative_to(output_dir)).replace("\\", "/")
        for p in output_dir.rglob("*")
        if p.is_file()
    )
    rendered_files = [str(output_dir / f) for f in all_rel_files]
    cap_hashes = {
        cap.id: sha256_json(
            {
                "input_schema": cap.input_schema,
                "output_schema": cap.output_schema,
                "constraints": cap.constraints,
            }
        )
        for cap in contract.capabilities
    }
    manifest = {
        "schema_version": "1.0",
        "skill_name": candidate.skill_metadata.get("skill_name", f"{contract.tool_id}-compiled"),
        "tool_id": contract.tool_id,
        "tool_version": contract.tool_version,
        "spec_revision": contract.spec_revision,
        "contract_hash": sha256_json(contract.to_dict()),
        "capability_hashes": cap_hashes,
        "knowledge_hash": sha256_json(candidate.knowledge),
        "workflows_hash": sha256_json(candidate.workflows),
        "model_id": candidate.skill_metadata.get("model_id", "local"),
        "prompt_version": prompt_version,
        "active_rule_ids": sorted(rules_by_id),
        "workflow_ids": [w["workflow_id"] for w in candidate.workflows.get("workflows", [])],
        "preserved_source_files": preserved_files,
        "files": all_rel_files,
    }
    manifest_path = output_dir / "skill_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rendered_files.append(str(manifest_path))

    return CompileResult(
        ok=True,
        errors=[],
        warnings=warnings,
        output_dir=output_dir,
        manifest=manifest,
        rendered_files=rendered_files,
    )
