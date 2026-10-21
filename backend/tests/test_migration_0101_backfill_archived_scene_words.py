"""迁移 20261021_0101：已归档、目录字数还是 0 的场按当前终稿补上字数（只改数据）。

AI 起草台归档的场以前不写 ``scene_cards.words_current``（只有写作台保存作者稿时写），目录、主页进度与全书字数都少算。
- 只补字数为 0 / 空、当前终稿已归档的场；作者存过字的场、终稿还没归档（准终稿）的场不动；
- 口径与写作台相同：HTML 去标签（script / style 里的不算）、去掉全部空白后的字符数；
- 降级是空操作，再升一次没有要补的行。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.support.migrations import insert_minimal_row, migrate

PREVIOUS_HEAD = "20261012_0100"
REVISION = "20261021_0101"


def _seed_scene(
    connection: sqlite3.Connection, scene_id: str, *, words: int | None, content: str, final_status: str
) -> None:
    seq = connection.execute("SELECT COUNT(*) FROM scene_cards").fetchone()[0] + 1
    insert_minimal_row(
        connection,
        "scene_cards",
        {
            "scene_id": scene_id, "chapter_id": "work-a_CH01", "project_id": "work-a", "scene_seq": seq,
            "words_current": words,
        },
    )
    insert_minimal_row(
        connection,
        "final_scenes",
        {
            "row_id": f"final_{scene_id}", "scene_id": scene_id, "chapter_id": "work-a_CH01",
            "content": content, "status": final_status,
        },
    )
    insert_minimal_row(
        connection,
        "scene_run_states",
        {"scene_id": scene_id, "scene_status": "archived", "current_final_scene_row_id": f"final_{scene_id}"},
    )


def _words(path: Path) -> dict[str, int | None]:
    with sqlite3.connect(path) as connection:
        return dict(connection.execute("SELECT scene_id, words_current FROM scene_cards ORDER BY scene_id"))


def test_0101_backfills_archived_scenes_that_still_read_zero_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "scene-words-0101.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        insert_minimal_row(connection, "story_projects", {"project_id": "work-a", "title": "雨城旧信"})
        insert_minimal_row(connection, "chapter_goals", {"chapter_id": "work-a_CH01", "project_id": "work-a"})
        # 起草台归档、写作台没打开过：字数 0（实库就是这样）
        _seed_scene(connection, "sc1_pipeline", words=0, content="林昭把旧信\n塞回案卷。", final_status="archived")
        # 字数列为空的旧行同样补
        _seed_scene(connection, "sc2_null", words=None, content="雨城 下了一夜", final_status="archived")
        # HTML 正文：标签与 script 里的字不算
        _seed_scene(
            connection, "sc3_html", words=0, content="<p>旧信</p><p>案 卷</p><script>var x = 1;</script>",
            final_status="archived",
        )
        # 作者在写作台存过字：以作者的为准，不动
        _seed_scene(connection, "sc4_author", words=321, content="林昭把旧信塞回案卷。", final_status="archived")
        # 终稿还没归档（准终稿也挂在 current_final_scene_row_id 上）：不补
        _seed_scene(connection, "sc5_near_final", words=0, content="林昭把旧信塞回案卷。", final_status="near_final_ready")

    migrate(path, REVISION, monkeypatch)

    assert _words(path) == {
        "sc1_pipeline": 10,
        "sc2_null": 6,
        "sc3_html": 4,
        "sc4_author": 321,
        "sc5_near_final": 0,
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (REVISION,)


def test_0101_downgrade_is_a_no_op_and_a_second_upgrade_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "scene-words-0101-down.db"
    migrate(path, PREVIOUS_HEAD, monkeypatch)
    with sqlite3.connect(path) as connection:
        insert_minimal_row(connection, "story_projects", {"project_id": "work-a", "title": "雨城旧信"})
        insert_minimal_row(connection, "chapter_goals", {"chapter_id": "work-a_CH01", "project_id": "work-a"})
        _seed_scene(connection, "sc1_pipeline", words=0, content="林昭把旧信塞回案卷。", final_status="archived")

    migrate(path, REVISION, monkeypatch)
    migrate(path, PREVIOUS_HEAD, monkeypatch, down=True)
    assert _words(path) == {"sc1_pipeline": 10}

    migrate(path, REVISION, monkeypatch)
    assert _words(path) == {"sc1_pipeline": 10}
