# 기존/신규 툴 파이프라인 연결 표준 명세서 (`Tool Onboarding Specification`)

다른 대상 툴(CLI, Python/REST API, 코드 리뷰어, 블랙박스 사양서, 표준 MCP 서버)을 Skill Factory 파이프라인에 연결할 때 **이 문서와 대표 템플릿 코드를 대상 툴과 함께 AI(또는 개발자)에게 전달**하면 공통 엔진 코드 수정 없이 즉시 연결·추출·검증할 수 있습니다.

---

## 1. 연결 시 생성해야 할 파일 위치 규약

대상 툴 ID를 `<tool_id>`라고 할 때, 아래 폴더에 파일을 배치하면 `skill_factory`가 **자동 인식**합니다.

| 파일 경로 | 역할 | 필수 여부 |
|---|---|---|
| `skill_factory/contracts/<tool_id>/contract.json` | L1–L4 표준 계약 명세 (`status: "verified"`여야 생성 통과) | **필수** |
| `skill_factory/contracts/<tool_id>/<tool_id>_adapter.py` | `register_adapters(registry)`로 실제 툴 호출 다리 등록 | **비-MCP 툴 필수** *(표준 MCP는 `contract.json`의 `entrypoints`로 자동 등록)* |
| `skill_factory/contracts/<tool_id>/<tool_id>_oracles.py` | `register_oracles(registry)`로 도메인 정답/산출물 검증기 추가 | 선택 *(기본 `generic_schema_valid`, `generic_error_contract`, `generic_numeric_match` 내장)* |
| `skill_factory/tests/<tool_id>/generated/*.json` | 입력 스키마 정상/경계/오류 계약 테스트 | **필수** |
| `skill_factory/tests/<tool_id>/regression/*.json` | 핵심 다단계 시나리오 및 `skill_check` | 권장 |
| `skill_factory/tests/<tool_id>/holdout/*.json` | 릴리스 게이트용 숨긴 평가(Holdout) | **필수** |
| `skill_factory/knowledge/<tool_id>/knowledge.json` | 수기 확정 규칙이 이미 있는 경우(없으면 AI가 원문에서 합성) | 선택 |
| `skill_factory/workflows/<tool_id>/workflow.json` | 참고 워크플로 DAG(없으면 AI가 생성 또는 빈 목록 허용) | 선택 |

> **중요:** 기존 툴에 이미 `knowledge/`, `reference/`, `docs/`, `examples/`, 줄글 `.md` 폴더가 있다면 `contract.json`의 `source_docs_dir` 경로만 지정하세요. 컴파일러가 **원본 파일들을 손실 없이 그대로 보존**합니다.

---

## 2. `contract.json` 표준 포맷 명세

```json
{
  "schema_version": "1.0",
  "tool_id": "<tool_id>",
  "tool_version": "1.0.0",
  "spec_revision": 1,
  "source_ref": "path/or/repo#entrypoint",
  "status": "verified",
  "default_adapter_id": "<tool_id>_adapter",
  "source_docs_dir": "path/to/original_docs_root",
  "error_oracle_id": "generic_error_contract",
  "skill_config": {
    "description": "Compiled skill summary (1-2 sentences).",
    "execution_notes": [
      "Core tool usage tip 1",
      "Core tool usage tip 2"
    ]
  },
  "entrypoints": [
    {"name": "<tool_id>", "kind": "mcp-stdio|cli|api", "command": "...", "args": []}
  ],
  "capabilities": [
    {
      "id": "capability_name",
      "description": "What this capability does.",
      "shape": "list|inspect|mutate|workflow|unknown",
      "destructive": false,
      "input_schema": {
        "type": "object",
        "required": ["..."],
        "properties": {"...": {"type": "string"}},
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "required": ["..."],
        "properties": {"...": {"type": "string"}}
      },
      "adapter_id": "<tool_id>_adapter",
      "oracle_ids": ["generic_schema_valid", "generic_error_contract"],
      "source_ref": "file.py#L10 or doc.md#section",
      "status": "verified",
      "constraints": [
        {"id": "C-DOMAIN-01", "rule": "Non-obvious boundary or cross-field constraint."}
      ],
      "dependencies": ["prerequisite_capability"],
      "execution_control": {
        "timeout_s": 60.0,
        "supports_async_job": false,
        "supports_status": false,
        "supports_cancel": false,
        "supports_resume": false,
        "idempotent": true,
        "concurrency_limit": 1,
        "retry_policy": "idempotent_only|never_on_side_effect"
      },
      "examples": [
        {"input": {"...": "..."}, "expected_contains": {"...": "..."}}
      ],
      "failure_examples": [
        {"input": {"...": "bad"}, "error_contains": "expected substring"}
      ]
    }
  ],
  "golden_scenarios": []
}
```

- **메타스키마 검증:** `skill_factory/contracts/schema.py`의 `validate_contract_meta_schema()`를 통과해야 하며, `status`가 `"partial"`이면 Admission Gate에서 차단되므로 실제 호출 검증 후 `"verified"`로 저장합니다.

---

## 3. 대표 어댑터 · 오라클 코드 (복붙용)

- **어댑터 템플릿:** [tool_adapter_template.py](templates/tool_adapter_template.py)
  - 필수 인터페이스 (`ToolAdapter`):
    - `execute(capability_id, validated_input, run_context) -> {"ok": bool, "output": Any, "error": str | None, "workspace_dir": str, ...}`
    - `status(handle) -> dict` (미지원 시 `{"supported": False, "status": "unsupported"}`)
    - `cancel(handle) -> dict` (미지원 시 `{"supported": False, "cancelled": False}`)
  - `register_adapters(registry)` 함수를 파일 하단에 두면 `default_registries()`가 **자동 등록**합니다.
- **오라클 템플릿:** [oracles_template.py](templates/oracles_template.py)
  - `register_oracles(registry)` 함수로 `OracleFn(evidence, case) -> OracleVerdict` 등록.
- **표준 MCP 서버인 경우:** `onboard-mcp` 실행 후 `entrypoints[0].kind == "mcp-stdio"`가 있으면 내장 [McpStdioAdapter](base_adapter.py)가 자동 연결되므로 별도 `_adapter.py` 없이도 실행됩니다.

---

## 4. 다른 툴 작업 시 AI에게 그대로 전달하는 요청 프롬프트 (복붙용)

아래 프롬프트의 `<...>` 부분만 채워 `TOOL_ONBOARDING_SPEC.md`, `tool_adapter_template.py`, `oracles_template.py`와 함께 AI에게 전달하세요.

```text
첨부한 TOOL_ONBOARDING_SPEC.md 규격과 tool_adapter_template.py / oracles_template.py 템플릿에 맞춰
대상 툴(<tool_id>)을 Skill Factory 파이프라인에 연결해 줘.

- 대상 툴 경로 / 문서: <대상 툴 소스, CLI --help, 벤더 문서, knowledge/ 또는 MCP 실행 커맨드>
- 기존 문서 폴더(보존 대상): <source_docs_dir 경로 또는 없음>

수행할 작업:
1. 대상 툴의 실제 기능/입출력 스키마/제약/성공·실패 예시를 분석해
   skill_factory/contracts/<tool_id>/contract.json을 작성해 줘.
   (validate_contract_meta_schema 통과 확인, 근거 불명확 항목은 추측하지 말 것)
2. MCP 서버가 아니라면 템플릿을 참고해
   skill_factory/contracts/<tool_id>/<tool_id>_adapter.py와 필요 시 <tool_id>_oracles.py를 구현해 줘.
3. 실제 툴을 샘플 입력과 오류 입력으로 호출 검증한 뒤 contract.json의 status를 "verified"로 확정해 줘.
4. skill_factory/tests/<tool_id>/(generated, regression, holdout) 테스트 케이스를 작성한 뒤,
   `.venv/bin/python -m skill_factory.cli run --tool <tool_id> --provider <agy|external|local>`
   실행 결과 `status == "RELEASE_CANDIDATE"` 및 생성된 `SKILL.md`, `examples/` 확인 결과만 보고해 줘.
   (※ `--version`을 생략하면 대상 툴 `tool_version`의 다음 패치 버전, 예: `0.1.1` → `0.1.2`로 자동 지정됩니다.)
```
