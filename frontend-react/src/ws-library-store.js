import { useSyncExternalStore } from "react";
import { apiDelete, apiGet, apiPatch, apiPost } from "./lib/client.js";
import { createSubscribers, storeAlert } from "./lib/store-utils.js";
import { adoptModuleListeners, retireModuleListeners } from "./lib/events.js";
import { isRealWorkId } from "./lib/work-id.js";
import { readyWorkId } from "./lib/ready-work.js";
import { WsWorks } from "./ws-works.jsx";
import { LIB_CATS, LIB_KIND_LABEL, LIB_KIND_OPTIONS } from "./labels/library.js";

/* ==========================================================
   资料库的 store（2026-09-30 从 ws-library-data.jsx / ws-library-edit.jsx 收拢，审计 F05-08 / F01-18）
   ----------------------------------------------------------
   档案库 = 后端 /api/v2/projects/{id}/library 的聚合：人物（StoryCharacter）、世界（LibraryEntity）、
   大事记（TimelineEvent），彼此由关系（LibraryRelation）与大事记的 entity_refs 连起来。
   条目形状：{ id, cat, name, code, kind, role?, entityKind?, accent, glyph, tags,
              summary, blurb, facts[], links[], pinned, updatedAt,
              timeLabel?, chapterRef?, ref, details }
   details 是服务端原样的扩展字段组：编辑时在它上面合并，未知键（旧数据）不会被冲掉。
   · 读：只按当前作品存一份。第一个订阅者（或 libEnsureLoaded / libRefetch）才发请求——import 本模块不拉数据，
     写作台的名字高亮、章节编排的视角候选可以直接 import 它，用到时再拉（审计 F05-01：它们以前读 window.LIB_*，
     没打开过「资料」页就是空的）。换作品时立即清掉旧快照，迟到的旧请求不写回。
   · 快照不可变：libLive() / useLibraryLive() 每次变化给一份新的 { entries, byId }；LIB_ENTRIES / LIB_BY_ID
     是原地更新的过渡容器，只给 window 接缝（ws-library-data.jsx）与旧导出用，接缝退役时一起删。
   · 写：全部直达后端（characters / entities / timeline / relations），写完以服务端为准重读；失败提示并重读。
   纯 ESM，不写 window（窗口接缝在门面 ws-library-data.jsx / ws-library-edit.jsx 里）。
   ========================================================== */

/* 类别与世界条目类型的表住在叶子模块 labels/library.js；这里照旧转出（门面与旧 import 路径用） */
export { LIB_CATS, LIB_KIND_LABEL, LIB_KIND_OPTIONS };
const LIB_CAT_ACCENT = { people: "crimson", world: "gold", events: "slate" };

/* 过渡容器（原地更新，身份不变）：window.LIB_ENTRIES / LIB_BY_ID 接缝与门面的旧导出指着它们 */
export const LIB_ENTRIES = [];
export const LIB_BY_ID = {};

/* 不可变快照：每次档案变化换一份新对象（身份变了 = 内容变了） */
const LIB_EMPTY = Object.freeze({ entries: [], byId: {} });
let libSnap = LIB_EMPTY;
let libRevision = 0;
const libSubscribers = createSubscribers();
let libSubscriberCount = 0;

function libTime(value) {
  const t = value ? Date.parse(value) : NaN;
  return Number.isFinite(t) ? t : 0;
}

/* 能发请求的当前作品（书架还在加载、空书架、新建作品还没拿到正式 id 时是 null） */
const libActiveId = () => readyWorkId(WsWorks);

function libBump() {
  libRevision += 1;
  libSubscribers.notify();
}

/* 档案换了一份：不可变快照换新，过渡容器原地跟上 */
function libReplace(entries) {
  const byId = {};
  entries.forEach(e => { byId[e.id] = e; });
  libSnap = entries.length ? { entries, byId } : LIB_EMPTY;
  LIB_ENTRIES.length = 0;
  LIB_ENTRIES.push(...entries);
  Object.keys(LIB_BY_ID).forEach(k => { delete LIB_BY_ID[k]; });
  Object.assign(LIB_BY_ID, byId);
}

/* 订阅：第一个订阅者到来时按需拉一次当前作品的资料库。每次订阅各包一层，同一个函数订阅两次也各算一个。 */
export function libSubscribe(listener) {
  const off = libSubscribers.subscribe(() => listener());
  libSubscriberCount += 1;
  if (libSubscriberCount === 1) libEnsureLoaded();
  let active = true;
  return () => {
    if (!active) return;
    active = false;
    libSubscriberCount -= 1;
    off();
  };
}

/* 修订号：档案或读取状态每变一次加一（useSyncExternalStore 的快照） */
export function libSnapshot() { return libRevision; }

/* 当前的档案快照 { entries, byId }：只读、不发请求（写作台逐次高亮、悬停都直接读它） */
export function libLive() { return libSnap; }

/* 订阅档案快照的 hook：挂上就按需拉一次；返回 { entries, byId } */
export function useLibraryLive() {
  return useSyncExternalStore(libSubscribe, libLive, libLive);
}

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

/* 服务端载荷 → 条目列表（关系在两端各挂一次：rel 只放作者写的关系标签，没写时留空，由视图显示关系类型的中文名——
   以前回退成 kind，作者会看到 conflict / related 这样的英文键） */
function libAdapt(data) {
  const relations = (data && data.relations) || [];
  const linkIndex = {};
  for (const rel of relations) {
    (linkIndex[rel.from_ref] = linkIndex[rel.from_ref] || []).push({ id: libStripRef(rel.to_ref), rel: rel.note || "", type: rel.kind, relationId: rel.relation_id });
    (linkIndex[rel.to_ref] = linkIndex[rel.to_ref] || []).push({ id: libStripRef(rel.from_ref), rel: rel.note || "", type: rel.kind, relationId: rel.relation_id });
  }
  const linksOf = (ref) => linkIndex[ref] || [];
  return [
    ...((data && data.characters) || []).map(c => libAdaptCharacter(c, linksOf)),
    ...((data && data.entities) || []).map(e => libAdaptEntity(e, linksOf)),
    ...((data && data.timeline) || []).map(ev => libAdaptEvent(ev)),
  ];
}

let libFetching = null;
let libFetchingProjectId = null;
let libVisibleProjectId = null;
let libRequestSerial = 0;

/* 当前作品的资料库读到哪一步了：视图靠它分清「真的是空的」「还在读」「读不到」——
   以前三种情况都是一张「档案库还是空的 · 新建第一份档案」，后端没起来时作者会以为档案全没了。
   status: idle（还没有作品）| loading | ready | error；message 只在 error 时有。 */
let libLoad = { pid: null, status: "idle", message: "" };
export function libLoadState() { return libLoad; }

function libClearForProject(projectId) {
  libVisibleProjectId = projectId || null;
  libLoad = { pid: projectId || null, status: projectId ? "loading" : "idle", message: "" };
  libReplace([]);
  libBump();
}

/* 拉当前作品的资料库。结果是 true = 当前作品的快照已经换成服务端的；false = 没读到（作品未定、请求失败或被新请求顶掉）。 */
export function libRefetch() {
  const pid = libActiveId();
  if (!isRealWorkId(pid)) {
    if (libVisibleProjectId !== null || libSnap.entries.length) {
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
  if (libLoad.status === "error") { libLoad = { pid, status: "loading", message: "" }; libBump(); }
  const pending = (async () => {
    try {
      const data = await apiGet(`/api/v2/projects/${pid}/library`);
      /* A→B 快速切换时，A 的迟到响应不得覆盖 B 的资料库。 */
      if (requestSerial !== libRequestSerial || libActiveId() !== pid || libVisibleProjectId !== pid) return false;
      libReplace(libAdapt(data));
      libLoad = { pid, status: "ready", message: "" };
      libBump();
      return true;
    } catch (e) {
      console.warn("[WsLibrary] 拉取资料库失败:", e);
      if (requestSerial === libRequestSerial && libActiveId() === pid && libVisibleProjectId === pid) {
        libLoad = { pid, status: "error", message: (e && e.message) || "读不到资料库。" };
        libBump();
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

/* 按需拉：当前作品还没读到（或正在读）才发请求；已经读到了就什么都不做。返回 libRefetch 的 Promise 或 true。 */
export function libEnsureLoaded() {
  const pid = libActiveId();
  if (isRealWorkId(pid) && libVisibleProjectId === pid && libLoad.status === "ready") return Promise.resolve(true);
  return libRefetch();
}

/* 换作品：这个 store 已经在用（有订阅者，或读过某部作品）才跟着重读；没人用时不因为切作品去拉数据。
   模块在 HMR / 测试 resetModules 后可能重新执行：先撤掉旧实例的监听器 */
retireModuleListeners("ws-library-store");
const libOnWorkChanged = () => {
  if (libSubscriberCount > 0 || libVisibleProjectId !== null) {
    try { libRefetch(); } catch (e) { /* 读取失败由 libLoad 报给视图 */ }
  }
};
window.addEventListener("ws:work-changed", libOnWorkChanged);
adoptModuleListeners("ws-library-store", () => window.removeEventListener("ws:work-changed", libOnWorkChanged));

/* ==========================================================
   写：人物 characters、世界 entities、大事记 timeline、关系 relations
   ========================================================== */

const libApiBase = () => `/api/v2/projects/${libActiveId()}/library`;
const libToast = (e, fallback) => storeAlert(e, fallback);

/* 世界条目的类型：接受后端枚举或中文名，其余值不写（后端只收四种） */
const LIB_KIND_VALUE = Object.keys(LIB_KIND_LABEL).reduce((m, k) => { m[k] = k; m[LIB_KIND_LABEL[k]] = k; return m; }, {});

/* 保存：edits = { [条目 id]: 这次保存的字段 }，逐条打到对应的端点（主对象 PATCH + 关系增删），全部成功才返回 true。
   每次都照发：以前按「这个会话上次发过的 patch」去重，别处（构思第 04 步）改过名之后再把名字改回来，这一次被静默跳过
   还报成功（审计 F05-02）。连点由表单的「保存中…」禁用挡住，重放由请求的幂等键挡住。 */
export async function LIB_persist(edits) {
  try {
    for (const id of Object.keys(edits || {})) {
      const patch = edits[id];
      if (!patch) continue;
      const base = libSnap.byId[id];
      if (!base) continue;
      await libPushPatch(base, patch);
    }
    libRefetch();
    return true;
  } catch (e) {
    libToast(e, "资料卡保存失败。");
    libRefetch();
    return false;
  }
}

async function libPushPatch(base, patch) {
  if (base.cat === "events") {
    const eventBody = {};
    if (patch.name != null) eventBody.label = patch.name;
    if (patch.blurb != null || patch.summary != null) eventBody.note = patch.blurb != null ? patch.blurb : patch.summary;
    if (patch.timeLabel != null) eventBody.time_label = patch.timeLabel;
    if (patch.chapterRef != null) eventBody.chapter_ref = patch.chapterRef;
    /* 大事记的相关档案就是 entity_refs：只认人物 / 世界（后端只收这两种引用） */
    if (patch.links != null) {
      eventBody.entity_refs = patch.links
        .map(l => libSnap.byId[l.id])
        .filter(t => t && t.ref && !String(t.ref).startsWith("event:"))
        .map(t => t.ref);
    }
    await apiPatch(`${libApiBase()}/timeline/${base.id}`, eventBody);
    return;
  }

  const body = {};
  if (patch.name != null) body.name = patch.name;
  if (patch.summary != null) body.summary = patch.summary;
  const details = {};
  if (patch.blurb != null) details.blurb = patch.blurb;
  if (patch.facts != null) details.facts = patch.facts;
  if (patch.pinned != null) details.pinned = !!patch.pinned;
  /* 人物没有 tags 列：标签存进扩展字段组（适配层从 details.tags 读回） */
  if (base.cat === "people" && patch.tags != null) details.tags = patch.tags;
  if (Object.keys(details).length) body.details = { ...(base.details || {}), ...details };
  if (base.cat === "people") {
    if (patch.kind != null) body.role = patch.kind;
    await apiPatch(`${libApiBase()}/characters/${base.id}`, body);
  } else {
    if (patch.tags != null) body.tags = patch.tags;
    if (patch.kind != null && LIB_KIND_VALUE[patch.kind]) body.kind = LIB_KIND_VALUE[patch.kind];
    await apiPatch(`${libApiBase()}/entities/${base.id}`, body);
  }
  if (patch.links) await libSyncLinks(base, patch.links);
}

const libLinkType = (l) => (l && l.type) || "related";
const libLinkNote = (l) => String((l && l.rel) || "").trim();

/* 关系 diff：删掉的 → DELETE；新加的 → POST；类型或标签改了的 → DELETE 旧的再 POST 新的
   （后端没有改关系的端点；以前改了类型 / 标签直接被跳过，保存后原样回来）。 */
async function libSyncLinks(base, nextLinks) {
  const prev = base.links || [];
  const prevById = prev.reduce((m, l) => { m[l.id] = l; return m; }, {});
  const nextIds = new Set((nextLinks || []).map(l => l.id));
  for (const link of prev) {
    if (!nextIds.has(link.id) && link.relationId) {
      await apiDelete(`${libApiBase()}/relations/${link.relationId}`);
    }
  }
  for (const link of nextLinks || []) {
    const before = prevById[link.id];
    if (before) {
      const changed = libLinkType(before) !== libLinkType(link) || libLinkNote(before) !== libLinkNote(link);
      if (!changed || !before.relationId) continue;
    }
    const target = libSnap.byId[link.id];
    if (!target || !target.ref || !base.ref || String(target.ref).startsWith("event:")) continue;
    if (before && before.relationId) await apiDelete(`${libApiBase()}/relations/${before.relationId}`);
    await apiPost(`${libApiBase()}/relations`, {
      from_ref: base.ref, to_ref: target.ref,
      kind: link.type || "related", note: link.rel || "",
    });
  }
}

/* 删除：人物 / 世界 / 大事记都有 DELETE 端点。返回 true / false；失败时按错误码说清楚为什么删不掉。 */
function libDeleteMessage(error, base) {
  const code = error && error.code;
  if (code === "LIBRARY_CHARACTER_IN_USE") {
    const deps = (error.details && error.details.dependencies) || {};
    const used = Object.values(deps).reduce((n, v) => n + (Number(v) || 0), 0);
    return `「${base.name}」还在构思或场景里被用到${used ? `（${used} 处）` : ""}，先在那里换掉这个人物，再回来删档案。`;
  }
  if (code === "TIMELINE_REALIZED_EVENT_IMMUTABLE") return `「${base.name}」已经写进定稿正文，不能删除。`;
  return null;
}

export async function LIB_deleteEntry(base) {
  if (!base || !base.id) return false;
  try {
    if (base.cat === "people") {
      await apiDelete(`${libApiBase()}/characters/${base.id}`);
    } else if (base.cat === "events") {
      await apiDelete(`${libApiBase()}/timeline/${base.id}`);
    } else {
      await apiDelete(`${libApiBase()}/entities/${base.id}`);
    }
    await libRefetch();
    return true;
  } catch (e) {
    const specific = libDeleteMessage(e, base);
    libToast(specific ? null : e, specific || "删除档案失败。");
    libRefetch();
    return false;
  }
}

/* 新建：先落后端，拿到服务端 id 再刷新列表——视图随后选中这条真实条目并打开编辑。
   以前先在本地造一条「u-…」副本再异步 POST：列表里出现两条，编辑落在副本上，保存时找不到 id 被静默丢弃。 */
export async function LIB_createEntry(cat, name, options = {}) {
  const label = String(name || "").trim();
  if (!label) return null;
  try {
    let created = null;
    let id = null;
    if (cat === "people") {
      created = await apiPost(`${libApiBase()}/characters`, { name: label, summary: "", details: {} });
      id = created && created.character_id;
    } else if (cat === "events") {
      created = await apiPost(`${libApiBase()}/timeline`, { label, ...(options.timeLabel ? { time_label: options.timeLabel } : {}) });
      id = created && created.event_id;
    } else {
      const kind = LIB_KIND_VALUE[options.kind] || "location";
      created = await apiPost(`${libApiBase()}/entities`, { name: label, kind, summary: "", tags: [], details: {} });
      id = created && created.entity_id;
    }
    await libRefetch();
    return id || null;
  } catch (e) {
    libToast(e, "新建档案失败。");
    return null;
  }
}
