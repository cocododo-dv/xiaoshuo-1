"""风格参考 v3（M1）：每个入口模块都能在一个新解释器里第一个被导入。

``style_policy`` → ``style_reference.binding_config`` 会先跑包的 ``__init__``；包的 ``__init__`` 曾经重导出
``binding_apply``，而 ``binding_apply`` 回头导入 ``style_policy.style_policy_live``——此时 ``style_policy`` 还没初始化
完，新解释器里 ``import novel_system.services.style_policy`` 直接 ImportError。测试套件里别的模块先把包导好了，这种
环在套件里看不见（``test_service_architecture`` 的静态图也不把「包的 ``__init__``」当成依赖），只有进程第一次导入
它的时候才炸——所以这里每个模块各起一个解释器。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"

FIRST_IMPORTS = (
    "novel_system.services.style_policy",
    "novel_system.services.style_prompt_injection",
    "novel_system.services.qc_engine",
    "novel_system.services.scene_blueprint",
    "novel_system.services.scene_diagnosis",
    "novel_system.services.style_reference.inject.render",
    "novel_system.services.style_reference.inject.preview",
    "novel_system.services.style_reference.inject.selection",
    "novel_system.services.style_reference.inject.routing",
    "novel_system.services.style_reference.binding_apply",
    "novel_system.services.style_reference.binding_config",
    "novel_system.services.style_reference.runtime_contract",
    "novel_system.services.style_reference.planning_context",
    "novel_system.services.style_reference.check_job",
    "novel_system.services.style_reference.card",
    "novel_system.services.style_reference.policy",
    "novel_system.services.style_reference",
    "novel_system.services.style_reference.inject",
)
# 包的 ``__init__`` 只留说明（2026-09-24 清理 S4）：导入包本身不能拉起任何子模块，也没有环
BARE_PACKAGES = (
    "novel_system.services.style_reference",
    "novel_system.services.style_reference.inject",
)


@pytest.mark.parametrize("module", FIRST_IMPORTS)
def test_module_imports_first_in_a_fresh_interpreter(module: str, tmp_path: Path) -> None:
    env = dict(os.environ)
    # 这个 worktree 的源码排在最前（venv 里的可编辑安装可能指向别的检出）
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    code = (
        "import importlib, pathlib, sys\n"
        f"importlib.import_module({module!r})\n"
        "import novel_system\n"
        f"assert pathlib.Path(novel_system.__file__).resolve().is_relative_to(pathlib.Path({str(SRC_ROOT)!r}).resolve())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"import {module} failed in a fresh interpreter:\n{result.stderr[-2000:]}"


@pytest.mark.parametrize("package", BARE_PACKAGES)
def test_package_import_pulls_no_submodule(package: str, tmp_path: Path) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(SRC_ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    code = (
        "import importlib, sys\n"
        f"importlib.import_module({package!r})\n"
        f"loaded = sorted(name for name in sys.modules if name.startswith({package + '.'!r}))\n"
        "print('\\n'.join(loaded))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = [line for line in result.stdout.splitlines() if line.strip()]
    assert loaded == [], f"importing {package} pulled submodules: {loaded}"
