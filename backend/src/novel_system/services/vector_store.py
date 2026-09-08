from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from novel_system.services.errors import DomainError
from novel_system.settings import get_settings


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


def _embed_text(text: str, dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for index, char in enumerate(text):
        bucket = (ord(char) + index) % dimensions
        vector[bucket] += 1.0
    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _is_native_windows_runtime() -> bool:
    return sys.platform.startswith("win")


def _ensure_backend_runtime_supported(selected_backend: str) -> None:
    if selected_backend != "chroma":
        return
    if _is_native_windows_runtime():
        raise DomainError(
            "CHROMA_RUNTIME_UNSUPPORTED",
            "Native Windows Chroma runtime is blocked on this machine. Run strict Chroma checks inside WSL/Linux.",
            status_code=503,
            details={
                "backend": selected_backend,
                "runtime": sys.platform,
                "recommended_runtime": "WSL Ubuntu-24.04",
            },
        )


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


class _DeterministicEmbeddingFunction:
    def __init__(self, dimensions: int = 64) -> None:
        self._dimensions = dimensions

    def __call__(self, input: Iterable[str]) -> list[list[float]]:
        return [_embed_text(text, dimensions=self._dimensions) for text in input]

    def embed_query(self, input: Iterable[str]) -> list[list[float]]:
        return self.__call__(input)

    @staticmethod
    def name() -> str:
        return "novel_system_deterministic"

    @classmethod
    def build_from_config(cls, config: dict) -> _DeterministicEmbeddingFunction:
        return cls(dimensions=int(config.get("dimensions", 64)))

    def get_config(self) -> dict[str, int]:
        return {"dimensions": self._dimensions}

    def is_legacy(self) -> bool:
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]


class ChromaVectorStore:
    def __init__(self, persist_directory: Path) -> None:
        _ensure_backend_runtime_supported("chroma")
        persist_directory.mkdir(parents=True, exist_ok=True)
        try:
            import chromadb
            from chromadb.errors import NotFoundError
        except ImportError as exc:
            raise DomainError(
                "CHROMA_DEPENDENCY_MISSING",
                "Chroma is an optional vector backend. Install the 'chroma' extra before selecting it.",
                status_code=503,
                details={
                    "backend": "chroma",
                    "install_command": "uv sync --locked --extra dev --extra chroma",
                },
            ) from exc

        self._client = chromadb.PersistentClient(path=str(persist_directory))
        self._embedding_function = _DeterministicEmbeddingFunction()
        self._not_found_error = NotFoundError

    def _reset_collection(self, collection_name: str):
        self.delete_collection(collection_name)
        return self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=self._embedding_function,
        )

    def _get_collection(self, collection_name: str):
        return self._client.get_collection(
            name=collection_name,
            embedding_function=self._embedding_function,
        )

    def write_collection(self, collection_name: str, documents: list[dict]) -> None:
        collection = self._reset_collection(collection_name)
        if not documents:
            return

        ids = [str(item["id"]) for item in documents]
        texts = [item.get("text", "") for item in documents]
        metadatas = []
        for item in documents:
            metadata = {
                key: value
                for key, value in item.items()
                if key not in {"id", "text"} and value is not None
            }
            metadatas.append(metadata)

        payload: dict[str, object] = {"ids": ids, "documents": texts}
        if any(metadatas):
            payload["metadatas"] = metadatas
        collection.add(**payload)

    def collection_exists(self, collection_name: str) -> bool:
        try:
            self._get_collection(collection_name)
        except self._not_found_error:
            return False
        return True

    def load_collection(self, collection_name: str) -> list[dict]:
        if not self.collection_exists(collection_name):
            return []

        collection = self._get_collection(collection_name)
        payload = collection.get(include=["documents", "metadatas"])
        documents: list[dict] = []
        for item_id, text, metadata in zip(
            payload.get("ids", []),
            payload.get("documents", []),
            payload.get("metadatas", []),
            strict=False,
        ):
            row = {"id": item_id, "text": text}
            if metadata:
                row.update(metadata)
            documents.append(row)
        return documents

    def query(
        self, collection_name: str, query_text: str, top_k: int = 3
    ) -> list[dict]:
        if not query_text.strip() or not self.collection_exists(collection_name):
            return []

        collection = self._get_collection(collection_name)
        collection_size = int(collection.count())
        if collection_size <= 0:
            return []
        payload = collection.query(
            query_texts=[query_text],
            # Some Chroma releases reject n_results larger than the collection.
            # RAG deliberately asks for a wide shortlist, including on tiny books.
            n_results=max(1, min(int(top_k), collection_size)),
            include=["documents", "metadatas"],
        )
        ids = payload.get("ids", [[]])[0]
        documents = payload.get("documents", [[]])[0]
        metadatas = payload.get("metadatas", [[]])[0]

        results: list[dict] = []
        for item_id, text, metadata in zip(ids, documents, metadatas, strict=False):
            row = {"id": item_id, "text": text}
            if metadata:
                row.update(metadata)
            results.append(row)
        return results

    def delete_collection(self, collection_name: str) -> None:
        try:
            self._client.delete_collection(name=collection_name)
        except self._not_found_error:
            return
        if self.collection_exists(collection_name):
            raise RuntimeError(
                f"vector collection still exists after deletion: {collection_name}"
            )

    def delete_documents(self, collection_name: str, document_ids: list[str]) -> None:
        if not document_ids or not self.collection_exists(collection_name):
            return
        collection = self._get_collection(collection_name)
        collection.delete(ids=[str(document_id) for document_id in document_ids])
        remaining_ids = {
            str(item.get("id")) for item in self.load_collection(collection_name)
        }
        leaked = sorted(set(map(str, document_ids)) & remaining_ids)
        if leaked:
            raise RuntimeError(
                f"vector documents still exist after deletion: {collection_name}: {leaked}"
            )


_MEMORY_VECTOR_STORES: dict[str, InMemoryVectorStore] = {}


def get_vector_store(
    *,
    backend: str | None = None,
    persist_directory: Path | None = None,
) -> VectorStore:
    # 只用 vector_backend / vector_store_dir 两个环境级设置;不叠加库内 api 配置快照
    # (向量库与 LLM 连接无关,且 chroma_smoke 会在未迁移的 sqlite 上调用这里)。
    settings = get_settings(include_runtime_config=False)
    selected_backend = (backend or settings.vector_backend).lower()
    _ensure_backend_runtime_supported(selected_backend)
    if selected_backend == "memory":
        namespace = str((persist_directory or settings.vector_store_dir).resolve())
        if namespace not in _MEMORY_VECTOR_STORES:
            _MEMORY_VECTOR_STORES[namespace] = InMemoryVectorStore()
        return _MEMORY_VECTOR_STORES[namespace]
    if selected_backend == "chroma":
        return ChromaVectorStore(persist_directory or settings.vector_store_dir)
    raise ValueError(f"Unsupported vector backend: {selected_backend}")
