// 资料 · 关系图谱的布局（纯函数）：边无向去重、布局确定（同样的档案与关系每次同一张图）、铺满抽象画布。
import { describe, expect, it } from "vitest";
import { GH, GW, buildEdges, computeLayout } from "./ws-library-graph-layout.js";

const entry = (id, cat, links = []) => ({ id, cat, name: id, links });

function sample() {
  const entries = [
    entry("lin", "people", [{ id: "zhou", type: "conflict", rel: "宿敌" }, { id: "arch", type: "place", rel: "" }]),
    entry("zhou", "people", [{ id: "lin", type: "conflict", rel: "宿敌" }]),
    entry("arch", "world", [{ id: "lin", type: "place", rel: "" }]),
    entry("tide", "events", [{ id: "lin", rel: "相关", viaEvent: true }, { id: "gone" }]),
  ];
  const byId = Object.fromEntries(entries.map((e) => [e.id, e]));
  return { entries, byId };
}

describe("关系图谱的布局", () => {
  it("边无向、按端点对去重；指向已不存在的档案的边不画；关系名与类型跟着关系类型的规则走", () => {
    const { entries, byId } = sample();
    const edges = buildEdges(entries, byId);
    expect(edges.map((e) => [e.a, e.b].sort().join("|")).sort()).toEqual(["arch|lin", "lin|tide", "lin|zhou"]);
    const conflict = edges.find((e) => [e.a, e.b].includes("zhou"));
    expect(conflict).toMatchObject({ rel: "宿敌", typeId: "conflict" });
    // 没写标签的关系用类型的中文名
    const place = edges.find((e) => [e.a, e.b].includes("arch"));
    expect(place.rel).toBe("处所");
  });

  it("同样的输入每次得到同一张图（确定性），每个节点都落在 GW × GH 里", () => {
    const { entries, byId } = sample();
    const edges = buildEdges(entries, byId);
    const a = computeLayout(edges, entries);
    const b = computeLayout(buildEdges(entries, byId), entries.map((e) => ({ ...e })));
    expect(b).toEqual(a);
    expect(Object.keys(a).sort()).toEqual(["arch", "lin", "tide", "zhou"]);
    for (const p of Object.values(a)) {
      expect(p.x).toBeGreaterThanOrEqual(-1e-6);
      expect(p.x).toBeLessThanOrEqual(GW + 1e-6);
      expect(p.y).toBeGreaterThanOrEqual(-1e-6);
      expect(p.y).toBeLessThanOrEqual(GH + 1e-6);
    }
  });

  it("连着的两个档案比没连着的离得近", () => {
    const entries = [
      entry("a", "people", [{ id: "b", type: "ally" }]),
      entry("b", "people", [{ id: "a", type: "ally" }]),
      entry("c", "world"),
      entry("d", "world"),
    ];
    const byId = Object.fromEntries(entries.map((e) => [e.id, e]));
    const layout = computeLayout(buildEdges(entries, byId), entries);
    const dist = (x, y) => Math.hypot(layout[x].x - layout[y].x, layout[x].y - layout[y].y);
    expect(dist("a", "b")).toBeLessThan(dist("a", "c"));
    expect(dist("a", "b")).toBeLessThan(dist("b", "d"));
  });

  it("没有档案给空布局", () => {
    expect(computeLayout([], [])).toEqual({});
    expect(buildEdges([], {})).toEqual([]);
  });
});
