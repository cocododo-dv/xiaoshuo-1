"""配置解析的两件基础件：YAML 解析入口与按内容记忆的解析缓存（叶子模块，只依赖 yaml）。

为什么有这个模块（2026-09-29 重构，审计 X01-01 / X04-02）：``config/prompts.yaml`` 231 KB，纯 Python
``yaml.safe_load`` 解析一遍约 0.3 s，而 ``PromptBuilder()`` / ``load_prompt_templates()`` /
``load_model_routing_config()`` 原来每次调用都重读、重解析——起草台一次只读 GET 要解析两遍，一次场景运行
解析提示词十几遍、models.yaml 五十来遍（租约 TTL 每次续租都读）。

* ``safe_load_yaml``：有 libyaml 就用 C 解析器（快约 10 倍，解析结果与纯 Python 版逐项相同——
  ``tests/test_config_cache.py`` 对仓库里每份 yaml 都比过）。C 解析器报错时换纯 Python 解析器重来一遍：
  错误信息（系统配置的校验提示会原样显示给作者）与原来逐字相同，个别只有纯 Python 版接受的写法也照旧接受。
* ``ContentKeyedCache``：**按配置源的内容**记忆解析结果——键是文件正文 / 活动快照在库里的原文本身，
  不是路径、修改时间或快照 id。于是缓存永远不会把旧的解析交给新的内容：系统配置里保存或切换快照、
  ``sync_prompt_templates`` / ``raise_llm_output_budget`` 在另一个进程里激活新快照、迁移就地改写快照、
  手改仓库里的 yaml，下一次读取读到的就是新内容；内容没变才命中。每次读取仍要读一遍源文本（一次窄查询或
  一次读文件，远小于解析的开销），这是「保存即生效」的代价，也是它成立的理由。

缓存的值由所有调用方共享：调用方不得就地修改（提示词模板 / 模型路由的现有调用方都只读——需要改的都先
深拷贝，``PromptBuilder.build`` 与 ``enrich_structured_schema`` 即如此）。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any, TypeVar

import yaml


_FAST_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

T = TypeVar("T")
_MISSING = object()


def safe_load_yaml(text: str) -> Any:
    """``yaml.safe_load`` 的等价物：先用 C 解析器，报错时用纯 Python 解析器重解析（错误照原样抛出）。"""
    try:
        return yaml.load(text, Loader=_FAST_SAFE_LOADER)
    except yaml.YAMLError:
        if _FAST_SAFE_LOADER is yaml.SafeLoader:
            raise
    return yaml.load(text, Loader=yaml.SafeLoader)


class ContentKeyedCache:
    """小容量 LRU：键是配置源的内容（连同来源种类），值是它的解析结果。

    只缓存成功的解析：解析抛错时什么都不记，下一次照样重新解析、照样抛错。多线程安全（请求线程池、
    作业线程、心跳线程都会读配置）；两个线程同时未命中时各解析一遍，结果相同，无害。
    """

    def __init__(self, *, maxsize: int = 4) -> None:
        self._maxsize = max(1, int(maxsize))
        self._entries: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()
        # 累计解析次数（未命中次数）：回归守卫用它断言「同一内容只解析一次」
        self.builds = 0

    def get_or_build(self, key: Hashable, build: Callable[[], T]) -> T:
        with self._lock:
            value = self._entries.get(key, _MISSING)
            if value is not _MISSING:
                self._entries.move_to_end(key)
                return value
        value = build()
        with self._lock:
            self.builds += 1
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
