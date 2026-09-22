import { LIB_CATS } from "./ws-library-data.jsx";

/* ==========================================================
   Library — 派生层 (selectors)
   单一数据源 + 纯函数。所有视图共享这里推导出的结构：
   · 双向关系（forward links ∪ backlinks）
   · 总览用到的事实：各类数量、还没写简述的、没有任何关联的、最近改过的、置顶的
   · 排序比较器、列表副标题、章节显示名
   不持有状态，输入 entries 数组 → 输出派生结构。
   2026-09-21：原型的状态机（待审核 / 学习中 / 发布…、就绪度、待处理队列）删掉了——
   后端条目根本没有这些状态，它只会画出「100% 就绪 · 已就绪 0」这种假数。
   ========================================================== */

/* ==========================================================
   关系类型 — 把自由文本的 rel 归入 6 个语义类别。
   每个 link 可显式声明 type（编辑时写入）；否则按关键词推断。
   6 类对齐全局 6 个强调色，图谱/详情/编辑共用同一套语言。
   ========================================================== */
const LIB_REL_TYPES = [
  { id: "kin",      label: "亲缘", icon: "Users",     accent: "rose",
    hint: "血亲、师承、情感羁绊",
    kw: ["父", "母", "女儿", "儿", "子", "师", "徒", "前任", "夫", "妻", "兄", "弟", "姐", "妹", "亲"] },
  { id: "conflict", label: "对立", icon: "Zap",       accent: "crimson",
    hint: "敌对、决裂、张力",
    kw: ["对立", "宿敌", "敌", "决裂", "触犯", "威胁", "张力", "推翻", "反目", "仇", "背叛"] },
  { id: "ally",     label: "同盟", icon: "Link",      accent: "sage",
    hint: "搭档、援手、协作",
    kw: ["搭档", "外援", "协助", "结盟", "同伴", "合作", "协作", "盟", "援"] },
  { id: "place",    label: "处所", icon: "MapPin",    accent: "gold",
    hint: "所在、发生地、比邻",
    kw: ["工作", "出入", "比邻", "事发", "发生", "行踪", "场景", "来自", "主事", "居", "所在", "地点", "罹难"] },
  { id: "belong",   label: "归属", icon: "Layers",    accent: "slate",
    hint: "执掌、卷入、隶属",
    kw: ["执掌", "主任", "主体", "经历", "受益", "卷入", "制定", "视角", "服务", "隶属", "成员", "牵连", "记录", "掌"] },
  { id: "source",   label: "源流", icon: "GitBranch", accent: "ink",
    hint: "来源、派生、佐证（默认）",
    kw: [] },  /* 默认兜底类别 */
];
const LIB_REL_TYPE_BY_ID = LIB_REL_TYPES.reduce((m, t) => { m[t.id] = t; return m; }, {});
const LIB_REL_DEFAULT = LIB_REL_TYPES[LIB_REL_TYPES.length - 1];

/* 解析关系类型：传入 link 对象（优先用显式 type）或裸 rel 字符串 */
function LIB_relType(x) {
  if (x && typeof x === "object") {
    if (x.type && LIB_REL_TYPE_BY_ID[x.type]) return LIB_REL_TYPE_BY_ID[x.type];
    x = x.rel;
  }
  const s = String(x || "");
  for (const t of LIB_REL_TYPES) {
    if (t.kw.some(k => s.includes(k))) return t;
  }
  return LIB_REL_DEFAULT;
}

/* 关系的显示文字：作者写的标签优先，没写就用关系类型的中文名 */
function LIB_relLabel(link) {
  const rel = link && String(link.rel || "").trim();
  return rel || LIB_relType(link).label;
}

/* ---- backlinks：谁引用了我 ---- */
/* → { [id]: [{ id, rel, type }] }  (reverse edges, 不含自身已声明的 forward) */
function LIB_buildBacklinks(entries) {
  const back = {};
  entries.forEach(e => {
    (e.links || []).forEach(l => {
      (back[l.id] = back[l.id] || []).push({ id: e.id, rel: l.rel, type: l.type });
    });
  });
  return back;
}

/* 统一关系视图：forward 优先，补上未被镜像的 backlinks。
   返回 [{ id, rel, type, typeId, dir: 'out' | 'in' }]，按目标去重。 */
function LIB_connections(entry, byId, backlinks) {
  if (!entry) return [];
  const seen = new Set();
  const out = [];
  const push = (id, rel, type, dir) => {
    if (!byId[id] || seen.has(id)) return;
    seen.add(id);
    out.push({ id, rel, type, typeId: LIB_relType({ rel, type }).id, dir });
  };
  (entry.links || []).forEach(l => push(l.id, l.rel, l.type, "out"));
  (backlinks[entry.id] || []).forEach(b => push(b.id, b.rel, b.type, "in"));
  return out;
}

/* 把一组 connections 按关系类型分组，保持 LIB_REL_TYPES 的顺序 */
function LIB_groupConnections(conns) {
  const buckets = {};
  (conns || []).forEach(c => { (buckets[c.typeId] = buckets[c.typeId] || []).push(c); });
  return LIB_REL_TYPES
    .map(t => ({ type: t, items: buckets[t.id] || [] }))
    .filter(g => g.items.length);
}

/* 关联总数（含反向，去重） */
function LIB_degree(entry, byId, backlinks) {
  return LIB_connections(entry, byId, backlinks).length;
}

/* 不重复的关联条数：关系在两端各挂一次，按端点对去重 */
function LIB_linkCount(entries, byId) {
  const seen = new Set();
  entries.forEach(e => (e.links || []).forEach(l => {
    if (!byId[l.id]) return;
    seen.add([e.id, l.id].sort().join("|"));
  }));
  return seen.size;
}

/* ---- 列表副标题 / 档案头的说明行 ----
   不再用类别名兜底（「人物」分组下每一行都写着「人物」）：
   人物 → 一句话摘要或角色定位；世界 → 摘要或类型；大事记 → 时间。 */
function LIB_entrySub(e) {
  if (!e) return "";
  if (e.cat === "events") return e.timeLabel || "未定时间";
  if (e.summary) return e.summary;
  if (e.cat === "people") return e.role || "";
  return e.kind || "";
}

/* ---- 章节显示名：chapter_ref 可能是章 id（目录里的 backendId）或 slug，也可能是作者手写的文字。
   目录里找得到 → 「第 N 章 · 标题」；形如内部 id 却找不到 → 说它不在目录里；其余原样显示。 */
function LIB_chapterLabel(ref, chapters) {
  const value = String(ref || "").trim();
  if (!value) return "";
  const list = Array.isArray(chapters) ? chapters : [];
  const hit = list.find(c => c && (c.backendId === value || c.id === value));
  if (hit) {
    const no = hit.n != null ? `第 ${hit.n} 章` : "";
    const title = String(hit.title || "").trim();
    if (no && title && title !== no) return `${no} · ${title}`;
    return title || no || "未命名章节";
  }
  if (/^[A-Za-z0-9]+_[A-Za-z0-9_]+$/.test(value) || /^ch\d+$/i.test(value)) return "章节已不在目录里";
  return value;
}

/* ---- 总览：只说数据里真有的事 ---- */
const LIB_RECENT_N = 6;
function LIB_overviewFacts(entries) {
  const byId = entries.reduce((m, e) => { m[e.id] = e; return m; }, {});
  const backlinks = LIB_buildBacklinks(entries);
  const total = entries.length;
  const linksN = LIB_linkCount(entries, byId);
  const byCat = LIB_CATS.map(c => ({ cat: c, n: entries.filter(e => e.cat === c.id).length }));
  const missingBlurb = entries.filter(e => !String(e.blurb || "").trim());
  const isolated = entries.filter(e => LIB_degree(e, byId, backlinks) === 0);
  const recent = entries
    .filter(e => e.updatedAt)
    .slice()
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, LIB_RECENT_N);
  const pinned = entries.filter(e => e.pinned);
  return { total, linksN, byCat, missingBlurb, isolated, recent, pinned };
}

/* ---- 排序比较器 ---- */
const LIB_SORTS = {
  recent: { label: "最近改动", cmp: (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0) },
  name:   { label: "名称",     cmp: (a, b) => (a.name || "").localeCompare(b.name || "", "zh") },
};
/* 在比较器之上，置顶永远优先 */
function LIB_sortWithPin(items, sortKey) {
  const cmp = (LIB_SORTS[sortKey] || LIB_SORTS.recent).cmp;
  return [...items].sort((a, b) => {
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
    return cmp(a, b);
  });
}

export {
  LIB_buildBacklinks, LIB_connections, LIB_degree, LIB_linkCount, LIB_overviewFacts,
  LIB_SORTS, LIB_sortWithPin, LIB_entrySub, LIB_chapterLabel,
  LIB_REL_TYPES, LIB_REL_TYPE_BY_ID, LIB_relType, LIB_relLabel, LIB_groupConnections,
};
