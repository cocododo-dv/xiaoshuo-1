from __future__ import annotations

import json
from pathlib import Path

from novel_system.services.vector_store import get_vector_store
from novel_system.settings import get_settings


def run_chroma_smoke(persist_directory: Path | None = None) -> dict:
    # 只需要 vector_store_dir 这类环境级设置;不叠加库内 api 配置快照——CI 的 Chroma
    # 作业在一个未迁移的 sqlite 上跑这个冒烟,读 system_config_snapshots 会直接报表不存在。
    settings = get_settings(include_runtime_config=False)
    store = get_vector_store(
        backend="chroma",
        persist_directory=persist_directory or settings.vector_store_dir,
    )
    collection_name = "novel_system_smoke_candidate_v1"
    store.write_collection(
        collection_name,
        [
            {"id": "doc-1", "text": "moonlit rooftops and quiet rain", "scope": "global"},
            {"id": "doc-2", "text": "battle drums under a red sky", "scope": "global"},
        ],
    )
    results = store.query(collection_name, "quiet rain over rooftops", top_k=1)
    return {
        "backend": "chroma",
        "collection_name": collection_name,
        "collection_exists": store.collection_exists(collection_name),
        "query_ids": [item["id"] for item in results],
    }


def main() -> None:
    print(json.dumps(run_chroma_smoke(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
