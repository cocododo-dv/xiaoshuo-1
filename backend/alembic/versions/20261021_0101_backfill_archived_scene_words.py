"""Backfill the catalog word count of scenes archived without one.

Revision ID: 20261021_0101
Revises: 20261012_0100
Create Date: 2026-10-21

目录里一场的字数（``scene_cards.words_current``）以前只在写作台保存作者稿时写：AI 起草台归档的场，写作台没打开过，
就一直记 0——目录与章节编排的字数、主页的全书进度、书架上的全书字数（按各场字数求和）都少算，旧的待办还据此报
「这一章有几场标了成稿却没有正文」。从这一版起每条归档路径都按归档的那一稿记字数（``Archiver.archive_final_scene``）；
这里把库里已经归档、字数还是 0 的场按同一份终稿补上一次：

- 只补 ``words_current`` 为 0 或空、当前终稿（``scene_run_states.current_final_scene_row_id``）已归档
  （``final_scenes.status = 'archived'``）的场——作者在写作台存过字的场（字数不是 0）一概不动；
- 字数口径与写作台相同：可见文本（HTML 去标签，script / style / template / noscript 里的不算）去掉全部空白后的
  字符数。这里冻结一份当时的算法，不导入应用代码。

只改数据，可重复执行（第二次没有要补的行）。降级是空操作：补上的字数本来就该在那里。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import sqlalchemy as sa
from alembic import op

revision = "20261021_0101"
down_revision = "20261012_0100"
branch_labels = None
depends_on = None

_WS_RE = re.compile(r"\s+")


class _VisibleText(HTMLParser):
    """写作台的字数口径（2026-10-21 时 ``services/writing_stats.py`` 的 ``_VisibleTextExtractor``）。"""

    _HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in self._HIDDEN_TAGS:
            self.hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def _count_words(text: str | None) -> int:
    if not text:
        return 0
    value = str(text)
    if "<" in value:
        parser = _VisibleText()
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    return len(_WS_RE.sub("", value))


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT scene_cards.scene_id AS scene_id, final_scenes.content AS content "
            "FROM scene_cards "
            "JOIN scene_run_states ON scene_run_states.scene_id = scene_cards.scene_id "
            "JOIN final_scenes ON final_scenes.row_id = scene_run_states.current_final_scene_row_id "
            "WHERE (scene_cards.words_current IS NULL OR scene_cards.words_current = 0) "
            "AND final_scenes.status = 'archived'"
        )
    ).fetchall()
    update = sa.text("UPDATE scene_cards SET words_current = :words WHERE scene_id = :scene_id")
    for row in rows:
        words = _count_words(row.content)
        if words > 0:
            bind.execute(update, {"words": words, "scene_id": row.scene_id})


def downgrade() -> None:
    pass
