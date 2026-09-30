import { LIB_CATS } from "./labels/library.js";
import { LIB_relLabel, LIB_relType } from "./ws-library-derive.js";

/* ==========================================================
   资料 · 关系图谱的布局（2026-09-30 从 ws-library-graph.jsx 拆出，审计 F05-17）
   纯函数：档案 + 关系 → 无向去重的边、确定性的力导向布局。布局算在抽象坐标里（GW × GH），拖拽记忆
   （ws-lib-graph-pos-v1）也存在这个坐标里，画的时候由视图按舞台的实际像素铺开。可单测，不碰 DOM。
   ========================================================== */

export const GW = 1000, GH = 660;

/* 无向、去重的边 */
export function buildEdges(entries, byId) {
  const seen = new Set();
  const edges = [];
  entries.forEach(e => {
    (e.links || []).forEach(l => {
      if (!byId[l.id]) return;
      const key = [e.id, l.id].sort().join("|");
      if (seen.has(key)) return;
      seen.add(key);
      edges.push({ a: e.id, b: l.id, rel: LIB_relLabel(l), typeId: LIB_relType(l).id });
    });
  });
  return edges;
}

/* 简单的力模拟 → { id: { x, y } }，铺满 [0, GW] × [0, GH]（边距在画的时候按像素加） */
export function computeLayout(edges, entries) {
  const nodes = entries.map(e => ({ id: e.id, cat: e.cat }));
  const idx = {};
  nodes.forEach((n, i) => { idx[n.id] = i; });

  // 确定性的初始位置：同类沿一圈聚在一起
  const catAngle = {};
  LIB_CATS.forEach((c, i) => { catAngle[c.id] = (i / LIB_CATS.length) * Math.PI * 2; });
  const catCount = {};
  const pos = nodes.map(n => {
    const k = (catCount[n.cat] = (catCount[n.cat] || 0) + 1);
    const a = (catAngle[n.cat] || 0) + (k * 0.7);
    const r = 150 + (k % 4) * 46;
    return { x: GW / 2 + Math.cos(a) * r, y: GH / 2 + Math.sin(a) * r };
  });

  const REP = 1650, SPRING = 0.045, L = 120, CENTER = 0.012, STEP = 0.9, MAXMOVE = 26;
  const CAT_COHESION = 0.021;
  /* 条目很多时少迭代几轮：O(n²) 的斥力是这里唯一的大头 */
  const ITERS = nodes.length > 160 ? 260 : 520;
  for (let it = 0; it < ITERS; it++) {
    const fx = new Array(nodes.length).fill(0);
    const fy = new Array(nodes.length).fill(0);
    // 各类的重心（松散聚类，读起来成片）
    const cc = {}, cn = {};
    for (let i = 0; i < nodes.length; i++) {
      const c = nodes[i].cat;
      if (!cc[c]) { cc[c] = { x: 0, y: 0 }; cn[c] = 0; }
      cc[c].x += pos[i].x; cc[c].y += pos[i].y; cn[c]++;
    }
    Object.keys(cc).forEach(c => { cc[c].x /= cn[c]; cc[c].y /= cn[c]; });
    // 斥力（两两）
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = pos[i].x - pos[j].x, dy = pos[i].y - pos[j].y;
        const d2 = dx * dx + dy * dy || 0.01;
        const d = Math.sqrt(d2);
        const f = REP / d2;
        const ux = dx / d, uy = dy / d;
        fx[i] += ux * f; fy[i] += uy * f;
        fx[j] -= ux * f; fy[j] -= uy * f;
      }
      // 向心 + 同类内聚
      fx[i] += (GW / 2 - pos[i].x) * CENTER;
      fy[i] += (GH / 2 - pos[i].y) * CENTER;
      const ctr = cc[nodes[i].cat];
      fx[i] += (ctr.x - pos[i].x) * CAT_COHESION;
      fy[i] += (ctr.y - pos[i].y) * CAT_COHESION;
    }
    // 弹簧
    edges.forEach(e => {
      const a = idx[e.a], b = idx[e.b];
      if (a == null || b == null) return;
      const dx = pos[b].x - pos[a].x, dy = pos[b].y - pos[a].y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const diff = (d - L) * SPRING;
      const ux = dx / d, uy = dy / d;
      fx[a] += ux * diff; fy[a] += uy * diff;
      fx[b] -= ux * diff; fy[b] -= uy * diff;
    });
    // 积分
    for (let i = 0; i < nodes.length; i++) {
      let mx = fx[i] * STEP, my = fy[i] * STEP;
      const m = Math.sqrt(mx * mx + my * my);
      if (m > MAXMOVE) { mx = mx / m * MAXMOVE; my = my / m * MAXMOVE; }
      pos[i].x += mx; pos[i].y += my;
    }
  }

  // 等比缩放铺进 GW × GH，居中
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  pos.forEach(p => { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); });
  const s = Math.min(GW / (maxX - minX || 1), GH / (maxY - minY || 1));
  const offX = (GW - (maxX - minX) * s) / 2;
  const offY = (GH - (maxY - minY) * s) / 2;
  const out = {};
  nodes.forEach((n, i) => {
    out[n.id] = { x: offX + (pos[i].x - minX) * s, y: offY + (pos[i].y - minY) * s };
  });
  return out;
}
