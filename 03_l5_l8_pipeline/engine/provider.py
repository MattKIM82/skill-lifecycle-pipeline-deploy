"""Provider Adapter isolating AI synthesis, independent judging, and refinement calls."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ProviderRequest:
    role: str  # "synthesize" | "judge" | "refine"
    prompt_version: str
    system_prompt: str
    payload: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    provider_id: str
    model_id: str
    content: dict[str, Any]
    raw_text: str = ""


class LLMProvider(Protocol):
    provider_id: str
    model_id: str

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        ...


class LocalDeterministicProvider:
    """Zero-external-API provider using structured contract rules and deterministic heuristics."""

    provider_id = "local_deterministic"
    model_id = "deterministic-rule-synth-v1"

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if request.role == "synthesize":
            out = self._synthesize(request.payload)
        elif request.role == "judge":
            out = self._judge(request.payload)
        elif request.role == "refine":
            out = self._refine(request.payload)
        else:
            raise ValueError(f"unsupported provider role: {request.role}")
        return ProviderResponse(
            provider_id=self.provider_id,
            model_id=self.model_id,
            content=out,
            raw_text=json.dumps(out, ensure_ascii=False),
        )

    def _synthesize(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract = payload["contract"]
        existing_k = payload.get("existing_knowledge") or {}
        existing_w = payload.get("existing_workflows") or {}

        rules = list(existing_k.get("rules", []))
        rules_by_id = {r["rule_id"]: r for r in rules}
        covered_constraints = {
            cid for r in rules for cid in r.get("constraint_refs", [])
        }
        # Derive baseline contract rules for any uncovered constraint and merge applies_to across capabilities.
        for cap in contract.get("capabilities", []):
            for idx, c in enumerate(cap.get("constraints", []), start=1):
                rid = c.get("id") or f"R-AUTO-{cap['id'].upper()}-{idx}"
                if rid in covered_constraints:
                    continue
                if rid in rules_by_id:
                    applies = rules_by_id[rid].setdefault("applies_to", [])
                    if cap["id"] not in applies:
                        applies.append(cap["id"])
                else:
                    new_rule = {
                        "rule_id": rid,
                        "category": "constraint",
                        "applies_to": [cap["id"]],
                        "statement": c["rule"],
                        "severity": "critical",
                        "mandatory": True,
                        "status": "verified",
                        "owner": "contract_extractor",
                        "source_ref": cap["source_ref"],
                    }
                    rules.append(new_rule)
                    rules_by_id[rid] = new_rule

        return {
            "knowledge": {
                "tool_id": contract["tool_id"],
                "version": existing_k.get("version", "1.0"),
                "rules": rules,
                "terminology": existing_k.get("terminology", {}),
                "assumptions": existing_k.get("assumptions", []),
            },
            "policy": payload.get("existing_policy")
            or {
                "tool_id": contract["tool_id"],
                "mandatory_rule_ids": [r["rule_id"] for r in rules if r.get("mandatory")],
                "forbid_unverified_claims": True,
                "forbid_guessing_user_specs": True,
            },
            "intent_catalog": payload.get("existing_intents")
            or {
                "tool_id": contract["tool_id"],
                "intents": [
                    {
                        "intent_id": f"use_{cap['id']}",
                        "description": cap.get("description", cap["id"]),
                        "target_capabilities": [cap["id"]],
                        "rule_ids": [
                            r["rule_id"] for r in rules if cap["id"] in r.get("applies_to", [])
                        ],
                    }
                    for cap in contract.get("capabilities", [])
                ],
            },
            "workflows": existing_w.get("workflows", []),
        }

    def _judge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Context-separated semantic check over execution evidence (never overrides failed rule oracles)."""
        records = payload.get("records", [])
        issues: list[dict[str, Any]] = []
        for rec in records:
            if not rec.get("rule_passed"):
                issues.append(
                    {
                        "case_id": rec["case_id"],
                        "verdict": "fail",
                        "failure_class": rec.get("suggested_failure_class", "skill_rule"),
                        "evidence_ref": rec.get("evidence_ref", ""),
                        "reason": rec.get("failure_reason", "deterministic oracle failed"),
                        "uncertainty": 0.05,
                    }
                )
        semantic_notes = payload.get("semantic_checks", [])
        for note in semantic_notes:
            if not note.get("ok", True):
                issues.append(
                    {
                        "case_id": note["case_id"],
                        "verdict": "fail",
                        "failure_class": note.get("failure_class", "skill_rule"),
                        "evidence_ref": note.get("evidence_ref", ""),
                        "reason": note.get("reason", "semantic check failed"),
                        "uncertainty": 0.15,
                    }
                )
        return {
            "verdict": "pass" if not issues else "fail",
            "issues": issues,
            "uncertainty": 0.05 if not issues else 0.1,
            "summary": "All rule & semantic judge checks passed" if not issues else f"{len(issues)} issue(s) flagged",
        }

    def _refine(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Generate scoped patches for known reproducible skill/knowledge/workflow defect patterns."""
        failures = payload.get("failures", [])
        patches: list[dict[str, Any]] = []
        for f in failures:
            fc = f.get("failure_class")
            if fc in ("contract", "tool", "eval_oracle"):
                continue
            target_layer = "knowledge" if fc == "knowledge_rule" else ("workflow" if fc == "workflow_dag" else "skill")
            patches.append(
                {
                    "case_id": f["case_id"],
                    "target_layer": target_layer,
                    "affected_ids": f.get("affected_ids", []),
                    "action": f.get("recommended_fix", {}).get("action", "enforce_rule"),
                    "rule_patch": f.get("recommended_fix", {}),
                    "evidence_ref": f.get("evidence_ref", ""),
                    "reason": f.get("summary", ""),
                }
            )
        return {"patches": patches}


class AgentHandoffProvider(LocalDeterministicProvider):
    """Writes request payloads to disk for Antigravity subagent review; reads override response if present."""

    provider_id = "antigravity_handoff"
    model_id = "antigravity-agent-handoff"

    def __init__(self, handoff_dir: Path) -> None:
        self.handoff_dir = Path(handoff_dir)
        self.handoff_dir.mkdir(parents=True, exist_ok=True)

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        req_file = self.handoff_dir / f"{request.role}_request.json"
        resp_file = self.handoff_dir / f"{request.role}_response.json"
        req_file.write_text(
            json.dumps(
                {
                    "role": request.role,
                    "prompt_version": request.prompt_version,
                    "system_prompt": request.system_prompt,
                    "payload": request.payload,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if resp_file.is_file():
            content = json.loads(resp_file.read_text(encoding="utf-8"))
            return ProviderResponse(
                provider_id=self.provider_id,
                model_id=self.model_id,
                content=content,
                raw_text=resp_file.read_text(encoding="utf-8"),
            )
        return super().complete_json(request)


class ExternalAPIProvider:
    """OpenAI-compatible / enterprise LLM endpoint adapter (activated via env vars when deployed)."""

    provider_id = "external_llm_api"

    def __init__(self, base_url: str, api_key: str, model_id: str, timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_id = model_id
        self.timeout_s = timeout_s

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        body = {
            "model": self.model_id,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": json.dumps(request.payload, ensure_ascii=False)},
            ],
            "temperature": 0.1,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"LLM API error HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}") from exc

        raw = data["choices"][0]["message"]["content"]
        return ProviderResponse(
            provider_id=self.provider_id,
            model_id=self.model_id,
            content=json.loads(raw),
            raw_text=raw,
        )


class AntigravityCLIProvider:
    """Invokes Antigravity's own model (`agy -p --output-format json --json-schema`) in an isolated temp dir."""

    provider_id = "antigravity_cli"

    def __init__(
        self,
        model_id: str = "Gemini 4 Hydrogen (Medium)",
        agy_bin: str | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        self.model_id = model_id
        self.agy_bin = (
            agy_bin
            or os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
            or shutil.which("agy")
            or next(
                (
                    str(p)
                    for p in (
                        Path.home() / ".local" / "bin" / "agy",
                        Path.home() / ".gemini" / "bin" / "agy",
                    )
                    if p.is_file()
                ),
                str(Path.home() / ".local" / "bin" / "agy"),
            )
        )
        self.timeout_s = timeout_s

    def complete_json(self, request: ProviderRequest) -> ProviderResponse:
        if not Path(self.agy_bin).is_file():
            raise RuntimeError(f"agy binary not found at {self.agy_bin}")

        prompt = (
            f"{request.system_prompt}\n\n"
            f"All required context is inline inside INPUT_PAYLOAD. Do NOT invoke any tools, shell commands, or file reads.\n"
            f"Return ONLY valid JSON satisfying the schema for role='{request.role}'.\n"
            f"INPUT_PAYLOAD:\n{json.dumps(request.payload, ensure_ascii=False)}"
        )
        with tempfile.TemporaryDirectory(prefix=f"agy_{request.role}_") as iso_dir:
            cmd = [
                self.agy_bin,
                "-p",
                prompt,
                "--model",
                self.model_id,
                "--output-format",
                "json",
                "--disable-slash-commands",
                "--print-timeout",
                "150s",
            ]
            if request.output_schema:
                schema_path = Path(iso_dir) / "schema.json"
                schema_path.write_text(json.dumps(request.output_schema), encoding="utf-8")
                cmd.extend(["--json-schema", str(schema_path)])

            proc = subprocess.run(
                cmd,
                cwd=iso_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )

        if proc.returncode != 0:
            raise RuntimeError(f"agy CLI failed ({proc.returncode}): {proc.stderr or proc.stdout}")

        raw_out = proc.stdout.strip()
        # agy may emit multiple lines if stderr/info precedes final JSON; parse the last JSON line/object
        parsed: dict[str, Any] | None = None
        for line in reversed(raw_out.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if parsed is None:
            parsed = json.loads(raw_out)

        if parsed.get("status") != "SUCCESS":
            raise RuntimeError(f"agy returned non-success: {parsed.get('error') or parsed}")

        structured = parsed.get("structured_output")
        if structured is None:
            text_resp = parsed.get("response", "").strip()
            if text_resp.startswith("```"):
                text_resp = "\n".join(text_resp.splitlines()[1:-1])
            structured = json.loads(text_resp)

        return ProviderResponse(
            provider_id=self.provider_id,
            model_id=self.model_id,
            content=structured,
            raw_text=json.dumps(structured, ensure_ascii=False),
        )


def resolve_provider(
    mode: str | None = None,
    handoff_dir: Path | None = None,
    *,
    role: str = "synthesize",
    model_id: str | None = None,
) -> LLMProvider:
    chosen = (mode or os.environ.get("SKILL_FACTORY_PROVIDER") or "local").lower()
    if chosen == "agy":
        default_model = (
            "Gemini 3.8 Flash (Medium)" if role == "judge" else "Gemini 4 Hydrogen (Medium)"
        )
        return AntigravityCLIProvider(model_id=model_id or default_model)
    if chosen == "external":
        url = os.environ.get("SKILL_FACTORY_LLM_URL", "")
        key = os.environ.get("SKILL_FACTORY_LLM_KEY", "")
        model = model_id or os.environ.get("SKILL_FACTORY_LLM_MODEL", "")
        if not (url and key and model):
            raise RuntimeError(
                "external provider requires SKILL_FACTORY_LLM_URL, SKILL_FACTORY_LLM_KEY, and SKILL_FACTORY_LLM_MODEL"
            )
        return ExternalAPIProvider(base_url=url, api_key=key, model_id=model)
    if chosen == "handoff":
        return AgentHandoffProvider(handoff_dir or Path("skill_factory/runs/handoffs"))
    return LocalDeterministicProvider()
