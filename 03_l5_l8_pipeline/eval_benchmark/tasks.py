"""Universal 2-Way Benchmark criteria distinguishing Runtime Tool Execution vs Skill Packaging & Policy."""

from __future__ import annotations

from typing import Any


BENCHMARK_TASKS: list[dict[str, Any]] = [
    {
        "task_id": "U01_native_discovery_and_budget",
        "title": "Antigravity 자동 규칙 분리 · 용량 경량화 · 미검증 메모 제거",
        "rule_id": "L7-DISCOVERY",
        "description": ".agents/skills + .agents/rules(<=24KB) + skill_manifest.json 구비 및 SKILL.md 내 미검증 초안('Notes (not verified)') 제거 여부",
    },
    {
        "task_id": "U02_source_preservation_and_index",
        "title": "원본 문서 100% 보존 · 전체 목차 · examples/ 3종 완비",
        "rule_id": "L5-PRESERVE",
        "description": "기존 원문 파일(reference/ 등) 100% 보존율, 전체 Capability 목차 수록률, 자동 생성 examples/·capabilities.md 구비 여부",
    },
    {
        "task_id": "U03_mandatory_rule_and_policy_checks",
        "title": "필수 규칙 승격 및 정책(skill_check) 구조화",
        "rule_id": "L5-POLICY",
        "description": "원문 하단/타 클라이언트 파일에 흩어진 주의사항을 Antigravity 상단 필수 규칙(R-*)으로 승격·활성화했는지 비교",
    },
    {
        "task_id": "U04_contract_traps_and_failure_guards",
        "title": "실패 사례 문서화 · MCP 음수 오류 거부 실호출 확인",
        "rule_id": "L2-GUARDS",
        "description": "원본 reference 문서 및 MCP 스키마의 기존 실패 방어력과, 컴파일된 진입 규칙·tool_examples.md 통합 가드 커버율 비교",
    },
    {
        "task_id": "U05_sandbox_e2e_and_holdout_suite",
        "title": "실제 툴 기능 테스트(15/15) + 릴리스 게이트(Main + Holdout 20/20)",
        "rule_id": "L8-GATE",
        "description": "툴 자체 기능·시나리오 실호출 통과율(기존 MCP 포함)과 L8 전체 릴리스 게이트 통과·호출당 지연시간(ms) 실측",
    },
]
