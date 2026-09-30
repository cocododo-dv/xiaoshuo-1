"""作者稿——写作台正文的服务端真相（包门面）。

``AuthorDraftService`` 由三部分拼成（B08-08，原来是一个 2,139 行的文件）：

- ``store``：建稿 / 读当前稿 / 保存、回包形状、场景目标与起步正文；
- ``revisions``：修订快照（自动保存按 5 分钟合并）与版本列表；
- ``proposals``：写作台「AI 续写」的三条候选（模型调用、风格前缀、抄袭门、新一组替换旧候选）。

外面照旧 ``from novel_system.services.author_drafts import AuthorDraftService``。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from novel_system.services.author_drafts.proposals import (  # noqa: F401  (re-exported)
    CONTINUATION_VARIANT_DIRECTIONS,
    CONTINUATION_VARIANTS_MODE,
    AuthorDraftProposalsMixin,
)
from novel_system.services.author_drafts.revisions import (  # noqa: F401  (re-exported)
    REVISION_COALESCE_SECONDS,
    AuthorDraftRevisionsMixin,
)
from novel_system.services.author_drafts.store import AuthorDraftStoreMixin
from novel_system.services.author_lifecycle import AuthorLifecycleService


class AuthorDraftService(AuthorDraftStoreMixin, AuthorDraftRevisionsMixin, AuthorDraftProposalsMixin):
    def __init__(self, session: Session) -> None:
        self.session = session
        self.lifecycle = AuthorLifecycleService(session)
