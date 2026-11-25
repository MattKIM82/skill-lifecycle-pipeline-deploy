"""Tool-agnostic local HTTP server for the Skill Factory monitoring, diff, benchmark, and release-gate UI."""

from __future__ import annotations

import json
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..cli import (
    PROJECT_ROOT,
    default_paths,
    discover_onboarded_tools,
    load_contract_for_tool,
    resolve_tool_id,
    run_build,
)
from ..contracts.registry import default_registries
from ..engine.orchestration import build_skill_pipeline
from ..engine.provider import resolve_provider
from ..engine.release import approve_and_publish, derive_next_skill_version, rollback_release
from ..engine.state import StateStore
from ..engine.synthesize import generate_candidate


STATIC_DIR = Path(__file__).resolve().parent / "static"


def _read_text_safe(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _read_json_safe(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _select_tool_id(requested: str | None) -> str | None:
    if requested:
        return requested
    tools = discover_onboarded_tools()
    if tools:
        return tools[0]
    return None


def _read_raw_baseline_docs(contract: Any) -> dict[str, str]:
    """Load baseline docs from contract.source_docs_dir / skill_config without tool hardcoding."""
    raw_skill = ""
    raw_extra = ""
    if contract.source_docs_dir:
        src = Path(contract.source_docs_dir)
        if not src.is_absolute():
            src = PROJECT_ROOT / src
        if src.is_dir():
            raw_skill = _read_text_safe(src / "SKILL.md")
            if not raw_skill:
                md_files = sorted(src.rglob("*.md"))
                if md_files:
                    raw_skill = _read_text_safe(md_files[0])
    extra_docs = contract.skill_config.get("extra_raw_docs", []) if isinstance(contract.skill_config, dict) else []
    if extra_docs:
        p = Path(extra_docs[0])
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        raw_extra = _read_text_safe(p)
    return {"raw_extra_md": raw_extra, "raw_skill_md": raw_skill}


def _build_overview_payload(tool_id: str | None = None) -> dict[str, Any]:
    tid = _select_tool_id(tool_id)
    tools = discover_onboarded_tools()
    if not tid:
        return {
            "empty": True,
            "tools": [],
            "tool_id": None,
            "tool_version": "-",
            "recommended_release_version": "-",
            "spec_revision": "-",
            "capabilities_count": 0,
            "constraints_count": 0,
            "ui_bindings_count": 0,
            "runs": [],
            "latest_run": None,
            "latest_main_eval": None,
            "latest_holdout_eval": None,
            "failure_corpus": [],
            "releases": [],
            "benchmarks": [],
            "diff": {"raw_extra_md": "", "raw_skill_md": "", "compiled_skill_md": "", "compiled_rules_md": "", "compiled_manifest": None},
        }
    paths = default_paths(tid)
    contract = load_contract_for_tool(tid, paths["contract"])
    state = StateStore(paths["db"])
    runs = state.list_runs(tool_id=tid, limit=20)
    releases = state.list_releases(tool_id=tid)
    benchmarks = state.list_benchmarks(tool_id=tid)

    latest_run = state.get_run(runs[0]["run_id"]) if runs else None
    latest_main_eval = None
    latest_holdout_eval = None
    failure_corpus: list[dict[str, Any]] = []
    if latest_run:
        run_dir = paths["runs"] / latest_run["run_id"]
        attempt = max(1, int(latest_run.get("attempt") or 1))
        latest_main_eval = _read_json_safe(run_dir / f"attempt_{attempt}" / "main" / "eval_report.json")
        latest_holdout_eval = _read_json_safe(run_dir / f"attempt_{attempt}" / "holdout" / "eval_report.json")
        fc_path = run_dir / "failure_cases.jsonl"
        if fc_path.is_file():
            failure_corpus = [
                json.loads(line) for line in fc_path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]

    baseline_docs = _read_raw_baseline_docs(contract)
    compiled_skill = _read_text_safe(paths["target_skill"] / "SKILL.md") or _read_text_safe(
        paths["build"] / tid / "SKILL.md"
    )
    compiled_rules = _read_text_safe(paths["target_rule"]) or _read_text_safe(
        paths["build"] / tid / "GEMINI.md"
    )
    compiled_manifest = _read_json_safe(paths["target_skill"] / "skill_manifest.json") or _read_json_safe(
        paths["build"] / tid / "skill_manifest.json"
    )

    return {
        "tools": tools,
        "tool_id": contract.tool_id,
        "tool_version": contract.tool_version,
        "recommended_release_version": derive_next_skill_version(contract.tool_version),
        "spec_revision": contract.spec_revision,
        "capabilities_count": len(contract.capabilities),
        "constraints_count": sum(len(c.constraints) for c in contract.capabilities),
        "ui_bindings_count": sum(1 for c in contract.capabilities if c.ui_binding),
        "runs": runs,
        "latest_run": latest_run,
        "latest_main_eval": latest_main_eval,
        "latest_holdout_eval": latest_holdout_eval,
        "failure_corpus": failure_corpus,
        "releases": releases,
        "benchmarks": benchmarks,
        "diff": {
            **baseline_docs,
            "compiled_skill_md": compiled_skill,
            "compiled_rules_md": compiled_rules,
            "compiled_manifest": compiled_manifest,
        },
    }


def _run_defect_demo(tool_id: str | None = None, version: str | None = None) -> dict[str, Any]:
    """Run pipeline with an injected defect (disabling first skill-checked rule) to test closed-loop refine."""
    tid = _select_tool_id(tool_id)
    paths = default_paths(tid)
    contract = load_contract_for_tool(tid, paths["contract"])
    adapters, oracles = default_registries()
    state = StateStore(paths["db"])
    provider = resolve_provider("local")

    defective = generate_candidate(
        contract,
        provider,
        knowledge_dir=paths["knowledge"],
        workflows_dir=paths["workflows"],
    )
    # Pick the first mandatory rule referenced by a skill_check test (or first mandatory rule generically)
    target_rule = None
    reg_dir = paths["tests"] / "regression"
    if reg_dir.is_dir():
        for p in sorted(reg_dir.glob("*.json")):
            data = json.loads(p.read_text(encoding="utf-8"))
            cases = data if isinstance(data, list) else data.get("cases", [])
            for c in cases:
                if c.get("kind") == "skill_check" and c.get("requirement_id"):
                    target_rule = c["requirement_id"]
                    break
            if target_rule:
                break
    if not target_rule:
        for r in defective.knowledge.get("rules", []):
            if r.get("mandatory") and r.get("rule_id"):
                target_rule = r["rule_id"]
                break
    if target_rule:
        defective.disabled_rules.append(target_rule)

    demo_ver = version or f"{derive_next_skill_version(contract.tool_version)}-demo"
    res = build_skill_pipeline(
        contract_data=contract,
        adapters=adapters,
        oracles=oracles,
        synth_provider=provider,
        judge_provider=provider,
        refine_provider=provider,
        state=state,
        knowledge_dir=paths["knowledge"],
        workflows_dir=paths["workflows"],
        tests_dir=paths["tests"],
        build_dir=paths["build"],
        runs_dir=paths["runs"],
        releases_root=paths["releases"],
        target_skill_dir=paths["target_skill"],
        target_rule_path=paths["target_rule"],
        version=demo_ver,
        auto_approve=False,
        candidate_override=defective,
    )
    return {
        "ok": res.ok,
        "run_id": res.run_id,
        "status": res.status,
        "version": demo_ver,
        "disabled_rule": target_rule,
        "attempts": res.attempts,
        "patches_applied": res.patches_applied,
        "release_candidate": res.release_candidate,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP API & SPA handler for Skill Factory Dashboard."""

    server_version = "SkillFactoryDashboard/0.2"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, path: Path) -> None:
        raw = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        req_tool = qs.get("tool", [None])[0]

        if path in ("/", "/index.html"):
            self._send_html(STATIC_DIR / "index.html")
            return
        if path in ("/share-report", "/share-report.html"):
            for folder_name in ("05_reports", "docs"):
                docs_dir = PROJECT_ROOT / folder_name
                candidates = sorted(docs_dir.glob("*skill_pipeline_shareable_report.html")) if docs_dir.is_dir() else []
                if candidates:
                    self._send_html(candidates[0])
                    return
            self._send_json({"error": "shareable report not found"}, status=404)
            return
        if path == "/api/health":
            self._send_json({"ok": True, "pid": os.getpid(), "ts": time.time()})
            return
        if path == "/api/tools":
            self._send_json({"tools": discover_onboarded_tools()})
            return
        if path == "/api/overview":
            self._send_json(_build_overview_payload(req_tool))
            return
        if path == "/api/contract":
            tid = _select_tool_id(req_tool)
            if not tid:
                self._send_json({"capabilities": []})
                return
            contract = load_contract_for_tool(tid, default_paths(tid)["contract"])
            self._send_json(contract.to_dict())
            return
        if path == "/api/knowledge":
            tid = _select_tool_id(req_tool)
            if not tid:
                self._send_json({"tool_id": None, "knowledge": None, "policy": None, "intent_catalog": None, "workflows": None, "dependency_graph": None})
                return
            paths = default_paths(tid)
            self._send_json(
                {
                    "tool_id": tid,
                    "knowledge": _read_json_safe(paths["knowledge"] / "knowledge.json"),
                    "policy": _read_json_safe(paths["knowledge"] / "policy.json"),
                    "intent_catalog": _read_json_safe(paths["knowledge"] / "intent_catalog.json"),
                    "workflows": _read_json_safe(paths["workflows"] / "workflow.json"),
                    "dependency_graph": _read_json_safe(paths["workflows"] / "dependency_graph.json"),
                }
            )
            return
        if path.startswith("/api/runs/"):
            tid = _select_tool_id(req_tool)
            run_id = path.removeprefix("/api/runs/")
            paths = default_paths(tid)
            run = StateStore(paths["db"]).get_run(run_id)
            if not run:
                self._send_json({"error": f"unknown run_id: {run_id}"}, status=404)
                return
            self._send_json(run)
            return
        if path == "/api/benchmarks":
            tid = _select_tool_id(req_tool)
            paths = default_paths(tid)
            self._send_json({"tool_id": tid, "benchmarks": StateStore(paths["db"]).list_benchmarks(tid)})
            return

        self._send_json({"error": f"not found: {path}"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        body = self._read_body_json()
        try:
            if path == "/api/pipeline/run":
                tid = _select_tool_id(body.get("tool_id"))
                if body.get("inject_defect"):
                    out = _run_defect_demo(tool_id=tid, version=body.get("version"))
                else:
                    out = run_build(
                        tool_id=tid,
                        version=body.get("version"),
                        provider_mode=body.get("provider", "local"),
                        auto_approve=bool(body.get("auto_approve", False)),
                        from_raw=bool(body.get("from_raw", False)),
                    )
                self._send_json(out)
                return

            if path == "/api/release/approve":
                tid = _select_tool_id(body.get("tool_id"))
                run_id = body["run_id"]
                paths = default_paths(tid)
                ver = body.get("version") or derive_next_skill_version(
                    load_contract_for_tool(tid, paths["contract"]).tool_version
                )
                rec = approve_and_publish(
                    run_id=run_id,
                    tool_id=tid,
                    version=ver,
                    rc_dir=paths["releases"] / f"{tid}-{ver}",
                    target_skill_dir=paths["target_skill"],
                    target_rule_path=paths["target_rule"],
                    state=StateStore(paths["db"]),
                    approved_by=body.get("approved_by", "dashboard_reviewer"),
                )
                self._send_json(
                    {
                        "ok": True,
                        "release_id": rec.release_id,
                        "status": rec.status,
                        "target_skill_dir": str(rec.target_skill_dir),
                    }
                )
                return

            if path == "/api/release/rollback":
                tid = _select_tool_id(body.get("tool_id"))
                paths = default_paths(tid)
                out = rollback_release(
                    tool_id=tid,
                    target_version=body.get("version"),
                    releases_root=paths["releases"],
                    target_skill_dir=paths["target_skill"],
                    target_rule_path=paths["target_rule"],
                    state=StateStore(paths["db"]),
                )
                self._send_json(out, status=200 if out.get("ok") else 400)
                return

            if path == "/api/benchmark/run":
                from ..eval_benchmark.run_benchmark import run_two_way_benchmark

                tid = _select_tool_id(body.get("tool_id"))
                self._send_json(run_two_way_benchmark(tool_id=tid))
                return

            self._send_json({"error": f"not found: {path}"}, status=404)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": str(exc)}, status=400)


def create_dashboard_server(host: str = "127.0.0.1", port: int = 8790) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), DashboardHandler)


def serve_dashboard(host: str = "127.0.0.1", port: int = 8790) -> None:
    srv = create_dashboard_server(host=host, port=port)
    print(f"Skill Factory Dashboard listening on http://{host}:{srv.server_port}/", flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
