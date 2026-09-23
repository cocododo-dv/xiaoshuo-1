"""参考书操作进度(进程内、线程安全的内存登记簿)——风格参考 v3 迁出中,P7 删除。

2026-09-23 起**段落分类不再用它**:导入 / 重新分类 / 就地重标类型都是作业表上的分类作业
(``import_job``),进度在作业行上,``GET …/imports/{key}/progress`` 按幂等键找作业(兼容别名)。
这里只剩还没迁到作业表的操作:合成画像、应用画像的 RAG 索引、回测 worker 的阶段进度(P3 / P5 迁出);
``GET …/activity``(``activity.py``)把它们与作业表、库里的 durable 行合成一份活动清单。

刻意不落库:进度只描述**本进程内正在执行**的那条操作——进程没了操作也没了;终态条目保留
``FINISHED_TTL_SECONDS`` 供最后几次轮询读到结果。

每种 kind 有自己的阶段表与权重(百分比 = 已完成阶段权重之和 + 当前阶段权重 × 阶段内进度);
只有申报了步骤计划的阶段能报阶段内进度,其余阶段在边界处跳变——进度条不必与时间成正比,
但必须单调、诚实。``import`` / ``reclassify`` 两个阶段表只剩登记簿自身的单测在用,随模块一起删。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---- 各 kind 的阶段表(顺序 + 权重)与文案 ---------------------------------------------
KIND_PHASES: dict[str, tuple[tuple[str, float], ...]] = {
    "import": (("prepare", 0.05), ("classify", 0.70), ("metrics", 0.15), ("persist", 0.10)),
    "reclassify": (("purge", 0.05), ("classify", 0.80), ("metrics", 0.10), ("persist", 0.05)),
    "synthesize": (
        ("collect", 0.08),
        ("voice", 0.12),
        ("llm", 0.45),
        ("filter", 0.05),
        ("derive", 0.10),
        ("persist", 0.05),
        ("index", 0.15),
    ),
    "rag_index": (("signatures", 0.85), ("write", 0.15)),
    "validate": (("local", 0.15), ("semantic", 0.55), ("forbidden", 0.30)),
}
KIND_LABELS: dict[str, str] = {
    "import": "导入",
    "reclassify": "重新分类",
    "synthesize": "合成画像",
    "rag_index": "应用画像 · 建索引",
    "validate": "回测",
    "extract": "抽取",
    "preview": "示例预览",
}
PHASE_LABELS_BY_KIND: dict[str, dict[str, str]] = {
    "import": {
        "prepare": "解码与切段",
        "classify": "段落分类",
        "metrics": "统计与声音签名",
        "persist": "写入书库",
        "done": "完成",
    },
    "reclassify": {
        "purge": "清理派生数据",
        "classify": "段落分类",
        "metrics": "统计与声音签名",
        "persist": "写入书库",
        "done": "完成",
    },
    "synthesize": {
        "collect": "汇总 findings 与引文",
        "voice": "计算声音签名",
        "llm": "模型合成",
        "filter": "安全过滤",
        "derive": "结构画像与样例索引",
        "persist": "写入画像",
        "index": "建立 RAG 索引",
        "done": "完成",
    },
    "rag_index": {
        "signatures": "计算段落签名",
        "write": "写入索引",
        "done": "完成",
    },
    "validate": {
        "local": "量化 · 抄袭 · 禁忌（本地）",
        "semantic": "语义评分（critic）",
        "forbidden": "禁忌语义判定",
        "done": "完成",
    },
}

# 旧契约常量:导入的阶段表(tests / 文档引用)。
PHASES: tuple[str, ...] = tuple(name for name, _weight in KIND_PHASES["import"])
PHASE_WEIGHTS: dict[str, float] = {name: weight for name, weight in KIND_PHASES["import"]}
PHASE_LABELS: dict[str, str] = PHASE_LABELS_BY_KIND["import"]

FINISHED_TTL_SECONDS = 600
MAX_ENTRIES = 200
IMPORT_KEY_MAX_LENGTH = 128
OPERATION_KEY_MAX_LENGTH = IMPORT_KEY_MAX_LENGTH


class NullImportProgress:
    """没有进度消费者时的空实现,让 ingest / 分类器 / 合成器不必处处判 None。"""

    def set_totals(self, **_fields: Any) -> None:
        return None

    def phase(self, _name: str, *, detail: str | None = None) -> None:  # noqa: ARG002
        return None

    def plan_steps(self, total: int, label: str | None = None) -> None:  # noqa: ARG002
        return None

    def set_steps(self, done: int, total: int, label: str | None = None) -> None:  # noqa: ARG002
        return None

    def step_done(self, label: str | None = None, *, node_id: str | None = None) -> None:  # noqa: ARG002
        return None

    def llm_call(self, node_id: str | None = None) -> None:  # noqa: ARG002
        return None

    def classify_plan(self, total_batches: int, mode: str) -> None:  # noqa: ARG002
        return None

    def classify_batch_done(self, node_id: str) -> None:  # noqa: ARG002
        return None

    def succeed(
        self,
        *,
        book_id: str | None = None,
        paragraphs_count: int | None = None,
        result: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        return None

    def fail(self, *, code: str, message: str) -> None:  # noqa: ARG002
        return None



def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _first_phase(kind: str) -> str:
    table = KIND_PHASES.get(kind)
    return table[0][0] if table else ""


@dataclass
class _ImportProgressEntry:
    import_key: str
    title: str | None
    source: str
    kind: str = "import"
    book_id: str | None = None
    target_id: str | None = None
    status: str = "running"  # running | succeeded | failed
    phase: str = ""
    phase_detail: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    finished_monotonic: float | None = None
    chars_total: int | None = None
    paragraphs_total: int | None = None
    # 当前阶段的步骤计划(任何 kind 通用)
    steps_total: int | None = None
    steps_done: int = 0
    step_label: str | None = None
    phase_started_monotonic: float | None = None
    # 导入 / 重新分类的分类批次(旧契约字段,跨阶段保留)
    classify_mode: str | None = None
    classify_batches_total: int | None = None
    classify_batches_done: int = 0
    eta_done_offset: int = 0
    llm_calls: int = 0
    last_node_id: str | None = None
    paragraphs_count: int | None = None
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.phase:
            self.phase = _first_phase(self.kind)

    # ---- derived -------------------------------------------------------
    def _phase_fraction(self) -> float:
        if self.steps_total is None:
            return 0.0
        if self.steps_total <= 0:
            return 1.0
        return max(0.0, min(1.0, self.steps_done / self.steps_total))

    def percent(self) -> int:
        if self.status == "succeeded" or self.phase == "done":
            return 100
        done = 0.0
        for name, weight in KIND_PHASES.get(self.kind, ()):
            if name == self.phase:
                done += weight * self._phase_fraction()
                break
            done += weight
        return max(0, min(99, int(round(done * 100))))

    def elapsed_seconds(self) -> float:
        end = self.finished_monotonic if self.finished_monotonic is not None else time.monotonic()
        return max(0.0, end - self.started_monotonic)

    def eta_seconds(self) -> float | None:
        measured = self.steps_done - self.eta_done_offset
        if (
            self.status != "running"
            or not self.steps_total
            or measured <= 0
            or self.phase_started_monotonic is None
        ):
            return None
        per_step = (time.monotonic() - self.phase_started_monotonic) / measured
        remaining = max(0, self.steps_total - self.steps_done)
        return round(per_step * remaining, 1)

    def phase_label(self) -> str:
        labels = PHASE_LABELS_BY_KIND.get(self.kind) or {}
        base = labels.get(self.phase, self.phase)
        if self.phase_detail and self.phase != "done":
            return f"{base} · {self.phase_detail}"
        return base

    def snapshot(self) -> dict[str, Any]:
        return {
            "import_key": self.import_key,
            "op_key": self.import_key,
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "title": self.title,
            "source": self.source,
            "status": self.status,
            "phase": self.phase,
            "phase_label": self.phase_label(),
            "phase_detail": self.phase_detail,
            "percent": self.percent(),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
            "eta_seconds": self.eta_seconds(),
            "chars_total": self.chars_total,
            "paragraphs_total": self.paragraphs_total,
            "classify": {
                "mode": self.classify_mode,
                "batches_done": self.classify_batches_done,
                "batches_total": self.classify_batches_total,
                "llm_calls": self.llm_calls,
                "node_id": self.last_node_id,
            },
            "steps": {
                "done": self.steps_done,
                "total": self.steps_total,
                "label": self.step_label,
            },
            "llm_calls": self.llm_calls,
            "book_id": self.book_id,
            "target_id": self.target_id,
            "paragraphs_count": self.paragraphs_count,
            "result": dict(self.result) if self.result else None,
            "error": dict(self.error) if self.error else None,
        }


class ImportProgressReporter:
    """一次操作的汇报句柄;每个方法都在登记簿锁内更新条目。"""

    def __init__(self, registry: ImportProgressRegistry, import_key: str) -> None:
        self._registry = registry
        self.import_key = import_key

    @property
    def op_key(self) -> str:
        return self.import_key

    def _update(self, mutate: Any) -> None:
        self._registry._mutate(self.import_key, mutate)

    def set_totals(
        self,
        *,
        chars_total: int | None = None,
        paragraphs_total: int | None = None,
        title: str | None = None,
    ) -> None:
        def mutate(entry: _ImportProgressEntry) -> None:
            if chars_total is not None:
                entry.chars_total = int(chars_total)
            if paragraphs_total is not None:
                entry.paragraphs_total = int(paragraphs_total)
            if title is not None:
                entry.title = title

        self._update(mutate)

    def phase(self, name: str, *, detail: str | None = None) -> None:
        """进入一个阶段;同一阶段再次调用只更新 ``detail``(如「模型合成 · 第 2 次」),
        不重置步骤计划。"""

        def mutate(entry: _ImportProgressEntry) -> None:
            known = {phase for phase, _weight in KIND_PHASES.get(entry.kind, ())}
            if name not in known and name != "done":
                raise ValueError(f"unknown {entry.kind} phase {name!r}")
            if entry.phase != name:
                entry.phase = name
                entry.steps_total = None
                entry.steps_done = 0
                entry.step_label = None
                entry.eta_done_offset = 0
                entry.phase_started_monotonic = time.monotonic()
            entry.phase_detail = detail

        self._update(mutate)

    def plan_steps(self, total: int, label: str | None = None) -> None:
        def mutate(entry: _ImportProgressEntry) -> None:
            entry.steps_total = max(0, int(total))
            entry.steps_done = 0
            if label is not None:
                entry.step_label = label
            if entry.phase_started_monotonic is None:
                entry.phase_started_monotonic = time.monotonic()

        self._update(mutate)

    def set_steps(self, done: int, total: int, label: str | None = None) -> None:
        """批量推进(如每 250 段汇报一次的签名计算);done 只增不减。"""

        def mutate(entry: _ImportProgressEntry) -> None:
            new_total = max(0, int(total))
            if entry.steps_total != new_total:
                # 新的步骤计划(如建索引从「段」切到「粒度」):重新计数,不继承旧计划的 done
                entry.steps_total = new_total
                entry.steps_done = max(0, min(int(done), new_total))
            else:
                entry.steps_done = max(entry.steps_done, min(int(done), new_total))
            if label is not None:
                entry.step_label = label
            if entry.phase_started_monotonic is None:
                entry.phase_started_monotonic = time.monotonic()

        self._update(mutate)

    def step_done(self, label: str | None = None, *, node_id: str | None = None) -> None:
        def mutate(entry: _ImportProgressEntry) -> None:
            entry.steps_done += 1
            if label is not None:
                entry.step_label = label
            if node_id is not None:
                entry.last_node_id = node_id

        self._update(mutate)

    def llm_call(self, node_id: str | None = None) -> None:
        def mutate(entry: _ImportProgressEntry) -> None:
            entry.llm_calls += 1
            if node_id is not None:
                entry.last_node_id = node_id

        self._update(mutate)

    # ---- 分类批次(导入 / 重新分类共用的旧契约) -------------------------------
    def classify_plan(self, total_batches: int, mode: str, done: int = 0) -> None:
        """申报分类批次计划;``done`` 给续跑的任务(已完成的批次不再计入 ETA 的均速)。"""

        def mutate(entry: _ImportProgressEntry) -> None:
            total = max(0, int(total_batches))
            finished = max(0, min(int(done), total))
            entry.classify_batches_total = total
            entry.classify_batches_done = finished
            entry.classify_mode = mode
            entry.steps_total = total
            entry.steps_done = finished
            entry.step_label = "批"
            entry.phase_started_monotonic = time.monotonic()
            entry.eta_done_offset = finished

        self._update(mutate)

    def classify_batch_done(self, node_id: str) -> None:
        def mutate(entry: _ImportProgressEntry) -> None:
            entry.classify_batches_done += 1
            entry.steps_done += 1
            entry.llm_calls += 1
            entry.last_node_id = node_id

        self._update(mutate)

    def succeed(
        self,
        *,
        book_id: str | None = None,
        paragraphs_count: int | None = None,
        result: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        """终态成功;``result``(dict)与其余关键字参数合并成快照的 ``result``。"""

        merged = {**(result or {}), **extra}

        def mutate(entry: _ImportProgressEntry) -> None:
            entry.status = "succeeded"
            entry.phase = "done"
            entry.phase_detail = None
            if book_id is not None:
                entry.book_id = book_id
            entry.paragraphs_count = paragraphs_count
            if merged:
                entry.result = {**(entry.result or {}), **merged}
            entry.finished_monotonic = time.monotonic()

        self._update(mutate)

    def fail(self, *, code: str, message: str) -> None:
        def mutate(entry: _ImportProgressEntry) -> None:
            entry.status = "failed"
            entry.error = {"code": str(code), "message": str(message)[:500]}
            entry.finished_monotonic = time.monotonic()

        self._update(mutate)



class ImportProgressRegistry:
    def __init__(
        self,
        *,
        finished_ttl_seconds: float = FINISHED_TTL_SECONDS,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _ImportProgressEntry] = {}
        self._finished_ttl_seconds = float(finished_ttl_seconds)
        self._max_entries = int(max_entries)

    # ---- public ---------------------------------------------------------
    def start(
        self,
        import_key: str,
        *,
        title: str | None = None,
        source: str = "upload",
        kind: str = "import",
        book_id: str | None = None,
        target_id: str | None = None,
    ) -> ImportProgressReporter | NullImportProgress:
        """登记一次操作。同一个键已有在跑的条目时返回空实现——幂等重放 / 在途冲突的那条
        请求不得改写真正在执行的进度。"""
        key = str(import_key or "").strip()
        if not key or len(key) > OPERATION_KEY_MAX_LENGTH:
            return NullImportProgress()
        if kind not in KIND_PHASES:
            raise ValueError(f"unknown operation kind {kind!r}")
        with self._lock:
            self._evict_locked()
            existing = self._entries.get(key)
            if existing is not None and existing.status == "running":
                return NullImportProgress()
            self._entries[key] = _ImportProgressEntry(
                import_key=key,
                title=title,
                source=source,
                kind=kind,
                book_id=book_id,
                target_id=target_id,
            )
        return ImportProgressReporter(self, key)

    def get(self, import_key: str) -> dict[str, Any] | None:
        key = str(import_key or "").strip()
        with self._lock:
            self._evict_locked()
            entry = self._entries.get(key)
            return entry.snapshot() if entry is not None else None

    def snapshots(self) -> list[dict[str, Any]]:
        """全部条目(在跑 + TTL 内的终态),供活动清单合并;不保证顺序。"""
        with self._lock:
            self._evict_locked()
            return [entry.snapshot() for entry in self._entries.values()]

    def find_running(
        self,
        *,
        kind: str | None = None,
        book_id: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any] | None:
        """找一条在跑的条目(同类操作的活跃守卫用);任一过滤条件为 None 表示不限。"""
        with self._lock:
            self._evict_locked()
            for entry in self._entries.values():
                if entry.status != "running":
                    continue
                if kind is not None and entry.kind != kind:
                    continue
                if book_id is not None and entry.book_id != book_id:
                    continue
                if target_id is not None and entry.target_id != target_id:
                    continue
                return entry.snapshot()
        return None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ---- internal -------------------------------------------------------
    def _mutate(self, import_key: str, mutate: Any) -> None:
        with self._lock:
            entry = self._entries.get(import_key)
            if entry is None:
                return
            mutate(entry)
            entry.updated_at = _now_iso()

    def _evict_locked(self) -> None:
        now = time.monotonic()
        stale = [
            key
            for key, entry in self._entries.items()
            if entry.finished_monotonic is not None
            and now - entry.finished_monotonic > self._finished_ttl_seconds
        ]
        for key in stale:
            del self._entries[key]
        if len(self._entries) <= self._max_entries:
            return
        finished = sorted(
            (entry for entry in self._entries.values() if entry.finished_monotonic is not None),
            key=lambda entry: entry.finished_monotonic or 0.0,
        )
        for entry in finished:
            if len(self._entries) <= self._max_entries:
                break
            del self._entries[entry.import_key]


_REGISTRY = ImportProgressRegistry()


def start_import_progress(
    import_key: str | None,
    *,
    title: str | None = None,
    source: str = "upload",
    kind: str = "import",
    book_id: str | None = None,
    target_id: str | None = None,
) -> ImportProgressReporter | NullImportProgress:
    if not import_key:
        return NullImportProgress()
    return _REGISTRY.start(
        import_key,
        title=title,
        source=source,
        kind=kind,
        book_id=book_id,
        target_id=target_id,
    )



def get_import_progress(import_key: str) -> dict[str, Any] | None:
    return _REGISTRY.get(import_key)



def list_operation_progress() -> list[dict[str, Any]]:
    return _REGISTRY.snapshots()


def find_running_operation(
    *,
    kind: str | None = None,
    book_id: str | None = None,
    target_id: str | None = None,
) -> dict[str, Any] | None:
    return _REGISTRY.find_running(kind=kind, book_id=book_id, target_id=target_id)


def reset_import_progress_registry() -> None:
    """测试用:清空进程内登记簿。"""
    _REGISTRY.clear()


__all__ = [
    "FINISHED_TTL_SECONDS",
    "IMPORT_KEY_MAX_LENGTH",
    "ImportProgressRegistry",
    "ImportProgressReporter",
    "KIND_LABELS",
    "KIND_PHASES",
    "NullImportProgress",
    "OPERATION_KEY_MAX_LENGTH",
    "PHASE_LABELS",
    "PHASE_LABELS_BY_KIND",
    "PHASE_WEIGHTS",
    "PHASES",
    "find_running_operation",
    "get_import_progress",
    "list_operation_progress",
    "reset_import_progress_registry",
    "start_import_progress",
]
