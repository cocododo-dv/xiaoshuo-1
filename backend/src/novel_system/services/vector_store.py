"""进程内的场景文本集合——只剩内存实现。

Chroma 后端已按批准 #1（重评 R1）删除：它的「向量」是按字符编码散列出来的假嵌入，并不懂意思，各个启动脚本也一直
强制 memory。剩下的调用方（起草提示的「相似场景」段、归档第 7 步的索引、清理时的删除）由各自的包逐个退役，
全部退役之后这个模块整个删掉。

集合只活在本进程里：生产进程只连一个库；测试进程每个用例换一个库，所以这份集合登记在 ``cache_registry``，
conftest 在每个用例前后清空它（以前靠每个用例各自的 ``NOVEL_SYSTEM_CHROMA_DIR`` 分出命名空间）。
"""

from __future__ import annotations

from typing import Protocol

from novel_system.cache_registry import register_cache_reset
from novel_system.services.errors import DomainError


def _score_text(text: str, query_text: str) -> int:
    if not text or not query_text.strip():
        return 0
    return len(set(query_text).intersection(set(text)))


def _rank_documents(documents: list[dict], query_text: str, top_k: int) -> list[dict]:
    scored: list[tuple[int, dict]] = []
    for item in documents:
        score = _score_text(item.get("text", ""), query_text)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:top_k]]


class VectorStore(Protocol):
    def write_collection(self, collection_name: str, documents: list[dict]) -> None: ...

    def collection_exists(self, collection_name: str) -> bool: ...

    def load_collection(self, collection_name: str) -> list[dict]: ...

    def query(
        self, collection_name: str, query_text: str, top_k: int = 3
    ) -> list[dict]: ...

    def delete_collection(self, collection_name: str) -> None: ...

    def delete_documents(
        self, collection_name: str, document_ids: list[str]
    ) -> None: ...


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._collections: dict[str, list[dict]] = {}

    def write_collection(self, collection_name: str, documents: list[dict]) -> None:
        self._collections[collection_name] = [dict(item) for item in documents]

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self._collections

    def load_collection(self, collection_name: str) -> list[dict]:
        return [dict(item) for item in self._collections.get(collection_name, [])]

    def query(
        self, collection_name: str, query_text: str, top_k: int = 3
    ) -> list[dict]:
        return _rank_documents(self.load_collection(collection_name), query_text, top_k)

    def delete_collection(self, collection_name: str) -> None:
        self._collections.pop(collection_name, None)

    def delete_documents(self, collection_name: str, document_ids: list[str]) -> None:
        if not document_ids or collection_name not in self._collections:
            return
        targets = set(document_ids)
        self._collections[collection_name] = [
            item
            for item in self._collections[collection_name]
            if str(item.get("id")) not in targets
        ]

    def clear(self) -> None:
        self._collections.clear()


_PROCESS_STORE = InMemoryVectorStore()
register_cache_reset("vector_store", _PROCESS_STORE.clear)


def get_vector_store(*, backend: str | None = None) -> InMemoryVectorStore:
    """本进程的集合。``backend`` 只为核对旧检查点里记下的后端名：``memory`` 以外的持久化索引已经不存在，
    没法核对，照旧失败（fail closed），不假装查过。"""
    selected_backend = (backend or "memory").strip().lower()
    if selected_backend != "memory":
        raise DomainError(
            "VECTOR_BACKEND_UNSUPPORTED",
            "只剩进程内的向量集合；Chroma 后端已删除，旧的持久化索引无法核对",
            status_code=503,
            details={"backend": selected_backend},
        )
    return _PROCESS_STORE
