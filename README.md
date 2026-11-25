# Skill Lifecycle Engineering 파이프라인 배포 패키지 사용 설명서

이 `deploy/` 폴더는 **L1–L4 계약 추출 도구**와 **L5–L8 공통 자동화 파이프라인**, **모니터링/승인 웹 콘솔**, **원본 설계 HTML 3종**, **검증 보고서**를 분리·정리한 **독립 배포용 패키지**입니다. 이 폴더만 통째로 복사해 다른 프로젝트나 사내 환경으로 가져가 바로 사용할 수 있습니다.

---

## 1. 폴더 구성 한눈에 보기

| 폴더 / 파일 | 역할 및 설명 |
|---|---|
| [01_original_specs_html/](01_original_specs_html/) | 처음 제공하신 **원본 아키텍처 설계 HTML 3종** (`skill_architecture_source.html`, `skill_architecture_eda.html`, `skill_architecture_pipeline.html`) 원본 보존 |
| [02_l1_l4_extractor/](02_l1_l4_extractor/) | **[L1–L4 분리]** 신규 툴의 기능·스키마·실행제어·어댑터·오라클 추출 도구 (`extract_cli.py`, 메타 스키마, 템플릿 2종, AI 추출 프롬프트, 온보딩 명세서) |
| [03_l5_l8_pipeline/](03_l5_l8_pipeline/) | **[L5–L8 분리]** 범용 지식(L5)·워크플로 DAG(L6)·스킬 컴파일(L7)·격리 검증/큐레이션/폐루프/배포 게이트(L8) 엔진 + 테스트 + 에이전트용 스킬 가이드 |
| [04_console/](04_console/) | **[콘솔 분리]** 웹 기반 모니터링·실행·Diff 비교·승인/롤백 콘솔 (`start_console.sh`, `server.py`, `static/index.html`, `CONSOLE_GUIDE.md`) |
| [05_reports/](05_reports/) | 파이프라인 검증 결과 보고서 (`osaw_skill_pipeline_shareable_report.html` / `.md` + 실기 그래프 PNG 자산) |
| [workspace/](workspace/) | 새 툴 온보딩 시 계약(`contracts/`), 지식(`knowledge/`), 워크플로(`workflows/`), 테스트(`tests/`), 실행 이력(`runs/`), 릴리스(`releases/`), 최종 `.agents/` 산출물이 쌓이는 작업공간 |
| [skill_factory/](skill_factory/) | 분리된 `02`·`03`·`04`·`workspace` 폴더를 하나의 파이프라인으로 연결하는 경량 부트스트랩 브리지 *(직접 수정 불필요)* |

---

## 2. Console(웹 대시보드) 위치 및 사용법

- **위치:** [deploy/04_console/](04_console/) (`start_console.sh` 및 `server.py`)
  *(※ 원본 작업 저장소 기준 위치는 `skill_factory/dashboard/`이며, 배포 패키지에서는 사용하기 쉽게 `04_console/` 폴더로 분리해 두었습니다.)*
- **실행 명령:**
  ```bash
  cd deploy
  ./04_console/start_console.sh 8790
  ```
- **접속 주소:**
  - **메인 콘솔:** `http://127.0.0.1:8790/` (툴 선택, 파이프라인 실행, 결함 주입 자동수정 시연, Before/After Diff, 승인·롤백 버튼 제공)
  - **공유용 보고서 바로 보기:** `http://127.0.0.1:8790/share-report`
- **특정 외부 작업공간 이력을 콘솔로 확인하고 싶을 때:**
  ```bash
  SKILL_FACTORY_WORKSPACE=/path/to/your_project/skill_factory \
    ./04_console/start_console.sh 8791
  ```
  자세한 화면 탭별 설명은 [04_console/CONSOLE_GUIDE.md](04_console/CONSOLE_GUIDE.md)를 참고하세요.

---

## 3. 새 툴 온보딩 및 실행 순서 (Step-by-Step)

새로 연결할 대상 툴 ID를 `<tool_id>`(예: `my_eda_tool`)라고 할 때 아래 순서대로 진행하면 **공통 엔진 코드 수정 없이** 스킬을 생성·검증·배포할 수 있습니다.

### Step 0. 작업 환경 준비
- Python 3.11+ 환경(기존 `.venv` 또는 사내 venv)에서 `mcp` 및 `pytest`가 설치되어 있으면 즉시 동작합니다.
- (선택) 에이전트(AI 코딩 도우미)에게 전체 온보딩을 맡기고 싶다면 [03_l5_l8_pipeline/agent_skill/skill-factory/](03_l5_l8_pipeline/agent_skill/skill-factory/) 폴더를 프로젝트의 `.agents/skills/`로 복사해 두세요.

### Step 1. [L1–L4] 계약 초안 추출 (`02_l1_l4_extractor/`)

대상 툴 형태에 따라 **A(표준 MCP 서버)** 또는 **B(소스/CLI/블랙박스 문서)** 중 하나를 실행합니다. 결과는 자동으로 `deploy/workspace/contracts/<tool_id>/contract.json`에 저장됩니다.

- **A. 표준 MCP 서버인 경우:**
  ```bash
  python 02_l1_l4_extractor/extract_cli.py onboard-mcp \
    --tool <tool_id> \
    --command /path/to/mcp-server \
    --args "--flag"
  ```
- **B. CLI · Python 라이브러리 · 벤더 문서(블랙박스)인 경우:**
  ```bash
  python 02_l1_l4_extractor/extract_cli.py onboard-ai \
    --tool <tool_id> \
    --version 1.0.0 \
    --provider agy \
    --docs /path/to/docs_or_help.md \
    --docs-dir /path/to/original_docs_root
  ```
  > AI가 사용할 프로바이더 옵션: `--provider agy` (로컬 CLI), `--provider external` (사내 LLM API: `SKILL_FACTORY_LLM_URL` / `SKILL_FACTORY_LLM_KEY` / `SKILL_FACTORY_LLM_MODEL`), `--provider local` (오프라인 결정론 모드)

### Step 2. [L1–L4] 어댑터 · 오라클 연결 및 계약 확정 (`status: "verified"`)
1. MCP 서버가 아니라면 [02_l1_l4_extractor/templates/tool_adapter_template.py](02_l1_l4_extractor/templates/tool_adapter_template.py)를 `workspace/contracts/<tool_id>/<tool_id>_adapter.py`로 복사해 `execute`, `status`, `cancel`을 구현합니다. *(표준 MCP는 내장 `McpStdioAdapter`가 자동 연결되므로 생략 가능)*
2. 필요 시 [02_l1_l4_extractor/templates/oracles_template.py](02_l1_l4_extractor/templates/oracles_template.py)를 `workspace/contracts/<tool_id>/<tool_id>_oracles.py`로 복사해 도메인 파일/수치 검증기를 추가합니다.
3. `contract.json`의 입출력·제약·예시를 실제 툴 호출로 확인한 뒤 `"status": "partial"` → **`"status": "verified"`**로 바꾸고 스키마 검사를 실행합니다:
   ```bash
   python 02_l1_l4_extractor/extract_cli.py validate --tool <tool_id>
   ```
   *(AI에게 Step 1~2를 위임하는 복붙용 프롬프트는 [02_l1_l4_extractor/TOOL_ONBOARDING_SPEC.md](02_l1_l4_extractor/TOOL_ONBOARDING_SPEC.md) 하단에 준비되어 있습니다.)*

### Step 3. [L8 테스트 준비] 계약·회귀·Holdout 케이스 배치
`deploy/workspace/tests/<tool_id>/` 아래 3개 폴더에 JSON 테스트 케이스를 준비합니다.
- `generated/` : 정상·경계·오류 계약 케이스
- `regression/` : 다단계 업무 시나리오 및 `skill_check` 규칙 검증
- `holdout/` : 수정 루프에서 열람·변경이 금지된 최종 배포 게이트용 숨긴 평가

### Step 4. [L5–L8] 공통 파이프라인 실행 (`03_l5_l8_pipeline/`)
```bash
python 03_l5_l8_pipeline/pipeline_cli.py run --tool <tool_id> --provider local
```
- **Admission Gate → L5 지식 → L6 워크플로 DAG → L7 컴파일 → L8 격리 실호출·판정 → 실패 시 폐루프 수정(최대 3회) → Holdout 게이트**까지 자동 실행됩니다.
- 모든 게이트 통과 시 `status == "RELEASE_CANDIDATE"`와 `run_id`가 출력됩니다.
- 컴파일된 스킬을 바로 특정 프로젝트 폴더의 `.agents/`로 내보내려면 실행 시 `SKILL_FACTORY_TARGET_ROOT=/path/to/target_project`를 함께 지정하세요.

### Step 5. [Console 확인 및 승인/롤백] (`04_console/`)
1. 콘솔 실행 (`./04_console/start_console.sh 8790`) 후 `http://127.0.0.1:8790/`에서 상태 머신·Diff·Eval 결과를 검토합니다.
2. 콘솔 우측 **`✓ Release 승인·배포`** 버튼을 누르거나 CLI로 최종 배포합니다:
   ```bash
   python 03_l5_l8_pipeline/pipeline_cli.py approve --tool <tool_id> --run-id <run_id>
   ```
3. 배포 후 문제가 생기면 직전 검증 버전으로 즉시 롤백할 수 있습니다:
   ```bash
   python 03_l5_l8_pipeline/pipeline_cli.py rollback --tool <tool_id>
   ```

---

## 4. 동작 검증 테스트 실행 방법
`deploy/` 폴더를 다른 곳으로 옮긴 뒤에도 엔진·추출기·콘솔이 정상 작동하는지 아래 명령 한 줄로 자가 점검할 수 있습니다:
```bash
cd deploy
/path/to/python -m pytest -v 03_l5_l8_pipeline/tests/
```
*(현재 저장소 가상환경에서는 `../.venv/bin/python -m pytest -v 03_l5_l8_pipeline/tests/`)*
