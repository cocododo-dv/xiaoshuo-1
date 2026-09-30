from __future__ import annotations

import logging
import os

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import event

from novel_system.api.app import SUPPORTED_DATABASE_REVISION, create_app
from novel_system.db.base import Base
from novel_system.env_config import DEFAULT_DATABASE_PATH
from novel_system.db.session import engine
from novel_system.settings import (
    BACKEND_ROOT,
    get_settings,
)


def _stamp_database_revision(revision: str = SUPPORTED_DATABASE_REVISION) -> None:
    with engine().begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql("DELETE FROM alembic_version")
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (revision,),
        )


def test_create_app_never_builds_the_schema_itself(monkeypatch) -> None:
    """库结构只由 Alembic 建。退役的 NOVEL_SYSTEM_AUTO_CREATE_TABLES（B12-20）即使设成 true 也不再让后端
    create_all 建表——那会盖住 ORM 与迁移之间的漂移。"""
    calls: list[object] = []

    monkeypatch.setenv("NOVEL_SYSTEM_AUTO_CREATE_TABLES", "true")
    monkeypatch.setattr(Base.metadata, "create_all", lambda *args, **kwargs: calls.append((args, kwargs)))

    with TestClient(create_app()) as client:
        client.get("/live")

    assert calls == []


def test_retired_quota_env_vars_log_one_startup_warning_and_block_nothing(monkeypatch, caplog) -> None:
    """重评 R3:额度闸删了,这些变量还设着时启动照常(以前只设金额上限不设单价、或值写错,后端起不来),
    每个进程只记一条警告,点名仍然设着的变量。"""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD", "5")
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS", "not-a-number")

    with caplog.at_level(logging.WARNING, logger="novel_system.env_config"):
        create_app()
        get_settings()
        get_settings(include_runtime_config=False)

    warnings = [record.getMessage() for record in caplog.records if "no longer have any effect" in record.getMessage()]
    assert len(warnings) == 1
    assert "NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD" in warnings[0]
    assert "NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS" in warnings[0]
    assert "NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT" not in warnings[0]


@pytest.mark.parametrize("value", ["0", "-60", "an-hour"])
def test_invalid_reservation_recovery_ttl_still_stops_startup(monkeypatch, value: str) -> None:
    """NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS 没有退役(重评 R3 只退役那八个):写错了照旧起不来。
    只在启动对账里才读的话,后端照常起来,对账只记一条 scan_failed,没有主人的非场景预留永远回收不了。"""
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS", value)

    with pytest.raises(ValueError, match="NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS"):
        get_settings(include_runtime_config=False)
    with pytest.raises(ValueError, match="NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS"):
        create_app()


def test_default_runtime_paths_do_not_depend_on_process_working_directory(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("NOVEL_SYSTEM_DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    settings = get_settings(include_runtime_config=False)

    assert settings.database_url == f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"


def test_relative_runtime_paths_are_resolved_from_backend_root(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS",
        os.pathsep.join(["runtime/books", str(tmp_path / "absolute-books")]),
    )

    settings = get_settings(include_runtime_config=False)

    assert settings.style_reference_import_roots == (
        BACKEND_ROOT / "runtime" / "books",
        tmp_path / "absolute-books",
    )


def test_cors_defaults_to_local_dev_origins_without_wildcard_credentials(monkeypatch) -> None:
    monkeypatch.delenv("NOVEL_SYSTEM_CORS_ORIGINS", raising=False)
    app = create_app()

    with TestClient(app) as client:
        response = client.options(
            "/api/v2/projects",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        disallowed = client.options(
            "/api/v2/projects",
            headers={
                "Origin": "https://example.invalid",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert disallowed.headers.get("access-control-allow-origin") != "*"


def test_request_body_limit_rejects_declared_oversize_with_standard_envelope(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_MAX_REQUEST_BODY_BYTES", "64")
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/chapters",
            content=b"x" * 65,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:5173",
            },
        )

    payload = response.json()
    assert response.status_code == 413
    assert payload["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
    assert payload["error"]["details"] == {"max_bytes": 64}
    assert payload["request_id"] == response.headers["X-Request-Id"]
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_request_body_limit_counts_chunked_stream_when_length_is_absent(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_MAX_REQUEST_BODY_BYTES", "64")

    def chunks():
        yield b"x" * 40
        yield b"y" * 40

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/chapters",
            content=chunks(),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"


def test_request_body_limit_configuration_must_be_positive(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_MAX_REQUEST_BODY_BYTES", "0")

    with pytest.raises(ValueError, match="MAX_REQUEST_BODY_BYTES"):
        create_app()


def test_unhandled_errors_return_request_id_without_leaking_exception_text() -> None:
    app = create_app()

    @app.get("/boom-for-test")
    def boom_for_test():
        raise RuntimeError("secret database password leaked here")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom-for-test")

    payload = response.json()
    assert response.status_code == 500
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "internal server error"
    assert "secret database password" not in response.text
    assert payload["request_id"].startswith("req_")


def _app_with_failing_route(exc: Exception):
    app = create_app()

    @app.get("/api/v2/boom-for-test")
    def boom_for_test():
        raise exc

    return app


def test_unhandled_errors_leave_through_cors_with_the_request_id(caplog) -> None:
    """B12-03：未处理的异常以前在 CORS 与请求编号中间件之外变成 500——浏览器读不到响应，前端只能报
    「连接接口失败」。现在它在 CORS 里面变成标准信封：带 Access-Control-Allow-Origin 与 X-Request-Id，只记一次日志。"""
    origin = "http://127.0.0.1:5174"
    app = _app_with_failing_route(RuntimeError("unique constraint failed: chapter_goals.display_order"))

    with caplog.at_level("ERROR", logger="novel_system.api"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v2/boom-for-test", headers={"Origin": origin})

    payload = response.json()
    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers["X-Request-Id"].startswith("req_")
    assert payload["request_id"] == response.headers["X-Request-Id"]
    assert payload["ok"] is False and payload["data"] is None
    assert payload["error"] == {
        "code": "INTERNAL_ERROR",
        "message": "internal server error",
        "details": {"retryable": False},
    }
    logged = [record for record in caplog.records if record.getMessage().startswith("Unhandled API error")]
    assert len(logged) == 1
    assert logged[0].getMessage() == f"Unhandled API error request_id={payload['request_id']}"
    assert logged[0].exc_info is not None


def test_unhandled_error_detail_is_exposed_only_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_EXPOSE_ERROR_DETAIL", "true")
    app = _app_with_failing_route(RuntimeError("boom detail for the developer"))

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v2/boom-for-test")

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "boom detail for the developer"


def test_unhandled_errors_still_propagate_to_the_server_and_test_client() -> None:
    """信封发出去之后异常照旧往外抛：服务器照常记录，测试客户端（默认 raise_server_exceptions）照常抛出。"""
    app = _app_with_failing_route(RuntimeError("still raised after the envelope"))

    with TestClient(app) as client:
        with pytest.raises(RuntimeError, match="still raised after the envelope"):
            client.get("/api/v2/boom-for-test", headers={"Origin": "http://127.0.0.1:5174"})


def test_local_only_default_rejects_non_loopback_clients() -> None:
    with TestClient(
        create_app(),
        client=("203.0.113.10", 45123),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/api/v1/chapters")
        live = client.get("/live")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REMOTE_ACCESS_DISABLED"
    assert response.headers["X-Request-Id"].startswith("req_")
    assert live.status_code == 200
    assert live.json() == {"status": "live"}


def test_local_only_rejects_forwarded_requests_even_from_loopback_proxy() -> None:
    with TestClient(create_app()) as client:
        response = client.get(
            "/api/v1/chapters",
            headers={"X-Forwarded-For": "198.51.100.44"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["details"]["forwarded_request_rejected"] is True


@pytest.mark.parametrize(
    ("configured_admin_token", "request_admin_token"),
    [
        (None, None),
        ("configured-secret", None),
        ("configured-secret", "configured-secret"),
    ],
)
def test_local_only_remote_system_config_rejection_does_not_leak_admin_state(
    monkeypatch,
    configured_admin_token: str | None,
    request_admin_token: str | None,
) -> None:
    if configured_admin_token is None:
        monkeypatch.delenv("NOVEL_SYSTEM_ADMIN_TOKEN", raising=False)
    else:
        monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", configured_admin_token)
    headers = {}
    if request_admin_token is not None:
        headers["X-Admin-Token"] = request_admin_token

    with TestClient(create_app(), client=("192.0.2.42", 50000)) as remote_client:
        response = remote_client.post(
            "/api/v1/system-config/llm/providers",
            headers=headers,
            json={"provider_id": "must-not-reach-admin-auth"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "REMOTE_ACCESS_DISABLED"
    assert "admin" not in response.text.lower()


def test_remote_mode_requires_token_at_startup(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LOCAL_ONLY", "false")
    monkeypatch.delenv("NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN"):
        create_app()


def test_remote_mode_authenticates_every_non_health_request(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LOCAL_ONLY", "false")
    monkeypatch.setenv("NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN", "remote-secret")
    _stamp_database_revision()
    with TestClient(create_app(), client=("203.0.113.11", 45124)) as client:
        denied = client.get("/api/v1/chapters")
        accepted = client.get(
            "/api/v1/chapters",
            headers={"X-Novel-Access-Token": "remote-secret"},
        )
        ready = client.get("/ready")

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "REMOTE_ACCESS_TOKEN_REQUIRED"
    assert accepted.status_code == 200
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_remote_mode_does_not_trust_client_supplied_operator_ref(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LOCAL_ONLY", "false")
    monkeypatch.setenv("NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN", "remote-secret")
    app = create_app()

    @app.get("/operator-ref-for-test")
    def operator_ref_for_test(request: Request):
        return {"operator_ref": request.state.operator_ref}

    with TestClient(app) as client:
        response = client.get(
            "/operator-ref-for-test",
            headers={
                "X-Novel-Access-Token": "remote-secret",
                "X-Operator-Ref": "forged-admin",
            },
        )

    assert response.status_code == 200
    assert response.json()["operator_ref"] == "remote-access-token"


def test_local_mode_keeps_operator_ref_for_single_author_audit() -> None:
    app = create_app()

    @app.get("/operator-ref-for-test")
    def operator_ref_for_test(request: Request):
        return {"operator_ref": request.state.operator_ref}

    with TestClient(app) as client:
        response = client.get(
            "/operator-ref-for-test",
            headers={"X-Operator-Ref": "local-author"},
        )

    assert response.status_code == 200
    assert response.json()["operator_ref"] == "local-author"


def test_ready_rejects_database_without_alembic_version() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_NOT_READY"
    assert error["details"]["reason"] == "database_probe_failed"
    assert error["details"]["expected_revision"] == SUPPORTED_DATABASE_REVISION


def test_ready_rejects_outdated_database_revision() -> None:
    _stamp_database_revision("20260716_0072")
    with TestClient(create_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_NOT_READY"
    assert error["details"] == {
        "retryable": False,
        "reason": "schema_revision_mismatch",
        "expected_revision": SUPPORTED_DATABASE_REVISION,
        "current_revision": "20260716_0072",
    }


def test_ready_rejects_head_stamp_with_missing_runtime_table() -> None:
    _stamp_database_revision()
    with engine().begin() as connection:
        connection.exec_driver_sql("DROP TABLE author_preference_profiles")
    with TestClient(create_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_NOT_READY"
    assert error["details"]["reason"] == "schema_tables_missing"
    assert error["details"]["missing_table_count"] == 1


def test_ready_rejects_head_stamp_with_missing_required_column() -> None:
    _stamp_database_revision()
    with engine().begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE author_preference_profiles DROP COLUMN created_by"
        )
    with TestClient(create_app()) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_NOT_READY"
    assert error["details"] == {
        "retryable": False,
        "reason": "schema_columns_missing",
        "expected_revision": SUPPORTED_DATABASE_REVISION,
        "missing_table_count": 1,
        "missing_column_count": 1,
    }


class _ReadyStatements:
    """记下 /ready 发出的语句：迁移版本每次都读，表 / 列结构检查（sqlite_master、table_xinfo）按版本只做一次。"""

    def __init__(self) -> None:
        self.statements: list[str] = []
        event.listen(engine(), "before_cursor_execute", self._record)

    def _record(self, _connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        self.statements.append(str(statement))

    def take(self) -> tuple[int, int]:
        revision_reads = sum("alembic_version" in statement for statement in self.statements)
        structure_reads = sum(
            "sqlite_master" in statement or "table_xinfo" in statement or "table_info" in statement
            for statement in self.statements
        )
        self.statements.clear()
        return revision_reads, structure_reads


def test_ready_checks_the_schema_structure_once_per_revision() -> None:
    """X01-17：以前每次探测都对 70 多张表各发一条 PRAGMA table_info；启动脚本、E2E 与部署探针都在轮询。"""
    _stamp_database_revision()
    statements = _ReadyStatements()
    with TestClient(create_app()) as client:
        # 启动时 API 的库结构闸（B12-19）已经用同一份检查查过一次结构：之后的探测只读版本
        startup_reads = statements.take()
        first = client.get("/ready")
        first_reads = statements.take()
        second = client.get("/ready")
        second_reads = statements.take()

    assert first.status_code == second.status_code == 200
    assert startup_reads[1] > len(Base.metadata.tables)
    assert first_reads == (1, 0)
    assert second_reads == (1, 0)


def test_ready_rechecks_the_structure_after_seeing_another_revision() -> None:
    _stamp_database_revision()
    with TestClient(create_app()) as client:
        assert client.get("/ready").status_code == 200
        with engine().begin() as connection:
            connection.exec_driver_sql("DROP TABLE author_preference_profiles")
        # 同一个版本上已经查过：结构检查不重做（手工改库而不动版本不是迁移的做法）
        assert client.get("/ready").status_code == 200

        _stamp_database_revision("20260716_0072")
        mismatch = client.get("/ready")
        _stamp_database_revision()
        rechecked = client.get("/ready")

    assert mismatch.json()["error"]["details"]["reason"] == "schema_revision_mismatch"
    assert rechecked.status_code == 503
    assert rechecked.json()["error"]["details"]["reason"] == "schema_tables_missing"


def test_ready_does_not_remember_a_failed_structure_check() -> None:
    _stamp_database_revision()
    with engine().begin() as connection:
        connection.exec_driver_sql("ALTER TABLE author_preference_profiles DROP COLUMN created_by")
    statements = _ReadyStatements()
    with TestClient(create_app()) as client:
        broken = client.get("/ready")
        broken_reads = statements.take()
        with engine().begin() as connection:
            connection.exec_driver_sql("ALTER TABLE author_preference_profiles ADD COLUMN created_by VARCHAR")
        repaired = client.get("/ready")
        repaired_reads = statements.take()

    assert broken.status_code == 503
    assert broken.json()["error"]["details"]["reason"] == "schema_columns_missing"
    assert repaired.status_code == 200
    assert broken_reads[1] > 0 and repaired_reads[1] > 0


def test_api_answers_schema_upgrade_needed_while_the_database_is_behind_the_code(monkeypatch) -> None:
    """B12-19（批准 #28）：代码比库新时 /api/* 统一回 503 与中文说明（带 CORS 与请求编号），不再各自报
    「database operation failed」；升级之后不用重启就放行。/live、/ready 不受这道闸影响。"""
    started: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.background_recovery.run_startup_recovery", lambda: started.append("recovery")
    )
    _stamp_database_revision("20260716_0072")
    with TestClient(create_app()) as client:
        behind = client.get("/api/v2/projects", headers={"Origin": "http://127.0.0.1:5173"})
        ready = client.get("/ready")
        live = client.get("/live")
        _stamp_database_revision()
        upgraded = client.get("/api/v2/projects")

    assert behind.status_code == 503
    payload = behind.json()
    assert payload["error"]["code"] == "SERVICE_NOT_READY"
    assert payload["error"]["message"] == "数据库结构需要升级：请重启后端（启动脚本会自动升级）"
    assert payload["error"]["details"]["reason"] == "schema_revision_mismatch"
    assert payload["error"]["details"]["current_revision"] == "20260716_0072"
    assert payload["request_id"] == behind.headers["X-Request-Id"]
    assert behind.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert ready.status_code == 503 and ready.json()["error"]["message"] == "database schema revision is not ready"
    assert live.status_code == 200
    assert upgraded.status_code == 200
    # 结构落后时不拿旧结构跑启动恢复
    assert started == []


def test_api_schema_gate_reports_missing_structure_at_the_current_revision() -> None:
    _stamp_database_revision()
    with engine().begin() as connection:
        connection.exec_driver_sql("DROP TABLE author_preference_profiles")
    with TestClient(create_app()) as client:
        response = client.post("/api/v2/projects", json={"title": "雨城旧信", "outline_text": "林昭翻开旧案卷。"})

    assert response.status_code == 503
    assert response.json()["error"]["details"]["reason"] == "schema_tables_missing"


def test_api_schema_gate_stays_open_for_databases_alembic_does_not_manage(monkeypatch) -> None:
    """测试库用 create_all 建、没有 alembic_version：这道闸不管它（放行，也不再为每个请求查库）。"""
    started: list[str] = []
    monkeypatch.setattr(
        "novel_system.services.background_recovery.run_startup_recovery", lambda: started.append("recovery")
    )
    statements = _ReadyStatements()
    with TestClient(create_app()) as client:
        statements.take()
        first = client.get("/api/v2/projects")
        second = client.get("/api/v2/projects")
        reads = statements.take()

    assert first.status_code == second.status_code == 200
    assert reads == (0, 0)
    assert started == ["recovery"]


def test_remote_mode_requires_token_for_loopback_proxy_peer(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LOCAL_ONLY", "false")
    monkeypatch.setenv("NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN", "remote-secret")
    with TestClient(create_app()) as client:
        denied = client.get("/api/v1/chapters")
        accepted = client.get(
            "/api/v1/chapters",
            headers={"X-Novel-Access-Token": "remote-secret"},
        )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "REMOTE_ACCESS_TOKEN_REQUIRED"
    assert accepted.status_code == 200


def test_remote_mode_allows_cors_preflight_but_authenticates_real_request(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_LOCAL_ONLY", "false")
    monkeypatch.setenv("NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN", "remote-secret")
    with TestClient(create_app()) as client:
        preflight = client.options(
            "/api/v2/projects",
            headers={
                "Origin": "http://127.0.0.1:5174",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-Novel-Access-Token",
            },
        )
        actual = client.get(
            "/api/v2/projects",
            headers={"Origin": "http://127.0.0.1:5174"},
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:5174"
    assert actual.status_code == 401
    assert actual.json()["error"]["code"] == "REMOTE_ACCESS_TOKEN_REQUIRED"
