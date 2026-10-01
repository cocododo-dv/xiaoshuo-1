"""请求体模型的家（B12-06）：路由收的每个请求体都定义在 ``novel_system.api.requests`` 下、且封闭多余字段。

模型挪进 ``api/requests/`` 时逐字搬、类名不改——``/openapi.json`` 的 schema 名就是类名（搬家前后逐字节比过）。
这里守住结果：新的请求体不再写回路由文件里，也不会忘了 ``extra="forbid"``（多余字段一律 422）。
"""

from __future__ import annotations

import typing

from pydantic import BaseModel

from novel_system.api.app import create_app

# 审过的例外：「本场预览」的请求体与它的参数校验同住在风格参考服务里（测试也从那里导入），只读、不写库
BODY_MODELS_OUTSIDE_REQUESTS = {"novel_system.services.style_reference.schemas.InjectionPreviewRequest"}


def _api_routes(routes):
    # FastAPI 的 include_router 是惰性挂载：真实 APIRoute 藏在 original_router 里（同 test_routes_all_manifest）
    for route in routes:
        if getattr(route, "endpoint", None) is not None and getattr(route, "methods", None):
            yield route
            continue
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _api_routes(nested.routes)
            continue
        yield from _api_routes(getattr(route, "routes", ()) or ())


def _body_models(annotation) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in typing.get_args(annotation) for model in _body_models(arg)]


def _route_body_models() -> dict[str, type[BaseModel]]:
    models: dict[str, type[BaseModel]] = {}
    for route in _api_routes(create_app().routes):
        hints = typing.get_type_hints(route.endpoint)
        for name, annotation in hints.items():
            if name == "return":
                continue
            for model in _body_models(annotation):
                models[f"{model.__module__}.{model.__qualname__}"] = model
    return models


def test_every_route_body_model_lives_in_api_requests_and_forbids_extra_fields() -> None:
    models = _route_body_models()
    assert len(models) > 60  # 不是空跑：路由真的解析出了请求体

    misplaced = sorted(
        name
        for name in models
        if not name.startswith("novel_system.api.requests.") and name not in BODY_MODELS_OUTSIDE_REQUESTS
    )
    assert misplaced == []
    assert BODY_MODELS_OUTSIDE_REQUESTS <= set(models)

    open_bodies = sorted(name for name, model in models.items() if model.model_config.get("extra") != "forbid")
    assert open_bodies == []
