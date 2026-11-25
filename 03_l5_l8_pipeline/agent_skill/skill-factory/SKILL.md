---
name: skill-factory
description: >-
  Run and operate the L1-L8 Skill Lifecycle Engineering Pipeline (`skill_factory/`) for any arbitrary target tool
  (source-accessible CLI/library, blackbox EDA/vendor tool, code reviewer, or MCP server).
  Use when onboarding a new tool, upgrading an existing tool, building/validating a compiled skill package with `examples/`,
  or generating verification reports.
---

# Skill Factory Operator Guide (`skill_factory/`)

## 1. Provider Connection Rules (Choose Any Runtime Model)
- **Subscription CLI (`agy`)**: `--provider agy` (or `SKILL_FACTORY_PROVIDER=agy`).
- **Company / External LLM Endpoint**: `--provider external` (or `SKILL_FACTORY_PROVIDER=external`) with `SKILL_FACTORY_LLM_URL`, `SKILL_FACTORY_LLM_KEY`, `SKILL_FACTORY_LLM_MODEL`.
- **Deterministic Offline Check**: `--provider local`.

## 2. Standard Onboarding & Execution Order (Works for Any Tool Structure)
1. **Extract L1-L4 Contract (`partial` -> `verified`)**:
   - Standard MCP server: `python -m skill_factory.cli onboard-mcp --tool <tool_id> --command <cmd>`
   - Arbitrary docs (`knowledge/*.md` prose, `reference/`, `docs/`, CLI `--help`, blackbox vendor specs):
     `python -m skill_factory.cli onboard-ai --tool <tool_id> --docs <files_or_dirs> --docs-dir <root_doc_dir> --provider <provider>`
   - Implement `ToolAdapter` (`execute`, `status`, `cancel`) + L4 domain oracles (or use built-in `generic_schema_valid` / `generic_error_contract`), verify with real calls, and promote `status` to `"verified"`.
2. **Preserve Original Knowledge & Generate `examples/`**:
   - `compile.py` preserves ANY `source_docs_dir` layout (`knowledge/`, `reference/`, `docs/`, `examples/`, prose `.md`) untouched without forcing a rigid folder layout, AND generates `examples/verified_scenarios.md`, `examples/tool_examples.md`, and `examples/verified_execution.md`.
   - Never strip existing `Output schema`, `Result contains`, or `When it fails` examples when compiling.
3. **Run L5-L8 Pipeline & Closed-Loop Verification**:
   - Run `python -m skill_factory.cli run --tool <tool_id> --version <ver> --provider <provider>`
   - Never report completion based solely on exit code 0 or unit tests alone; require `status == "RELEASE_CANDIDATE"` (`main_eval` + `holdout_eval` 100%, `false_completions == 0`).
4. **Tool Upgrade & Rule Collision Check**:
   - Re-run extraction on upgraded tools; inspect `detect_upgraded_impact()` (`capability_hashes` diff) for impacted rules.
   - Use explicit `"supersedes": ["OLD_RULE_ID"]` when replacing outdated directives, and run BOTH existing regression/holdout suites and new test cases before approving publication (`cli approve` or `rollback`).

## 3. Concrete Examples
- [MCP & Arbitrary Knowledge/Prose Onboarding Example](examples/example_workflows.md)
