"""退役接口一张表（X04-15）：删掉的路由不在路由表里、不进 OpenAPI，请求它们只得到 404（路径还为别的方法服务的得 405）。

以前每删一组接口就在各自的测试文件里加一条「已删」用例，各建一次应用和库，有的还先造一整套书 / 草稿 / 学习血缘，
只为拿一个真 id 去请求——可只看状态码分不清「路由没了」和「路由还在、只是这个 id 不存在」。这里直接查路由表：
具体路径（占位段换成 ``retired-<名>``）对这个方法没有任何路由完整匹配，整条路径对别的方法也不匹配（只有
``STILL_SERVED_PATHS`` 里路径本身还在的几条例外），路径模板不在 OpenAPI 里；整段退役的前缀下，路由表（连不进
OpenAPI 的 ``include_in_schema=False`` 路由在内）一条都不许有；再经一个真客户端各请求一遍。
``test_fixture_import_boundary.py`` 是 CLAUDE.md 点名的守卫，照旧单独钉着它那一条。
"""

from __future__ import annotations

import re

from fastapi.routing import iter_route_contexts
from starlette.routing import Match

from novel_system.api.app import create_app

# (方法, 路径模板, 为什么没了)
RETIRED_ROUTES: tuple[tuple[str, str, str], ...] = (
    # 作者稿续写候选：界面从没调过（批准 #7）；空白稿由 ensure 直接给（批准 #24a）
    ("POST", "/api/v1/author-drafts/{draft_id}/proposals/generate", "批准 #7"),
    ("POST", "/api/v1/author-drafts/{draft_id}/apply-proposal", "批准 #7"),
    ("GET", "/api/v1/author-drafts/{draft_id}/proposals", "批准 #7"),
    ("GET", "/api/v1/author-drafts/{draft_id}/proposals/{proposal_id}/diff", "批准 #7"),
    ("POST", "/api/v1/author-draft-proposals/{proposal_id}/apply", "批准 #7"),
    ("POST", "/api/v1/author-draft-proposals/{proposal_id}/reject", "批准 #7"),
    ("POST", "/api/v1/author-drafts/{object_type}/{object_id}/ensure-blank", "批准 #24a"),
    # 写作偏好学习整条链（批准 #6）
    ("GET", "/api/v1/author-preference-profile", "批准 #6"),
    # 收件箱：单独的角标接口与单条详情没有界面在用（批准 #24a）
    ("GET", "/api/v1/review-items/badge", "批准 #24a"),
    ("GET", "/api/v1/review-items/{review_id}", "批准 #24a"),
    # 测试夹具导入（test_fixture_import_boundary.py 同样钉着）
    ("POST", "/api/v1/review-items/import-demo", "夹具导入端点"),
    # 起草台预检铸卡：声线卡 / 关系卡闸门退役（2026-09-20），连同铸卡支路
    ("POST", "/api/v1/scenes/{scene_id}/preflight/create-cards", "声线卡 / 关系卡闸门退役"),
    # 系统配置 OAuth 登录
    ("POST", "/api/v1/system-config/llm/oauth/{provider}/start", "OAuth 登录退役"),
    ("GET", "/api/v1/system-config/llm/oauth/callback", "OAuth 登录退役"),
    # v1 雪花规划器与 /api/v1/projects/{id}/snowflake*（批准 #16a，R9）
    ("GET", "/api/v1/projects/{project_id}/snowflake", "批准 #16a"),
    ("POST", "/api/v1/projects/{project_id}/snowflake/steps/{step_key}/generate", "批准 #16a"),
    ("PATCH", "/api/v1/projects/{project_id}/snowflake/artifacts/{artifact_id}", "批准 #16a"),
    ("POST", "/api/v1/projects/{project_id}/snowflake/artifacts/{artifact_id}/approve", "批准 #16a"),
    ("POST", "/api/v1/projects/{project_id}/snowflake/materialize-outline-plan", "批准 #16a"),
    # 目录：整份导入；删章 / 删场走 v1 trash、恢复作品走统一回收站（批准 #24a）
    ("POST", "/api/v2/projects/{project_id}/catalog/import", "目录导入退役"),
    ("DELETE", "/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}", "批准 #24a"),
    ("DELETE", "/api/v2/projects/{project_id}/catalog/scenes/{scene_id}", "批准 #24a"),
    ("POST", "/api/v2/projects/{project_id}/restore", "批准 #24a"),
    # 构思：逐场 accept-stale 并进「已复核」（批准 #24a，R15a）
    ("POST", "/api/v2/projects/{project_id}/snowflake-workspace/scenes/accept-stale", "批准 #24a"),
    # 风格参考 v3：旧学习链路的写接口与只读血缘 / 调试端点（批准 #24a）、旧导入轮询、旧样例预览
    ("POST", "/api/v2/style-reference/books/{book_id}/runs", "风格参考 v3"),
    ("POST", "/api/v2/style-reference/runs/{run_id}/synthesize", "风格参考 v3"),
    ("POST", "/api/v2/style-reference/findings/{finding_id}/review", "风格参考 v3"),
    ("POST", "/api/v2/style-reference/findings/{finding_id}/user-feedback", "风格参考 v3"),
    ("GET", "/api/v2/style-reference/runs/{run_id}", "批准 #24a"),
    ("GET", "/api/v2/style-reference/books/{book_id}/runs", "批准 #24a"),
    ("GET", "/api/v2/style-reference/runs/{run_id}/findings", "批准 #24a"),
    ("GET", "/api/v2/style-reference/profiles", "批准 #24a"),
    ("GET", "/api/v2/style-reference/injection/layers", "批准 #24a"),
    ("GET", "/api/v2/style-reference/injection/task-defaults", "批准 #24a"),
    ("GET", "/api/v2/style-reference/readings/{reading_id}", "批准 #24a"),
    ("GET", "/api/v2/style-reference/imports/{op_key}/progress", "导入进度看 job_id + 活动清单"),
    ("POST", "/api/v2/style-reference/profiles/{profile_id}/preview", "旧样例预览"),
    ("GET", "/api/v2/style-reference/bindings/{binding_id}/injection-preview", "预览一律走 POST …/injection-preview"),
    # 旧「回测」：像不像一律走对照检查（迁移 0091）
    ("POST", "/api/v2/style-reference/profiles/{profile_id}/validate", "迁移 0091"),
    ("GET", "/api/v2/style-reference/profiles/{profile_id}/reports", "迁移 0091"),
    ("GET", "/api/v2/style-reference/reports/{report_id}", "迁移 0091"),
    # 一次性的参考书演示（整段前缀同样钉在下面）
    ("GET", "/api/v1/demo/{slug}/status", "一次性的参考书演示"),
)

#: 路径本身还在、只是为别的方法服务的退役行：请求它们答 405。其余退役路径整条都不在了，任何方法都只得 404。
STILL_SERVED_PATHS: dict[str, str] = {
    "/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}": "PATCH 改章",
    "/api/v2/projects/{project_id}/catalog/scenes/{scene_id}": "PATCH 改场",
}

# 整段前缀下一条路由都不许有
RETIRED_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/api/v1/reference-books", "旧参考书接口"),
    ("/api/v1/projects/{project_id}/snowflake", "批准 #16a"),
    ("/api/v1/demo", "一次性的参考书演示"),
)


METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _concrete(template: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda match: f"retired-{match.group(1)}", template)


def _shape(template: str) -> str:
    """去掉占位段的名字与转换器（``{project_id}`` / ``{path:path}`` → ``{}``）：换个参数名复活的路由同样认得出。"""
    return re.sub(r"\{[^{}]*\}", "{}", template)


def _match(app, method: str, path: str) -> Match:
    """路由表对这个方法 + 具体路径的最好匹配：FULL 有路由在服务，PARTIAL 只有别的方法（405），NONE 没有（404）。"""
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": [], "app": app}
    best = Match.NONE
    for route in app.routes:
        matched = route.matches(scope)[0]
        if matched == Match.FULL:
            return matched
        if matched == Match.PARTIAL:
            best = matched
    return best


def test_the_retired_table_has_no_duplicates() -> None:
    keys = [(method, template) for method, template, _why in RETIRED_ROUTES]
    assert len(keys) == len(set(keys))


def test_no_route_serves_a_retired_method_and_path() -> None:
    app = create_app()
    served = [
        f"{method} {template}（{why}）"
        for method, template, why in RETIRED_ROUTES
        if _match(app, method, _concrete(template)) == Match.FULL
    ]
    assert served == []
    # 整条路径都没了：换个方法也不匹配（只得 404）。路径本身还在的几条例外，例外清单也不许过期
    templates = {template for _method, template, _why in RETIRED_ROUTES}
    assert set(STILL_SERVED_PATHS) <= templates
    reachable = sorted(
        f"{method} {template}"
        for template in templates - set(STILL_SERVED_PATHS)
        for method in METHODS
        if _match(app, method, _concrete(template)) != Match.NONE
    )
    assert reachable == []
    gone = [
        template
        for template in STILL_SERVED_PATHS
        if not any(_match(app, method, _concrete(template)) == Match.FULL for method in METHODS)
    ]
    assert gone == [], "这几条路径已经没有路由在服务了：从 STILL_SERVED_PATHS 删掉，它们就该只答 404"


def test_retired_routes_and_prefixes_are_absent_from_openapi_and_the_route_table() -> None:
    app = create_app()
    paths = app.openapi()["paths"]
    operations = {(method.upper(), _shape(path)) for path, item in paths.items() for method in item}
    documented = [
        f"{method} {template}"
        for method, template, _why in RETIRED_ROUTES
        if (method, _shape(template)) in operations
    ]
    assert documented == []
    # 路由表里每条路由的完整路径模板（不进 OpenAPI 的 include_in_schema=False 路由也在这里）；include_router 是惰性
    # 挂载，app.routes 里是包装对象，iter_route_contexts 展开成一条条带前缀的路由
    templates = [context.path or "" for context in iter_route_contexts(app.routes)]
    for prefix, why in RETIRED_PREFIXES:
        assert not [path for path in paths if _shape(path).startswith(_shape(prefix))], why
        assert not [path for path in templates if _shape(path).startswith(_shape(prefix))], why
        probes = [_concrete(prefix), _concrete(prefix) + "/retired-a", _concrete(prefix) + "/retired-a/retired-b"]
        reached = [
            f"{method} {probe}"
            for probe in probes
            for method in METHODS
            if _match(app, method, probe) != Match.NONE
        ]
        assert reached == [], why


def test_retired_routes_answer_404_or_405(client) -> None:
    """路径整条没了的答 404；路径还为别的方法服务的（``STILL_SERVED_PATHS``）答 405。"""
    wrong = {}
    for method, template, _why in RETIRED_ROUTES:
        response = client.request(method, _concrete(template), json={} if method in {"POST", "PUT", "PATCH"} else None)
        expected = 405 if template in STILL_SERVED_PATHS else 404
        if response.status_code != expected:
            wrong[f"{method} {template}"] = (expected, response.status_code)
    assert wrong == {}
