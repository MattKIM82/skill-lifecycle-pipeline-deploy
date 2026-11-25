"""Tool-agnostic command-line entry point for Skill Lifecycle Engineering Factory."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path

from . import DEPLOY_ROOT, WORKSPACE_ROOT
from .contracts.base_extractor import extract_from_docs_via_ai, extract_from_mcp_stdio
from .contracts.registry import default_registries
from .contracts.schema import Contract
from .engine.orchestration import build_skill_pipeline
from .engine.provider import resolve_provider
from .engine.release import approve_and_publish, derive_next_skill_version, rollback_release
from .engine.state import StateStore


PROJECT_ROOT = DEPLOY_ROOT
FACTORY_ROOT = WORKSPACE_ROOT
TARGET_ROOT = Path(os.environ.get("SKILL_FACTORY_TARGET_ROOT", FACTORY_ROOT)).resolve()


def discover_onboarded_tools() -> list[str]:
    """Return sorted tool_ids with contract.json (excluding shared templates/internal folders)."""
    contracts_root = FACTORY_ROOT / "contracts"
    if not contracts_root.is_dir():
        return []
    tools: list[str] = []
    for child in sorted(contracts_root.iterdir()):
        if not child.is_dir() or child.name.startswith("_") or child.name in ("templates", "prompts", "examples"):
            continue
        if (child / "contract.json").is_file() or any(child.glob("extract_*_contract.py")):
            tools.append(child.name)
    return tools


def resolve_tool_id(tool_arg: str | None) -> str:
    """Resolve explicit tool_id or infer the single onboarded tool automatically."""
    if tool_arg:
        return tool_arg
    tools = discover_onboarded_tools()
    if len(tools) == 1:
        return tools[0]
    if not tools:
        raise ValueError("No onboarded tools found under workspace/contracts/. Run `onboard-mcp` or `onboard-ai` first.")
    raise ValueError(f"Multiple onboarded tools available {tools}; specify --tool explicitly.")


def default_paths(tool_id: str) -> dict[str, Path]:
    return {
        "contract": FACTORY_ROOT / "contracts" / tool_id / "contract.json",
        "knowledge": FACTORY_ROOT / "knowledge" / tool_id,
        "workflows": FACTORY_ROOT / "workflows" / tool_id,
        "tests": FACTORY_ROOT / "tests" / tool_id,
        "build": FACTORY_ROOT / "skills",
        "runs": FACTORY_ROOT / "runs",
        "releases": FACTORY_ROOT / "releases",
        "db": FACTORY_ROOT / "runs" / "factory.db",
        "target_skill": TARGET_ROOT / ".agents" / "skills" / f"{tool_id}-compiled",
        "target_rule": TARGET_ROOT / ".agents" / "rules" / f"{tool_id}-compiled.md",
    }


def _discover_tool_extractor(tool_id: str):
    """Find an optional per-tool contract extractor module dynamically without hardcoding."""
    pkg_dir = FACTORY_ROOT / "contracts" / tool_id
    if not pkg_dir.is_dir():
        return None
    for mod_file in sorted(pkg_dir.glob("extract_*_contract.py")):
        mod = importlib.import_module(f"skill_factory.contracts.{tool_id}.{mod_file.stem}")
        fn = getattr(mod, "extract_contract", None)
        if callable(fn):
            return fn
    return None


def load_contract_for_tool(tool_id: str, contract_path: Path | None = None, *, refresh: bool = False) -> Contract:
    cpath = contract_path or default_paths(tool_id)["contract"]
    if cpath.is_file() and not refresh:
        return Contract.from_dict(json.loads(cpath.read_text(encoding="utf-8")))
    extractor = _discover_tool_extractor(tool_id)
    if extractor is not None:
        return extractor(cpath)
    if cpath.is_file():
        return Contract.from_dict(json.loads(cpath.read_text(encoding="utf-8")))
    raise FileNotFoundError(
        f"Missing contract for tool {tool_id!r} at {cpath}. Run `onboard-mcp` or `onboard-ai` first."
    )


def run_build(
    *,
    tool_id: str,
    version: str | None = None,
    provider_mode: str = "local",
    auto_approve: bool = False,
    from_raw: bool = False,
) -> dict:
    paths = default_paths(tool_id)
    contract = load_contract_for_tool(tool_id, paths["contract"])
    resolved_version = version or derive_next_skill_version(contract.tool_version)
    adapters, oracles = default_registries()
    state = StateStore(paths["db"])
    provider = resolve_provider(provider_mode, role="synthesize", handoff_dir=paths["runs"] / "handoffs")
    judge_provider = resolve_provider(provider_mode, role="judge", handoff_dir=paths["runs"] / "judge_handoffs")

    res = build_skill_pipeline(
        contract_data=contract,
        adapters=adapters,
        oracles=oracles,
        synth_provider=provider,
        judge_provider=judge_provider,
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
        version=resolved_version,
        auto_approve=auto_approve,
        from_raw=from_raw,
    )
    return {
        "ok": res.ok,
        "run_id": res.run_id,
        "status": res.status,
        "version": resolved_version,
        "tool_version": contract.tool_version,
        "attempts": res.attempts,
        "blocked_reason": res.blocked_reason,
        "main_eval": res.main_report.to_dict() if res.main_report else None,
        "holdout_eval": res.holdout_report.to_dict() if res.holdout_report else None,
        "release_candidate": res.release_candidate,
        "patches_applied": res.patches_applied,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skill_factory.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="Extract or load L1-L4 contract")
    p_ext.add_argument("--tool", default=None)

    p_run = sub.add_parser("run", help="Run L5-L8 pipeline")
    p_run.add_argument("--tool", default=None)
    p_run.add_argument("--version", default=None, help="Release version (defaults to patch bump of tool_version)")
    p_run.add_argument("--provider", default="local")
    p_run.add_argument("--auto-approve", action="store_true")
    p_run.add_argument("--from-raw", action="store_true", help="Synthesize directly from contract and raw source docs")

    p_omcp = sub.add_parser("onboard-mcp", help="Extract L1-L4 partial contract from any stdio MCP server")
    p_omcp.add_argument("--tool", required=True)
    p_omcp.add_argument("--command", required=True)
    p_omcp.add_argument("--args", nargs="*", default=[])
    p_omcp.add_argument("--adapter-id", default=None)
    p_omcp.add_argument("--version", default="0.1.0")
    p_omcp.add_argument("--out", default=None)

    p_oai = sub.add_parser("onboard-ai", help="Draft L1-L4 partial contract from source docs via AI provider")
    p_oai.add_argument("--tool", required=True)
    p_oai.add_argument("--version", default="0.1.0")
    p_oai.add_argument("--provider", default="agy")
    p_oai.add_argument("--docs", nargs="+", required=True)
    p_oai.add_argument("--docs-dir", default="", help="Optional source docs root directory to preserve passthrough")
    p_oai.add_argument("--adapter-id", default=None)
    p_oai.add_argument("--out", default=None)

    p_app = sub.add_parser("approve", help="Approve and publish release candidate")
    p_app.add_argument("--tool", default=None)
    p_app.add_argument("--run-id", required=True)
    p_app.add_argument("--version", default=None)
    p_app.add_argument("--by", default="human_reviewer")

    p_rb = sub.add_parser("rollback", help="Rollback published release")
    p_rb.add_argument("--tool", default=None)
    p_rb.add_argument("--version", default=None)

    p_dash = sub.add_parser("dashboard", help="Launch Skill Factory Dashboard UI")
    p_dash.add_argument("--port", type=int, default=8790)

    p_bench = sub.add_parser("benchmark", help="Run universal 2-Way Baseline vs Compiled benchmark")
    p_bench.add_argument("--tool", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "extract":
        tid = resolve_tool_id(args.tool)
        paths = default_paths(tid)
        c = load_contract_for_tool(tid, paths["contract"], refresh=True)
        print(json.dumps({"tool_id": c.tool_id, "version": c.tool_version, "capabilities": len(c.capabilities)}))
        return 0

    if args.cmd == "run":
        tid = resolve_tool_id(args.tool)
        out = run_build(
            tool_id=tid,
            version=args.version,
            provider_mode=args.provider,
            auto_approve=args.auto_approve,
            from_raw=args.from_raw,
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["ok"] else 1

    if args.cmd == "onboard-mcp":
        paths = default_paths(args.tool)
        out_path = Path(args.out) if args.out else paths["contract"]
        c = extract_from_mcp_stdio(
            tool_id=args.tool,
            command=args.command,
            args=args.args,
            adapter_id=args.adapter_id,
            tool_version=args.version,
            output_path=out_path,
        )
        print(json.dumps({"tool_id": c.tool_id, "status": c.status, "capabilities": len(c.capabilities), "path": str(out_path)}))
        return 0

    if args.cmd == "onboard-ai":
        paths = default_paths(args.tool)
        out_path = Path(args.out) if args.out else paths["contract"]
        source_docs: dict[str, str] = {}
        for item in args.docs:
            p = Path(item)
            if p.is_file():
                source_docs[str(p)] = p.read_text(encoding="utf-8")
            elif p.is_dir():
                for child in sorted(p.rglob("*.md")):
                    source_docs[str(child)] = child.read_text(encoding="utf-8")
        provider = resolve_provider(args.provider, role="extract_contract", handoff_dir=paths["runs"] / "handoffs")
        c = extract_from_docs_via_ai(
            tool_id=args.tool,
            tool_version=args.version,
            source_docs=source_docs,
            provider=provider,
            adapter_id=args.adapter_id,
            source_docs_dir=args.docs_dir,
            output_path=out_path,
        )
        print(json.dumps({"tool_id": c.tool_id, "status": c.status, "capabilities": len(c.capabilities), "path": str(out_path)}))
        return 0

    if args.cmd == "approve":
        tid = resolve_tool_id(args.tool)
        paths = default_paths(tid)
        state = StateStore(paths["db"])
        ver = args.version
        if not ver:
            run_rec = state.get_run(args.run_id)
            ver = (run_rec or {}).get("summary", {}).get("release_version")
        if not ver:
            ver = derive_next_skill_version(load_contract_for_tool(tid, paths["contract"]).tool_version)
        rc_dir = paths["releases"] / f"{tid}-{ver}"
        rec = approve_and_publish(
            run_id=args.run_id,
            tool_id=tid,
            version=ver,
            rc_dir=rc_dir,
            target_skill_dir=paths["target_skill"],
            target_rule_path=paths["target_rule"],
            state=state,
            approved_by=args.by,
        )
        print(json.dumps({"release_id": rec.release_id, "status": rec.status, "target": str(rec.target_skill_dir)}))
        return 0

    if args.cmd == "rollback":
        tid = resolve_tool_id(args.tool)
        paths = default_paths(tid)
        state = StateStore(paths["db"])
        out = rollback_release(
            tool_id=tid,
            target_version=args.version,
            releases_root=paths["releases"],
            target_skill_dir=paths["target_skill"],
            target_rule_path=paths["target_rule"],
            state=state,
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    if args.cmd == "dashboard":
        from .dashboard.server import serve_dashboard

        serve_dashboard(port=args.port)
        return 0

    if args.cmd == "benchmark":
        from .eval_benchmark.run_benchmark import run_two_way_benchmark

        tid = resolve_tool_id(args.tool)
        report = run_two_way_benchmark(tool_id=tid)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
