"""Public validation API; implementations live in acyclic leaf modules."""

from novel_system.services.style_reference.schemas import ValidationReport
from novel_system.services.style_reference.validation.core import (
    _compute_full_verdict,
    _load_plagiarism_corpus,
    clear_plagiarism_corpus_cache,
    run_sync_validate,
    run_sync_validate_profiles,
)
from novel_system.services.style_reference.validation.forbidden_local import check_forbidden_local
from novel_system.services.style_reference.validation.forbidden_semantic import check_forbidden_semantic
from novel_system.services.style_reference.validation.plagiarism import check_plagiarism
from novel_system.services.style_reference.validation.quantitative import check_quantitative
from novel_system.services.style_reference.validation.runner import (
    ValidationOrchestrator,
    start_style_reference_validation_worker,
)
from novel_system.services.style_reference.validation.semantic import check_semantic

__all__ = [
    "ValidationReport",
    "ValidationOrchestrator",
    "start_style_reference_validation_worker",
    "run_sync_validate",
    "run_sync_validate_profiles",
    "check_plagiarism",
    "check_forbidden_local",
    "check_quantitative",
    "check_semantic",
    "check_forbidden_semantic",
    "clear_plagiarism_corpus_cache",
]
