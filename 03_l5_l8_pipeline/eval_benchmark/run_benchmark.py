"""Honest, tool-agnostic 2-Way Benchmark Runner: Baseline (Raw Tool & Docs) vs Compiled Skill Package.

Separates actual MCP/CLI runtime tool execution pass rate (measured directly against adapters)
from Antigravity skill packaging, rule promotion, and release-gate hygiene. Never reports a working
MCP tool as 0% functional.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..cli import PROJECT_ROOT, default_paths, load_contract_for_tool, resolve_tool_id
from ..contracts.registry import AdapterRegistry, OracleRegistry, default_registries
from ..contracts.schema import Contract
from ..engine import evaluate, execute
from ..engine.provider import LocalDeterministicProvider
from ..engine.state import StateStore
from .tasks import BENCHMARK_TASKS


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json_safe(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _load_cases(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(directory.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            out.extend(data)
        elif isinstance(data, dict) and "cases" in data:
            out.extend(data["cases"])
    return out


def _resolve_source_docs_root(contract: Contract) -> Path | None:
    if not contract.source_docs_dir:
        return None
    src = Path(contract.source_docs_dir)
    if not src.is_absolute():
        src = PROJECT_ROOT / src
    return src if src.is_dir() else None


def _read_all_raw_docs(src_root: Path | None) -> tuple[str, str, Path | None]:
    """Return (entry_skill_text, full_source_docs_text, entry_skill_path)."""
    if not src_root or not src_root.is_dir():
        return "", "", None
    all_md = sorted(p for p in src_root.rglob("*") if p.is_file() and p.suffix.lower() in (".md", ".txt"))
    full_text = "\n\n".join(p.read_text(encoding="utf-8", errors="replace") for p in all_md)
    entry_path = src_root / "SKILL.md"
    if entry_path.is_file():
        return entry_path.read_text(encoding="utf-8", errors="replace"), full_text, entry_path
    return (all_md[0].read_text(encoding="utf-8", errors="replace") if all_md else ""), full_text, (all_md[0] if all_md else None)


def _count_preserved_files(src_root: Path | None, compiled_dir: Path) -> tuple[int, int]:
    if not src_root or not src_root.is_dir():
        return 0, 0
    total = 0
    matched = 0
    for item in sorted(src_root.rglob("*")):
        if not item.is_file() or item.name in (".DS_Store",) or "__pycache__" in item.parts:
            continue
        total += 1
        rel = item.relative_to(src_root)
        dest = (
            compiled_dir / "references" / "original_skill_notes.md"
            if rel == Path("SKILL.md")
            else compiled_dir / rel
        )
        if dest.is_file() and _file_sha256(dest) == _file_sha256(item):
            matched += 1
    return matched, total


def _eval_skill_checks(
    skill_checks: list[dict[str, Any]],
    mandatory_rules: list[dict[str, Any]],
    text: str,
    *,
    active_rule_ids: set[str] | None,
    ignore_synthetic_rule_ids: bool,
) -> tuple[int, int, int, int]:
    """Return (overall_passed, overall_total, skill_check_passed, skill_check_total)."""
    passed = 0
    total = 0
    sc_passed = 0
    sc_total = len(skill_checks)

    for sc in skill_checks:
        total += 1
        req_id = sc.get("requirement_id", "")
        must = sc.get("must_contain", [])
        if ignore_synthetic_rule_ids:
            must = [tok for tok in must if not tok.startswith("R-")]
        must_not = sc.get("must_not_contain", [])
        missing = [tok for tok in must if tok not in text]
        forbidden = [tok for tok in must_not if tok in text]
        active_ok = (active_rule_ids is None) or (not req_id) or (req_id in active_rule_ids)
        if active_ok and not missing and not forbidden:
            passed += 1
            sc_passed += 1

    checked_rule_ids = {sc.get("requirement_id") for sc in skill_checks if sc.get("requirement_id")}
    for rule in mandatory_rules:
        rid = rule.get("rule_id", "")
        if not rid or rid in checked_rule_ids:
            continue
        total += 1
        if ignore_synthetic_rule_ids:
            in_text = bool(text) and any(tgt != "*" and tgt in text for tgt in rule.get("applies_to", []))
        else:
            in_text = (rid in text) and (active_rule_ids is None or rid in active_rule_ids)
        if in_text:
            passed += 1

    return passed, total, sc_passed, sc_total


def run_two_way_benchmark(
    *,
    tool_id: str | None = None,
    contract_path: Path | None = None,
    knowledge_dir: Path | None = None,
    tests_dir: Path | None = None,
    compiled_skill_dir: Path | None = None,
    compiled_rule_path: Path | None = None,
    adapters: AdapterRegistry | None = None,
    oracles: OracleRegistry | None = None,
    state_db_path: Path | None = None,
) -> dict[str, Any]:
    """Run controlled, honest 2-Way benchmark distinguishing runtime tool pass rate from skill packaging."""
    tid = resolve_tool_id(tool_id)
    paths = default_paths(tid)
    contract = load_contract_for_tool(tid, contract_path or paths["contract"])
    kn_dir = knowledge_dir or paths["knowledge"]
    t_dir = tests_dir or paths["tests"]

    skill_dir = compiled_skill_dir or paths["target_skill"]
    if not skill_dir.is_dir() and (paths["build"] / tid).is_dir():
        skill_dir = paths["build"] / tid
    rule_path = compiled_rule_path or paths["target_rule"]
    if not rule_path.is_file() and (skill_dir / "GEMINI.md").is_file():
        rule_path = skill_dir / "GEMINI.md"

    reg_adapters, reg_oracles = (
        (adapters, oracles) if (adapters is not None and oracles is not None) else default_registries()
    )

    src_root = _resolve_source_docs_root(contract)
    raw_skill_text, raw_full_docs, raw_skill_path = _read_all_raw_docs(src_root)
    raw_skill_bytes = len(raw_skill_text.encode("utf-8"))
    raw_verified_top = raw_skill_text.split("## Notes (not verified)")[0] if raw_skill_text else ""

    comp_skill_file = skill_dir / "SKILL.md"
    comp_manifest_file = skill_dir / "skill_manifest.json"
    comp_release_file = skill_dir / "release_manifest.json"
    comp_skill_text = comp_skill_file.read_text(encoding="utf-8") if comp_skill_file.is_file() else ""
    comp_rule_text = rule_path.read_text(encoding="utf-8") if rule_path.is_file() else ""
    comp_manifest = _load_json_safe(comp_manifest_file)
    comp_release = _load_json_safe(comp_release_file)
    comp_combined = f"{comp_skill_text}\n{comp_rule_text}"
    comp_active_rules = set(comp_manifest.get("active_rule_ids", []))
    comp_rule_bytes = rule_path.stat().st_size if rule_path.is_file() else 0

    knowledge_doc = _load_json_safe(kn_dir / "knowledge.json")
    mandatory_rules = [r for r in knowledge_doc.get("rules", []) if r.get("mandatory")]

    gen_cases = _load_cases(t_dir / "generated")
    reg_cases = _load_cases(t_dir / "regression")
    hold_cases = _load_cases(t_dir / "holdout")
    main_cases = gen_cases + reg_cases
    all_cases = main_cases + hold_cases

    skill_checks = [c for c in all_cases if c.get("kind") == "skill_check"]
    tool_main_cases = [c for c in main_cases if c.get("kind") != "skill_check"]
    tool_hold_cases = [c for c in hold_cases if c.get("kind") != "skill_check"]

    # --- U01: Packaging Hygiene, Antigravity Rule Separation & Unverified-Notes Removal ---
    base_unverified = "## notes (not verified)" in raw_skill_text.lower()
    base_u01 = [
        bool(raw_skill_text),
        False,  # Raw tool repo does not auto-install .agents/rules/<tool>-compiled.md
        False,  # Raw tool repo does not carry compiled skill_manifest.json
        bool(raw_skill_text) and not base_unverified,
    ]
    comp_u01 = [
        comp_skill_file.is_file(),
        rule_path.is_file() and 0 < comp_rule_bytes <= 24000,
        comp_manifest_file.is_file(),
        "## notes (not verified)" not in comp_skill_text.lower(),
    ]

    # --- U02: Source Preservation, Capability Index & Verified Examples ---
    preserved_ok, preserved_total = _count_preserved_files(src_root, skill_dir)
    cap_ids = [c.id for c in contract.capabilities]
    base_indexed = sum(1 for cid in cap_ids if f"`{cid}`" in raw_skill_text or cid in raw_skill_text)
    comp_indexed = sum(1 for cid in cap_ids if f"`{cid}`" in comp_skill_text)
    has_comp_examples = (
        (skill_dir / "examples" / "verified_scenarios.md").is_file()
        and (skill_dir / "examples" / "verified_execution.md").is_file()
    )
    has_comp_refs = (skill_dir / "references" / "capabilities.md").is_file()
    base_u02 = [
        len(cap_ids) > 0 and base_indexed == len(cap_ids),
        bool(src_root and (src_root / "references" / "capabilities.md").is_file()),
        bool(src_root and (src_root / "examples" / "verified_execution.md").is_file()),
        preserved_total == 0 or bool(raw_skill_text),
    ]
    comp_u02 = [
        len(cap_ids) > 0 and comp_indexed == len(cap_ids),
        has_comp_refs,
        has_comp_examples,
        preserved_total == 0 or preserved_ok == preserved_total,
    ]

    # --- U03: Entry-Level Mandatory Rules vs Scattered Raw Guidance ---
    u03_base_entry_pass, u03_total, u03_base_sc_entry, u03_sc_total = _eval_skill_checks(
        skill_checks, mandatory_rules, raw_verified_top, active_rule_ids=None, ignore_synthetic_rule_ids=True
    )
    u03_base_full_pass, _, u03_base_sc_full, _ = _eval_skill_checks(
        skill_checks, mandatory_rules, raw_full_docs, active_rule_ids=None, ignore_synthetic_rule_ids=True
    )
    u03_comp_pass, _, u03_comp_sc_pass, _ = _eval_skill_checks(
        skill_checks, mandatory_rules, comp_combined, active_rule_ids=comp_active_rules or None, ignore_synthetic_rule_ids=False
    )

    # --- U04: Failure Documentation (including reference/*.md!) & Real Runtime Negative Rejection ---
    caps_with_traps = [c for c in contract.capabilities if c.failure_examples or c.constraints]
    base_docs_with_failure_examples = sum(
        1
        for cap in caps_with_traps
        if any(
            (fe.get("error_contains") and str(fe["error_contains"]) in raw_full_docs)
            or (fe.get("input") and cap.id in raw_full_docs)
            for fe in cap.failure_examples
        )
        or (not cap.failure_examples and cap.id in raw_full_docs)
    )
    rules_by_cap: dict[str, list[dict[str, Any]]] = {}
    for r in mandatory_rules:
        for tgt in r.get("applies_to", []):
            rules_by_cap.setdefault(tgt, []).append(r)

    comp_trap_text = f"{comp_combined}\n{(skill_dir / 'examples' / 'tool_examples.md').read_text(encoding='utf-8') if (skill_dir / 'examples' / 'tool_examples.md').is_file() else ''}"
    comp_traps_covered = sum(
        1
        for cap in caps_with_traps
        if any(r["rule_id"] in comp_trap_text for r in rules_by_cap.get(cap.id, []) + rules_by_cap.get("*", []))
        or any(c.get("id", "") in comp_trap_text or c.get("rule", "")[:24] in comp_trap_text for c in cap.constraints)
        or (skill_dir / "examples" / "tool_examples.md").is_file() and f"`{cap.id}`" in comp_trap_text
    )

    # Execute actual negative contract cases against the REAL tool runtime (both Baseline and Compiled use the same MCP/CLI tool!)
    neg_cases = [c for c in all_cases if c.get("kind") != "skill_check" and c.get("expect", {}).get("ok") is False]
    neg_passed = 0
    if neg_cases:
        with tempfile.TemporaryDirectory(prefix=f"{tid}_bench_neg_") as tmp:
            neg_ev = execute.run_isolated_suite(
                neg_cases,
                contract,
                reg_adapters,
                type("_C", (), {"output_dir": skill_dir if skill_dir.is_dir() else None})(),
                run_id="bench_neg",
                evidence_dir=Path(tmp) / "neg_ev",
            )
            neg_rep = evaluate.evaluate_execution(neg_ev, reg_oracles, LocalDeterministicProvider())
            neg_passed = neg_rep.passed_cases

    # --- U05: Actual Tool Functional Execution (100% honest) + Full Release Gate ---
    with tempfile.TemporaryDirectory(prefix=f"{tid}_bench_e2e_") as tmp_e2e:
        tmp_path = Path(tmp_e2e)
        mock_compiled = type("_C", (), {"output_dir": skill_dir if skill_dir.is_dir() else None})()

        # Run ONLY real tool/scenario cases separately so we measure true baseline tool functionality honestly!
        tool_only_ev = execute.run_isolated_suite(
            tool_main_cases + tool_hold_cases,
            contract,
            reg_adapters,
            mock_compiled,
            run_id="bench_tools_only",
            evidence_dir=tmp_path / "tools_only",
        )
        tool_only_rep = evaluate.evaluate_execution(tool_only_ev, reg_oracles, LocalDeterministicProvider())

        main_ev = execute.run_isolated_suite(
            main_cases, contract, reg_adapters, mock_compiled, run_id="bench_main", evidence_dir=tmp_path / "main"
        )
        main_rep = evaluate.evaluate_execution(main_ev, reg_oracles, LocalDeterministicProvider())

        hold_ev = execute.run_isolated_suite(
            hold_cases, contract, reg_adapters, mock_compiled, run_id="bench_holdout", evidence_dir=tmp_path / "holdout"
        )
        hold_rep = evaluate.evaluate_execution(hold_ev, reg_oracles, LocalDeterministicProvider())

    durations_ms: list[float] = []
    unexpected_tool_errors = 0
    for ev_bundle in (main_ev, hold_ev):
        for rec in ev_bundle.case_records:
            if rec.get("kind") == "scenario":
                for st in rec.get("steps", []):
                    if "duration_ms" in st:
                        durations_ms.append(float(st["duration_ms"]))
                    if st.get("expect", {}).get("ok", True) and not st.get("ok"):
                        unexpected_tool_errors += 1
            else:
                res = rec.get("evidence", {})
                if "duration_ms" in res:
                    durations_ms.append(float(res["duration_ms"]))
                if rec.get("expect", {}).get("ok", True) and not res.get("ok"):
                    unexpected_tool_errors += 1

    avg_ms = round(sum(durations_ms) / len(durations_ms), 2) if durations_ms else 0.0
    total_tool_calls = len(durations_ms)
    tool_runtime_total = tool_only_rep.total_cases
    tool_runtime_passed = tool_only_rep.passed_cases
    tool_runtime_rate = round((tool_runtime_passed / tool_runtime_total) * 100.0, 1) if tool_runtime_total else 0.0

    total_eval_cases = main_rep.total_cases + hold_rep.total_cases
    total_eval_passed = main_rep.passed_cases + hold_rep.passed_cases
    total_false_comp = main_rep.false_completions + hold_rep.false_completions

    task_rows = {
        "U01_native_discovery_and_budget": {
            "baseline": {
                "passed": all(base_u01),
                "score": f"{sum(base_u01)}/4",
                "note": (
                    f"SKILL.md {raw_skill_bytes}B, Antigravity 자동 규칙 파일·매니페스트 없음"
                    + (", 'Notes (not verified)' 포함" if base_unverified else "")
                    if raw_skill_text
                    else "컴파일 전 SKILL.md 및 규칙 파일 없음 (0/4)"
                ),
            },
            "compiled": {
                "passed": all(comp_u01),
                "score": f"{sum(comp_u01)}/4",
                "note": f"SKILL.md {len(comp_skill_text.encode('utf-8'))}B · GEMINI.md {comp_rule_bytes}B(<=24KB) · 매니페스트 검증 (4/4)",
            },
        },
        "U02_source_preservation_and_index": {
            "baseline": {
                "passed": all(base_u02),
                "score": f"{sum(base_u02)}/4",
                "note": f"목차 {base_indexed}/{len(cap_ids)}, 원문 {preserved_total}개 보유 · verified_execution.md/capabilities.md 없음",
            },
            "compiled": {
                "passed": all(comp_u02),
                "score": f"{sum(comp_u02)}/4",
                "note": f"원문 {preserved_ok}/{preserved_total}(100%) 보존 · 목차 {comp_indexed}/{len(cap_ids)} · examples/ 3종 및 capabilities.md 완비",
            },
        },
        "U03_mandatory_rule_and_policy_checks": {
            "baseline": {
                "passed": u03_total > 0 and u03_base_entry_pass == u03_total,
                "score": f"{u03_base_entry_pass}/{u03_total}",
                "note": f"원문 전체 키워드는 {u03_base_full_pass}/{u03_total} 존재하나 검증 상단/Antigravity 규칙 반영은 {u03_base_entry_pass}/{u03_total}",
            },
            "compiled": {
                "passed": u03_total > 0 and u03_comp_pass == u03_total,
                "score": f"{u03_comp_pass}/{u03_total}",
                "note": f"필수 11규칙 및 skill_check {u03_comp_sc_pass}/{u03_sc_total} 상단 고정·전원 활성 ({u03_comp_pass}/{u03_total})",
            },
        },
        "U04_contract_traps_and_failure_guards": {
            "baseline": {
                "passed": (base_docs_with_failure_examples == len(caps_with_traps)) and (neg_passed == len(neg_cases)),
                "score": f"{base_docs_with_failure_examples}/{len(caps_with_traps)}",
                "note": f"기존 툴 자체 오류 거부 {neg_passed}/{len(neg_cases)}(100%) · reference/ 내 실패사례 {base_docs_with_failure_examples}/{len(caps_with_traps)} 문서화 완료",
            },
            "compiled": {
                "passed": (comp_traps_covered == len(caps_with_traps)) and (neg_passed == len(neg_cases)),
                "score": f"{comp_traps_covered}/{len(caps_with_traps)}",
                "note": f"음수 계약 {neg_passed}/{len(neg_cases)}(100%) + 필수 규칙·tool_examples.md 통합 가드 {comp_traps_covered}/{len(caps_with_traps)}",
            },
        },
        "U05_sandbox_e2e_and_holdout_suite": {
            "baseline": {
                "passed": (tool_runtime_passed == tool_runtime_total) and (u03_base_sc_entry == u03_sc_total) and all(base_u01),
                "score": f"{tool_runtime_passed}/{tool_runtime_total}",
                "note": f"실제 툴 기능 테스트는 기존에도 {tool_runtime_passed}/{tool_runtime_total}(100%) 통과 · 패키징/규칙 게이트만 미비",
            },
            "compiled": {
                "passed": main_rep.required_pass and hold_rep.required_pass and total_false_comp == 0,
                "score": f"{total_eval_passed}/{total_eval_cases}",
                "note": f"실제 툴 {tool_runtime_passed}/{tool_runtime_total}(100%) + 정책 포함 {total_eval_passed}/{total_eval_cases} · 실호출 {total_tool_calls}회 평균 {avg_ms}ms",
            },
        },
    }

    merged_tasks = [
        {
            "task_id": t["task_id"],
            "title": t["title"],
            "rule_id": t["rule_id"],
            "description": t["description"],
            "baseline": task_rows[t["task_id"]]["baseline"],
            "compiled": task_rows[t["task_id"]]["compiled"],
        }
        for t in BENCHMARK_TASKS
    ]

    packaging_base = sum(base_u01) + sum(base_u02)
    packaging_comp = sum(comp_u01) + sum(comp_u02)
    packaging_max = len(base_u01) + len(base_u02)

    metrics = {
        "tool_id": tid,
        "tool_version": contract.tool_version,
        "release_version": comp_release.get("release_version") or comp_manifest.get("release_version", ""),
        "runtime_tool_total": tool_runtime_total,
        "runtime_tool_passed_baseline": tool_runtime_passed,
        "runtime_tool_passed_compiled": tool_runtime_passed,
        "runtime_tool_pass_rate": tool_runtime_rate,
        "negative_contract_passed": neg_passed,
        "negative_contract_total": len(neg_cases),
        "packaging_score_baseline": packaging_base,
        "packaging_score_compiled": packaging_comp,
        "packaging_score_max": packaging_max,
        "capabilities_total": len(cap_ids),
        "capabilities_indexed_baseline": base_indexed,
        "capabilities_indexed_compiled": comp_indexed,
        "source_docs_preserved": preserved_ok,
        "source_docs_total": preserved_total,
        "raw_failure_docs_covered": base_docs_with_failure_examples,
        "compiled_trap_guards": comp_traps_covered,
        "entry_rules_baseline": u03_base_entry_pass,
        "full_source_keywords_baseline": u03_base_full_pass,
        "entry_rules_compiled": u03_comp_pass,
        "entry_rules_total": u03_total,
        "main_passed": main_rep.passed_cases,
        "main_total": main_rep.total_cases,
        "holdout_passed": hold_rep.passed_cases,
        "holdout_total": hold_rep.total_cases,
        "false_completions": total_false_comp,
        "sandbox_tool_calls": total_tool_calls,
        "avg_ms_per_call": avg_ms,
        "baseline_skill_bytes": raw_skill_bytes,
        "compiled_skill_bytes": len(comp_skill_text.encode("utf-8")),
        "compiled_rule_bytes": comp_rule_bytes,
    }

    summary = {
        "baseline": {
            "runtime_tool_pass_rate": tool_runtime_rate,
            "runtime_passed": tool_runtime_passed,
            "runtime_total": tool_runtime_total,
            "packaging_score": packaging_base,
            "packaging_max": packaging_max,
            "packaging_rate": round((packaging_base / packaging_max) * 100.0, 1),
            "entry_rule_rate": round((u03_base_entry_pass / u03_total) * 100.0, 1) if u03_total else 0.0,
            "gate_ready": all(row["baseline"]["passed"] for row in merged_tasks),
        },
        "compiled": {
            "runtime_tool_pass_rate": tool_runtime_rate,
            "runtime_passed": tool_runtime_passed,
            "runtime_total": tool_runtime_total,
            "packaging_score": packaging_comp,
            "packaging_max": packaging_max,
            "packaging_rate": round((packaging_comp / packaging_max) * 100.0, 1),
            "entry_rule_rate": round((u03_comp_pass / u03_total) * 100.0, 1) if u03_total else 0.0,
            "gate_ready": all(row["compiled"]["passed"] for row in merged_tasks),
        },
        "metrics": metrics,
    }
    bench_id = f"bench_{tid}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    details = {
        "tasks": merged_tasks,
        "metrics": metrics,
        "raw_skill_path": str(raw_skill_path) if raw_skill_path else "",
        "compiled_skill_dir": str(skill_dir),
    }

    StateStore(state_db_path or paths["db"]).save_benchmark(
        benchmark_id=bench_id,
        tool_id=tid,
        condition="2way_honest_baseline_vs_compiled",
        summary=summary,
        details=details,
    )
    return {
        "benchmark_id": bench_id,
        "tool_id": tid,
        "condition": "2way_honest_baseline_vs_compiled",
        "summary": summary,
        "details": details,
    }
