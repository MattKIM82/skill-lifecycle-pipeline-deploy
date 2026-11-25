# Skill Factory Console (웹 모니터링 · 제어 콘솔) 위치 및 사용법

## 1. 콘솔 위치
- **배포 패키지 기준 위치 (`deploy/`):** `deploy/04_console/`
  - 실행 스크립트: `deploy/04_console/start_console.sh`
  - 백엔드 HTTP 서버: `deploy/04_console/server.py`
  - 프론트엔드 SPA UI: `deploy/04_console/static/index.html`
- **원본 개발 저장소 기준 위치:** `skill_factory/dashboard/` (`server.py`, `static/index.html`)

## 2. 실행 방법

### 방법 A) `deploy/04_console/` 단독 실행 (권장)
```bash
cd deploy
./04_console/start_console.sh 8790
```
- 브라우저에서 **`http://127.0.0.1:8790/`** 접속
- 검증 보고서 바로 보기: **`http://127.0.0.1:8790/share-report`** (우측 상단 `📄 검증 보고서 열기` 버튼으로도 이동 가능)

### 방법 B) 특정 외부 작업공간(예: 기존 프로젝트의 `skill_factory/` 또는 별도 경로)을 지정해 실행
```bash
SKILL_FACTORY_WORKSPACE=/path/to/your/workspace ./04_console/start_console.sh 8790
```

## 3. 콘솔 주요 기능 및 화면 구성

| 영역 / 탭 | 역할 |
|---|---|
| **상단 컨트롤 바** | 툴 선택(`Tool: ...`), `▶ 파이프라인 실행`, `↺ 결함 주입·자동수정 시연`, `📊 범용 2-Way 벤치마크 갱신`, `📄 검증 보고서 열기` |
| **KPI & 상태 머신** | 계약 정규화 수, Main/Holdout Eval 통과율, 현재 배포 버전, `NEW → ADMITTED → GENERATED → TESTED → EVALUATED → RELEASE_CANDIDATE → APPROVED → PUBLISHED` 진행 표시 및 승인·롤백 버튼 |
| **L1–L4 계약 정규화** | 추출된 기능 목록, 입출력/어댑터/오라클 매핑, 제약조건 필터링 |
| **L5 지식 & L6 워크플로 DAG** | Single Source of Truth 규칙 레지스트리와 승인된 DAG 연결 그래프 |
| **L7 컴파일 Diff** | Before(원본 툴 문서) vs After(경량 `SKILL.md` + 상시 규칙) 비교 |
| **L8 Eval & 실패 큐레이션** | Main + Locked Holdout 케이스별 판정 및 `failure_cases.jsonl` 실패 내역 |
| **범용 2-Way 성능 비교** | 적용 전 vs 적용 후 패키징/규칙/게이트 점수 막대 비교 |

## 4. 주요 HTTP API 엔드포인트
- `GET /` : 콘솔 UI (`index.html`)
- `GET /share-report` : `05_reports/` 내 공유용 보고서 HTML
- `GET /api/health` : 서버 상태 확인
- `GET /api/tools` : `workspace/contracts/`에 등록된 툴 목록
- `GET /api/overview?tool=<id>` : KPI, 최신 실행, Diff, Eval 종합 조회
- `POST /api/pipeline/run` : 파이프라인 실행 (`{"tool_id":"...", "provider":"local|agy|external"}`)
- `POST /api/release/approve` / `POST /api/release/rollback` : 버전 승인 배포 및 직전 버전 복구
