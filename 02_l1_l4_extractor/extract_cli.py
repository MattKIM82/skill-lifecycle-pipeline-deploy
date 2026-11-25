#!/usr/bin/env python3
"""Standalone CLI for L1-L4 Contract Extraction & Onboarding (02_l1_l4_extractor)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEPLOY_ROOT = Path(__file__).resolve().parents[1]
if str(DEPLOY_ROOT) not in sys.path:
    sys.path.insert(0, str(DEPLOY_ROOT))

from skill_factory.cli import default_paths, load_contract_for_tool, resolve_tool_id  # noqa: E402
from skill_factory.contracts.base_extractor import extract_from_docs_via_ai, extract_from_mcp_stdio  # noqa: E402
from skill_factory.contracts.schema import validate_contract_meta_schema  # noqa: E402
from skill_factory.engine.provider import resolve_provider  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="extract_cli", description="L1-L4 Contract Extraction & Validation CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_omcp = sub.add_parser("onboard-mcp", help="Extract L1-L4 partial contract from any stdio MCP server")
    p_omcp.add_argument("--tool", required=True)
    p_omcp.add_argument("--command", required=True)
    p_omcp.add_argument("--args", nargs="*", default=[])
    p_omcp.add_argument("--adapter-id", default=None)
    p_omcp.add_argument("--version", default="0.1.0")
    p_omcp.add_argument("--out", default=None)

    p_oai = sub.add_parser("onboard-ai", help="Draft L1-L4 partial contract from docs/CLI help via AI provider")
    p_oai.add_argument("--tool", required=True)
    p_oai.add_argument("--version", default="0.1.0")
    p_oai.add_argument("--provider", default="agy")
    p_oai.add_argument("--docs", nargs="+", required=True)
    p_oai.add_argument("--docs-dir", default="")
    p_oai.add_argument("--adapter-id", default=None)
    p_oai.add_argument("--out", default=None)

    p_ext = sub.add_parser("extract", help="Load/refresh an already onboarded tool's L1-L4 contract")
    p_ext.add_argument("--tool", default=None)

    p_val = sub.add_parser("validate", help="Validate contract.json against L1-L4 meta-schema")
    p_val.add_argument("--tool", default=None)
    p_val.add_argument("--contract", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "onboard-mcp":
        out_path = Path(args.out) if args.out else default_paths(args.tool)["contract"]
        c = extract_from_mcp_stdio(
            tool_id=args.tool,
            command=args.command,
            args=args.args,
            adapter_id=args.adapter_id,
            tool_version=args.version,
            output_path=out_path,
        )
        print(json.dumps({"tool_id": c.tool_id, "status": c.status, "capabilities": len(c.capabilities), "path": str(out_path)}, ensure_ascii=False, indent=2))
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
        print(json.dumps({"tool_id": c.tool_id, "status": c.status, "capabilities": len(c.capabilities), "path": str(out_path)}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "extract":
        tid = resolve_tool_id(args.tool)
        c = load_contract_for_tool(tid, default_paths(tid)["contract"], refresh=True)
        print(json.dumps({"tool_id": c.tool_id, "version": c.tool_version, "capabilities": len(c.capabilities)}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "validate":
        cpath = Path(args.contract) if args.contract else default_paths(resolve_tool_id(args.tool))["contract"]
        data = json.loads(cpath.read_text(encoding="utf-8"))
        errs = validate_contract_meta_schema(data)
        print(json.dumps({"ok": len(errs) == 0, "path": str(cpath), "status": data.get("status"), "errors": errs}, ensure_ascii=False, indent=2))
        return 0 if not errs else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
