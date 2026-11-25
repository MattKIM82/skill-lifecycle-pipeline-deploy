# 신규 툴 온보딩 공통 가이드 (L1–L4 Adapter & Contract Onboarding)

새로운 대상 툴(`<tool_id>`)을 Skill Factory 파이프라인(`L5–L8` 공통 엔진)에 온보딩할 때 **공통 엔진 코드는 수정하지 않고** 아래 4단계만 수행합니다.

## 1. L1–L4 계약(`contract.json`) 자동 초안 추출
다음 두 가지 공통 추출기 중 대상 툴 형태에 맞는 방법을 사용합니다([base_extractor.py](base_extractor.py)).

- **A. 표준 MCP 서버인 경우 (코드 작성 없이 자동 추출):**
  ```bash
  python extract_cli.py onboard-mcp \
    --tool <tool_id> --command /path/to/mcp-server --args "--flag"
  ```
  `list_tools()`로 모든 툴의 이름, 설명, `inputSchema`, `annotations` 및 L2 스키마 제약을 추출해 `workspace/contracts/<tool_id>/contract.json` 초안을 생성합니다.

- **B. CLI / 소스 코드 / 블랙박스 사양서 기반인 경우 (AI 추출 프롬프트 사용):**
  [extract_contract_v1.txt](prompts/extract_contract_v1.txt) 프롬프트와 `extract_from_docs_via_ai()`를 사용해 소스 진입점·`--help`·벤더 문서에서 `contract.json` 초안을 추출합니다.

> 추출 직후 계약의 `status`는 `"partial"`입니다. L4 어댑터·오라클 연결과 실호출 검증이 끝나야 `"verified"`로 승격되어 Admission Gate를 통과합니다.

## 2. `ToolAdapter` 구현 (`workspace/contracts/<tool_id>/<tool_id>_adapter.py`)
[schema.py](schema.py)의 `ToolAdapter` 프로토콜 3개 메서드를 구현하고 `AdapterRegistry`에 등록합니다:
```python
class MyToolAdapter:
    adapter_id = "<tool_id>_adapter"
    def execute(self, capability_id: str, validated_input: dict, run_context: RunContext) -> dict: ...
    def status(self, handle: dict) -> dict: ...   # 미지원 시 {"supported": False, "status": "unsupported"}
    def cancel(self, handle: dict) -> dict: ...   # 미지원 시 {"supported": False, "cancelled": False}
```

## 3. L4 결정론적 오라클 등록 (`workspace/contracts/<tool_id>/<tool_id>_oracles.py`)
`OracleRegistry`에 스키마 검사·오류 계약 검사·도메인 허용오차·파일 산출물 검증기를 등록합니다. (종료 코드 0만으로 성공 판정 금지)

## 4. 공통 엔진으로 L5–L8 빌드 및 검증 실행
```bash
python ../03_l5_l8_pipeline/pipeline_cli.py run --tool <tool_id> --provider local
```
(`--version` 생략 시 대상 툴 `tool_version`에서 패치 버전을 +1 자동 부여합니다. 예: `0.1.1` → `0.1.2`)
- L5 지식(`knowledge.json`)·L6 DAG(`workflow.json`)·L7 `SKILL.md`·L8 Eval까지 **공통 엔진 소스 수정 없이** 실행됩니다.
