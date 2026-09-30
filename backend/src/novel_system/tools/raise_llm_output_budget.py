"""把库内活动 models 快照里某些节点的输出预算抬到下限（写成快照里的显式覆盖）。

2026-09-30 起（批准#5a / 重评 R4）models 快照只存作者为每个节点选的服务与模型；输出预算、温度等参数
解析时取节点注册表（llm_node_registry）的默认值——代码里修好的默认值随发布直接到达实例，迁移
20260929_0096 把已有快照里抄进去的参数都去掉了。所以一般不再需要这个工具，只剩两种用处：
- 想让某个节点的输出预算高于默认值（例如某个中转的思考 token 特别多）：写成显式覆盖；
- 迁移之前另存、后来又被重新激活的老快照，路由里仍带着当年抄进去的完整参数。

不要为此整份重新导入 models 配置：那会把界面上配好的 provider/model 路由一起冲掉。这里只改
max_output_tokens，其余字段原样保留。两张表都看：运行时 `resolve_node_route` 先查 `node_routing`，
再退回老快照的 `task_routing`。路由没写输出预算时按节点默认值算「当前值」。

    python -m novel_system.tools.raise_llm_output_budget            # 干跑，只看会改什么
    python -m novel_system.tools.raise_llm_output_budget --execute  # 落库并激活新快照
"""

from __future__ import annotations

import argparse
from typing import Any

from novel_system.services.llm_node_registry import get_llm_node_spec
from novel_system.services.system_config import SystemConfigService
from novel_system.tools._checkout_guard import refuse_foreign_checkout
from novel_system.tools._cli import activate_snapshot_payload, active_snapshot_payload, open_checked_session

# 客户端降级阶梯的上限（MAX_OUTPUT_TOKENS_CEILING），配置值与之对齐才有意义。
DEFAULT_FLOOR = 8192
# 默认只管整步生成——它是唯一"一次调用要输出整张表"的节点。
DEFAULT_NODES = ("snowflake_step_generate",)
ACTOR = "raise_llm_output_budget"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--floor", type=int, default=DEFAULT_FLOOR, help=f"输出预算下限（默认 {DEFAULT_FLOOR}）")
    parser.add_argument(
        "--node",
        action="append",
        dest="nodes",
        help=f"要抬高的节点，可重复；默认 {', '.join(DEFAULT_NODES)}。传 --node all 表示全部节点",
    )
    parser.add_argument("--execute", action="store_true", help="真正写入并激活新快照（默认只干跑）")
    return parser.parse_args(argv)


# 运行时优先级顺序（resolve_node_route）：node_routing 赢，task_routing 兜底。
ROUTING_TABLES = ("node_routing", "task_routing")


def _current_budget(node_id: str, config: dict[str, Any]) -> int | None:
    """这条路由实际生效的输出预算：写了就是它，没写取节点默认值（退役节点没有默认值 → None）。"""
    current = config.get("max_output_tokens")
    if isinstance(current, int):
        return current
    if current is None:
        spec = get_llm_node_spec(node_id)
        return spec.max_output_tokens if spec is not None else None
    return None


def _targets(routing: dict[str, Any], nodes: list[str] | None, floor: int) -> dict[str, int]:
    selected = nodes or list(DEFAULT_NODES)
    keys = routing.keys() if selected == ["all"] else selected
    hits: dict[str, int] = {}
    for key in keys:
        config = routing.get(key)
        if not isinstance(config, dict):
            continue
        current = _current_budget(key, config)
        if current is not None and current < floor:
            hits[key] = current
    return hits


def _targets_by_table(payload: dict[str, Any], nodes: list[str] | None, floor: int) -> dict[str, dict[str, int]]:
    """每张路由表里要抬的节点：{table: {node_id: current}}；没有该表或表里没命中就不出现。"""
    hits: dict[str, dict[str, int]] = {}
    for table in ROUTING_TABLES:
        routing = payload.get(table)
        if not isinstance(routing, dict):
            continue
        table_hits = _targets(routing, nodes, floor)
        if table_hits:
            hits[table] = table_hits
    return hits


def main(argv: list[str] | None = None) -> int:
    refuse_foreign_checkout("raise_llm_output_budget")
    args = _parse_args(argv)
    with open_checked_session(ACTOR, writes=args.execute) as session:
        service = SystemConfigService(session)
        snapshot, payload = active_snapshot_payload(service, "models")
        if not snapshot:
            print("库内没有活动的 models 配置快照——节点路由取节点注册表（llm_node_registry）的默认值，改默认值即可生效，无需处理。")
            return 0

        if not any(isinstance(payload.get(table), dict) for table in ROUTING_TABLES):
            print("活动快照里既没有 node_routing 也没有 task_routing，无需处理。")
            return 0

        hits = _targets_by_table(payload, args.nodes, args.floor)
        if not hits:
            print(f"活动快照 v{snapshot['version']}：目标节点的输出预算都已达到 {args.floor}，无需改动。")
            return 0

        print(f"活动快照 v{snapshot['version']}（{snapshot['snapshot_id']}）将被抬高的节点：")
        for table, table_hits in hits.items():
            for node_id, current in sorted(table_hits.items()):
                print(f"  {table}.{node_id}: {current} → {args.floor}")

        if not args.execute:
            print("\n干跑结束——加 --execute 才会写入并激活新快照。")
            return 0

        for table, table_hits in hits.items():
            for node_id in table_hits:
                payload[table][node_id]["max_output_tokens"] = args.floor
        activated = activate_snapshot_payload(service, "models", payload, actor=ACTOR)
        print(f"\n已激活新快照 v{activated['version']}（{activated['snapshot_id']}）。")
        print("重启后端后生效。")
        return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
