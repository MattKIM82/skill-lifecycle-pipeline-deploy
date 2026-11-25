"""End-to-end L5-L8 pipeline orchestration (build_skill_pipeline)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.registry import AdapterRegistry, OracleRegistry
from ..contracts.schema import Contract
from . import admission, compile as compiler, curate, evaluate, execute, refine, release, synthesize
from .provider import LLMProvider
from .state import StateStore, sha256_json


@dataclass
class PipelineResult:
    ok: bool
    run_id: str
    status: str
    attempts: int
    blocked_reason: str | None = None
    compile_result: compiler.CompileResult | None = None
    main_report: evaluate.EvalReport | None = None
    holdout_report: evaluate.EvalReport | None = None
    release_candidate: dict[str, Any] | None = None
    release_record: release.ReleaseRecord | None = None
    patches_applied: list[str] = field(default_factory=list)


def _load_cases_from_dir(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        return []
    cases: list[dict[str, Any]] = []
    for p in sorted(directory.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            cases.extend(data)
        elif isinstance(data, dict) and "cases" in data:
            cases.extend(data["cases"])
    return cases


def build_skill_pipeline(
    *,
    contract_data: dict[str, Any] | Contract,
    adapters: AdapterRegistry,
    oracles: OracleRegistry,
    synth_provider: LLMProvider,
    judge_provider: LLMProvider,
    refine_provider: LLMProvider,
    state: StateStore,
    knowledge_dir: Path,
    workflows_dir: Path,
    tests_dir: Path,
    build_dir: Path,
    runs_dir: Path,
    releases_root: Path,
    target_skill_dir: Path,
    target_rule_path: Path | None = None,
    version: str | None = None,
    max_attempts: int = 3,
    auto_approve: bool = False,
    candidate_override: synthesize.CandidateBundle | None = None,
    prompt_version: str = "v1",
    from_raw: bool = False,
) -> PipelineResult:
    """Execute Admission -> Synthesize -> Compile -> Execute -> Evaluate -> Curate/Refine -> Release Gate."""
    raw_dict = contract_data.to_dict() if isinstance(contract_data, Contract) else contract_data
    tool_id = str(raw_dict.get("tool_id", "unknown"))
    resolved_version = version or release.derive_next_skill_version(str(raw_dict.get("tool_version", "0.1.0")))
    c_ver = f"{raw_dict.get('tool_version', '0')}-spec{raw_dict.get('spec_revision', '0')}"

    run_id = state.create_run(
        tool_id=tool_id,
        input_hash=sha256_json(raw_dict),
        contract_version=c_ver,
        model_id=synth_provider.model_id,
        prompt_version=prompt_version,
    )
    run_dir = Path(runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. Admission Gate (L1-L4)
    admitted = admission.verify(raw_dict, adapters, oracles)
    if not admitted.ok or admitted.contract is None:
        state.transition(
            run_id,
            stage="admission",
            status="BLOCKED",
            payload=admitted.evidence,
            summary={"blocked_reason": "; ".join(admitted.errors)},
        )
        return PipelineResult(
            ok=False,
            run_id=run_id,
            status="BLOCKED",
            attempts=0,
            blocked_reason="; ".join(admitted.errors),
        )

    contract = admitted.contract
    state.transition(
        run_id,
        stage="admission",
        status="ADMITTED",
        payload=admitted.evidence,
        summary={"admitted_capabilities": len(admitted.admitted_capabilities)},
    )

    # 2. Synthesize initial L5-L7 candidate (or accept test candidate with injected defect)
    candidate = candidate_override or synthesize.generate_candidate(
        contract,
        synth_provider,
        knowledge_dir=knowledge_dir,
        workflows_dir=workflows_dir,
        prompt_version=prompt_version,
        from_raw=from_raw,
    )
    state.transition(
        run_id,
        stage="synthesize",
        status="GENERATED",
        payload={"skill_name": candidate.skill_metadata.get("skill_name")},
    )

    main_cases = (
        _load_cases_from_dir(tests_dir / "generated")
        + _load_cases_from_dir(tests_dir / "regression")
    )
    holdout_cases = _load_cases_from_dir(tests_dir / "holdout")

    previous_fps: set[str] = set()
    patches_applied: list[str] = []
    corpus_path = run_dir / "failure_cases.jsonl"

    for attempt in range(1, max_attempts + 1):
        attempt_dir = run_dir / f"attempt_{attempt}"
        skill_build_dir = Path(build_dir) / tool_id

        # 3. Deterministic compile & validation (L5-L7)
        compiled = compiler.validate_and_compile(
            candidate,
            contract,
            skill_build_dir,
            prompt_version=prompt_version,
            scenario_cases=main_cases,
        )
        if not compiled.ok:
            state.transition(
                run_id,
                stage="compile",
                status="BLOCKED",
                attempt=attempt,
                payload={"errors": compiled.errors},
                summary={"blocked_reason": "; ".join(compiled.errors)},
            )
            return PipelineResult(
                ok=False,
                run_id=run_id,
                status="BLOCKED",
                attempts=attempt,
                blocked_reason="; ".join(compiled.errors),
                compile_result=compiled,
                patches_applied=patches_applied,
            )

        # 4. Execute main test suite in isolated sandbox
        ev = execute.run_isolated_suite(
            main_cases,
            contract,
            adapters,
            compiled,
            run_id=f"{run_id}_a{attempt}",
            evidence_dir=attempt_dir / "main",
        )
        state.transition(
            run_id,
            stage="execute",
            status="TESTED",
            attempt=attempt,
            artifact_refs={"main_evidence": str(attempt_dir / "main" / "execution_evidence.json")},
        )

        # 5. Evaluate main suite (Rule Oracle + Context-Separated Judge)
        main_report = evaluate.evaluate_execution(
            ev,
            oracles,
            judge_provider,
            prompt_version=prompt_version,
        )
        state.transition(
            run_id,
            stage="evaluate",
            status="EVALUATED",
            attempt=attempt,
            artifact_refs={"main_eval": str(attempt_dir / "main" / "eval_report.json")},
            summary={"main_pass_rate": main_report.pass_rate, "main_passed": main_report.passed_cases, "main_total": main_report.total_cases},
        )

        if main_report.required_pass:
            # 6. Execute locked holdout suite
            holdout_ev = execute.run_isolated_suite(
                holdout_cases,
                contract,
                adapters,
                compiled,
                run_id=f"{run_id}_holdout",
                evidence_dir=attempt_dir / "holdout",
            )
            holdout_report = evaluate.evaluate_execution(
                holdout_ev,
                oracles,
                judge_provider,
                prompt_version=prompt_version,
            )
            if holdout_report.required_pass:
                if compiled.output_dir:
                    compiler.export_verified_execution_example(
                        compiled.output_dir,
                        contract,
                        {"case_records": ev.case_records},
                        {"case_records": holdout_ev.case_records},
                    )
                gate_ok, rc_info = release.create_release_candidate(
                    run_id=run_id,
                    version=resolved_version,
                    compiled=compiled,
                    main_report=main_report,
                    holdout_report=holdout_report,
                    releases_root=releases_root,
                    state=state,
                )
                if not gate_ok:
                    state.transition(run_id, stage="release_candidate", status="BLOCKED", attempt=attempt, payload=rc_info)
                    return PipelineResult(
                        ok=False,
                        run_id=run_id,
                        status="BLOCKED",
                        attempts=attempt,
                        blocked_reason="; ".join(rc_info.get("reasons", [])),
                        compile_result=compiled,
                        main_report=main_report,
                        holdout_report=holdout_report,
                        patches_applied=patches_applied,
                    )

                rel_record = None
                final_status = "RELEASE_CANDIDATE"
                if auto_approve:
                    rel_record = release.approve_and_publish(
                        run_id=run_id,
                        tool_id=tool_id,
                        version=resolved_version,
                        rc_dir=Path(rc_info["rc_dir"]),
                        target_skill_dir=target_skill_dir,
                        target_rule_path=target_rule_path,
                        state=state,
                        approved_by="pipeline_auto_gate",
                    )
                    final_status = "PUBLISHED"

                return PipelineResult(
                    ok=True,
                    run_id=run_id,
                    status=final_status,
                    attempts=attempt,
                    compile_result=compiled,
                    main_report=main_report,
                    holdout_report=holdout_report,
                    release_candidate=rc_info,
                    release_record=rel_record,
                    patches_applied=patches_applied,
                )

            # Holdout failed -> curate holdout failures
            eval_to_curate = holdout_report
        else:
            eval_to_curate = main_report

        # 7. Curate failures + propose scoped refinement patch
        curation = curate.curate_failures(
            eval_to_curate,
            corpus_path,
            previous_fingerprints=previous_fps,
        )
        previous_fps.update(r.fingerprint for r in curation.records)
        state.transition(
            run_id,
            stage="curate",
            status="TRIAGED",
            attempt=attempt,
            artifact_refs={"failure_corpus": str(corpus_path)},
            payload={"curated_count": len(curation.records), "requires_owner": curation.requires_owner},
        )

        candidate, proposal = refine.propose_scoped_patch(
            candidate,
            curation,
            refine_provider,
            prompt_version=prompt_version,
        )
        if not proposal.applied:
            state.transition(
                run_id,
                stage="refine",
                status="BLOCKED",
                attempt=attempt,
                summary={"blocked_reason": proposal.blocked_reason},
            )
            return PipelineResult(
                ok=False,
                run_id=run_id,
                status="BLOCKED",
                attempts=attempt,
                blocked_reason=proposal.blocked_reason,
                compile_result=compiled,
                main_report=main_report,
                patches_applied=patches_applied,
            )

        patches_applied.extend(proposal.diff_summary)
        state.transition(
            run_id,
            stage="refine",
            status="PATCH_PROPOSED",
            attempt=attempt,
            payload={"diff_summary": proposal.diff_summary},
        )
        state.transition(run_id, stage="retest", status="RETEST", attempt=attempt)

    state.transition(
        run_id,
        stage="refine",
        status="BLOCKED",
        attempt=max_attempts,
        summary={"blocked_reason": "iteration_limit"},
    )
    return PipelineResult(
        ok=False,
        run_id=run_id,
        status="BLOCKED",
        attempts=max_attempts,
        blocked_reason="iteration_limit",
        patches_applied=patches_applied,
    )
