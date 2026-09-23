from __future__ import annotations

from novel_system.api import routes as routes_package
from novel_system.api.app import create_app

_ROUTES_MODULE_PREFIX = "novel_system.api.routes."


def _iter_endpoints(routes):
    # FastAPI 0.139 的 include_router 是惰性挂载：app.routes 中出现的是
    # _IncludedRouter 包装对象，真实 APIRoute 藏在 original_router 里，必须递归展开。
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None:
            yield endpoint
            continue
        nested_router = getattr(route, "original_router", None)
        if nested_router is not None:
            yield from _iter_endpoints(nested_router.routes)
            continue
        nested_routes = getattr(route, "routes", None)
        if nested_routes is not None:
            yield from _iter_endpoints(nested_routes)


def _mounted_route_modules() -> set[str]:
    app = create_app()
    modules: set[str] = set()
    for endpoint in _iter_endpoints(app.routes):
        module = getattr(endpoint, "__module__", "")
        if module.startswith(_ROUTES_MODULE_PREFIX):
            # 路由包(例如 style_reference/ 下按领域拆的子模块)按包名计:__all__ 里列的是 app.py 挂载的名字
            modules.add(module.removeprefix(_ROUTES_MODULE_PREFIX).split(".", 1)[0])
    return modules


def test_routes_all_matches_mounted_modules() -> None:
    # __all__ 必须与 create_app() 实际挂载的路由模块集合一致：
    # 新挂载模块必须补进清单，摘除模块必须同步删除。
    mounted = _mounted_route_modules()
    declared = set(routes_package.__all__)
    assert mounted == declared, (
        f"missing_in_all={sorted(mounted - declared)} "
        f"stale_in_all={sorted(declared - mounted)}"
    )


def test_routes_all_has_no_duplicates_and_is_sorted() -> None:
    # 清单按字典序维护，禁止重复项，保证 diff 可读。
    assert len(routes_package.__all__) == len(set(routes_package.__all__))
    assert list(routes_package.__all__) == sorted(routes_package.__all__)
