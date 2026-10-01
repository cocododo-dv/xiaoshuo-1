from __future__ import annotations

import os
import shutil
import sys
import threading
from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# 会话开始：只跑本检出的代码、不带开发者的 NOVEL_SYSTEM_* 环境（都必须先于下面任何 novel_system 导入）
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).resolve().parents[1]
# 本检出里 novel_system 只能来自 backend/src 下的包（backend/ 下那个 extend_path 导入垫片已删，B12-08）
_CHECKOUT_PACKAGE_DIRS = ((_BACKEND_DIR / "src" / "novel_system").resolve(),)
# 只有这两个 NOVEL_SYSTEM_* 可以从外面带进测试进程：可选的本地私有语料通道、E2E 通道用的解释器路径
ENV_PASSTHROUGH = frozenset({"NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS", "NOVEL_SYSTEM_PYTHON"})


def _inside_checkout(path: str | os.PathLike[str]) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in _CHECKOUT_PACKAGE_DIRS)


def foreign_novel_system_modules() -> list[str]:
    """已经导入的 novel_system 模块里，文件不在本检出的（``名字 → 路径``）。"""
    foreign: list[str] = []
    for name, module in list(sys.modules.items()):
        if name != "novel_system" and not name.startswith("novel_system."):
            continue
        origin = getattr(module, "__file__", None)
        if origin and not _inside_checkout(origin):
            foreign.append(f"{name} → {origin}")
    return sorted(foreign)


def _refuse_foreign_code(when: str) -> None:
    foreign = foreign_novel_system_modules()
    if foreign:
        raise pytest.UsageError(
            f"测试会话（{when}）导入了本检出之外的 novel_system 代码，拒绝运行。本检出：{_CHECKOUT_PACKAGE_DIRS[0]}\n  "
            + "\n  ".join(foreign[:20])
            + "\n多半是在 git worktree 里跑、共用 venv 的可编辑安装指向了另一个检出：从本检出的 backend/ 目录跑 pytest，"
            "PYTHONPATH 以本检出的 backend/src 开头。"
        )


def confine_novel_system_to_this_checkout() -> None:
    """测试会话只导入本检出的 novel_system（X04-06）。

    各 git worktree 共用一个 venv，venv 里可编辑安装的 .pth 指向另一个检出。以前 backend/novel_system/__init__.py
    那个垫片用 ``pkgutil.extend_path``，于是 ``novel_system.__path__`` 里混进了那个检出的包目录——在本检出里删掉 /
    搬走的模块还能从那边悄悄导进来，测试照样绿。垫片已删（B12-08：``pythonpath = ["src"]`` 让 ``novel_system`` 是
    backend/src 下的普通包）；这里仍把 ``__path__`` 里任何外来目录拿掉（之后本检出里没有的模块就是
    ModuleNotFoundError），已经从外面导进来的模块直接拒跑——比如别的检出的 src 被排到了 sys.path 前面。
    """
    import novel_system

    novel_system.__path__[:] = [entry for entry in novel_system.__path__ if _inside_checkout(entry)]
    _refuse_foreign_code("会话开始")


def developer_env_keys() -> list[str]:
    """当前环境里要清掉的 NOVEL_SYSTEM_* 变量（``ENV_PASSTHROUGH`` 以外的全部）。"""
    return sorted(key for key in os.environ if key.startswith("NOVEL_SYSTEM_") and key not in ENV_PASSTHROUGH)


confine_novel_system_to_this_checkout()
# 测试进程不认开发者 shell 里的 NOVEL_SYSTEM_*（X04-05）：作者的机器为实跑导出了 LLM_ENABLED / BASE_URL / API_KEY，
# 不清掉的话「没配 LLM 就 fail-closed」的用例会真的把提示词发给服务商。这里在导入期清一次（模块级 skipif、会话级
# 夹具与子进程都看不到它们），每个用例开始前 ``_hermetic_test_process`` 再清一次，``isolated_database`` 再设测试默认值。
for _key in developer_env_keys():
    del os.environ[_key]

from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.support.api_client import AutoKeyTestClient

from novel_system.api.app import create_app
from novel_system.cache_registry import reset_all_caches
from novel_system.db.base import Base
from novel_system.db.session import SessionLocal, reset_engine
from novel_system.services.style_reference.jobs import SWEEPER_THREAD_NAME, shutdown_job_workers

# 按需启用的具名夹具（online_pipeline / skeleton_snowflake / style_workers …）：测试文件写
# ``pytestmark = pytest.mark.usefixtures("…")``，不再各自包一个一行的 autouse 夹具
pytest_plugins = ["tests.support.fixtures"]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """会话结束再查一遍：整个会话导入过的 novel_system 模块都来自本检出。"""
    foreign = foreign_novel_system_modules()
    if foreign:
        session.exitstatus = pytest.ExitCode.USAGE_ERROR
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        message = "测试会话导入了本检出之外的 novel_system 代码：\n  " + "\n  ".join(foreign[:20])
        if reporter is not None:
            reporter.write_line("\n" + message, red=True)
        else:
            print(message, file=sys.stderr)


@pytest.fixture(scope="session")
def _schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """整套 ORM 表结构每个会话只建一次，每个测试拿一份文件副本。

    逐测试 drop_all + create_all（72 张表、113 个索引、每条 DDL 一次 fsync）约 1 s / 测试，
    占本机全量的一半多；文件副本 1–3 ms。模板用一个不带 WAL / 外键钩子的临时 engine 建，
    不经 db.session.engine()，所以不会把引擎状态带进任何测试；每次会话现建、从不入库，
    表结构漂移守卫照旧拿活的 ORM 与 Alembic 对比。
    """
    path = tmp_path_factory.mktemp("schema_template") / "template.db"
    template_engine = sa.create_engine(f"sqlite:///{path}")
    try:
        Base.metadata.create_all(bind=template_engine)
    finally:
        template_engine.dispose()
    return path


@pytest.fixture(autouse=True)
def _hermetic_test_process(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """每个用例：先清掉会话里冒出来的 NOVEL_SYSTEM_*（X04-05），前后各复位一次登记过的进程级缓存（X04-16）。

    它必须先于设测试默认值的 ``isolated_database`` 运行。pytest 对同一处定义的自动夹具按**名字**排序（不是按定义
    顺序），所以真正的保证是显式依赖：``isolated_database`` 以及按名覆盖它的测试文件都把本夹具列为参数。
    缓存为什么要复位见 ``novel_system.cache_registry``。
    """
    for key in developer_env_keys():
        monkeypatch.delenv(key)
    reset_all_caches()
    yield
    reset_all_caches()


def _wait_for_job_sweepers(timeout: float = 30.0) -> None:
    """用例的 app 关掉后，它的风格作业清扫线程可能还在最后一拍里：换库之前等它收尾（X04-20），免得那一拍落到
    下一个用例的库上。到时还没停的是 lifespan 没停掉的清扫线程——停掉它，算这个用例的错。"""
    sweepers = [thread for thread in threading.enumerate() if thread.name == SWEEPER_THREAD_NAME]
    for thread in sweepers:
        thread.join(timeout)
    leaked = [thread for thread in sweepers if thread.is_alive()]
    if leaked:
        shutdown_job_workers()
        for thread in leaked:
            thread.join(timeout)
        pytest.fail(f"{len(leaked)} 个风格作业清扫线程在用例结束后仍在运行（没有经过 shutdown_job_workers）", pytrace=False)


@pytest.fixture(autouse=True)
def isolated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _schema_template: Path,
    _hermetic_test_process: None,
) -> Generator[None, None, None]:
    database_path = tmp_path / "test.db"
    shutil.copyfile(_schema_template, database_path)
    monkeypatch.setenv("NOVEL_SYSTEM_DATABASE_URL", f"sqlite:///{database_path}")
    # Path-import tests are isolated to the per-test temporary directory. In
    # production this capability is disabled unless an operator configures one
    # or more roots explicitly.
    import_roots = [str(tmp_path), str(Path(__file__).resolve().parent)]
    local_corpus = os.environ.get("NOVEL_SYSTEM_STYLE_REF_LOCAL_CORPUS", "").strip()
    if local_corpus:
        import_roots.append(str(Path(local_corpus).expanduser().resolve().parent))
    monkeypatch.setenv(
        "NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS",
        os.pathsep.join(import_roots),
    )
    # 场景 token 生命周期预算现在**产品默认关闭**（单作者不预设硬闸门）。整套测试仍以历史
    # 「武装 5×」为基线运行——绝大多数断言都建立在这道闸门存在之上（5×基线、耗尽码、topup
    # 审计等）。解除武装本身由 test_scene_token_budget.py 里显式设 0 的专门用例覆盖。
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", "5")

    reset_engine()
    yield
    _wait_for_job_sweepers()
    # 关掉本测试的连接池（Windows 上不关掉删不了 tmp 目录里的库文件）。
    reset_engine()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    # 写请求没带幂等键时自动配一个新键（产品的写接口一律要键，见 tests/support/api_client.py）
    with AutoKeyTestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def raw_client(client: TestClient) -> TestClient:
    """同一个应用、不自动配幂等键的客户端：测「缺键 400」之类的边界（不再跑一遍 lifespan）。"""
    return TestClient(client.app)


@pytest.fixture
def session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def build_fake_paragraph_classifier():
    """Deterministic LLMClient mock 的类(不经 fixture 也能用:路由测试的导入助手需要它——
    2026-09-15 严格 LLM 后,产品路由的导入 / 重新分类没有 LLM 就 409)。

    返回一个 FakeLLMClient 类(测试调 `FakeLLMClient(rule="dialogue_heavy")` 实例化)。
    `rule` 控制启发式行为,便于覆盖锚定校准 agreement >= 0.85 与 < 0.85 两条路径。
    """
    import json

    class _FakeLLMResponse:
        def __init__(self, classifications: list[dict]) -> None:
            self.structured_output = {"classifications": classifications}
            self.text = json.dumps(self.structured_output, ensure_ascii=False)
            self.usage: dict = {}
            self.finish_reason = "stop"
            self.request_id = None
            self.provider = "fake"
            self.model = "fake"
            self.raw_response: dict = {}
            self.response_format = "json_object"

    class FakeLLMClient(AccountedGenerateMixin):
        def __init__(self, rule: str = "default") -> None:
            self.rule = rule
            self.call_count = 0
            self.call_log: list[dict] = []

        def generate(self, request):  # noqa: ANN001
            self.call_count += 1
            self.call_log.append(
                {
                    "node_id": getattr(request, "node_id", None),
                    "model": getattr(request, "model", None),
                }
            )
            user_msg = request.messages[-1]["content"]
            paragraphs: list[dict] = []
            # 精确匹配"包含 paragraphs 字段的 JSON 块":在 user_msg 中找
            # 形如 {"paragraphs": [...]} 的子串。task_prompt 模板里可能含其他 `{`,
            # 所以不能用最长贪婪;改为按 "paragraphs" 关键字定位。
            anchor = '"paragraphs"'
            anchor_pos = user_msg.find(anchor)
            if anchor_pos >= 0:
                # 从 anchor 向左找最近的 {
                start = user_msg.rfind("{", 0, anchor_pos)
                if start >= 0:
                    # 平衡括号扫描
                    depth = 0
                    end = -1
                    for i in range(start, len(user_msg)):
                        ch = user_msg[i]
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                end = i + 1
                                break
                    if end > start:
                        try:
                            data = json.loads(user_msg[start:end])
                            paragraphs = data.get("paragraphs", []) or []
                        except json.JSONDecodeError:
                            paragraphs = []
            classifications = [
                {
                    "paragraph_index": p.get("paragraph_index", i),
                    "paragraph_type": self._classify(p.get("text", ""), request, i),
                    "confidence": "high",
                }
                for i, p in enumerate(paragraphs)
            ]
            return _FakeLLMResponse(classifications)

        def _classify(self, text: str, request, idx: int) -> str:
            # rule="disagree_after_anchor":anchor 节点稳定;bulk 节点强制变型,
            # 用于校验 agreement < 0.85 时 fallback 路径。
            if self.rule == "disagree_after_anchor":
                node_id = getattr(request, "node_id", "") or ""
                if node_id.endswith("_bulk"):
                    return "transition"  # 与 anchor 大量不一致
            if any(q in text for q in ('"', "“", "”", "「", "」")):
                return "dialogue"
            if "记得" in text or "想起" in text:
                return "flashback"
            if "想着" in text or "心里" in text:
                return "psychology"
            if len(text) < 30:
                return "transition"
            return "narration"

    return FakeLLMClient


@pytest.fixture
def fake_paragraph_classifier():
    """见 build_fake_paragraph_classifier;fixture 形式供 segmentation / 路由测试注入。"""
    return build_fake_paragraph_classifier()
