"""Read-only L1-L4 contract extractor for OSAW 0.1.1 (never modifies OSAW source)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from osaw._rrea_runtime import load_manifest

from ..base_extractor import build_standard_contract, derive_schema_constraints
from ..schema import CapabilitySpec, Contract, ExecutionControl


PROJECT_ROOT = Path(__file__).resolve().parents[3]
OSAW_DIR = PROJECT_ROOT / "OSAW-0.1.1"
OSAW_REF_DIR = OSAW_DIR / "skills" / "osaw" / "reference"
DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.json"

_DOMAIN_ASSUMPTION_CONSTRAINTS: dict[str, list[dict[str, Any]]] = {
    "stat_t_test": [
        {
            "id": "C-ASSUMP-NORMALITY",
            "rule": "Before interpreting parametric t-test results, check group sample counts (tabulate value_counts) and normality (stat_normality / qq_plot).",
        }
    ],
    "stat_process_capability": [
        {
            "id": "C-ASSUMP-CAPABILITY",
            "rule": "Check normality (stat_normality) before interpreting Cp/Cpk; LSL/USL must come from user or spec, never guessed.",
        }
    ],
}


def _extract_l2_constraints(
    tool_id: str,
    input_schema: dict[str, Any],
    ref: dict[str, Any],
) -> list[dict[str, Any]]:
    """Derive L2 constraints dynamically from manifest JSON schemas and reference docs."""
    constraints = list(_DOMAIN_ASSUMPTION_CONSTRAINTS.get(tool_id, []))
    props = input_schema.get("properties", {})
    defs = input_schema.get("$defs", {})
    schema_constraints = derive_schema_constraints(input_schema)
    schema_ids = {c["id"] for c in schema_constraints}

    # 1. Extract half-open filter interval constraint directly from $defs.filter schema
    if "filters" in props or "filter" in defs:
        filter_desc = defs.get("filter", {}).get("description", "")
        if "last: true" in filter_desc or "[start, end)" in filter_desc:
            constraints.append(
                {
                    "id": "C-FILTER-RANGE-HALF-OPEN",
                    "rule": "filters[].kind='range' is half-open [start, end); set last=true whenever end equals the column maximum. Same-column rules OR; different columns AND.",
                }
            )

    # 2. Extract plot params nesting & operation enum count from plot schema
    if tool_id == "plot":
        params_desc = props.get("params", {}).get("description", "")
        op_count = len(props.get("operation", {}).get("enum", []))
        if "inside params" in params_desc or "C-SCHEMA-NO-EXTRA-PROPS" in schema_ids:
            constraints.append(
                {
                    "id": "C-PLOT-PARAMS-NESTING",
                    "rule": "All plot options (axis, sampling, caption, group_x, group_y, include_image, bins, x, y, color, group) MUST sit inside params, never at top level.",
                }
            )
        if op_count:
            constraints.append(
                {
                    "id": "C-PLOT-17-OPS",
                    "rule": f"Only {op_count} enumerated plot operations are supported (no violin, pareto, pie, or moving-range panel).",
                }
            )

    # 3. Extract UI link constraints from reference summary & failure cases
    summary = ref.get("summary", "")
    if tool_id == "ui_open" and "never starts" in summary:
        constraints.append(
            {
                "id": "C-UI-OPEN-NO-SPAWN",
                "rule": "ui_open never spawns a server itself; when status != running, execute the returned command in the server's cwd then call ui_open again.",
            }
        )
    if tool_id == "ui_show":
        if "tab graph" in summary or "Graph Builder" in summary:
            constraints.append(
                {
                    "id": "C-UI-SHOW-TAB-GRAPH",
                    "rule": "tab='graph' requires dataset_id and a result_id whose kind is 'plot' (non-plot stat results fail).",
                }
            )
        if "One-way" in summary:
            constraints.append(
                {
                    "id": "C-UI-SHOW-ONE-WAY",
                    "rule": "ui_show writes workspace/ui/show.json one-way; agent cannot observe the screen directly.",
                }
            )

    return constraints


def _parse_reference_docs(ref_dir: Path) -> dict[str, dict[str, Any]]:
    """Extract tool summary, shape, effects, success/failure examples from OSAW reference/*.md."""
    parsed: dict[str, dict[str, Any]] = {}
    for md_path in sorted(ref_dir.glob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        sections = re.split(r"^### `([^`]+)`\n", text, flags=re.M)
        for idx in range(1, len(sections), 2):
            tool_id = sections[idx].strip()
            body = sections[idx + 1]

            paragraphs = [line.strip() for line in body.strip().splitlines() if line.strip()]
            summary = paragraphs[0] if paragraphs else tool_id

            m_shape = re.search(r"^- Shape:\s*(.+)$", body, flags=re.M)
            m_effects = re.search(r"^- Effects:\s*(.+)$", body, flags=re.M)
            shape = m_shape.group(1).strip() if m_shape else "unknown"
            effects = [e.strip() for e in m_effects.group(1).split(",")] if m_effects else []

            examples: list[dict[str, Any]] = []
            m_succ = re.search(r"A call that succeeds:\s*\n```json\n(.*?)\n```", body, flags=re.S)
            m_res = re.search(r"Its result contains:\s*\n```json\n(.*?)\n```", body, flags=re.S)
            if m_succ:
                try:
                    ex_in = json.loads(m_succ.group(1))
                    ex_out = json.loads(m_res.group(1)) if m_res else {}
                    examples.append({"input": ex_in, "expected_contains": ex_out})
                except json.JSONDecodeError:
                    pass

            failure_examples: list[dict[str, Any]] = []
            for raw_in, expected_sub in re.findall(
                r"- Input `([^`]+)` fails(?: with a message containing `([^`]+)`)?", body
            ):
                try:
                    parsed_in = json.loads(raw_in)
                    failure_examples.append(
                        {"input": parsed_in, "error_contains": expected_sub or ""}
                    )
                except json.JSONDecodeError:
                    continue

            parsed[tool_id] = {
                "summary": summary,
                "shape": shape,
                "effects": effects,
                "examples": examples,
                "failure_examples": failure_examples,
                "source_file": str(md_path.relative_to(PROJECT_ROOT)),
            }
    return parsed


def _oracles_for_tool(tool_id: str, input_schema: dict[str, Any]) -> list[str]:
    oracles = ["osaw_schema_valid", "osaw_error_contract", "osaw_numeric_match"]
    if tool_id in ("plot", "report_generate"):
        oracles.append("osaw_file_artifact")
    if tool_id in ("ui_open", "ui_show"):
        oracles.append("osaw_ui_directive")
    if "filters" in input_schema.get("properties", {}) or tool_id == "plot":
        oracles.append("osaw_invariants")
    return oracles


def _dependencies_for_tool(tool_id: str, input_schema: dict[str, Any]) -> list[str]:
    deps: list[str] = []
    req = set(input_schema.get("required", []))
    props = input_schema.get("properties", {})
    if "dataset_id" in req or "dataset_id" in props:
        if tool_id not in ("dataset_load_sample", "dataset_load_file", "dataset_create"):
            deps.append("dataset_load_sample|dataset_load_file|dataset_create")
    if tool_id == "ui_show":
        deps.append("ui_open")
    if tool_id in ("report_generate", "project_save"):
        deps.append("stat_*|plot|tabulate")
    if tool_id == "project_load":
        deps.append("project_save")
    return deps


def extract_contract(output_path: Path | None = None) -> Contract:
    """Build normalized L1-L4 Contract from OSAW manifest and verified reference docs."""
    manifest = load_manifest()
    ref_docs = _parse_reference_docs(OSAW_REF_DIR)
    ui_binding_map = {b["id"]: b for b in manifest.get("ui_bindings", [])}

    delivery = manifest.get("delivery", {})
    tool_version = str(delivery.get("version", "0.1.1"))
    spec_rev = manifest.get("spec_revision", 0)

    capabilities: list[CapabilitySpec] = []
    for tool_id, raw in manifest.get("tools", {}).items():
        ref = ref_docs.get(tool_id, {})
        input_schema = raw["input_schema"]
        output_schema = raw["output_schema"]
        shape = ref.get("shape", "unknown")
        destructive = bool(raw.get("destructive", False))

        constraints = _extract_l2_constraints(tool_id, input_schema, ref)

        # OSAW mutating/stat tools accept Context cancellation; ui_open / simple inspection are idempotent.
        idempotent = shape in ("list", "inspect") or tool_id in ("ui_open",)
        supports_cancel = shape in ("mutate", "workflow")

        exec_ctrl = ExecutionControl(
            timeout_s=60.0 if shape == "workflow" else 30.0,
            supports_async_job=tool_id == "ui_open",
            supports_status=tool_id in ("ui_open", "ui_show"),
            supports_cancel=supports_cancel,
            supports_resume=False,
            idempotent=idempotent,
            concurrency_limit=1 if destructive else 4,
            retry_policy="idempotent_only" if idempotent else "never_on_side_effect",
        )

        # Use osaw_core by default for fast isolated execution; UI capabilities explicitly tag ui binding too.
        adapter_id = "osaw_ui" if tool_id in ("ui_open", "ui_show") else "osaw_core"
        source_ref = f"osaw:{tool_version}:spec_rev_{spec_rev}#{tool_id}"
        if ref.get("source_file"):
            source_ref = f"{source_ref}|{ref['source_file']}"

        capabilities.append(
            CapabilitySpec(
                id=tool_id,
                description=ref.get("summary") or raw.get("summary") or tool_id,
                input_schema=input_schema,
                output_schema=output_schema,
                adapter_id=adapter_id,
                oracle_ids=_oracles_for_tool(tool_id, input_schema),
                source_ref=source_ref,
                status="verified",
                shape=shape,
                effects=ref.get("effects", []),
                destructive=destructive,
                constraints=constraints,
                dependencies=_dependencies_for_tool(tool_id, input_schema),
                execution_control=exec_ctrl,
                examples=ref.get("examples", []),
                failure_examples=ref.get("failure_examples", []),
                ui_binding=ui_binding_map.get(tool_id),
            )
        )

    return build_standard_contract(
        tool_id=manifest.get("app_id", "osaw"),
        tool_version=tool_version,
        spec_revision=spec_rev,
        source_ref=f"OSAW-{tool_version}/osaw-0.1.1-py3-none-any.whl#osaw/_rrea_manifest.json",
        default_adapter_id="osaw_core",
        capabilities=capabilities,
        entrypoints=delivery.get("entrypoints", []),
        auth_requirements={
            "kind": "local_stdio_and_localhost_http",
            "network_exposure": "127.0.0.1 only",
            "license_required": False,
        },
        environment_checks=manifest.get("env_checks", []),
        golden_scenarios=manifest.get("scenario_cases", []),
        source_docs_dir="OSAW-0.1.1/skills/osaw",
        error_oracle_id="osaw_error_contract",
        skill_config={
            "description": (
                f"Compiled OSAW statistical & quality workbench skill (v{tool_version}, spec {spec_rev}). "
                "Use when loading CSV/XLSX tables, running descriptive/inferential/multivariate/quality analyses, "
                "creating plots, synchronizing with the OSAW Web UI (ui_open, ui_show, history_list), or writing HTML reports."
            ),
            "execution_notes": [
                "MCP already provides every tool's input schema (`~/.gemini/antigravity/mcp/osaw/`). **Do NOT pre-read all preserved reference files**; open ONLY the single reference file needed for output schemas, verified results, failure cases, or parameter tables.",
                "Prefer calling `dataset_profile` once per newly loaded dataset and checking `modeling_suggestions` before statistical routing (`platform_fit_y_by_x`).",
                "Never shell out (`ls`) to relative `workspace/...` paths from the project root — the OSAW MCP server runs inside `osaw_workspace/`, so rely directly on MCP tool outputs (`bytes`, `path`, `result_id`, `seq`).",
            ],
            "rule_id_hints": "e.g. R-FILT-01, R-PLOT-01, R-ASSUMP-01, R-UI-01, R-UI-02, R-UI-03, R-SPEC-01, R-MODEL-01, R-NO-FAKE-01, R-LANG-01, R-NUM-01",
            "extra_raw_docs": ["OSAW-0.1.1/claude/CLAUDE.md"],
        },
        status="verified",
        output_path=output_path or DEFAULT_CONTRACT_PATH,
    )


def load_or_extract_contract(contract_path: Path | None = None) -> Contract:
    path = contract_path or DEFAULT_CONTRACT_PATH
    if not path.is_file():
        return extract_contract(path)
    return Contract.from_dict(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    c = extract_contract()
    print(f"Extracted {c.tool_id} v{c.tool_version} (spec {c.spec_revision}): {len(c.capabilities)} capabilities")
