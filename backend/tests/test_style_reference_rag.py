"""立项 C — Strategy C(RAG)三粒度向量召回测试。

确定性单测(memory 向量后端):内容克制索引构建 / 切句 / scene 聚合 /
三粒度召回 / 风格距离 rerank / C 策略注入(红线随注)/ 防漂移随上下文变化 /
优雅退化 / 清理。chroma 集成由 WSL 跑(本文件全部用 memory,Windows 安全)。
"""

from __future__ import annotations

import pytest

from novel_system.services.style_reference import rag
from novel_system.services.style_reference.cleanup import purge_derived_data
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.rag_evaluation import (
    load_rag_ab_manifest,
    run_rag_content_independence_ab,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.style_signature import (
    STYLE_SIGNATURE_VERSION,
)
from novel_system.services.vector_store import InMemoryVectorStore

# 参考语料:覆盖对话、环境、动作、心理与叙述的不同结构。
_PARAGRAPHS = [
    ("p0", "dialogue", "“你来了。”他轻声说，没有回头。"),
    ("p1", "description_env", "窗外的雨下个不停，青灰色的瓦檐滴着水珠。"),
    ("p2", "action", "她猛地推开门，冲进漆黑的走廊，脚步声急促。"),
    ("p3", "psychology", "他心里清楚，这一别也许就是永远，可终究说不出口。"),
    ("p4", "narration", "那一年的冬天格外漫长，雪落了又化，化了又落。"),
]


def _seed_book_with_paragraphs(
    session,
    *,
    seed: str,
    status: str = "active",
    cloud_policy: str = "allow_full_cloud",
):
    repo = StyleReferenceRepository(session)
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    profile_id = f"sr_profile_{seed}"
    repo.create_book(
        book_id=book_id,
        title="t",
        source_kind="upload",
        cloud_policy=cloud_policy,
        text_checksum=f"chk_{seed}",
        total_chars=200,
        status="ready",
        stats_json=(
            {
                "rights_declaration": {
                    "declared": True,
                    "analysis_rights": True,
                    "send_rights": True,
                }
            }
            if cloud_policy != "local_only"
            else {}
        ),
    )
    repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
    for i, (pid, ptype, text) in enumerate(_PARAGRAPHS):
        repo.create_paragraph(
            paragraph_id=f"{seed}_{pid}",
            book_id=book_id,
            paragraph_index=i,
            paragraph_type=ptype,
            start_offset=0,
            end_offset=len(text),
            text=text,
            char_count=len(text),
            classifier_confidence=0.9,
        )
    profile = repo.create_profile(
        profile_id=profile_id,
        book_id=book_id,
        run_id=run_id,
        title="t",
        status=status,
        profile_json={
            "narrative_summary": "雨夜离别的克制叙事",
            "style_features": ["短句"],
        },
        coverage_json={},
        source_finding_ids_json=[],
    )
    session.flush()
    return profile


# --------------------------------------------------------------------------- 纯函数


def test_split_sentences_keeps_punctuation_and_filters_short():
    out = rag.split_sentences("他走了。她问：你来了吗？嗯。", min_chars=3)
    assert out == ["他走了。", "她问：你来了吗？"]  # "嗯。" 过短被滤


def test_aggregate_scenes_windows_by_chars():
    class P:
        def __init__(self, idx, t, txt):
            self.paragraph_index, self.paragraph_type, self.text = idx, t, txt

    paras = [
        P(0, "narration", "甲" * 400),
        P(1, "dialogue", "乙" * 400),
        P(2, "action", "丙" * 100),
    ]
    blocks = rag.aggregate_scenes(paras, target_chars=600)
    # 前两段累加 800≥600 封一块;第三段单独成块(flush 尾部)
    assert len(blocks) == 2
    assert blocks[0]["paragraph_index"] == 0
    assert blocks[1]["paragraph_index"] == 2


# --------------------------------------------------------------------------- 构建 + 召回


def test_build_rag_index_creates_three_granularities(session):
    profile = _seed_book_with_paragraphs(session, seed="b1")
    store = InMemoryVectorStore()
    counts = rag.build_rag_index(session, profile, vector_store=store)
    assert counts["paragraph"] == len(_PARAGRAPHS)
    assert counts["sentence"] >= len(_PARAGRAPHS)  # 每段至少一句
    assert counts["scene"] >= 1
    for gran in rag.GRANULARITIES:
        assert store.collection_exists(
            rag.rag_collection_name(profile.profile_id, gran)
        )


def test_retrieve_per_granularity_hits_relevant(session):
    profile = _seed_book_with_paragraphs(session, seed="b2")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    retriever = rag.RagRetriever(session, vector_store=store)
    # query 取自 p2(动作段)文字 → 该段应在 paragraph 粒度命中 top1
    per = retriever.retrieve_per_granularity(
        profile.profile_id, "她推开门冲进漆黑的走廊"
    )
    assert set(per.keys()) == set(rag.GRANULARITIES)
    para_texts = [s.text for s in per["paragraph"]]
    assert any("推开门" in t for t in para_texts)


def test_retrieve_merges_dedupes_and_caps(session):
    profile = _seed_book_with_paragraphs(session, seed="b3")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    retriever = rag.RagRetriever(session, vector_store=store)
    out = retriever.retrieve(profile.profile_id, "雨夜走廊离别", inject_max=4)
    assert 0 < len(out) <= 4
    texts = [s.text for s in out]
    assert len(texts) == len(set(texts))  # 去重


def test_retrieve_empty_query_returns_nothing(session):
    profile = _seed_book_with_paragraphs(session, seed="b4")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    retriever = rag.RagRetriever(session, vector_store=store)
    assert retriever.retrieve(profile.profile_id, "   ") == []


def test_retrieve_deterministic(session):
    profile = _seed_book_with_paragraphs(session, seed="b5")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    retriever = rag.RagRetriever(session, vector_store=store)
    q = "推开门冲进走廊"
    first = [s.snippet_id for s in retriever.retrieve(profile.profile_id, q)]
    second = [s.snippet_id for s in retriever.retrieve(profile.profile_id, q)]
    assert first == second


# --------------------------------------------------------------------------- 渲染


def test_render_rag_block_labels_and_red_line_contract(session):
    profile = _seed_book_with_paragraphs(session, seed="b6")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    retriever = rag.RagRetriever(session, vector_store=store)
    snippets = retriever.retrieve(profile.profile_id, "雨夜推开门离别")
    block = rag.render_rag_block(snippets)
    assert block.startswith("[风格检索样例]")
    assert "严禁照抄" in block


def test_render_rag_block_empty_on_no_snippets():
    assert rag.render_rag_block([]) == ""


# --------------------------------------------------------------------------- 删除/清理


def test_delete_rag_index_removes_collections(session):
    profile = _seed_book_with_paragraphs(session, seed="b7")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    rag.delete_rag_index(profile.profile_id, vector_store=store)
    for gran in rag.GRANULARITIES:
        assert not store.collection_exists(
            rag.rag_collection_name(profile.profile_id, gran)
        )
    assert not store.collection_exists(
        rag.rag_manifest_collection_name(profile.profile_id)
    )


# --------------------------------------------------------------------------- C 策略注入(env memory 后端)


def _bind_c_strategy(session, profile, *, project_id):
    repo = StyleReferenceRepository(session)
    repo.create_binding(
        binding_id=f"bind_{profile.profile_id}",
        profile_id=profile.profile_id,
        scope="project",
        scope_ref_id=project_id,
        task_type="scene_generation",
        strategy="C",
        config_json={},
        status="active",
    )
    session.flush()


def test_c_strategy_injects_rag_block_with_red_line(session):
    # 用 env memory 后端(conftest 设 NOVEL_SYSTEM_VECTOR_BACKEND=memory),
    # 与 InjectionService._render_rag 内部 get_vector_store() 一致。
    profile = _seed_book_with_paragraphs(session, seed="cinj")
    rag.build_rag_index(session, profile)  # 不传 store → 写入 env 后端
    _bind_c_strategy(session, profile, project_id="proj_cinj")
    svc = InjectionService(session)
    svc.context_text = "她推开门冲进漆黑的走廊，脚步声急促"
    frags = svc.fragments_for("proj_cinj", "scene_generation")
    assert frags.rag_block  # C 真召回非空
    prefix = frags.to_system_prompt_prefix()
    assert "风格检索样例" in prefix
    assert "严格禁止" in prefix or "严禁" in prefix  # 红线段随注


def test_c_strategy_drift_changes_snippets_with_styled_context(session):
    """v2:只有调用方显式标记 styled_context(前文已风格化)时,RAG query 才用前文签名。"""
    profile = _seed_book_with_paragraphs(session, seed="cdrift")
    rag.build_rag_index(session, profile)
    _bind_c_strategy(session, profile, project_id="proj_cdrift")
    svc = InjectionService(session)
    svc.styled_context = True
    svc.context_text = "窗外的雨下个不停，青灰色的瓦檐"  # 偏 description_env
    block_env = svc.fragments_for("proj_cdrift", "scene_generation").rag_block
    svc2 = InjectionService(session)
    svc2.styled_context = True
    svc2.context_text = "她猛地推开门，冲进漆黑的走廊"  # 偏 action
    block_act = svc2.fragments_for("proj_cdrift", "scene_generation").rag_block
    assert block_env and block_act
    assert block_env != block_act  # 召回随(已风格化的)上下文变化


def test_c_strategy_default_query_is_profile_signature_not_context(session):
    """v2 默认(中性稿不代表目标风格):query = 画像代表签名,召回不随 context 变;审计 rag_outcome=hit。"""
    profile = _seed_book_with_paragraphs(session, seed="csig")
    rag.build_rag_index(session, profile)
    _bind_c_strategy(session, profile, project_id="proj_csig")
    svc = InjectionService(session)
    svc.context_text = "窗外的雨下个不停，青灰色的瓦檐"
    block_env = svc.fragments_for("proj_csig", "scene_generation").rag_block
    assert svc.last_runtime_audit["rag_outcome"] == "hit"
    svc2 = InjectionService(session)
    svc2.context_text = "她猛地推开门，冲进漆黑的走廊"
    block_act = svc2.fragments_for("proj_csig", "scene_generation").rag_block
    assert block_env and block_act
    assert block_env == block_act


def test_c_strategy_ensures_index_when_missing(session, monkeypatch):
    """v2:未建索引 → _render_rag 前调用幂等 ensure_rag_index(memory 后端重建),C 仍有召回。"""
    profile = _seed_book_with_paragraphs(session, seed="cdeg")
    _bind_c_strategy(session, profile, project_id="proj_cdeg")
    calls: list[str] = []
    real_ensure = rag.ensure_rag_index

    def _spy(sess, prof, **kwargs):
        calls.append(str(prof.profile_id))
        return real_ensure(sess, prof, **kwargs)

    monkeypatch.setattr(rag, "ensure_rag_index", _spy)
    svc = InjectionService(session)
    svc.context_text = "任意上下文"
    frags = svc.fragments_for("proj_cdeg", "scene_generation")
    assert calls == [profile.profile_id]
    assert frags.rag_block  # 重建后真召回
    assert frags.positive_block
    assert svc.last_runtime_audit["rag_outcome"] == "hit"
    # 第二次:索引已就绪,ensure 幂等(ready,不重建)
    assert real_ensure(session, profile)["status"] == "ready"


def test_c_strategy_empty_recall_writes_rag_outcome_unavailable(session, monkeypatch):
    """空召回(向量后端不可用)→ rag_block 空、last_runtime_audit.rag_outcome == "unavailable"。"""
    profile = _seed_book_with_paragraphs(session, seed="cunav2")
    _bind_c_strategy(session, profile, project_id="proj_cunav2")
    monkeypatch.setattr(rag, "_resolve_store", lambda vs: None)
    svc = InjectionService(session)
    svc.context_text = "她推开门冲进漆黑的走廊"
    frags = svc.fragments_for("proj_cunav2", "scene_generation")
    assert frags.rag_block == ""
    assert frags.positive_block
    assert svc.last_runtime_audit["rag_outcome"] == "unavailable"
    assert svc.last_runtime_audit["outcome"] == "hit"  # 抽象块仍命中


def test_build_query_signatures_mean_drives_retriever_without_text(session):
    """build_query_signatures 给三粒度均值签名;retriever 用预设签名可在空 query 下召回。"""
    profile = _seed_book_with_paragraphs(session, seed="qsig")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    texts = [text for _pid, _ptype, text in _PARAGRAPHS]
    signatures = rag.build_query_signatures(texts)
    assert set(signatures) == set(rag.GRANULARITIES)
    for gran, sig in signatures.items():
        assert sig.granularity == gran
        assert all(0.0 <= value <= 1.0 for value in sig.features.values())
    # 均值签名 = 各段签名的逐特征平均
    per_paragraph = [rag.extract_style_signature(t, granularity="paragraph") for t in texts]
    name = "shape.unit_length"
    expected = sum(sig.features[name] for sig in per_paragraph) / len(per_paragraph)
    assert abs(signatures["paragraph"].features[name] - expected) < 1e-9
    retriever = rag.RagRetriever(session, vector_store=store, query_signatures=signatures)
    hits = retriever.retrieve(profile.profile_id, "")
    assert hits  # 无文本 query 也能凭签名召回
    assert all(hit.content_overlap == 0.0 for hit in hits)  # 无 query 文本 → 无内容惩罚
    # 无签名、空 query 仍返回空(旧契约不变)
    assert rag.RagRetriever(session, vector_store=store).retrieve(profile.profile_id, "  ") == []
    assert rag.build_query_signatures(["", "  "]) == {}


def test_retriever_preferred_paragraph_types_soft_filter(session):
    """场景段型软过滤:命中段型的片段前置;无命中时不丢任何片段。"""
    profile = _seed_book_with_paragraphs(session, seed="pref")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    texts = [text for _pid, _ptype, text in _PARAGRAPHS]
    signatures = rag.build_query_signatures(texts)
    plain = rag.RagRetriever(session, vector_store=store, query_signatures=signatures)
    baseline_hits = plain.retrieve(profile.profile_id, "")
    assert baseline_hits
    preferred = rag.RagRetriever(
        session,
        vector_store=store,
        query_signatures=signatures,
        preferred_paragraph_types={"dialogue"},
    )
    hits = preferred.retrieve(profile.profile_id, "")
    assert hits
    matched = [h for h in hits if h.paragraph_type == "dialogue"]
    assert matched, "fixture 必须召回 dialogue 片段,否则前置断言无从检验"
    # 前置:所有 dialogue 命中都排在所有非 dialogue 命中之前(软过滤只重排,不丢片段)
    assert [h.paragraph_type for h in hits[: len(matched)]] == ["dialogue"] * len(matched)
    assert all(h.paragraph_type != "dialogue" for h in hits[len(matched) :])
    # 偏好确实改变了保留集合:无偏好时按分数只留下更少的 dialogue 片段
    assert len(matched) > sum(1 for h in baseline_hits if h.paragraph_type == "dialogue")
    # 偏好一个不存在的段型 → 与无偏好结果一致(软过滤不丢片段)
    none_match = rag.RagRetriever(
        session,
        vector_store=store,
        query_signatures=signatures,
        preferred_paragraph_types={"no_such_type"},
    ).retrieve(profile.profile_id, "")
    assert [h.snippet_id for h in none_match] == [h.snippet_id for h in baseline_hits]


def test_purge_derived_data_deletes_rag_index(session):
    profile = _seed_book_with_paragraphs(session, seed="cpurge")
    rag.build_rag_index(session, profile)
    from novel_system.services.vector_store import get_vector_store

    store = get_vector_store()
    assert store.collection_exists(
        rag.rag_collection_name(profile.profile_id, "paragraph")
    )
    purge_derived_data(session, "sr_book_cpurge")
    assert not store.collection_exists(
        rag.rag_collection_name(profile.profile_id, "paragraph")
    )


# --------------------------------------------------------------------------- 审查补强:退化路径 / 红线契约 / 隐私


def test_c_strategy_local_only_skips_rag(session):
    # 附录 B — local_only 的书:RAG 原文片段不得送往云端 LLM,C 跳过 RAG;
    # 但抽象正向特征(positive)仍注入。
    profile = _seed_book_with_paragraphs(
        session, seed="clocal", cloud_policy="local_only"
    )
    rag.build_rag_index(session, profile)
    _bind_c_strategy(session, profile, project_id="proj_clocal")
    svc = InjectionService(session)
    svc.context_text = "她推开门冲进漆黑的走廊"
    frags = svc.fragments_for("proj_clocal", "scene_generation")
    assert frags.rag_block == ""  # local_only:原文不注入
    assert frags.positive_block  # 抽象特征仍注入


def test_c_strategy_inactive_profile_no_injection(session):
    # profile 非 active(draft)→ binding 解析返回空 fragments(退化路径)
    profile = _seed_book_with_paragraphs(session, seed="cinact", status="draft")
    rag.build_rag_index(session, profile)
    _bind_c_strategy(session, profile, project_id="proj_cinact")
    svc = InjectionService(session)
    svc.context_text = "她推开门"
    frags = svc.fragments_for("proj_cinact", "scene_generation")
    assert frags.rag_block == "" and frags.positive_block == ""
    assert frags.to_system_prompt_prefix() == ""


def test_c_strategy_degrades_when_vector_store_unavailable(session, monkeypatch):
    # 向量后端不可用(_resolve_store 返 None,如 Windows 原生 chroma)→ rag_block 空,
    # 但 positive 仍注入,不报错。
    profile = _seed_book_with_paragraphs(session, seed="cunavail")
    rag.build_rag_index(session, profile)
    _bind_c_strategy(session, profile, project_id="proj_cunavail")
    monkeypatch.setattr(rag, "_resolve_store", lambda vs: None)
    svc = InjectionService(session)
    svc.context_text = "她推开门冲进漆黑的走廊"
    frags = svc.fragments_for("proj_cunavail", "scene_generation")
    assert frags.rag_block == ""
    assert frags.positive_block


def test_anti_plagiarism_attached_when_rag_present(session):
    # 红线契约:rag_block 非空 ⟹ anti_plagiarism_block 非空(直接断言字段,非仅 prefix 子串)
    profile = _seed_book_with_paragraphs(session, seed="credline")
    rag.build_rag_index(session, profile)
    _bind_c_strategy(session, profile, project_id="proj_credline")
    svc = InjectionService(session)
    svc.context_text = "她推开门冲进漆黑的走廊"
    frags = svc.fragments_for("proj_credline", "scene_generation")
    assert frags.rag_block
    assert frags.anti_plagiarism_block
    # 拼装顺序:红线段在所有风格块之后(prefix 尾部)
    prefix = frags.to_system_prompt_prefix()
    assert prefix.index("风格检索样例") < prefix.index("严格禁止")


def test_anti_plagiarism_omitted_when_all_blocks_empty(session):
    # 所有风格块全空 ⟹ 红线段不输出,整体 no-op
    repo = StyleReferenceRepository(session)
    repo.create_book(
        book_id="sr_book_empty",
        title="t",
        source_kind="upload",
        cloud_policy="allow_full_cloud",
        text_checksum="chk_empty",
        total_chars=0,
        status="ready",
        stats_json={
            "rights_declaration": {
                "declared": True,
                "analysis_rights": True,
                "send_rights": True,
            }
        },
    )
    repo.create_run(
        run_id="sr_run_empty", book_id="sr_book_empty", status="done", phase="done"
    )
    profile = repo.create_profile(
        profile_id="sr_profile_empty",
        book_id="sr_book_empty",
        run_id="sr_run_empty",
        title="t",
        status="active",
        profile_json={},
        coverage_json={},
        source_finding_ids_json=[],
    )
    session.flush()
    _bind_c_strategy(session, profile, project_id="proj_empty")
    svc = InjectionService(session)
    frags = svc.fragments_for("proj_empty", "scene_generation")
    assert (
        frags.rag_block == ""
        and frags.positive_block == ""
        and frags.anti_plagiarism_block == ""
    )
    assert frags.to_system_prompt_prefix() == ""


def test_drift_retrieve_snippet_sets_differ_by_context(session):
    # 强化防漂移断言:不同 context 召回的 snippet **id 集合**确实不同(非仅字符串不等)
    profile = _seed_book_with_paragraphs(session, seed="driftids")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    r = rag.RagRetriever(session, vector_store=store)
    env_ids = {
        s.snippet_id for s in r.retrieve(profile.profile_id, "窗外的雨青灰色瓦檐滴水珠")
    }
    act_ids = {
        s.snippet_id
        for s in r.retrieve(profile.profile_id, "她推开门冲进漆黑走廊脚步急促")
    }
    assert env_ids and act_ids
    assert env_ids != act_ids


def test_retrieve_inject_max_zero_returns_empty(session):
    profile = _seed_book_with_paragraphs(session, seed="cap0")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    r = rag.RagRetriever(session, vector_store=store)
    assert r.retrieve(profile.profile_id, "推开门", inject_max=0) == []


# --------------------------------------------------------------------------- 内容独立风格召回 A/B


def test_index_search_documents_contain_style_bins_not_source_prose(session):
    profile = _seed_book_with_paragraphs(session, seed="signaturedocs")
    store = InMemoryVectorStore()
    rag.build_rag_index(session, profile, vector_store=store)
    docs = store.load_collection(
        rag.rag_collection_name(profile.profile_id, "paragraph")
    )

    assert docs
    for doc in docs:
        assert doc["signature_version"] == STYLE_SIGNATURE_VERSION
        assert doc["source_text"] in {text for _pid, _ptype, text in _PARAGRAPHS}
        assert doc["source_text"] not in doc["text"]
        assert all("\ue000" <= char <= "\uf8ff" for char in doc["text"])


def test_ensure_rag_index_rebuilds_partial_v2_then_becomes_ready(session):
    profile = _seed_book_with_paragraphs(session, seed="ensurev2")
    store = InMemoryVectorStore()
    store.write_collection(
        rag.rag_collection_name(profile.profile_id, "sentence"),
        [],
    )

    rebuilt = rag.ensure_rag_index(session, profile, vector_store=store)

    assert rebuilt["status"] == "rebuilt"
    assert rebuilt["signature_version"] == STYLE_SIGNATURE_VERSION
    assert all(
        store.collection_exists(
            rag.rag_collection_name(profile.profile_id, granularity)
        )
        for granularity in rag.GRANULARITIES
    )
    assert store.collection_exists(rag.rag_manifest_collection_name(profile.profile_id))

    ready = rag.ensure_rag_index(session, profile, vector_store=store)
    assert ready == {
        "profile_id": profile.profile_id,
        "status": "ready",
        "signature_version": STYLE_SIGNATURE_VERSION,
    }


def test_failed_index_build_never_leaves_completion_marker(session):
    profile = _seed_book_with_paragraphs(session, seed="atomicmarker")

    class FailOnParagraphStore(InMemoryVectorStore):
        def write_collection(self, collection_name, documents):
            if collection_name.endswith("_paragraph"):
                raise RuntimeError("synthetic paragraph write failure")
            super().write_collection(collection_name, documents)

    store = FailOnParagraphStore()

    with pytest.raises(RuntimeError, match="synthetic paragraph write failure"):
        rag.build_rag_index(session, profile, vector_store=store)

    assert not store.collection_exists(
        rag.rag_manifest_collection_name(profile.profile_id)
    )


def test_retriever_refuses_legacy_raw_text_documents(session):
    profile_id = "legacy_raw_profile"
    store = InMemoryVectorStore()
    store.write_collection(
        rag.rag_collection_name(profile_id, "paragraph"),
        [
            {
                "id": "legacy_raw",
                "text": "雨夜窗纸旧信",
                "granularity": "paragraph",
                "paragraph_type": "narration",
            }
        ],
    )

    hits = rag.RagRetriever(session, vector_store=store).retrieve_per_granularity(
        profile_id,
        "雨夜窗纸旧信",
    )

    assert hits["paragraph"] == []


def test_frozen_content_independence_ab_beats_legacy_content_control():
    report = run_rag_content_independence_ab()

    assert report["case_count"] == 6
    assert report["control"]["style_hit_at_1"] == 0.0
    assert report["treatment"]["style_hit_at_1"] == 1.0
    assert report["treatment"]["content_distractor_rate"] == 0.0
    assert report["treatment"]["mean_style_margin"] >= 0.05
    assert report["passed"] is True
    assert report["human_verified"] is False
    assert report["policy_evidence_eligible"] is False


def test_real_retriever_selects_different_topic_same_style_over_content_distractor(
    session,
):
    case = load_rag_ab_manifest()["cases"][0]
    granularity = case["granularity"]
    profile_id = "rag_ab_profile"
    target = case["style_target"]
    distractor = case["content_distractor"]
    store = InMemoryVectorStore()
    store.write_collection(
        rag.rag_collection_name(profile_id, granularity),
        [
            rag._style_index_document(
                doc_id=target["id"],
                source_text=target["text"],
                granularity=granularity,
                paragraph_type="narration",
                paragraph_index=0,
            ),
            rag._style_index_document(
                doc_id=distractor["id"],
                source_text=distractor["text"],
                granularity=granularity,
                paragraph_type="narration",
                paragraph_index=1,
            ),
        ],
    )

    hits = rag.RagRetriever(session, vector_store=store).retrieve_per_granularity(
        profile_id,
        case["query"],
        top_k=2,
    )[granularity]

    assert [hit.snippet_id for hit in hits] == [
        "different_topic_same_style",
        "same_topic_wrong_style",
    ]
    assert hits[0].style_score > hits[1].style_score
    assert hits[0].content_overlap < hits[1].content_overlap
