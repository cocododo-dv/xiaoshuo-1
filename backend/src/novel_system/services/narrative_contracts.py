"""Shared narrative fact taxonomy without service-to-service dependencies."""

INFORMATION_ASYMMETRY_FACT_KEYS = frozenset(
    {
        "secret_held_by",
        "believes_false",
        "revealed_to",
        "scene_revelation",
    }
)


def require_scene_boundary(scene_seq: None, scene_id: str | None) -> str:
    """摘要只认「这一场之前」的场景边界。

    按章内 scene_seq 截断的旧游标已经删掉（B11-07：它是章内序号，多章作品里拿它当全书边界是错的，
    产品调用方早就只传 scene_id）；位置参数 ``scene_seq`` 留着只是为了照旧传 ``None`` 的调用方。
    """
    if scene_seq is not None:
        raise TypeError("the chapter-local scene_seq cursor was removed; pass scene_id")
    if not scene_id:
        raise TypeError("a narrative digest needs the scene_id it is written for")
    return scene_id
