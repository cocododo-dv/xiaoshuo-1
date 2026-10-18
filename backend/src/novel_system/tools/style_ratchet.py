"""风格棘轮：改风格配置之前，用参考书里留出的原文比两套配置谁写得更像（``services/style_reference/ratchet.py``）。

两套配置 A / B 各按同一组「情节要点」（从留出原文提炼、不带原措辞）用真实的风格先行首稿模板写一遍，量谁离原文
更近；另出一张盲读页（原文 / A / B 打乱）。配置的写法是逗号分隔的几项，不写的项 = 现在的配置：

    model=<模型名>          换「风格稿」路由的模型（同一个服务）
    windows=<0–16>          每次送几窗样例（现在绑定的是多少就默认多少）
    placement=user_tail|system   样例放 user 末尾（现在的做法）还是 system 前缀（吃提示缓存的做法）
    prompts=<路径>          用另一份 prompts.yaml 里的 style_first_draft（改提示词之前先比）
    rep=<n>                 同一配置的第几份（A/A：同一配置写两遍，量噪声）

不给 --b 就是 A/A（B = A 再写一遍）：量同一配置两次之间差多少（噪声，记进 noise.json）。之后的比较 ε 取给定值与
实测噪声中较大的那个——先跑一次 A/A 再比别的。

用法（backend 目录下；在工作树里用 ``PYTHONPATH=<worktree>/backend/src``）：
    python -m novel_system.tools.style_ratchet                                  # 干跑：留出段、配置、要调几次模型
    python -m novel_system.tools.style_ratchet --b "windows=8" --execute         # 现在的配置 对 只送 8 窗
    python -m novel_system.tools.style_ratchet --a "model=X" --b "model=Y" --passages 4 --execute

结果在 ``.codex-run/style-ratchet/<套件>/runs/<这一次>/``：report.md（数字与结论）、reading.md（盲读页，先读它）、
key.json（盲读答案）、result.json。同一套件里写过的要点与稿子会复用，换对手时只补写新配置的稿子。
模型调用照常记账；没有设 NOVEL_SYSTEM_CONFIG_SECRET 时读启动脚本生成的 ``.codex-run/config.secret``（解开界面里存的密钥）。
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

from novel_system.services.errors import DomainError
from novel_system.services.style_reference import ratchet as R
from novel_system.tools._checkout_guard import REFUSED_EXIT_CODE, refuse_foreign_checkout
from novel_system.tools._cli import open_checked_session

TOOL = "style_ratchet"
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUT_DIR = REPO_ROOT / ".codex-run" / "style-ratchet"
SECRET_FILE = REPO_ROOT / ".codex-run" / "config.secret"
FAILED_EXIT_CODE = 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="要比的画像（缺省：唯一一份活动绑定的画像）")
    parser.add_argument("--a", default="", help="配置 A（缺省：现在的配置）")
    parser.add_argument("--b", default=None, help="配置 B（缺省：A 再写一遍，即 A/A 量噪声）")
    parser.add_argument("--passages", type=int, default=R.DEFAULT_PASSAGES, help=f"留出段数（缺省 {R.DEFAULT_PASSAGES}，2–8；段越多越不容易被运气骗）")
    parser.add_argument("--seed", default="ratchet", help="挑留出段的种子（换一组留出段就换种子）")
    parser.add_argument(
        "--epsilon", type=float, default=R.DEFAULT_EPSILON, help=f"平均要近多少才算赢（缺省 {R.DEFAULT_EPSILON}；跑过 A/A 后至少取实测噪声）"
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="套件目录的上级（缺省 .codex-run/style-ratchet）")
    parser.add_argument("--execute", action="store_true", help="真正调用模型（默认只干跑）")
    return parser.parse_args(argv)


def _specs(args: argparse.Namespace) -> tuple[R.ArmSpec, R.ArmSpec]:
    arm_a = R.parse_arm("A", args.a)
    if args.b is None:
        arm_b = dataclasses.replace(arm_a, label="B", rep=arm_a.rep + 1)
    else:
        arm_b = R.parse_arm("B", args.b)
    return arm_a, arm_b


def _print_plan(plan: R.Plan) -> None:
    subject = plan.subject
    print(f"画像 {subject.profile.profile_id}（{'活动绑定的配置' if subject.binding_id else '缺省配置'}） · 书 {subject.book.book_id}")
    print(f"套件 {plan.suite_id} → {plan.suite_dir}")
    print("留出段：")
    for passage in plan.passages:
        cached = "要点已有" if passage.window_no in plan.plots else "要提炼要点"
        print(f"  第 {passage.window_no} 窗 · 第 {passage.chapter_no} 章 · {passage.position} · {passage.chars} 字 · {cached}")
    for arm in plan.arms:
        needed = len(plan.drafts_needed(arm))
        print(f"配置 {arm.label}：{arm.describe()}（签名 {arm.key}）")
        print(f"  风格参考约 {plan.style_chars.get(arm.key, 0):,} 字 / 次；要写 {needed} 段（缓存里已有 {len(plan.passages) - needed} 段）")
    calls = len(plan.plots_needed) + sum(len(plan.drafts_needed(arm)) for arm in plan.arms)
    print(f"这一次约要调用模型 {calls} 次（要点不带原文措辞 / 没写出正文时各会多一次）。")


def _load_secret() -> None:
    if os.environ.get("NOVEL_SYSTEM_CONFIG_SECRET") or not SECRET_FILE.is_file():
        return
    os.environ["NOVEL_SYSTEM_CONFIG_SECRET"] = SECRET_FILE.read_text(encoding="utf-8").strip()
    print(f"（读了 {SECRET_FILE} 来解开界面里存的密钥）")


def main(argv: list[str] | None = None) -> int:
    refuse_foreign_checkout(TOOL)
    args = _parse_args(argv)
    try:
        specs = _specs(args)
    except R.RatchetError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return REFUSED_EXIT_CODE
    if args.execute:
        _load_secret()
    with open_checked_session(TOOL, writes=args.execute) as session:
        try:
            plan = R.plan_run(
                session,
                profile_id=args.profile,
                specs=specs,
                passages=args.passages,
                seed=args.seed,
                out_dir=Path(args.out_dir),
            )
        except (R.RatchetError, DomainError) as exc:
            session.rollback()
            print(f"{TOOL}: {exc}", file=sys.stderr)
            return REFUSED_EXIT_CODE
        _print_plan(plan)
        if not args.execute:
            session.rollback()
            print("干跑：没有调用模型、没有写文件。加 --execute 真正比较。")
            return 0
        from novel_system.services.llm_service_base import runtime_llm_client_and_enabled

        try:
            llm_client, enabled = runtime_llm_client_and_enabled()
        except DomainError as exc:
            print(f"{TOOL}: {exc}", file=sys.stderr)
            return REFUSED_EXIT_CODE
        if not enabled or llm_client is None:
            print(f"{TOOL}: 模型没有启用（系统配置里没有可用的模型接入）。", file=sys.stderr)
            return REFUSED_EXIT_CODE
        try:
            result = R.execute_run(
                session, plan, llm_client=llm_client, epsilon=args.epsilon, progress=lambda line: print(line, flush=True)
            )
        except (R.RatchetError, DomainError) as exc:
            session.rollback()
            print(f"{TOOL}: {exc}", file=sys.stderr)
            refused = isinstance(exc, R.RatchetError) and exc.refused
            return REFUSED_EXIT_CODE if refused else FAILED_EXIT_CODE
        print(f"结论：{result.headline()}")
        usage = result.usage
        print(f"新调用 {usage['calls']} 次：输入 {usage['prompt_tokens']:,} / 输出 {usage['completion_tokens']:,} token")
        print(f"报告 {result.run_dir / 'report.md'}")
        print(f"盲读 {result.run_dir / 'reading.md'}（先读它，答案在 key.json）")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
