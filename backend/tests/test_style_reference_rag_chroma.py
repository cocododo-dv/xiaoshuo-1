"""立项 C — 内容克制 RAG 在**真实 chroma 后端**上的集成测试。

Windows 自动跳过(conftest:@pytest.mark.chroma_integration + sys.platform=win32 → skip);
经 WSL/Linux 跑(.venv-wsl,见 CLAUDE.md)。验证 memory 单测覆盖不到的 chroma 代码路径
(ChromaVectorStore.write_collection/query + 确定性 embedding + None 元数据过滤)，
并验证 ANN 只接收风格签名搜索码，最终选择不同内容但同风格的候选。

注:向量后端仍用确定性 embedding，但输入已从原文换成私用区风格分箱 token；
连续风格距离在 shortlist 后重算。合成 A/B 是链路诊断，不是人类文学质量证据。

本文件只导入 rag/repository/models/vector_store(干净链),不触发 system_config 重导入,
故可在仅装 chromadb 的 WSL venv 上单独运行。
"""

from __future__ import annotations

import pytest

from novel_system.services.style_reference import rag
from novel_system.services.style_reference.rag_evaluation import load_rag_ab_manifest
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.vector_store import get_vector_store

pytestmark = pytest.mark.chroma_integration

# 8 段区分度高的语料(不同场景/题材),供 partial-query hit@k 评测。
_CORPUS = [
    (
        "p0",
        "dialogue",
        "“你终于肯回来了。”母亲倚在门框上，声音很轻，却带着十年的埋怨与释然。",
    ),
    (
        "p1",
        "description_env",
        "深秋的码头泛着铁锈味，海风把缆绳吹得呜呜作响，灰白的雾压在桅杆顶端。",
    ),
    (
        "p2",
        "action",
        "他翻身越过断墙，靴底擦着碎玻璃，三步并作两步冲上锈蚀的铁梯，直扑顶楼。",
    ),
    (
        "p3",
        "psychology",
        "她盯着那封没有署名的信，心跳逐渐失序，既盼望是他，又害怕真的是他。",
    ),
    (
        "p4",
        "narration",
        "战争结束后的第三个春天，小城重新支起了茶摊，孩子们在弹坑边追逐纸鸢。",
    ),
    (
        "p5",
        "description_char",
        "老兵的左手缺了两根指头，掌心的老茧像干涸的河床，握起茶杯时微微发抖。",
    ),
    (
        "p6",
        "dialogue",
        "“情报已经送出去了。”他压低嗓音，把怀表塞进她手里，“按约定，子夜的钟声响三下就走。”",
    ),
    (
        "p7",
        "action",
        "火车汽笛长鸣，她攥紧裙角追着月台跑，皮箱在石板上磕出闷响，终究没能追上那节车厢。",
    ),
]


def _seed(session):
    repo = StyleReferenceRepository(session)
    book, run, prof = "chroma_book", "chroma_run", "chroma_profile"
    repo.create_book(
        book_id=book,
        title="t",
        source_kind="upload",
        cloud_policy="allow_full_cloud",
        text_checksum="chk_chroma",
        total_chars=600,
        status="ready",
        stats_json={
            "rights_declaration": {
                "declared": True,
                "analysis_rights": True,
                "send_rights": True,
            }
        },
    )
    repo.create_run(run_id=run, book_id=book, status="done", phase="done")
    for i, (pid, ptype, text) in enumerate(_CORPUS):
        repo.create_paragraph(
            paragraph_id=f"{pid}",
            book_id=book,
            paragraph_index=i,
            paragraph_type=ptype,
            start_offset=0,
            end_offset=len(text),
            text=text,
            char_count=len(text),
            classifier_confidence=0.9,
        )
    profile = repo.create_profile(
        profile_id=prof,
        book_id=book,
        run_id=run,
        title="t",
        status="active",
        profile_json={
            "narrative_summary": "战后离别与重逢的克制叙事",
            "style_features": ["短句"],
        },
        coverage_json={},
        source_finding_ids_json=[],
    )
    session.flush()
    return profile


def test_chroma_build_creates_three_granularity_collections(session):
    profile = _seed(session)
    counts = rag.build_rag_index(
        session, profile
    )  # 不传 store → get_vector_store()=chroma
    assert counts.get("paragraph") == len(_CORPUS)
    assert counts.get("sentence", 0) >= len(_CORPUS)
    assert counts.get("scene", 0) >= 1
    store = get_vector_store()
    for gran in rag.GRANULARITIES:
        assert store.collection_exists(
            rag.rag_collection_name(profile.profile_id, gran)
        )


def test_chroma_content_independent_style_pair_selects_target(session):
    profile = _seed(session)
    rag.build_rag_index(session, profile)
    case = load_rag_ab_manifest()["cases"][0]
    granularity = case["granularity"]
    target = case["style_target"]
    distractor = case["content_distractor"]
    get_vector_store().write_collection(
        rag.rag_collection_name(profile.profile_id, granularity),
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
    retriever = rag.RagRetriever(session)
    hits = retriever.retrieve_per_granularity(
        profile.profile_id,
        case["query"],
        top_k=2,
    )[granularity]

    assert [hit.snippet_id for hit in hits] == [
        "different_topic_same_style",
        "same_topic_wrong_style",
    ]
    assert hits[0].style_score > hits[1].style_score


def test_chroma_sentence_and_scene_recall_nonempty(session):
    """sentence/scene 粒度在 chroma 上能召回(plumbing + 打分链路走通)。"""
    profile = _seed(session)
    rag.build_rag_index(session, profile)
    retriever = rag.RagRetriever(session)
    query = "码头的海风与缆绳，铁锈味弥漫"
    per = retriever.retrieve_per_granularity(profile.profile_id, query, top_k=5)
    assert len(per["sentence"]) > 0
    assert len(per["scene"]) > 0
    # 合并去重截断后仍有结果
    merged = retriever.retrieve(profile.profile_id, query, inject_max=5)
    assert 0 < len(merged) <= 5


def test_chroma_delete_rag_index_removes_collections(session):
    profile = _seed(session)
    rag.build_rag_index(session, profile)
    store = get_vector_store()
    assert store.collection_exists(
        rag.rag_collection_name(profile.profile_id, "paragraph")
    )
    rag.delete_rag_index(profile.profile_id)
    for gran in rag.GRANULARITIES:
        assert not store.collection_exists(
            rag.rag_collection_name(profile.profile_id, gran)
        )
    assert not store.collection_exists(
        rag.rag_manifest_collection_name(profile.profile_id)
    )
