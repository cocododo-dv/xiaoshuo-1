"""参考书活动清单:风格参考模块里在跑 / 十分钟内结束的耗时操作,一份统一形状(见 ``services/style_reference/activity``)。

界面只认作业表条目(键 ``job:<id>``,kind 为 classify / learn / check);带 ``compat_alias_of`` 的是给旧前端的
别名条目,新界面不显示。导入 / 重新分类 / 学习的响应里都带 ``job_id``,界面据此在这里跟进度——旧的
``GET /imports/{key}/progress`` 轮询已删除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.response import ok
from novel_system.api.routes.style_reference._common import PATH_PREFIX, ROUTE_TAGS, req_id
from novel_system.db.models import utcnow
from novel_system.services.style_reference.activity import list_activity

router = APIRouter(tags=ROUTE_TAGS)


@router.get(f"{PATH_PREFIX}/activity")
def get_activity(request: Request, session: Session = Depends(get_session)):
    """参考书活动清单(只读):作业表(分类 / 学习文风 / 对照检查)+ 尚未迁出的进程内登记簿条目。"""
    return ok(
        {"items": list_activity(session), "server_time": utcnow()},
        req_id=req_id(request),
    )
