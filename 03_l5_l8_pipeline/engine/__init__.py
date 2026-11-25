"""Core L5-L8 Skill Lifecycle Engineering engine."""

from .admission import AdmissionResult, verify as verify_admission
from .compile import CompileResult, validate_and_compile
from .curate import FailureRecord, curate_failures
from .evaluate import EvalReport, evaluate_execution
from .execute import ExecutionEvidence, run_isolated_suite
from .orchestration import PipelineResult, build_skill_pipeline
from .provider import (
    AgentHandoffProvider,
    ExternalAPIProvider,
    LLMProvider,
    LocalDeterministicProvider,
    resolve_provider,
)
from .refine import PatchProposal, propose_scoped_patch
from .release import ReleaseRecord, approve_and_publish, create_release_candidate, rollback_release
from .state import StateStore

__all__ = [
    "AdmissionResult",
    "AgentHandoffProvider",
    "CompileResult",
    "EvalReport",
    "ExecutionEvidence",
    "ExternalAPIProvider",
    "FailureRecord",
    "LLMProvider",
    "LocalDeterministicProvider",
    "PatchProposal",
    "PipelineResult",
    "ReleaseRecord",
    "StateStore",
    "approve_and_publish",
    "build_skill_pipeline",
    "create_release_candidate",
    "curate_failures",
    "evaluate_execution",
    "propose_scoped_patch",
    "resolve_provider",
    "rollback_release",
    "run_isolated_suite",
    "validate_and_compile",
    "verify_admission",
]
