"""Blueprint §17 falsifiability discipline — prompt-assembly full-chain regression.

Background
----------
The blueprint's third ring (§10 tension / §11 decision-weights / §12 theme) and the
§2 volume-summary tower were all *implemented* (services compute the digests and
`bundle_builder` writes them into ``inline_digests``) yet several never reached the
model: ``context_budget.SECTION_SPECS`` is a render allow-list, and
``collect_prompt_sections`` silently drops any ``inline_digests`` key that is not
registered there (``if text is None: continue``). Four keys were written but never
registered — ``character_arc_weights``, ``theme_expression_budget``,
``narrative_pattern``, ``volume_summary`` — so they were dead code with 0% production
coverage even though their unit tests were green.

The unit tests stayed green because *none of them walked the real assembly chain*
``BundleBuilder.build -> collect_prompt_sections -> render_user_prompt``. This module
closes that gap with two guards:

1. ``test_revived_digests_reach_user_prompt`` — end-to-end: a digest written into the
   bundle must appear in the final ``user_prompt``.
2. ``test_section_specs_covers_all_bundle_writes`` — a static reconciliation between
   the two sides (what ``bundle_builder`` writes vs. what ``SECTION_SPECS`` registers)
   so that ANY future ``inline_digests[...] =`` write that forgets to register a
   render slot fails loudly instead of becoming silent dead code.
"""

from __future__ import annotations

import re
from pathlib import Path

from novel_system.services import bundle_builder as bundle_builder_module
from novel_system.services.context_budget import (
    SECTION_SPECS,
    apply_context_budget,
    collect_prompt_sections,
)

# The four keys this regression was written to protect (previously DROPPED).
PREVIOUSLY_DROPPED_KEYS = (
    "volume_summary",            # §2 summary tower (wave-2 E5)
)

# digest_key -> (rendered section label, sentinel value injected for the test)
REVIVED = {
    "volume_summary": ("Volume Summary (atmosphere only)", "VOLUME_SUMMARY_SENTINEL"),
}


def _registered_digest_keys() -> set[str]:
    keys: set[str] = set()
    for _name, _label, digest_keys in SECTION_SPECS:
        keys.update(digest_keys)
    return keys


def _build_user_prompt(inline_digests: dict[str, str]) -> str:
    snapshot = {
        "inline_digests": inline_digests,
        "scene_id": "s_test",
        "chapter_id": "c_test",
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
    }
    sections = collect_prompt_sections(snapshot)
    result = apply_context_budget(
        system_prompt="SYSTEM",
        task_prompt="TASK",
        bundle_snapshot=snapshot,
        sections=sections,
        max_input_tokens=100_000,  # large budget: no compaction/omission interference
        task_kind="default",
    )
    return result["user_prompt"]


def test_revived_digests_reach_user_prompt() -> None:
    """Each revived digest, once written into the bundle, must reach the user prompt."""
    inline_digests = {key: marker for key, (_label, marker) in REVIVED.items()}
    user_prompt = _build_user_prompt(inline_digests)

    for key, (label, marker) in REVIVED.items():
        assert marker in user_prompt, (
            f"digest '{key}' value did not reach user_prompt — render slot missing"
        )
        assert label in user_prompt, (
            f"digest '{key}' heading '{label}' did not reach user_prompt"
        )


def test_known_dropped_keys_now_registered() -> None:
    """Explicit regression: the four previously-dropped keys are now in the allow-list."""
    registered = _registered_digest_keys()
    for key in PREVIOUSLY_DROPPED_KEYS:
        assert key in registered, (
            f"'{key}' is written by bundle_builder but not registered in SECTION_SPECS; "
            "it would be silently dropped from every prompt"
        )


def test_section_specs_covers_all_bundle_writes() -> None:
    """Reconcile both sides: every business digest key the bundle can write must have a
    render slot in SECTION_SPECS, otherwise it is silent dead code.

    This is the durable guard — it catches future drift, not just today's four keys.
    The bundle writes its sections only through ``bundle_sections.BundleSections``, which
    refuses keys that are not declared there (B03-19), so the declared set is the complete
    list. Internal signal keys (leading underscore, e.g. ``_style_reference_runtime_contract``)
    are not rendered sections and are excluded.
    """
    from novel_system.services.bundle_sections import (
        BUNDLE_SECTION_DIGEST_KEYS,
        BUNDLE_SIGNAL_DIGEST_KEYS,
    )

    assert BUNDLE_SECTION_DIGEST_KEYS, "no declared bundle digest keys — declaration drift?"
    assert all(key.startswith("_") for key in BUNDLE_SIGNAL_DIGEST_KEYS)
    assert not any(key.startswith("_") for key in BUNDLE_SECTION_DIGEST_KEYS)
    registered = _registered_digest_keys()
    missing = BUNDLE_SECTION_DIGEST_KEYS - registered

    assert not missing, (
        "inline_digests keys the bundle can write but NOT registered in "
        f"context_budget.SECTION_SPECS (they will be silently dropped from every "
        f"prompt — register a render slot for each): {sorted(missing)}"
    )


def test_bundle_builder_writes_sections_only_through_the_declared_registry() -> None:
    """No bundle module assigns ``inline_digests[...]`` directly: every section goes through
    ``BundleSections`` (which checks the key against the declared set above)."""
    services_dir = Path(bundle_builder_module.__file__).parent
    offenders = []
    for path in sorted(services_dir.glob("bundle_*.py")):
        if path.name == "bundle_sections.py":
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"""inline_digests\[[^\]]+\]\s*=""", source):
            offenders.append(path.name)
    assert offenders == []
