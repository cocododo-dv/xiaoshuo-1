"""测试进程的隔离：开发者 shell 里的 NOVEL_SYSTEM_*（X04-05）、别的检出的代码（X04-06）、上一个 app 的
风格作业清扫线程（X04-20）都进不了用例。"""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import novel_system
from novel_system.services.style_reference import jobs
from novel_system.settings import get_settings
from tests.conftest import (
    ENV_PASSTHROUGH,
    _wait_for_job_sweepers,
    confine_novel_system_to_this_checkout,
    foreign_novel_system_modules,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
# isolated_database 给每个用例设的测试默认值
TEST_DEFAULT_ENV = frozenset(
    {
        "NOVEL_SYSTEM_DATABASE_URL",
        "NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS",
        "NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER",
    }
)


# ---------------------------------------------------------------------------
# 开发者环境
# ---------------------------------------------------------------------------


def test_no_developer_novel_system_env_is_visible_inside_a_test() -> None:
    visible = {key for key in os.environ if key.startswith("NOVEL_SYSTEM_")}
    assert visible - ENV_PASSTHROUGH - TEST_DEFAULT_ENV == set()
    assert get_settings().llm_enabled is False


def test_coach_is_fail_closed_inside_the_test_process(client) -> None:
    """下一条用例在一个导出了「真」LLM 配置的环境里跑这一条；单独跑时它就是一条普通的 fail-closed 用例。"""
    created = client.post(
        "/api/v2/projects",
        json={"title": "隔离之书", "outline_text": "测试进程隔离验证用项目。"},
        headers={"X-Idempotency-Key": "isolation-project"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["data"]["project"]["project_id"]
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/assistant",
        json={"step_key": "book_brief", "message": "这一步还缺什么？"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SNOWFLAKE_LLM_NOT_CONFIGURED"


class _ConnectionCounter:
    """本机一个真在监听的端口：有人连上来就记一笔、马上断开。"""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="isolation_probe_listener", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            conn.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._sock.close()


def test_exported_llm_settings_never_reach_a_provider(tmp_path: Path) -> None:
    """作者的机器为实跑导出了 LLM 配置：在那样的 shell 里跑测试，fail-closed 的用例也不能真的连服务商。"""
    listener = _ConnectionCounter()
    try:
        env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
        env.update(
            {
                "NOVEL_SYSTEM_LLM_ENABLED": "true",
                "NOVEL_SYSTEM_LLM_PROVIDER": "openai_compatible",
                "NOVEL_SYSTEM_LLM_BASE_URL": f"http://127.0.0.1:{listener.port}/v1",
                "NOVEL_SYSTEM_LLM_API_KEY": "sk-from-the-developer-shell",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--basetemp={tmp_path / 'inner'}",
                "tests/test_test_isolation.py::test_coach_is_fail_closed_inside_the_test_process",
            ],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        listener.close()
    output = result.stdout[-3000:] + result.stderr[-3000:]
    assert result.returncode == 0, output
    assert "1 passed" in result.stdout, output
    assert listener.connections == 0, "测试进程把请求发到了开发者环境里配置的服务商地址"


# ---------------------------------------------------------------------------
# 别的检出的代码
# ---------------------------------------------------------------------------


def _other_checkout(tmp_path: Path, module: str) -> Path:
    """另一个检出的 novel_system 包目录（共用 venv 的可编辑安装就是这样把它带进来的），里面有本检出没有的模块。"""
    package = tmp_path / "other_checkout" / "src" / "novel_system"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / f"{module}.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
    importlib.invalidate_caches()
    return package


def test_every_loaded_novel_system_module_comes_from_this_checkout() -> None:
    # backend/novel_system 那个 extend_path 导入垫片已删（B12-08）：包只有 backend/src 这一处
    package = (BACKEND_DIR / "src" / "novel_system").resolve()
    assert {Path(entry).resolve() for entry in novel_system.__path__} == {package}
    assert Path(novel_system.__file__).resolve() == package / "__init__.py"
    assert foreign_novel_system_modules() == []


def test_a_module_only_another_checkout_has_cannot_be_imported(tmp_path: Path, monkeypatch) -> None:
    other = _other_checkout(tmp_path, "zz_deleted_in_this_checkout")
    monkeypatch.setattr(novel_system, "__path__", [*novel_system.__path__, str(other)])  # extend_path 会这样做
    confine_novel_system_to_this_checkout()
    assert str(other) not in novel_system.__path__
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("novel_system.zz_deleted_in_this_checkout")


def test_a_session_whose_novel_system_is_another_checkout_refuses_to_run(tmp_path: Path) -> None:
    """``novel_system`` 包本身解析到了另一个检出（比如那边的 src 排在了 sys.path 前面）：整个会话拒跑，说清楚原因。"""
    other_src = tmp_path / "other_checkout" / "src"
    (other_src / "novel_system").mkdir(parents=True)
    (other_src / "novel_system" / "__init__.py").write_text("", encoding="utf-8")
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "other_checkout_first.py").write_text(
        f"import sys\nsys.path.insert(0, {str(other_src)!r})\nimport novel_system  # noqa: F401\n", encoding="utf-8"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_CURRENT_TEST"}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(plugin_dir), env.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "other_checkout_first",
            f"--basetemp={tmp_path / 'inner'}",
            "tests/test_test_isolation.py::test_no_developer_novel_system_env_is_visible_inside_a_test",
        ],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "本检出之外的 novel_system 代码" in output and str(other_src / "novel_system") in output, output
    assert " passed" not in result.stdout, output


def test_code_already_imported_from_another_checkout_is_refused(tmp_path: Path, monkeypatch) -> None:
    name = "novel_system.zz_imported_from_elsewhere"
    other = _other_checkout(tmp_path, "zz_imported_from_elsewhere")
    monkeypatch.setattr(novel_system, "__path__", [*novel_system.__path__, str(other)])
    try:
        assert importlib.import_module(name).VALUE == "foreign"
        assert foreign_novel_system_modules() == [f"{name} → {other / 'zz_imported_from_elsewhere.py'}"]
        with pytest.raises(pytest.UsageError, match="本检出之外"):
            confine_novel_system_to_this_checkout()
    finally:
        sys.modules.pop(name, None)
        if hasattr(novel_system, "zz_imported_from_elsewhere"):
            delattr(novel_system, "zz_imported_from_elsewhere")
    assert foreign_novel_system_modules() == []


# ---------------------------------------------------------------------------
# 风格作业清扫线程
# ---------------------------------------------------------------------------


def test_a_sweeper_still_in_its_tick_stops_when_the_next_one_starts(monkeypatch) -> None:
    """lifespan 重启时旧清扫线程还在一拍里：新线程启动不能让旧线程接着循环（各有各的停止信号）。"""
    entered = threading.Event()
    release = threading.Event()
    ticks: dict[int, int] = {}

    def fake_tick(*, now: float | None = None) -> None:
        ident = threading.get_ident()
        ticks[ident] = ticks.get(ident, 0) + 1
        if not entered.is_set():
            entered.set()
            release.wait(10)

    monkeypatch.setattr(jobs, "sweeper_tick", fake_tick)
    jobs.start_job_sweeper(interval_seconds=1.0)
    first = jobs._SWEEPER
    second = None
    try:
        assert entered.wait(5)
        jobs.shutdown_job_workers()  # 旧 app 关掉时，它的清扫线程还卡在第一拍里
        jobs.start_job_sweeper(interval_seconds=1.0)  # 下一个 app 马上启动
        second = jobs._SWEEPER
        assert second is not None and second is not first
        release.set()
        first.join(5)
        assert not first.is_alive(), "旧清扫线程在下一个 lifespan 启动后还在循环"
        assert ticks[first.ident] == 1
    finally:
        release.set()
        jobs.shutdown_job_workers()
        for thread in (first, second):
            if thread is not None:
                thread.join(5)
    assert not second.is_alive()


def test_teardown_stops_and_reports_a_sweeper_nobody_shut_down(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "sweeper_tick", lambda *, now=None: None)
    jobs.start_job_sweeper(interval_seconds=1.0)
    leaked = jobs._SWEEPER
    with pytest.raises(pytest.fail.Exception, match="仍在运行"):
        _wait_for_job_sweepers(timeout=0.5)
    assert jobs._SWEEPER is None and not leaked.is_alive()
