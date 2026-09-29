"""进程级缓存的复位登记处（叶子模块：不 import ``novel_system`` 的任何东西）。

为什么有它：好几处服务在进程里缓存按库内容算出来的东西（参考书的抄袭索引、风格策略、渲染好的文风段、参照分布、
校验过的运行时契约、章题画像、场景诊断、作业取消提示……），键是书 / 画像 / 场景 / 作业的 id 加版本或内容指纹。
生产进程只连一个库，这没问题；测试进程每个用例一个新库，却反复用同样的字面 id，上一个用例的缓存会被下一个用例
当成自己的数据读回去——只有在用例换了顺序或跑得更快时才露出来。以前各测试文件手写十几处复位，还有几处缓存根本
没有复位入口。现在每个缓存在自己的定义处登记复位函数，``tests/conftest.py`` 在每个用例前后调 ``reset_all_caches()``。

该登记的：键里带库里的 id / 版本（新库会重用它们）、或者测试会换掉其来源（monkeypatch 路径 / 配置）的进程级缓存。
每次读都自己核对来源的缓存（例如按文件 mtime + size 做键）可以不登记。生产代码不调 ``reset_all_caches``。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_RESETS: dict[str, Callable[[], object]] = {}
_LOCK = threading.Lock()


def register_cache_reset(name: str, reset: Callable[[], object]) -> Callable[[], object]:
    """登记一个缓存的复位函数（在缓存的定义处、模块导入时调用），返回 ``reset`` 本身。

    同名再登记会覆盖：模块被重新导入时换成新缓存对象的复位函数。"""
    key = str(name or "").strip()
    if not key:
        raise ValueError("cache reset name is required")
    if not callable(reset):
        raise TypeError(f"cache reset for {key!r} is not callable")
    with _LOCK:
        _RESETS[key] = reset
    return reset


def reset_all_caches() -> None:
    """按登记顺序复位全部已登记的缓存（还没导入的模块没有缓存，也就不在表里）。"""
    with _LOCK:
        resets = list(_RESETS.values())
    for reset in resets:
        reset()


def registered_cache_names() -> tuple[str, ...]:
    with _LOCK:
        return tuple(_RESETS)


__all__ = ["register_cache_reset", "registered_cache_names", "reset_all_caches"]
