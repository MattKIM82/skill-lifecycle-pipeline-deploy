"""Release Gate, atomic versioned publish to Antigravity workspace, and rollback."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compile import CompileResult
from .evaluate import EvalReport
from .state import StateStore, sha256_json, utc_now


@dataclass
class ReleaseRecord:
    release_id: str
    version: str
    status: str
    release_dir: Path
    target_skill_dir: Path
    target_rule_path: Path | None
    manifest_hash: str


def derive_next_skill_version(tool_version: str) -> str:
    """Derive default skill release version from source tool_version (e.g. '0.1.1' -> '0.1.2')."""
    import re

    clean = str(tool_version or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", clean)
    if m:
        return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"
    m2 = re.match(r"^(\d+)\.(\d+)$", clean)
    if m2:
        return f"{m2.group(1)}.{m2.group(2)}.1"
    return "0.1.2"


def _passes_release_gate(
    main_report: EvalReport,
    holdout_report: EvalReport,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not main_report.required_pass:
        reasons.append(
            f"main eval failed ({main_report.failed_cases} failed, "
            f"{main_report.critical_failures} critical, {main_report.false_completions} false_completions)"
        )
    if not holdout_report.required_pass:
        reasons.append(
            f"locked holdout eval failed ({holdout_report.failed_cases} failed, "
            f"{holdout_report.false_completions} false_completions)"
        )
    return len(reasons) == 0, reasons


def create_release_candidate(
    *,
    run_id: str,
    version: str,
    compiled: CompileResult,
    main_report: EvalReport,
    holdout_report: EvalReport,
    releases_root: Path,
    state: StateStore,
) -> tuple[bool, dict[str, Any]]:
    """Verify Release Gate and stage immutable release candidate snapshot."""
    ok, reasons = _passes_release_gate(main_report, holdout_report)
    if not ok:
        return False, {"ok": False, "reasons": reasons}

    if not compiled.output_dir or not compiled.output_dir.is_dir():
        return False, {"ok": False, "reasons": ["missing compiled skill directory"]}

    releases_root = Path(releases_root)
    rc_dir = releases_root / f"{compiled.manifest['tool_id']}-{version}"
    if rc_dir.exists():
        shutil.rmtree(rc_dir)
    shutil.copytree(compiled.output_dir, rc_dir)

    release_manifest = {
        **compiled.manifest,
        "release_version": version,
        "run_id": run_id,
        "created_at": utc_now(),
        "main_eval": {
            "total": main_report.total_cases,
            "passed": main_report.passed_cases,
            "pass_rate": main_report.pass_rate,
            "false_completions": main_report.false_completions,
        },
        "holdout_eval": {
            "total": holdout_report.total_cases,
            "passed": holdout_report.passed_cases,
            "pass_rate": holdout_report.pass_rate,
            "false_completions": holdout_report.false_completions,
        },
    }
    manifest_hash = sha256_json(release_manifest)
    release_manifest["release_manifest_hash"] = manifest_hash
    (rc_dir / "release_manifest.json").write_text(
        json.dumps(release_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    state.transition(
        run_id,
        stage="release_candidate",
        status="RELEASE_CANDIDATE",
        artifact_refs={"release_candidate_dir": str(rc_dir)},
        summary={
            "release_version": version,
            "manifest_hash": manifest_hash,
            "holdout_pass_rate": holdout_report.pass_rate,
        },
    )
    return True, {
        "ok": True,
        "release_id": f"{compiled.manifest['tool_id']}-{version}",
        "version": version,
        "rc_dir": str(rc_dir),
        "manifest_hash": manifest_hash,
        "release_manifest": release_manifest,
    }


def _atomic_deploy_dir(source_dir: Path, target_dir: Path) -> None:
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = target_dir.parent / f".{target_dir.name}.staging"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    shutil.copytree(source_dir, tmp_dir)
    # Remove release-only metadata from runtime skill dir if desired, keep manifest for audit
    if target_dir.exists():
        shutil.rmtree(target_dir)
    tmp_dir.rename(target_dir)


def approve_and_publish(
    *,
    run_id: str,
    tool_id: str,
    version: str,
    rc_dir: Path,
    target_skill_dir: Path,
    target_rule_path: Path | None,
    state: StateStore,
    approved_by: str = "human_reviewer",
) -> ReleaseRecord:
    """Approve staged release candidate and atomically publish into Antigravity target directory."""
    rc_dir = Path(rc_dir)
    release_manifest = json.loads((rc_dir / "release_manifest.json").read_text(encoding="utf-8"))
    manifest_hash = release_manifest["release_manifest_hash"]
    release_id = f"{tool_id}-{version}"

    state.transition(
        run_id,
        stage="approval",
        status="APPROVED",
        payload={"approved_by": approved_by, "version": version},
    )

    # Mark any previously published release as superseded
    for prev in state.list_releases(tool_id=tool_id):
        if prev["status"] == "PUBLISHED" and prev["release_id"] != release_id:
            state.update_release_status(prev["release_id"], "SUPERSEDED")

    _atomic_deploy_dir(rc_dir, target_skill_dir)
    if target_rule_path is not None:
        gemini_src = rc_dir / "GEMINI.md"
        if gemini_src.is_file():
            target_rule_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_rule = target_rule_path.with_suffix(".md.tmp")
            tmp_rule.write_text(gemini_src.read_text(encoding="utf-8"), encoding="utf-8")
            tmp_rule.replace(target_rule_path)

    state.save_release(
        release_id=release_id,
        tool_id=tool_id,
        version=version,
        run_id=run_id,
        manifest_hash=manifest_hash,
        status="PUBLISHED",
        target_path=str(target_skill_dir),
        manifest=release_manifest,
    )
    state.transition(
        run_id,
        stage="publish",
        status="PUBLISHED",
        artifact_refs={
            "published_skill_dir": str(target_skill_dir),
            **({"published_rule_path": str(target_rule_path)} if target_rule_path else {}),
        },
        summary={"published_version": version, "approved_by": approved_by},
    )
    return ReleaseRecord(
        release_id=release_id,
        version=version,
        status="PUBLISHED",
        release_dir=rc_dir,
        target_skill_dir=target_skill_dir,
        target_rule_path=target_rule_path,
        manifest_hash=manifest_hash,
    )


def rollback_release(
    *,
    tool_id: str,
    target_version: str | None,
    releases_root: Path,
    target_skill_dir: Path,
    target_rule_path: Path | None,
    state: StateStore,
) -> dict[str, Any]:
    """Atomically restore an earlier verified release snapshot."""
    releases = state.list_releases(tool_id=tool_id)
    chosen = None
    if target_version:
        chosen = next((r for r in releases if r["version"] == target_version), None)
    else:
        chosen = next((r for r in releases if r["status"] in ("SUPERSEDED", "ROLLED_BACK")), None)

    if chosen is None:
        return {"ok": False, "reason": "no previous verified release found for rollback"}

    snap_dir = Path(releases_root) / f"{tool_id}-{chosen['version']}"
    if not snap_dir.is_dir():
        return {"ok": False, "reason": f"missing release snapshot directory: {snap_dir}"}

    for r in releases:
        if r["status"] == "PUBLISHED":
            state.update_release_status(r["release_id"], "ROLLED_BACK")

    _atomic_deploy_dir(snap_dir, target_skill_dir)
    if target_rule_path is not None and (snap_dir / "GEMINI.md").is_file():
        target_rule_path.write_text((snap_dir / "GEMINI.md").read_text(encoding="utf-8"), encoding="utf-8")

    state.update_release_status(chosen["release_id"], "PUBLISHED")
    return {
        "ok": True,
        "restored_release_id": chosen["release_id"],
        "version": chosen["version"],
        "target_skill_dir": str(target_skill_dir),
    }
