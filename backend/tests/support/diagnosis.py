"""场景诊断测试共用的作品骨架、发现查找与脚本化节点运行器（B05-20：以前 round3 直接 import test_scene_diagnosis）。"""

from __future__ import annotations

from types import SimpleNamespace

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.author_drafts import AuthorDraftService


# ---------------------------------------------------------------- 一章两场、带作者稿正文的诊断用例（test_scene_diagnosis）


PROJECT_ID = "PROJECT_DIAG"
CHAPTER_ID = "DIAG_CH01"
SCENE_ID = "DIAG_CH01_SC01"


# 第 1 段：贴邻叠句「安静，安静」+ 模型腔「突然意识到」；第 3 段：连续三句以「她」开头 + 模型腔「她知道」
DRAFT_HTML = (
    "<p>门外很安静，安静到能听见潮水。她突然意识到，自己一直在等这一刻。</p>"
    "<p>许望没有回答。录音里传来三声钟响。</p>"
    "<p>她知道真相必须公开。她把证据袋压进袖口。她没有再看他。</p>"
)


def seed_scene(session, *, draft_html: str | None = DRAFT_HTML) -> str | None:
    session.add(StoryProject(project_id=PROJECT_ID, title="诊断统一", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="她必须决定是否公开证据。",
            main_plot_push="把旧档案线推进到公开真相的选择。",
            emotional_target="从职业克制转向道德压力。",
            ending_effect="读者知道她已经不能只做修复师。",
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="她发现关键录音，但必须决定公开还是隐藏。",
            beats_json=["修复录音", "听见编号", "决定暂缓公开"],
            exit_change="她把一半证据藏起来。",
            hook="录音最后出现她自己的心跳声。",
        )
    )
    session.commit()
    if draft_html is None:
        return None
    service = AuthorDraftService(session)
    draft = service.ensure("scene", SCENE_ID, actor_ref="writer")["draft"]
    saved = service.save(draft["draft_id"], {"content": draft_html, "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()
    return saved["draft"]["draft_id"] if "draft" in saved else draft["draft_id"]


def finding_of(payload: dict, source: str, dimension: str) -> dict:
    hits = [item for item in payload["findings"] if item["source"] == source and item["dimension"] == dimension]
    assert hits, f"expected a {source}:{dimension} finding, got {[(f['source'], f['dimension']) for f in payload['findings']]}"
    return hits[0]


def distinct_chars(count: int) -> str:
    return "".join(chr(0x4E00 + index) for index in range(count))


def scripted_runner(output: dict, calls: list):
    class _Runner:
        def __init__(self, session, **kwargs) -> None:
            self.session = session

        @property
        def provider_execution_mode(self):
            return "online"

        def run(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                llm_call_id=f"llm_call_scripted_{len(calls)}",
                response=SimpleNamespace(structured_output=output),
            )

    return _Runner


SCENE2_ID = "DIAG_CH01_SC02"
SCENE2_HTML = "<p>许望把钟停了。</p><p>她终于开口，把证据袋放在桌上。</p>"


def add_second_scene(session, html: str = SCENE2_HTML) -> str:
    session.add(
        SceneCard(
            scene_id=SCENE2_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=2,
            scene_goal="她开口。",
            beats_json=[],
            exit_change="",
            hook="",
        )
    )
    session.commit()
    service = AuthorDraftService(session)
    draft = service.ensure("scene", SCENE2_ID, actor_ref="writer")["draft"]
    service.save(draft["draft_id"], {"content": html, "base_revision_no": draft["revision_no"]}, actor_ref="writer")
    session.commit()
    return draft["draft_id"]
