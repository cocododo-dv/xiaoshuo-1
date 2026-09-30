"""风格参考 v3 · 渲染结果的进程内缓存（J1：一场的几道工序不重复渲染；从 ``inject/render.py`` 拆出）。

键里有：库、契约哈希与模式、参考方式、bundle / 场景、角色与落点、实际窗数、章内位置、场面标签、对白多寡、呈现方式、
改稿维、近期偏差、窗口索引的根哈希、接收节点与路由（H1：换了节点路由不会拿到旧渲染）、没有 bundle 的渲染用的是
哪一份冻结选窗（M4）。按最近使用淘汰，至多 64 份；``reset_render_cache`` 登记在缓存复位表里。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from sqlalchemy.orm import Session

from novel_system.cache_registry import register_cache_reset
from novel_system.services.hash_engine import sha256_text
from novel_system.services.style_reference.inject.request import StyleRenderRequest

_CACHE_MAX = 64
_CACHE: "OrderedDict[str, Any]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def reset_render_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


register_cache_reset("style_reference.inject.render", reset_render_cache)


def cache_key(
    session: Session,
    policy: Any,
    request: StyleRenderRequest,
    *,
    root: str | None,
    reference_mode: str,
    route_token: str,
    selection_anchor: str = "",
) -> str:
    try:
        db = str(session.get_bind().url)
    except Exception:  # noqa: BLE001
        db = str(id(session))
    material = "|".join(
        [
            db,
            str(getattr(policy, "contract_hash", "") or ""),
            str(getattr(policy, "mode", "") or ""),
            str(reference_mode or ""),
            str(request.bundle_id or "live"),
            str(request.scene_id or ""),
            request.role,
            request.placement,
            str(request.effective_k(getattr(policy, "sample_windows", 0))),
            str(request.position or ""),
            ",".join(request.situation_tags),
            str(request.dialogue_heavy),
            str(request.rendering_mode or ""),
            ",".join(request.revise_dimensions),
            sha256_text("\x1f".join(request.recent_gaps))[:16],
            str(root or ""),
            # 接收提示的节点、路由是否本机、这一次送什么（H1：换了节点路由不会拿到旧渲染）
            route_token,
            # 没有 bundle 的渲染用的是哪一份冻结选窗（这一场当前 bundle 的，还是自己的 live 行，M4）
            selection_anchor,
        ]
    )
    return sha256_text(material)


def cache_get(key: str) -> Any:
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
        return cached


def cache_put(key: str, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


__all__ = ["cache_get", "cache_key", "cache_put", "reset_render_cache"]
