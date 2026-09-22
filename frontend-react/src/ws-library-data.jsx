import { apiGet } from "./lib/client.js";
import { createSubscribers } from "./lib/store-utils.js";
import { WsWorks } from "./ws-works.jsx";

/* ==========================================================
   Library data — 档案库（后端 /library 聚合的适配层）
   三类档案：人物（StoryCharacter）、世界（LibraryEntity）、大事记（TimelineEvent），
   彼此由关系（LibraryRelation）与大事记的 entity_refs 连起来。
   条目形状：{ id, cat, name, code, kind, role?, entityKind?, accent, glyph, tags,
              summary, blurb, facts[], links[], pinned, updatedAt,
              timeLabel?, chapterRef?, ref, details }
   details 是服务端原样的扩展字段组：编辑时在它上面合并，未知键（旧数据）不会被冲掉。
   ========================================================== */

/* 2026-09-21：原型里还有「参考 / 风格 / 知识」三类，适配层从来不产出它们（风格参考有自己的页面，
   知识簇已在 2026-09 减法里删除），在这些类别里新建会以「概念」落进世界。只保留真有数据的三类。
   window.LIB_CATS 仍按原名导出（写作台的档案悬停卡按 id 读类别名）。 */
const LIB_CATS = [
  { id: "people", label: "人物",   icon: "Users",  accent: "crimson", noun: "位角色" },
  { id: "world",  label: "世界",   icon: "MapPin", accent: "gold",    noun: "处设定" },
  { id: "events", label: "大事记", icon: "Clock",  accent: "slate",   noun: "起事件" },
];


/* ==========================================================
   FE-ALIGN Phase 6：资料库接真。
   LIB_ENTRIES 退化为可变缓存数组（保持引用——视图随访问重挂载读取
   最新内容）；数据来自 /api/v2/projects/{id}/library（人物/实体/关系/
   时间线聚合），适配为条目形状。
   ========================================================== */

const LIB_ENTRIES = [];
const LIB_BY_ID = {};

/* 世界条目的类型：后端枚举 ↔ 中文。编辑时反查，写回 entity.kind。 */
const LIB_KIND_LABEL = { location: "地点", item: "物品", faction: "机构", concept: "概念" };
const LIB_KIND_OPTIONS = Object.keys(LIB_KIND_LABEL).map(value => ({ value, label: LIB_KIND_LABEL[value] }));
const LIB_CAT_ACCENT = { people: "crimson", world: "gold", events: "slate" };

function libTime(value) {
  const t = value ? Date.parse(value) : NaN;
  return Number.isFinite(t) ? t : 0;
}

const libActiveId = () => { try { return WsWorks ? WsWorks.activeId() : null; } catch (e) { return null; } };

/* 关系 id 缓存（编辑层 diff 删边用）：refPair "a|b" → relation_id */
let LIB_RELATIONS = [];
let LIB_REVISION = 0;
const libSubscribers = createSubscribers();

function libNotify() {
  LIB_REVISION += 1;
  libSubscribers.notify();
  /* 兼容仍通过 window 事件读取资料库的过渡期模块。 */
  try { window.dispatchEvent(new CustomEvent("ws:library-changed")); } catch (e) {}
}

/* 读取状态变了（开始重试 / 读失败）但档案本身没变：只叫醒本模块的订阅者（视图），
   不广播 ws:library-changed——那是「档案变了」的信号，写作台的高亮等过渡期模块会据此重读。 */
function libNotifyLoadState() {
  LIB_REVISION += 1;
  libSubscribers.notify();
}

function libSubscribe(listener) {
  return libSubscribers.subscribe(listener);
}

function libSnapshot() { return LIB_REVISION; }

function libStripRef(ref) { return String(ref || "").split(":").slice(1).join(":"); }

const libFactList = (value) => (Array.isArray(value) ? value.filter(f => f && typeof f === "object") : []);
const libTagList = (value) => (Array.isArray(value) ? value.map(String).filter(Boolean) : []);

/* 人物：role 是作者写的角色定位（主角 / 对立…），可能为空；kind 给写作台悬停卡一个非空的说明。 */
function libAdaptCharacter(c, linksOf) {
  const d = c.details || {};
  return {
    id: c.character_id, cat: "people", name: c.name,
    code: d.code || "", kind: c.role || "角色", role: c.role || "",
    accent: d.accent || LIB_CAT_ACCENT.people, glyph: d.glyph || Array.from(c.name)[0],
    pinned: !!d.pinned, updatedAt: libTime(c.updated_at),
    summary: c.summary || "", tags: libTagList(d.tags),
    blurb: d.blurb || "",
    facts: libFactList(d.facts),
    links: linksOf(`character:${c.character_id}`),
    ref: c.ref, details: d,
  };
}

function libAdaptEntity(e, linksOf) {
  const d = e.details || {};
  return {
    id: e.entity_id, cat: "world", name: e.name,
    code: d.code || "", kind: LIB_KIND_LABEL[e.kind] || e.kind || "设定", entityKind: e.kind || "concept",
    accent: d.accent || LIB_CAT_ACCENT.world, glyph: d.glyph || Array.from(e.name)[0],
    pinned: !!d.pinned, updatedAt: libTime(e.updated_at),
    summary: e.summary || "", tags: libTagList(e.tags),
    blurb: d.blurb || "",
    facts: libFactList(d.facts),
    links: linksOf(e.ref),
    ref: e.ref, details: d,
  };
}

/* 大事记：时间与所在章是专门的列（time_label / chapter_ref），不是自由的「关键信息」；
   相关档案来自 entity_refs（只有指向，没有关系类型与标签）。大事记没有扩展字段组，所以不能置顶、不能加标签。 */
function libAdaptEvent(ev) {
  return {
    id: ev.event_id, cat: "events", name: ev.label,
    code: "", kind: "事件",
    accent: LIB_CAT_ACCENT.events, glyph: Array.from(ev.label)[0],
    pinned: false, updatedAt: libTime(ev.updated_at),
    summary: "", tags: [],
    blurb: ev.note || "",
    facts: [],
    timeLabel: ev.time_label || "",
    chapterRef: ev.chapter_ref || "",
    realized: ev.realization_status === "realized",
    links: (ev.entity_refs || []).map(ref => ({ id: libStripRef(ref), rel: "相关", viaEvent: true })).filter(l => l.id),
    ref: `event:${ev.event_id}`,
  };
}

let libFetching = null;
let libFetchingProjectId = null;
let libVisibleProjectId = null;
let libRequestSerial = 0;

/* 当前作品的资料库读到哪一步了：视图靠它分清「真的是空的」「还在读」「读不到」——
   以前三种情况都是一张「档案库还是空的 · 新建第一份档案」，后端没起来时作者会以为档案全没了。
   status: idle（还没有作品）| loading | ready | error；message 只在 error 时有。 */
let LIB_LOAD = { pid: null, status: "idle", message: "" };
function libLoadState() { return LIB_LOAD; }

function libClearForProject(projectId) {
  libVisibleProjectId = projectId || null;
  LIB_LOAD = { pid: projectId || null, status: projectId ? "loading" : "idle", message: "" };
  LIB_RELATIONS = [];
  LIB_ENTRIES.length = 0;
  Object.keys(LIB_BY_ID).forEach(k => { delete LIB_BY_ID[k]; });
  libNotify();
}

/* 结果是 true = 当前作品的资料库快照已经换成服务端的；false = 没读到（作品未定、请求失败或被新请求顶掉）。
   旧覆盖层迁移靠它区分「资料库确实是空的」和「还没读到」。 */
function libFetch() {
  const pid = libActiveId();
  if (!pid || pid === "__loading__") {
    if (libVisibleProjectId !== null || LIB_ENTRIES.length || LIB_RELATIONS.length) {
      libRequestSerial += 1; // 让尚未返回的旧作品请求失效
      libFetching = null;
      libFetchingProjectId = null;
      libClearForProject(null);
    }
    return Promise.resolve(false);
  }

  /* 切换作品时立即清空旧快照；新请求完成前绝不展示上一部作品的数据。 */
  if (libVisibleProjectId !== pid) {
    libRequestSerial += 1;
    libFetching = null;
    libFetchingProjectId = null;
    libClearForProject(pid);
  }
  if (libFetching && libFetchingProjectId === pid) return libFetching;

  const requestSerial = ++libRequestSerial;
  libFetchingProjectId = pid;
  /* 读失败之后的重试：先回到「正在读」，视图不再停在上一次的错误上 */
  if (LIB_LOAD.status === "error") { LIB_LOAD = { pid, status: "loading", message: "" }; libNotifyLoadState(); }
  const pending = (async () => {
    try {
      const data = await apiGet(`/api/v2/projects/${pid}/library`);
      /* A→B 快速切换时，A 的迟到响应不得覆盖 B 的资料库。 */
      if (requestSerial !== libRequestSerial || libActiveId() !== pid || libVisibleProjectId !== pid) return false;
      LIB_RELATIONS = (data && data.relations) || [];
      const linkIndex = {};
      /* rel 只放作者写的关系标签；没写标签时留空，由视图显示关系类型的中文名
         （以前回退成 kind，作者会看到 conflict / related 这样的英文键）。 */
      for (const rel of LIB_RELATIONS) {
        (linkIndex[rel.from_ref] = linkIndex[rel.from_ref] || []).push({ id: libStripRef(rel.to_ref), rel: rel.note || "", type: rel.kind, relationId: rel.relation_id });
        (linkIndex[rel.to_ref] = linkIndex[rel.to_ref] || []).push({ id: libStripRef(rel.from_ref), rel: rel.note || "", type: rel.kind, relationId: rel.relation_id });
      }
      const linksOf = (ref) => linkIndex[ref] || [];
      const next = [
        ...((data && data.characters) || []).map(c => libAdaptCharacter(c, linksOf)),
        ...((data && data.entities) || []).map(e => libAdaptEntity(e, linksOf)),
        ...((data && data.timeline) || []).map(ev => libAdaptEvent(ev)),
      ];
      LIB_ENTRIES.length = 0;
      LIB_ENTRIES.push(...next);
      Object.keys(LIB_BY_ID).forEach(k => { delete LIB_BY_ID[k]; });
      next.forEach(e => { LIB_BY_ID[e.id] = e; });
      LIB_LOAD = { pid, status: "ready", message: "" };
      libNotify();
      return true;
    } catch (e) {
      console.warn("[WsLibrary] 拉取资料库失败:", e);
      if (requestSerial === libRequestSerial && libActiveId() === pid && libVisibleProjectId === pid) {
        LIB_LOAD = { pid, status: "error", message: (e && e.message) || "读不到资料库。" };
        libNotifyLoadState();
      }
      return false;
    } finally {
      if (requestSerial === libRequestSerial) {
        libFetching = null;
        libFetchingProjectId = null;
      }
    }
  })();
  libFetching = pending;
  return pending;
}

try { libFetch(); } catch (e) {}
if (window.__wsLibraryDataWorkChanged) {
  window.removeEventListener("ws:work-changed", window.__wsLibraryDataWorkChanged);
}
const libOnWorkChanged = () => { try { libFetch(); } catch (e) {} };
window.addEventListener("ws:work-changed", libOnWorkChanged);
window.__wsLibraryDataWorkChanged = libOnWorkChanged;
Object.assign(window, {
  LIB_relationsRaw: () => LIB_RELATIONS,
  LIB_refetch: libFetch,
  LIB_subscribe: libSubscribe,
  LIB_snapshot: libSnapshot,
});

Object.assign(window, { LIB_CATS, LIB_ENTRIES, LIB_BY_ID });

/* ESM 导出；上面的 window.* 赋值是写作台等过渡期模块还在读的契约，别删 */
export { LIB_CATS, LIB_ENTRIES, LIB_BY_ID, LIB_KIND_LABEL, LIB_KIND_OPTIONS, libSubscribe, libSnapshot, libLoadState, libFetch as libRefetch };
