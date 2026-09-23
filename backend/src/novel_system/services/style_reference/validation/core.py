"""Pure validation composition shared by API callers and the async runner."""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Sequence

from sqlalchemy.orm import Session

from novel_system.services.style_reference.schemas import (
    ValidationMode,
    ValidationReport,
    ValidationVerdict,
)
from novel_system.services.style_reference.validation.forbidden_local import (
    check_forbidden_local,
    check_forbidden_terms,
)
from novel_system.services.style_reference.validation.plagiarism import check_plagiarism
from novel_system.services.style_reference.validation.quantitative import (
    check_quantitative,
)
from novel_system.services.style_reference.validation.quantitative import (
    check_quantitative_against_baseline,
)

if TYPE_CHECKING:
    from novel_system.db.models import StyleReferenceProfile


_CORPUS_CACHE: "OrderedDict[tuple[str, str], list[str]]" = OrderedDict()
_CORPUS_CACHE_MAX = 4


def _load_plagiarism_corpus(repo, book_id: str) -> list[str]:
    book = repo.get_book(book_id)
    checksum = (getattr(book, "text_checksum", "") or "") if book is not None else ""
    key = (book_id, checksum)
    cached = _CORPUS_CACHE.get(key)
    if cached is not None:
        _CORPUS_CACHE.move_to_end(key)
        return cached
    texts = [
        paragraph.text for paragraph in repo.list_paragraphs(book_id) if paragraph.text
    ]
    _CORPUS_CACHE[key] = texts
    _CORPUS_CACHE.move_to_end(key)
    while len(_CORPUS_CACHE) > _CORPUS_CACHE_MAX:
        _CORPUS_CACHE.popitem(last=False)
    return texts


def clear_plagiarism_corpus_cache() -> None:
    _CORPUS_CACHE.clear()


def run_sync_validate(
    generated_text: str,
    profile: "StyleReferenceProfile",
    session: Session,
    *,
    profile_quotes: list[str] | None = None,
) -> ValidationReport:
    from novel_system.services.style_reference.repository import (
        StyleReferenceRepository,
    )

    repo = StyleReferenceRepository(session)
    corpus = _load_plagiarism_corpus(repo, profile.book_id)
    if profile_quotes:
        corpus = corpus + [quote for quote in profile_quotes if quote]

    plagiarism = check_plagiarism(generated_text, corpus)
    forbidden = check_forbidden_local(generated_text, profile.profile_id, session)
    quantitative = check_quantitative(generated_text, profile)
    verdict = _compute_full_verdict(
        quant=quantitative,
        semantic=[],
        plag=plagiarism,
        forbid=forbidden,
    )
    return ValidationReport(
        verdict=verdict,
        mode_executed=ValidationMode.SYNC_ONLY,
        quantitative_json=[item.model_dump() for item in quantitative],
        semantic_json=[],
        plagiarism_json=plagiarism.model_dump(),
        forbidden_hits_json=[item.model_dump() for item in forbidden],
    )


def run_sync_validate_profiles(
    generated_text: str,
    profiles: Sequence[Any],
    session: Session,
) -> ValidationReport:
    """Validate once against the same layered baseline frozen for generation."""
    from novel_system.services.style_reference.repository import (
        StyleReferenceRepository,
    )
    from novel_system.services.style_reference.runtime_contract import (
        blend_profile_metric_baselines,
    )

    repo = StyleReferenceRepository(session)
    corpus: list[str] = []
    forbidden = []
    seen_forbidden: set[str] = set()
    for profile in profiles:
        book_id = str(getattr(profile, "book_id", "") or "")
        profile_id = str(getattr(profile, "profile_id", "") or "")
        if book_id:
            frozen_book = getattr(profile, "runtime_contract_book", None)
            if isinstance(frozen_book, dict):
                current_book = repo.get_book(book_id)
                if current_book is None or str(
                    getattr(current_book, "text_checksum", "") or ""
                ) != str(frozen_book.get("text_checksum") or ""):
                    raise ValueError(
                        "frozen style reference source changed before validation"
                    )
            corpus.extend(_load_plagiarism_corpus(repo, book_id))
        if profile_id:
            frozen_terms = getattr(profile, "runtime_contract_banned_terms", None)
            hits = (
                check_forbidden_terms(generated_text, list(frozen_terms))
                if isinstance(frozen_terms, list)
                else check_forbidden_local(generated_text, profile_id, session)
            )
            for hit in hits:
                key = str(hit.model_dump(mode="json"))
                if key not in seen_forbidden:
                    seen_forbidden.add(key)
                    forbidden.append(hit)
    plagiarism = check_plagiarism(
        generated_text,
        list(dict.fromkeys(text for text in corpus if text)),
    )
    quantitative = check_quantitative_against_baseline(
        generated_text,
        blend_profile_metric_baselines(profiles),
    )
    verdict = _compute_full_verdict(
        quant=quantitative,
        semantic=[],
        plag=plagiarism,
        forbid=forbidden,
    )
    return ValidationReport(
        verdict=verdict,
        mode_executed=ValidationMode.SYNC_ONLY,
        quantitative_json=[item.model_dump() for item in quantitative],
        semantic_json=[],
        plagiarism_json=plagiarism.model_dump(),
        forbidden_hits_json=[item.model_dump() for item in forbidden],
    )


def _compute_full_verdict(
    *,
    quant,
    semantic,
    plag,
    forbid,
    semantic_degraded: bool = False,
) -> ValidationVerdict:
    if not plag.passed:
        return ValidationVerdict.PLAGIARISM
    if any(hit.severity == "error" for hit in forbid):
        return ValidationVerdict.FAIL

    quant_pass_rate = (
        sum(1 for item in quant if item.passed) / len(quant) if quant else 1.0
    )
    semantic_mean = (
        sum(item.score for item in semantic) / len(semantic) if semantic else 6.0
    )
    if quant_pass_rate >= 0.8 and semantic_mean >= 6.0:
        return (
            ValidationVerdict.PARTIAL if semantic_degraded else ValidationVerdict.PASS
        )
    if quant_pass_rate >= 0.5 or semantic_mean >= 4.5:
        return ValidationVerdict.PARTIAL
    return ValidationVerdict.FAIL
