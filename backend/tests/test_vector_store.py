"""进程内的向量集合（``services/vector_store.py``）：Chroma 删掉之后只剩 ``memory`` 一种后端（批准 #1）。

``NOVEL_SYSTEM_VECTOR_BACKEND`` 设成别的值时 fail closed，和以前没装 Chroma 一样：归档第 7 步记一份 ``failed``
产品，续跑时不碰集合；不会把进程内集合记成一份「已索引」的持久化索引——那样续跑时要去核对一个已经不存在的后端，
这一场的归档检查点就再也续不上了。整个模块随最后几个调用方退役一起删（重评 R1 第 5 步），这个文件跟着删。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.services.errors import DomainError
from novel_system.services.scene_archive_effects import SceneArchiveEffects
from novel_system.services.vector_store import get_vector_store


def test_the_default_backend_is_the_process_store() -> None:
    assert get_vector_store() is get_vector_store(backend="memory")


@pytest.mark.parametrize("backend", ["chroma", " Chroma "])
def test_a_retired_backend_is_refused_whether_configured_or_asked_for(monkeypatch, backend: str) -> None:
    with pytest.raises(DomainError) as asked:
        get_vector_store(backend=backend)
    assert (asked.value.code, asked.value.status_code, asked.value.details) == (
        "VECTOR_BACKEND_UNSUPPORTED",
        503,
        {"backend": "chroma"},
    )

    monkeypatch.setenv("NOVEL_SYSTEM_VECTOR_BACKEND", backend)
    with pytest.raises(DomainError) as configured:
        get_vector_store()
    assert (configured.value.code, configured.value.status_code) == ("VECTOR_BACKEND_UNSUPPORTED", 503)
    assert "NOVEL_SYSTEM_VECTOR_BACKEND" in configured.value.message


def test_archive_indexing_under_a_retired_backend_records_a_failure_not_a_persistent_index(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_VECTOR_BACKEND", "chroma")
    scene = SimpleNamespace(scene_id="SC_VECTOR", chapter_id="CH_VECTOR", project_id="P_VECTOR")

    product = SceneArchiveEffects._index_scene_to_vector_store(scene, "一段终稿正文")

    assert {key: product[key] for key in ("backend", "validation_scope", "outcome", "write_status", "error_code")} == {
        "backend": "chroma",
        "validation_scope": "persistent",
        "outcome": "failed",
        "write_status": "failed",
        "error_code": "DomainError",
    }
    assert not get_vector_store(backend="memory").collection_exists("scenes_P_VECTOR")
