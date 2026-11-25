# Skill Factory Execution Examples

## Example 1: Onboarding a Prose-Based Tool (`knowledge/` folder, No `reference/` folder)
```bash
# 1. Draft L1-L4 contract from arbitrary knowledge/*.md or docs/*.md via your chosen model
.venv/bin/python -m skill_factory.cli onboard-ai \
  --tool code_reviewer \
  --docs ./my_tool/knowledge/ \
  --docs-dir ./my_tool \
  --provider external   # or: --provider agy

# 2. Register ToolAdapter + L4 domain oracle and verify sample calls -> status="verified"
# 3. Run full L5-L8 pipeline (omit --version to auto-bump tool_version patch, e.g. 0.1.1 -> 0.1.2)
.venv/bin/python -m skill_factory.cli run --tool code_reviewer --provider external
```

## Example 2: Onboarding a Standard MCP Tool
```bash
.venv/bin/python -m skill_factory.cli onboard-mcp \
  --tool my_mcp --command /path/to/server --args "--stdio"
```

## Example 3: Expected Release Gate Output Before Reporting Done
Require:
- `"status": "RELEASE_CANDIDATE"` (or `"PUBLISHED"` after approve)
- `main_eval.required_pass == true` and `holdout_eval.required_pass == true`
- `false_completions == 0`
- Compiled bundle contains `SKILL.md`, preserved original docs, `references/capabilities.md`, and `examples/verified_execution.md`.
