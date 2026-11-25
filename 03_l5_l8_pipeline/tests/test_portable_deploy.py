"""Self-contained portable verification tests for deploy/ (L1-L4 extractor, L5-L8 pipeline, Console)."""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parents[2]
if str(DEPLOY_ROOT) not in sys.path:
    sys.path.insert(0, str(DEPLOY_ROOT))

from skill_factory.contracts.base_extractor import build_standard_contract, derive_schema_constraints  # noqa: E402
from skill_factory.contracts.registry import AdapterRegistry, OracleRegistry  # noqa: E402
from skill_factory.contracts.schema import CapabilitySpec, OracleVerdict, RunContext, validate_contract_meta_schema  # noqa: E402
from skill_factory.dashboard.server import create_dashboard_server  # noqa: E402
from skill_factory.engine.orchestration import build_skill_pipeline  # noqa: E402
from skill_factory.engine.provider import LocalDeterministicProvider  # noqa: E402
from skill_factory.engine.release import approve_and_publish, rollback_release  # noqa: E402
from skill_factory.engine.state import StateStore  # noqa: E402


class _DemoAdapter:
    adapter_id = "demo_cli"

    def execute(self, capability_id: str, validated_input: dict, run_context: RunContext) -> dict:
        run_context.workspace_dir.mkdir(parents=True, exist_ok=True)
        if capability_id == "ping":
            name = validated_input.get("name")
            if not isinstance(name, str) or not name.strip():
                return {"ok": False, "capability_id": capability_id, "output": None, "error": "invalid name", "workspace_dir": str(run_context.workspace_dir)}
            return {"ok": True, "capability_id": capability_id, "output": {"greeting": f"hello {name}"}, "error": None, "workspace_dir": str(run_context.workspace_dir)}
        return {"ok": False, "capability_id": capability_id, "output": None, "error": f"unknown capability {capability_id}", "workspace_dir": str(run_context.workspace_dir)}

    def status(self, handle: dict) -> dict:
        return {"supported": False, "status": "unsupported"}

    def cancel(self, handle: dict) -> dict:
        return {"supported": False, "cancelled": False}


def test_portable_l1_to_l8_pipeline_and_release(tmp_path):
    schema_in = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    }
    cap = CapabilitySpec(
        id="ping",
        description="Return greeting message.",
        input_schema=schema_in,
        output_schema={"type": "object", "required": ["greeting"], "properties": {"greeting": {"type": "string"}}},
        adapter_id="demo_cli",
        oracle_ids=["generic_schema_valid", "generic_error_contract"],
        source_ref="demo.py#L1",
        status="verified",
        shape="inspect",
        constraints=derive_schema_constraints(schema_in),
        examples=[{"input": {"name": "world"}, "expected_contains": {"greeting": "hello world"}}],
        failure_examples=[{"input": {"name": ""}, "error_contains": "invalid name"}],
    )
    contract = build_standard_contract(
        tool_id="demo",
        tool_version="0.1.0",
        spec_revision=1,
        source_ref="demo.py",
        default_adapter_id="demo_cli",
        capabilities=[cap],
    )
    assert validate_contract_meta_schema(contract.to_dict()) == []

    adapters = AdapterRegistry()
    adapters.register(_DemoAdapter())
    oracles = OracleRegistry()
    from skill_factory.contracts.base_oracles import register_generic_oracles
    register_generic_oracles(oracles)

    tests_dir = tmp_path / "tests"
    (tests_dir / "generated").mkdir(parents=True)
    (tests_dir / "regression").mkdir(parents=True)
    (tests_dir / "holdout").mkdir(parents=True)
    (tests_dir / "generated" / "cases.json").write_text(
        json.dumps([
            {"case_id": "g_ok", "capability_id": "ping", "input": {"name": "demo"}, "expect": {"ok": True}, "split": "generated"},
            {"case_id": "g_err", "capability_id": "ping", "input": {"name": ""}, "expect": {"ok": False}, "split": "generated"},
        ]),
        encoding="utf-8",
    )
    (tests_dir / "holdout" / "cases.json").write_text(
        json.dumps([
            {"case_id": "h_ok", "capability_id": "ping", "input": {"name": "holdout"}, "expect": {"ok": True}, "split": "holdout"},
        ]),
        encoding="utf-8",
    )

    state = StateStore(tmp_path / "factory.db")
    provider = LocalDeterministicProvider()
    target_skill = tmp_path / ".agents" / "skills" / "demo-compiled"
    target_rule = tmp_path / ".agents" / "rules" / "demo-compiled.md"
    releases_root = tmp_path / "releases"

    res = build_skill_pipeline(
        contract_data=contract,
        adapters=adapters,
        oracles=oracles,
        synth_provider=provider,
        judge_provider=provider,
        refine_provider=provider,
        state=state,
        knowledge_dir=tmp_path / "k",
        workflows_dir=tmp_path / "w",
        tests_dir=tests_dir,
        build_dir=tmp_path / "skills",
        runs_dir=tmp_path / "runs",
        releases_root=releases_root,
        target_skill_dir=target_skill,
        target_rule_path=target_rule,
        auto_approve=True,
    )
    assert res.ok is True
    assert res.status == "PUBLISHED"
    assert res.release_record is not None and res.release_record.version == "0.1.1"
    assert (target_skill / "SKILL.md").is_file()
    assert target_rule.is_file()

    # Rollback test with second release
    res2 = build_skill_pipeline(
        contract_data=contract,
        adapters=adapters,
        oracles=oracles,
        synth_provider=provider,
        judge_provider=provider,
        refine_provider=provider,
        state=state,
        knowledge_dir=tmp_path / "k",
        workflows_dir=tmp_path / "w",
        tests_dir=tests_dir,
        build_dir=tmp_path / "skills",
        runs_dir=tmp_path / "runs",
        releases_root=releases_root,
        target_skill_dir=target_skill,
        target_rule_path=target_rule,
        version="0.1.2",
        auto_approve=False,
    )
    assert res2.ok and res2.release_candidate
    approve_and_publish(
        run_id=res2.run_id,
        tool_id="demo",
        version="0.1.2",
        rc_dir=releases_root / "demo-0.1.2",
        target_skill_dir=target_skill,
        target_rule_path=target_rule,
        state=state,
    )
    rb = rollback_release(
        tool_id="demo",
        target_version="0.1.1",
        releases_root=releases_root,
        target_skill_dir=target_skill,
        target_rule_path=target_rule,
        state=state,
    )
    assert rb["ok"] is True


def test_portable_console_endpoints_and_share_report():
    srv = create_dashboard_server(host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        with urllib.request.urlopen(f"{base}/api/health", timeout=5) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True
        with urllib.request.urlopen(f"{base}/api/tools", timeout=5) as resp:
            assert isinstance(json.loads(resp.read().decode("utf-8"))["tools"], list)
        with urllib.request.urlopen(f"{base}/api/overview", timeout=5) as resp:
            ov = json.loads(resp.read().decode("utf-8"))
            assert "runs" in ov
        with urllib.request.urlopen(f"{base}/", timeout=5) as resp:
            assert "Skill Factory Console" in resp.read().decode("utf-8")
        with urllib.request.urlopen(f"{base}/share-report", timeout=5) as resp:
            html = resp.read().decode("utf-8")
            assert "<!doctype html>" in html.lower()
            assert "L5–L8" in html
    finally:
        srv.shutdown()
        srv.server_close()
