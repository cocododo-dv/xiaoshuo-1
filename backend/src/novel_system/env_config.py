"""运行时设置里来自环境变量的那一半，与全系统共用的几个常量（叶子模块）。

只依赖标准库、``env_parsing`` 与 ``cache_registry``：Alembic 的引导（``load_database_runtime``）、底层记账、
``system_config``（``settings.get_settings`` 要读它的活动快照，它就不能反过来 import ``settings``）都能直接用，
不会闭环。以前为了绕开这一条依赖边拆成了五个小模块（core_runtime / database_runtime / llm_accounting_runtime /
runtime_defaults / accounting_contract），环境变量各解析一份（B09-05）；现在全在这里：

* ``load_env_settings()``：只看环境变量的 ``Settings``；``settings.get_settings()`` 在它上面叠库里的活动 api 快照。
* ``load_database_runtime()``：数据库地址与外键开关，Alembic 与会话工厂用（不碰其余变量）。
* ``env_admin_token()`` / ``env_config_secret()`` / ``reservation_recovery_ttl_seconds()``：各自一个变量
  （最后这个 ``load_env_settings`` 也读一遍：写错了启动就失败）。
* ``warn_retired_env_vars()``：已经没有效果、还设着的变量，每个进程警告一次。

布尔变量的宽松 / 严格两种读法按变量保持原样（见 ``env_parsing``）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from novel_system.cache_registry import register_cache_reset
from novel_system.env_parsing import (
    bool_env,
    list_env,
    non_negative_float_env,
    path_list_env,
    positive_int_env,
    quota_int_env,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = BACKEND_ROOT / "novel_system.db"
DEFAULT_VECTOR_STORE_DIR = BACKEND_ROOT / ".vector_store"

# Long-context local models can legitimately take several minutes, but an
# unlimited read blocks a synchronous worker forever when an upstream accepts
# the connection and never produces a response. Authors may still explicitly
# choose 0 to disable the ceiling for a known local runtime.
DEFAULT_LLM_TIMEOUT_SECONDS = 900.0
# Frozen application-level default shared by the accounting schema (server default of
# scene_run_states.provider_attempt_budget) and the runtime model config.
DEFAULT_PROVIDER_ATTEMPT_BUDGET = 32
PROVIDER_ATTEMPT_BUDGET_CONFIG_KEY = "retry_budget.provider_attempt_budget"

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------- 数据库（Alembic 引导也读这里）


@dataclass(frozen=True, slots=True)
class DatabaseRuntime:
    database_url: str
    sqlite_foreign_keys_enabled: bool


def load_database_runtime() -> DatabaseRuntime:
    return DatabaseRuntime(
        database_url=os.environ.get(
            "NOVEL_SYSTEM_DATABASE_URL",
            f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}",
        ),
        sqlite_foreign_keys_enabled=bool_env(
            "NOVEL_SYSTEM_SQLITE_FOREIGN_KEYS_ENABLED",
            True,
            strict=True,
        ),
    )


# ---------------------------------------------------------------- 单个变量


def env_admin_token() -> str | None:
    return os.environ.get("NOVEL_SYSTEM_ADMIN_TOKEN")


def env_config_secret() -> str | None:
    return os.environ.get("NOVEL_SYSTEM_CONFIG_SECRET")


def reservation_recovery_ttl_seconds() -> int:
    """启动对账只动这么久以前、没有主人的非场景预留；要远大于正常的供应商重试，免得把活着的调用当崩溃。"""
    return positive_int_env("NOVEL_SYSTEM_LLM_RESERVATION_RECOVERY_TTL_SECONDS", 3_600)


# ---------------------------------------------------------------- 退役变量


# 2026-09-30 重评 R3（批准#3a）：这些环境变量曾经打开六道全局额度闸、给金额闸提供单价。闸删了，设着也没有效果——
# 启动时（第一次读设置）记一条警告，不报错（以前只设金额上限不设单价会让后端起不来）。
RETIRED_ENV_VARS = (
    "NOVEL_SYSTEM_LLM_DAILY_TOKEN_LIMIT",
    "NOVEL_SYSTEM_LLM_MONTHLY_TOKEN_LIMIT",
    "NOVEL_SYSTEM_LLM_PROJECT_DAILY_TOKEN_LIMIT",
    "NOVEL_SYSTEM_LLM_DAILY_REQUEST_LIMIT",
    "NOVEL_SYSTEM_LLM_MAX_CONCURRENT_REQUESTS",
    "NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD",
    "NOVEL_SYSTEM_LLM_INPUT_COST_PER_MILLION_USD",
    "NOVEL_SYSTEM_LLM_OUTPUT_COST_PER_MILLION_USD",
)
_retired_env_warning: dict[str, bool] = {"logged": False}
register_cache_reset("env_config.retired_env_warning", lambda: _retired_env_warning.update(logged=False))


def warn_retired_env_vars() -> list[str]:
    """仍然设着的退役环境变量（按上表顺序）；每个进程第一次发现时记一条警告。"""
    in_use = [name for name in RETIRED_ENV_VARS if os.environ.get(name) is not None]
    if in_use and not _retired_env_warning["logged"]:
        _retired_env_warning["logged"] = True
        _LOGGER.warning(
            "environment variables %s no longer have any effect: the global LLM quota fences and the "
            "env cost prices were removed (usage readings stay on the cost dashboard); unset them",
            ", ".join(in_use),
        )
    return in_use


# ---------------------------------------------------------------- 设置


@dataclass(slots=True)
class Settings:
    database_url: str
    vector_backend: str
    vector_store_dir: Path
    sqlite_foreign_keys_enabled: bool = True
    idempotency_ttl_seconds: int = 90
    llm_provider: str = "openai_compatible"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str | None = None
    # Per-call LLM wall-clock ceiling. The 15-minute default allows slow local
    # generation while preventing a live-but-silent upstream from holding a
    # worker indefinitely. ``0`` remains an explicit operator opt-out.
    llm_timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS
    llm_enabled: bool = False
    # §8 opt-in: layer an independent LLM "editor" critic on top of the rule-based pass.
    llm_auto_critique_enabled: bool = False
    # §2 opt-in: extract narrative events from finished prose (not just the spec).
    llm_event_extraction_enabled: bool = False
    # 2026-09-14 opt-in: multi-candidate style drafts on standard / critical scenes
    # (criticality-driven N, blinded author terminal selection on critical scenes).
    scene_best_of_n_enabled: bool = False
    # Per-scene lifecycle budget multiplier: the scene end-to-end token ceiling is
    # ``N × single-shot baseline`` plus finite business/provider attempt caps. This
    # was the one hard fence that still shipped armed. A single-author desktop
    # install has no third party to fence off and a finite per-scene ceiling only
    # ever fires at the author mid-draft, so it now ships DISARMED like the rest of
    # the fence family: ``0`` = no scene ceiling (finite sentinel budgets, the CAS
    # gate is a no-op). Accounting is unchanged — the ledger and 成本看板 still
    # record every token. Set a positive value to re-arm ``N × baseline`` (and the
    # attempt caps fall back to their config/model defaults).
    scene_token_budget_multiplier: int = 0
    # Snowflake workspace prompt input budget override, in estimated tokens.
    # ``0`` = use each template's declared ``input_token_budget``. Set a positive
    # value to tighten it for a small-context local model (e.g. ollama), where the
    # per-template defaults would overflow the window. Over-budget payloads are
    # shed by relevance (never the step contract or the focused members) and the
    # shedding is reported, never silent.
    snowflake_input_token_budget: int = 0
    auto_create_tables: bool = False
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        # FE-ALIGN: React 前端 dev(5174) / preview(5175)
        "http://127.0.0.1:5174",
        "http://localhost:5174",
        "http://127.0.0.1:5175",
        "http://localhost:5175",
        "http://127.0.0.1:8081",
        "http://localhost:8081",
    )
    cors_allow_credentials: bool = True
    expose_error_detail: bool = False
    # The desktop service is local-only by default. Remote access is an explicit
    # deployment mode and must be protected by a shared access token.
    local_only: bool = True
    remote_access_token: str | None = None
    # The application reads request bodies in memory.  Keep one global ceiling
    # above the 10 MiB reference-book limit so JSON and multipart parsing can
    # never allocate an attacker-controlled, unbounded buffer.
    max_request_body_bytes: int = 16 * 1024 * 1024
    # Server-side path imports are disabled unless one or more roots are listed.
    # Browser uploads remain available and are the preferred import path.
    style_reference_import_roots: tuple[Path, ...] = ()
    # ``review`` prevents unattended archive for high-risk heuristic matches;
    # ``audit`` records the same findings without blocking publication.
    content_safety_mode: str = "review"
    # 2026-09-13 阶段 A：雪花 / 章节编排写下的场景结构（形态、坩埚、三拍、代价）作为
    # ``Scene Structure (Snowflake)`` 事实 section 进入起草 bundle、蓝图与近终稿快照。
    # 默认开；``NOVEL_SYSTEM_SCENE_STRUCTURE_BRIEF=false`` 只作回滚开关。
    scene_structure_brief_enabled: bool = True
    # 2026-09-13 阶段 F：已确认的雪花设计（一句话 / 五句脊柱 / 道德前提 / 章位置 / POV 角色摘要 /
    # 视角故事 / 相邻两场）作为可压缩的 ``Scene Design Context (Snowflake)`` 背景 section 进入
    # 起草 bundle 与蓝图快照。默认开；``NOVEL_SYSTEM_SCENE_DESIGN_CONTEXT=false`` 只作回滚开关。
    scene_design_context_enabled: bool = True


def _resolve_runtime_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BACKEND_ROOT / path


def load_env_settings() -> Settings:
    """只看环境变量的设置（不读库）；``settings.get_settings()`` 在它上面叠活动 api 快照与密钥。"""
    database_runtime = load_database_runtime()
    database_url = database_runtime.database_url
    sqlite_foreign_keys_enabled = database_runtime.sqlite_foreign_keys_enabled
    vector_backend = os.environ.get("NOVEL_SYSTEM_VECTOR_BACKEND", "memory")
    vector_store_dir = _resolve_runtime_path(
        os.environ.get("NOVEL_SYSTEM_CHROMA_DIR", DEFAULT_VECTOR_STORE_DIR)
    )
    llm_provider = os.environ.get("NOVEL_SYSTEM_LLM_PROVIDER", "openai_compatible")
    llm_base_url = os.environ.get("NOVEL_SYSTEM_LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key = os.environ.get("NOVEL_SYSTEM_LLM_API_KEY")
    llm_timeout_seconds = non_negative_float_env("NOVEL_SYSTEM_LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS)
    llm_enabled = bool_env("NOVEL_SYSTEM_LLM_ENABLED", False)
    llm_auto_critique_enabled = bool_env("NOVEL_SYSTEM_LLM_AUTO_CRITIQUE_ENABLED", False)
    llm_event_extraction_enabled = bool_env("NOVEL_SYSTEM_LLM_EVENT_EXTRACTION_ENABLED", False)
    scene_best_of_n_enabled = bool_env("NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED", False)
    scene_structure_brief_enabled = bool_env("NOVEL_SYSTEM_SCENE_STRUCTURE_BRIEF", True)
    scene_design_context_enabled = bool_env("NOVEL_SYSTEM_SCENE_DESIGN_CONTEXT", True)
    scene_token_budget_multiplier = quota_int_env(
        "NOVEL_SYSTEM_SCENE_TOKEN_BUDGET_MULTIPLIER", 0
    )
    snowflake_input_token_budget = quota_int_env(
        "NOVEL_SYSTEM_SNOWFLAKE_INPUT_TOKEN_BUDGET", 0
    )
    auto_create_tables = bool_env("NOVEL_SYSTEM_AUTO_CREATE_TABLES", False)
    cors_origins = list_env(
        "NOVEL_SYSTEM_CORS_ORIGINS",
        (
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            # FE-ALIGN: React 前端 dev(5174) / preview(5175)
            "http://127.0.0.1:5174",
            "http://localhost:5174",
            "http://127.0.0.1:5175",
            "http://localhost:5175",
            "http://127.0.0.1:8081",
            "http://localhost:8081",
        ),
    )
    cors_allow_credentials = bool_env("NOVEL_SYSTEM_CORS_ALLOW_CREDENTIALS", True)
    expose_error_detail = bool_env("NOVEL_SYSTEM_EXPOSE_ERROR_DETAIL", False)
    local_only = bool_env("NOVEL_SYSTEM_LOCAL_ONLY", True, strict=True)
    remote_access_token = os.environ.get("NOVEL_SYSTEM_REMOTE_ACCESS_TOKEN") or None
    max_request_body_bytes = positive_int_env(
        "NOVEL_SYSTEM_MAX_REQUEST_BODY_BYTES",
        16 * 1024 * 1024,
    )
    style_reference_import_roots = path_list_env(
        "NOVEL_SYSTEM_STYLE_REFERENCE_IMPORT_ROOTS",
        _resolve_runtime_path,
    )
    content_safety_mode = os.environ.get("NOVEL_SYSTEM_CONTENT_SAFETY_MODE", "review").strip().lower()
    if content_safety_mode not in {"review", "audit"}:
        raise ValueError("NOVEL_SYSTEM_CONTENT_SAFETY_MODE must be review or audit")
    # 启动对账才用这个值，但写错了要在这里就起不来：等到对账时才读，它只记一条 scan_failed，后端照常跑，
    # 没有主人的非场景预留就永远回收不了（重评 R3 退役的是另外八个变量，它留着）。
    reservation_recovery_ttl_seconds()
    return Settings(
        database_url=database_url,
        vector_backend=vector_backend,
        vector_store_dir=vector_store_dir,
        sqlite_foreign_keys_enabled=sqlite_foreign_keys_enabled,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_enabled=llm_enabled,
        llm_auto_critique_enabled=llm_auto_critique_enabled,
        llm_event_extraction_enabled=llm_event_extraction_enabled,
        scene_best_of_n_enabled=scene_best_of_n_enabled,
        scene_structure_brief_enabled=scene_structure_brief_enabled,
        scene_design_context_enabled=scene_design_context_enabled,
        scene_token_budget_multiplier=scene_token_budget_multiplier,
        snowflake_input_token_budget=snowflake_input_token_budget,
        auto_create_tables=auto_create_tables,
        cors_origins=cors_origins,
        cors_allow_credentials=cors_allow_credentials,
        expose_error_detail=expose_error_detail,
        local_only=local_only,
        remote_access_token=remote_access_token,
        max_request_body_bytes=max_request_body_bytes,
        style_reference_import_roots=style_reference_import_roots,
        content_safety_mode=content_safety_mode,
    )
