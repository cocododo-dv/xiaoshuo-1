"""写作台深评 / 局部深评 / 通读本章 / 局部改写送给模型的材料（从 writer_deep_review 拆出）：可见文字、
段落范围、只通读改过的场时的范围说明与沿用的发现、局部改写的快照与用户消息尾。"""

from __future__ import annotations

import json
from typing import Any

from novel_system.db.models import AuthorDraft, SceneCard, WriterEvaluation
from novel_system.services.manuscript_html import manuscript_paragraphs
from novel_system.services.scene_diagnosis import locate_in_paragraphs
from novel_system.services.value_coercion import optional_text

PASSAGE_MAX_FOCUS_PARAGRAPHS = 40
CHAPTER_DIGEST_EDGE_CHARS = 200
CHAPTER_DIGEST_MAX_FINDINGS = 6


def _prompt_text(content: Any) -> str:
    """作者稿是 HTML：给模型看的是可见文字（段落之间空一行），否则它会把 <p> 引进证据里。"""

    text = str(content or "")
    if "<" not in text:
        return text
    return "\n\n".join(part for part in manuscript_paragraphs(text) if part.strip())


def _passage_review_user_prompt(
    base_prompt: str,
    *,
    scope: dict[str, Any],
    about: dict[str, Any] | None,
    excerpt: str | None,
    question: str | None,
) -> str:
    focus = [int(index) + 1 for index in scope["focus"]]
    focus_label = f"{focus[0]}" if len(focus) == 1 else (f"{focus[0]}–{focus[-1]}" if focus == list(range(focus[0], focus[-1] + 1)) else ", ".join(str(index) for index in focus))
    coverage = (
        "the whole scene is shown: focus paragraphs are marked 【焦点段 N】, their neighbours 【上下文 N】, every other paragraph 【第 N 段】"
        if scope.get("whole_scene")
        else f"focus paragraphs are marked 【焦点段 N】, their neighbours 【上下文 N】; {int(scope.get('abbreviated') or 0)} far paragraphs are abbreviated to their opening and marked 【第 N 段·略】"
    )
    lines = [
        base_prompt,
        "",
        "## Passage Under Review",
        f"Focus paragraph{'s' if len(focus) > 1 else ''}: {focus_label} ({coverage}).",
    ]
    if excerpt:
        lines.append(f"Selected text: {excerpt}")
    if about is not None:
        lines.extend(
            [
                "",
                "## Finding To Verify",
                f"Source: {about.get('source')} · Dimension: {about.get('dimension')} ({about.get('label')}) · Severity: {about.get('severity')}",
                f"Issue: {about.get('issue')}",
                f"Suggested fix: {about.get('recommendation')}",
            ]
        )
        related = about.get("related") or {}
        if related.get("excerpt"):
            lines.append(f"Related passage (paragraph {int(related['paragraph_index']) + 1 if isinstance(related.get('paragraph_index'), int) else '?'}): {related['excerpt']}")
    else:
        lines.extend(["", "## Finding To Verify", "(none — judge the focus paragraphs on their own; verdict is no_finding unless you find something)"])
    lines.extend(
        [
            "",
            "## Cross-Paragraph Check",
            "Check the focus paragraphs against every other paragraph shown: a fact, object, time, place, injury, or who-knows-what that contradicts another paragraph; a beat, image or sentence the focus repeats from elsewhere; a setup elsewhere that the focus fails to pick up. Report such a finding with evidence_excerpt copied verbatim from a focus paragraph, related_excerpt copied verbatim (at most 80 characters) from the other paragraph, related_paragraph_index as the number in that paragraph's marker, and relation = contradiction | repetition | continuity. Findings that concern only the focus paragraphs leave these fields empty.",
        ]
    )
    if question:
        lines.extend(["", "## Author's Question", str(question)])
    return "\n".join(lines)


def _previous_findings_by_scene(
    previous: WriterEvaluation | None,
    scenes: list[SceneCard],
    texts: list[Any],
) -> dict[str, list[dict[str, Any]]]:
    """上一轮通读的发现按「引文钉在哪一场」分组（钉不到任何一场的是章级发现，不在这里）。"""

    grouped: dict[str, list[dict[str, Any]]] = {}
    if previous is None:
        return grouped
    for item in previous.findings_json or []:
        if not isinstance(item, dict):
            continue
        excerpt = _compact_text(str(item.get("evidence_excerpt") or ""), 400)
        if not excerpt:
            continue
        for scene, text in zip(scenes, texts):
            if text.layer != "none" and locate_in_paragraphs(text.paragraphs, excerpt) is not None:
                grouped.setdefault(scene.scene_id, []).append(item)
                break
    return grouped


def _carry_previous_scene_findings(
    previous: WriterEvaluation | None,
    scenes: list[SceneCard],
    texts: list[Any],
    full_scene_ids: set[str],
) -> list[dict[str, Any]]:
    """只通读改过的场时，未改的场沿用上一轮的发现（带 ``carried_from``）；改过的场由模型重判，章级发现由模型重说。"""

    if previous is None:
        return []
    carried: list[dict[str, Any]] = []
    for scene_id, items in _previous_findings_by_scene(previous, scenes, texts).items():
        if scene_id in full_scene_ids:
            continue
        for item in items:
            carried.append({**{key: value for key, value in item.items() if key != "carried_from"}, "carried_from": str(item.get("carried_from") or previous.evaluation_id)})
    return carried


def _chapter_review_prompt_tail(
    scenes: list[SceneCard],
    texts: list[Any],
    full_scene_ids: set[str],
    previous: WriterEvaluation | None,
    carried: list[dict[str, Any]],
) -> str:
    full_numbers = [str(index) for index, scene in enumerate(scenes, start=1) if scene.scene_id in full_scene_ids]
    digest_numbers = [str(index) for index, (scene, text) in enumerate(zip(scenes, texts), start=1) if scene.scene_id not in full_scene_ids and text.layer != "none"]
    lines = [
        "## Read-Through Scope",
        f"This is an incremental read-through. Scenes {', '.join(full_numbers)} changed since the previous read-through and are shown in full under 【第 N 场 · 本次通读】: judge them completely. "
        f"Scenes {', '.join(digest_numbers) or '—'} did not change and appear only as 【第 N 场 · 未改 · 摘要】 (opening, ending, the previous read-through's findings on them); their findings are kept automatically — do not restate them, and report a finding on an unchanged scene only when a changed scene now contradicts or undercuts it (quote the changed scene as evidence). "
        "Judge the chapter as a whole again (promise, escalation, payoff, ending) with the changed scenes in place.",
    ]
    if previous is not None:
        located = {(str(item.get("dimension")), _compact_text(str(item.get("evidence_excerpt") or ""), 200)) for item in carried}
        chapter_level = [
            item
            for item in (previous.findings_json or [])
            if isinstance(item, dict)
            and (str(item.get("dimension")), _compact_text(str(item.get("evidence_excerpt") or ""), 200)) not in located
            and not _compact_text(str(item.get("evidence_excerpt") or ""), 200)
        ]
        if chapter_level:
            lines.extend(
                [
                    "",
                    "### Previous Chapter-Level Findings",
                    "These chapter-level findings came from the previous read-through. Re-issue each one that still holds (the wording may stay), drop the ones the changes resolved, add new ones:",
                ]
            )
            lines.extend(f"- [{item.get('dimension')}] {item.get('issue')}（改法：{item.get('recommendation')}）" for item in chapter_level[:CHAPTER_DIGEST_MAX_FINDINGS])
    return "\n".join(lines)


def _passage_patch_snapshot(
    *,
    payload: dict[str, Any],
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
    source_draft: AuthorDraft | None,
    instruction: str | None = None,
    issue_note: str | None = None,
) -> dict[str, Any]:
    inline_digests = {
        "scene_summary": json.dumps(
            {
                "object_type": payload.get("object_type"),
                "object_id": payload.get("object_id"),
                "target_text_ref": target_text_ref,
                "source_excerpt": source_excerpt,
                "issue_dimension": issue_dimension,
                "instruction": instruction or "",
                "issue_note": issue_note or "",
                "source_draft_id": source_draft.draft_id if source_draft is not None else None,
                "source_draft_context": _compact_text(source_draft.content if source_draft is not None else source_excerpt, 1200),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    }
    return {
        "contract_version": "WRITER_PASSAGE_PATCH_SOURCE_v1",
        "stage_allowlist_name": "writer_passage_patch",
        "scene_id": optional_text(payload.get("scene_id")) or "",
        "chapter_id": optional_text(payload.get("chapter_id")) or "",
        "source_version_refs": {
            "target_text_ref": target_text_ref,
            "source_draft_id": source_draft.draft_id if source_draft is not None else None,
        },
        "resolved_ref_ids": {},
        "ordered_injections": [
            {"slot": "passage_patch_target", "ref_id": target_text_ref, "digest_key": "scene_summary"},
        ],
        "inline_digests": inline_digests,
    }


def _passage_patch_user_prompt(
    base_prompt: str,
    *,
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
    source_draft: AuthorDraft | None,
    instruction: str | None = None,
    issue_note: str | None = None,
) -> str:
    target_lines = [
        "## Passage Patch Target",
        f"Target Text Ref: {target_text_ref}",
        f"Issue Dimension: {issue_dimension}",
    ]
    if issue_note:
        target_lines.append(f"Diagnosed Issue: {issue_note}")
    if instruction:
        target_lines.append(f"Author Instruction: {instruction}")
    return "\n".join(
        [
            base_prompt,
            "",
            *target_lines,
            "Source Excerpt:",
            source_excerpt,
            "",
            "## Current Author Draft Context",
            _compact_text(source_draft.content if source_draft is not None else "", 1400),
        ]
    )


def _compact_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head = max(0, limit // 2)
    tail = max(0, limit - head)
    return f"{text[:head]}\n...\n{text[-tail:]}"
