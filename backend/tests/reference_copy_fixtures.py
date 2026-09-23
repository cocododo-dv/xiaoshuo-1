"""风格参考 v3 抄袭门测试共用的种子：一本参考书（段落）+ 画像（生成期受保护专名）+ 场景生成绑定。

全部是合成文本，不含任何真实参考书的原文或人名。
"""

from __future__ import annotations

from novel_system.db.models import (
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
)

# 一段 60 字以上的合成「参考原文」：评审探针用的就是「连续 60 字照抄」
REFERENCE_PASSAGE = (
    "灯塔守夜人把最后一盏煤油灯挂上铁钩，海风从裂开的窗缝里钻进来，"
    "吹得账本一页页翻过去，他数着潮水退去的次数，直到天边泛起鱼肚白。"
)
PROTECTED_NAME = "欧文·灰港"


def seed_bound_reference(
    session,
    *,
    seed: str,
    scope: str,
    scope_ref_id: str,
    paragraphs: tuple[str, ...] = (REFERENCE_PASSAGE,),
    protected_terms: tuple[str, ...] = (),
    protected_source: str = "protected_auto",
    config_json: dict | None = None,
) -> dict[str, str]:
    book_id = f"sr_book_copy_{seed}"
    run_id = f"sr_run_copy_{seed}"
    profile_id = f"sr_profile_copy_{seed}"
    binding_id = f"sr_bind_copy_{seed}"
    session.add(
        StyleReferenceBook(
            book_id=book_id,
            title="合成参考",
            source_kind="upload",
            cloud_policy="allow_full_cloud",
            text_checksum=f"checksum-copy-{seed}",
            status="ready",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
        )
    )
    session.flush()
    session.add(StyleReferenceRun(run_id=run_id, book_id=book_id, status="done", phase="done"))
    session.flush()
    session.add(
        StyleReferenceProfile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title="合成画像",
            status="active",
            profile_json={"narrative_summary": "n"},
        )
    )
    session.flush()
    offset = 0
    for index, text in enumerate(paragraphs):
        session.add(
            StyleReferenceParagraph(
                paragraph_id=f"sr_par_copy_{seed}_{index}",
                book_id=book_id,
                paragraph_index=index,
                paragraph_type="narration",
                start_offset=offset,
                end_offset=offset + len(text),
                text=text,
                char_count=len(text),
            )
        )
        offset += len(text)
    for index, term in enumerate(protected_terms):
        session.add(
            StyleReferenceBannedTerm(
                term_id=f"sr_term_copy_{seed}_{index}",
                profile_id=profile_id,
                term=term,
                source=protected_source,
                scope="generation",
            )
        )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id=binding_id,
            profile_id=profile_id,
            scope=scope,
            scope_ref_id=scope_ref_id,
            task_type="scene_generation",
            strategy="mixed",
            config_json=dict(config_json or {"draft_mode": "style_first"}),
            status="active",
        )
    )
    session.commit()
    return {"book_id": book_id, "profile_id": profile_id, "binding_id": binding_id}
