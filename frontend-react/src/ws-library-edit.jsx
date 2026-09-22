import React from "react";
import { I } from "./icons.jsx";
import { LIB_BY_ID, LIB_CATS, LIB_ENTRIES, LIB_KIND_LABEL, LIB_KIND_OPTIONS } from "./ws-library-data.jsx";
import { LIB_REL_TYPES, LIB_chapterLabel, LIB_relType } from "./ws-library-derive.jsx";
import { apiDelete, apiPatch, apiPost } from "./lib/client.js";
import { storeAlert } from "./lib/store-utils.js";
import { wsKey, WsWorks } from "./ws-works.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { LibGlyph, libAccClass, libCatLabel } from "./ws-library-parts.jsx";
import { Segmented } from "./ws-ui.jsx";

const { useState: useEdSt, useEffect: useEdEffect, useMemo: useEdMemo, useId: useEdId } = React;

/* ==========================================================
   Library — 档案的写入层 + 编辑 / 新建表单
   · 写入全部直达后端（人物 characters、世界 entities、大事记 timeline、关系 relations）；
   · localStorage 覆盖层只剩一次性上行旧数据（LIB_migrateLegacy），视图不再读它；
   · 表单只提供后端真能存下的字段——存不下的字段宁可不给，也不让作者白填。
   ========================================================== */

const LIB_EDIT_KEY = "ws-lib-edits-v1";
const LIB_ADD_KEY  = "ws-lib-additions-v1";
const LIB_K = (k) => (wsKey ? wsKey(k) : k);  // per-work namespace
const LIB_MIGRATED_KEY = "ws-lib-migrated-v1";

const libProjectId = () => { try { return WsWorks ? WsWorks.activeId() : null; } catch (e) { return null; } };
const libApiBase = () => `/api/v2/projects/${libProjectId()}/library`;
/* 经 window 调用：单测在 window.LIB_refetch 上打桩，断言失败路径以服务端为准回滚 */
const libRefetch = () => { try { if (window.LIB_refetch) return window.LIB_refetch(); } catch (e) {} return Promise.resolve(); };
const libToast = (e, fallback) => storeAlert(e, fallback);

/* —— FE-ALIGN P6：编辑/新建直接落后端（base 已来自 API），
   localStorage 覆盖层退化为「读空」；旧键一次性上行后保留（P8 清理）。 —— */

function LIB_loadEdits() {
  LIB_migrateLegacy();
  return {};
}

/* 条目 patch → 各对象的 PATCH/关系 CRUD；调用粒度=单次保存的 edits 全量 diff */
const libSentEdits = {};
async function LIB_persist(edits) {
  try {
    for (const id of Object.keys(edits || {})) {
      const patch = edits[id];
      if (!patch || JSON.stringify(libSentEdits[id]) === JSON.stringify(patch)) continue;
      const base = LIB_BY_ID[id];
      if (!base) continue;
      await libPushPatch(base, patch);
      // 只有主对象和关系操作都成功后才去重；部分失败必须允许同载荷重试。
      libSentEdits[id] = patch;
    }
    libRefetch();
    return true;
  } catch (e) {
    libToast(e, "资料卡保存失败。");
    libRefetch();
    return false;
  }
}

/* 世界条目的类型：接受后端枚举或中文名，其余值不写（后端只收四种） */
const LIB_KIND_VALUE = Object.keys(LIB_KIND_LABEL).reduce((m, k) => { m[k] = k; m[LIB_KIND_LABEL[k]] = k; return m; }, {});

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
        .map(l => LIB_BY_ID[l.id])
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
    const target = LIB_BY_ID[link.id];
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

async function LIB_deleteEntry(base) {
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
async function LIB_createEntry(cat, name, options = {}) {
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

/* merge a stored patch over a base entry（过渡期窗口契约；视图已不再使用覆盖层） */
function LIB_applyEdit(entry, edits) {
  const p = entry && edits ? edits[entry.id] : null;
  return p ? { ...entry, ...p } : entry;
}

/* ---- additions（旧的「先本地后上行」新建；只剩旧数据迁移与窗口调用方在用） ---- */
function LIB_loadAdds() { return []; }

const libSentAdds = new Set();
const libSendingAdds = new Set();
async function LIB_persistAdds(adds) {
  let firstError = null;
  for (const add of adds || []) {
    if (!add || libSentAdds.has(add.id) || libSendingAdds.has(add.id)) continue;
    libSendingAdds.add(add.id);
    try {
      if (add.cat === "people") {
        await apiPost(`${libApiBase()}/characters`, {
          name: add.name, role: add.kind, summary: add.summary || "",
          details: { blurb: add.blurb, facts: add.facts, glyph: add.glyph },
        });
      } else if (add.cat === "events") {
        await apiPost(`${libApiBase()}/timeline`, {
          label: add.name, note: add.blurb || add.summary || "",
        });
      } else {
        await apiPost(`${libApiBase()}/entities`, {
          name: add.name, kind: "concept", summary: add.summary || "",
          tags: add.tags || [], details: { blurb: add.blurb, facts: add.facts, glyph: add.glyph, code: add.code },
        });
      }
      libSentAdds.add(add.id);
    } catch (e) {
      if (!firstError) firstError = e;
    } finally {
      libSendingAdds.delete(add.id);
    }
  }
  if (firstError) {
    libToast(firstError, "新建资料失败，可在网络恢复后重试。");
    libRefetch();
    return false;
  }
  libRefetch();
  return true;
}

/* 旧 localStorage 覆盖层（edits/additions）一次性上行；失败不写完成标记，下次重试。
   WsLibrary 挂载和换作品时调用。改动要找到服务端底稿才能 PATCH（LIB_persist 跳过找不到底稿的条目），
   所以先确认本作资料库确实读到了——没读到就留到下次，不能当成「没有可迁移的」写上完成标记。 */
let libMigrationPromise = null;
function LIB_migrateLegacy() {
  if (libMigrationPromise) return libMigrationPromise;
  libMigrationPromise = (async () => {
    try {
      const pid = libProjectId();
      if (!pid || pid === "__loading__") return false;
      const flag = LIB_K(LIB_MIGRATED_KEY);
      if (localStorage.getItem(flag)) return true;
      const edits = JSON.parse(localStorage.getItem(LIB_K(LIB_EDIT_KEY)) || "{}") || {};
      const adds = JSON.parse(localStorage.getItem(LIB_K(LIB_ADD_KEY)) || "[]");
      const hasEdits = typeof edits === "object" && Object.keys(edits).length > 0;
      if (hasEdits && (await libRefetch()) !== true) return false;
      const editsOk = !hasEdits || await LIB_persist(edits);
      const addsOk = !Array.isArray(adds) || !adds.length || await LIB_persistAdds(adds);
      if (editsOk && addsOk) localStorage.setItem(flag, new Date().toISOString());
      return editsOk && addsOk;
    } catch (e) {
      libToast(e, "旧资料迁移失败，已保留本地数据供下次重试。");
      return false;
    }
  })().finally(() => { libMigrationPromise = null; });
  return libMigrationPromise;
}
/* 构造一个新档案的种子，cat = 类别 id，name = 名称（LIB_persistAdds 的输入形状） */
function LIB_newEntry(cat, name) {
  const meta = LIB_CATS.find(c => c.id === cat) || LIB_CATS[0];
  const glyph = (name || "新").trim().charAt(0) || "新";
  const id = "u-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 6);
  return {
    id, cat, name: name || "未命名档案", code: "",
    kind: "", accent: meta.accent, glyph, user: true,
    summary: "", blurb: "", tags: [], facts: [], links: [],
  };
}

/* ---- 编辑表单 ---- */

/* 表单快照：用来判断「有没有没保存的改动」 */
function libFormSnapshot(form) {
  return JSON.stringify({
    ...form,
    links: (form.links || []).map(l => [l.id, l.type || "", String(l.rel || "").trim()]),
    facts: (form.facts || []).map(f => [String(f.k || "").trim(), String(f.v || "").trim()]),
  });
}

function libInitialForm(e) {
  return {
    name: e.name || "",
    kind: e.cat === "people" ? (e.role || "") : e.cat === "world" ? (e.entityKind || "location") : "",
    summary: e.summary || "",
    blurb: e.blurb || "",
    facts: (e.facts || []).map(f => ({ k: f.k || "", v: f.v || "" })),
    tags: [...(e.tags || [])],
    links: (e.links || []).map(l => ({ id: l.id, rel: l.rel || "", type: l.type, relationId: l.relationId })),
    timeLabel: e.timeLabel || "",
    chapterRef: e.chapterRef || "",
  };
}

function LibField({ label, htmlFor, hint, children }) {
  return (
    <div className="dform-field">
      <label className="ws-section-label" htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="dform-hint">{hint}</div>}
    </div>
  );
}

function DossierEdit({ entry: e, allEntries, byId, chapters, onSave, onCancel, onDirtyChange }) {
  const [form, setForm] = useEdSt(() => libInitialForm(e));
  const [initial] = useEdSt(() => libFormSnapshot(libInitialForm(e)));
  const [tagInput, setTagInput] = useEdSt("");
  const [addId, setAddId] = useEdSt("");
  const [addType, setAddType] = useEdSt("kin");
  const [saving, setSaving] = useEdSt(false);
  const uid = useEdId();
  const isEvent = e.cat === "events";
  const ents = allEntries || [];
  const bid = byId || {};
  const set = (key, value) => setForm(f => ({ ...f, [key]: value }));

  const dirty = libFormSnapshot(form) !== initial || tagInput.trim() !== "";
  useEdEffect(() => { if (onDirtyChange) onDirtyChange(dirty); }, [dirty]); // eslint-disable-line react-hooks/exhaustive-deps
  useEdEffect(() => () => { if (onDirtyChange) onDirtyChange(false); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const links = form.links;
  const setLinks = (fn) => setForm(f => ({ ...f, links: fn(f.links) }));
  const setLinkRel = (i, rel) => setLinks(prev => prev.map((l, j) => j === i ? { ...l, rel } : l));
  const setLinkType = (i, type) => setLinks(prev => prev.map((l, j) => j === i ? { ...l, type } : l));
  const delLink = (i) => setLinks(prev => prev.filter((_, j) => j !== i));
  const addLink = () => {
    if (!addId || links.some(l => l.id === addId)) { setAddId(""); return; }
    setLinks(prev => [...prev, isEvent ? { id: addId, rel: "相关" } : { id: addId, rel: "", type: addType }]);
    setAddId("");
  };
  /* 可关联的档案：排除自身、已关联的，以及大事记——关系的两端只能是人物或世界；
     大事记要关联人物，在大事记自己的「相关档案」里加。 */
  const linkOptions = ents.filter(x => x.id !== e.id && x.cat !== "events" && !links.some(l => l.id === x.id));
  const facts = form.facts;
  const setFacts = (fn) => setForm(f => ({ ...f, facts: fn(f.facts) }));
  const setFactK = (i, k) => setFacts(prev => prev.map((f, j) => j === i ? { ...f, k } : f));
  const setFactV = (i, v) => setFacts(prev => prev.map((f, j) => j === i ? { ...f, v } : f));
  const addFact = () => setFacts(prev => [...prev, { k: "", v: "" }]);
  const delFact = (i) => setFacts(prev => prev.filter((_, j) => j !== i));
  const addTag = () => {
    const t = tagInput.trim();
    if (t && !form.tags.includes(t)) set("tags", [...form.tags, t]);
    setTagInput("");
  };

  const chapterOptions = useEdMemo(() => {
    const list = (chapters || []).filter(c => c && c.backendId).map(c => ({ value: c.backendId, label: LIB_chapterLabel(c.backendId, chapters) }));
    if (form.chapterRef && !list.some(o => o.value === form.chapterRef)) {
      list.unshift({ value: form.chapterRef, label: LIB_chapterLabel(form.chapterRef, chapters) });
    }
    return list;
  }, [chapters, form.chapterRef]);

  const nameOk = form.name.trim().length > 0;
  const save = async () => {
    if (!nameOk || saving) return;
    const pendingTag = tagInput.trim();
    const tags = pendingTag && !form.tags.includes(pendingTag) ? [...form.tags, pendingTag] : form.tags;
    const cleanFacts = form.facts
      .map(f => ({ k: String(f.k || "").trim(), v: String(f.v || "").trim() }))
      .filter(f => f.k || f.v);
    const patch = isEvent
      ? { name: form.name.trim(), blurb: form.blurb, timeLabel: form.timeLabel.trim(), chapterRef: form.chapterRef, links: form.links }
      : { name: form.name.trim(), kind: form.kind, summary: form.summary, blurb: form.blurb, facts: cleanFacts, tags, links: form.links };
    setSaving(true);
    try {
      await onSave(patch);
    } finally {
      setSaving(false);
    }
  };
  const onFormKeyDown = (ev) => {
    if (isImeComposing(ev)) return;
    if ((ev.metaKey || ev.ctrlKey) && String(ev.key).toLowerCase() === "s") { ev.preventDefault(); save(); }
    if (ev.key === "Escape") { ev.stopPropagation(); onCancel(); }
  };

  const catLabel = libCatLabel(e.cat);
  const ids = {
    name: `${uid}-name`, kind: `${uid}-kind`, summary: `${uid}-summary`, blurb: `${uid}-blurb`,
    time: `${uid}-time`, chapter: `${uid}-chapter`, tag: `${uid}-tag`, link: `${uid}-link`,
  };

  return (
    <div className={`${libAccClass(e.accent)} dform`} onKeyDown={onFormKeyDown}>
      <header className="dossier-head">
        <LibGlyph entry={e} glyph={form.name.trim().charAt(0) || e.glyph} size="xl" />
        <div className="dossier-head-main dform-head">
          <div className="dossier-code">编辑{catLabel}</div>
          <label className="ws-sr-only" htmlFor={ids.name}>名称</label>
          <input id={ids.name} className="dform-name" value={form.name} onChange={ev => set("name", ev.target.value)} placeholder="名称" autoFocus />
          {e.cat === "people" && (
            <>
              <label className="ws-sr-only" htmlFor={ids.kind}>角色定位</label>
              <input id={ids.kind} className="dform-kind" value={form.kind} onChange={ev => set("kind", ev.target.value)} placeholder="角色定位，如 主角、对手、导师" />
            </>
          )}
          {e.cat === "world" && (
            <Segmented label="类型" value={form.kind} onChange={(v) => set("kind", v)} options={LIB_KIND_OPTIONS} />
          )}
        </div>
      </header>

      <div className="dossier-body">
        {isEvent ? (
          <div className="dform-grid">
            <LibField label="时间" htmlFor={ids.time} hint="故事里的时间，写法随意：1998 年冬、开篇前三年……">
              <input id={ids.time} className="dform-input" value={form.timeLabel} onChange={ev => set("timeLabel", ev.target.value)} placeholder="未定时间" />
            </LibField>
            <LibField label="所在章" htmlFor={ids.chapter} hint="这件事发生在正文哪一章；时间线按章排开。">
              <select id={ids.chapter} className="dform-input dform-select" value={form.chapterRef} onChange={ev => set("chapterRef", ev.target.value)}>
                <option value="">不关联章节</option>
                {chapterOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
            </LibField>
          </div>
        ) : (
          <LibField label="一句话摘要" htmlFor={ids.summary} hint="显示在列表里名字的下面。">
            <input id={ids.summary} className="dform-input" value={form.summary} onChange={ev => set("summary", ev.target.value)} placeholder="例如：守着旧档案馆的人" />
          </LibField>
        )}

        <LibField label={isEvent ? "经过" : "简述"} htmlFor={ids.blurb}>
          <textarea id={ids.blurb} className="dform-area" value={form.blurb} onChange={ev => set("blurb", ev.target.value)} rows={5}
            placeholder={isEvent ? "这件事是怎么发生的，留下了什么后果……" : "这份档案的描述……"} />
        </LibField>

        {!isEvent && (
          <section className="dossier-section">
            <div className="ws-section-label">关键信息</div>
            {facts.length > 0 && (
              <div className="dform-facts">
                {facts.map((f, i) => (
                  <div key={i} className="dform-fact">
                    <input className="dform-fact-key" value={f.k} onChange={ev => setFactK(i, ev.target.value)} placeholder="项目，如 年龄" aria-label={`第 ${i + 1} 项的名称`} />
                    <input className="dform-fact-val" value={f.v} onChange={ev => setFactV(i, ev.target.value)} placeholder="内容" aria-label={`第 ${i + 1} 项的内容`} />
                    <button type="button" className="dform-fact-del" onClick={() => delFact(i)} aria-label={`删除第 ${i + 1} 项`} title="删除这一项"><I.X size={12} /></button>
                  </div>
                ))}
              </div>
            )}
            <button type="button" className="dform-add" onClick={addFact}><I.Plus size={13} /> 添加一项</button>
          </section>
        )}

        {!isEvent && (
          <section className="dossier-section">
            <label className="ws-section-label" htmlFor={ids.tag}>标签</label>
            <div className="dform-tags">
              {form.tags.map(t => (
                <span key={t} className="dform-tag">
                  {t}
                  <button type="button" onClick={() => set("tags", form.tags.filter(x => x !== t))} aria-label={`移除标签 ${t}`} title="移除"><I.X size={11} /></button>
                </span>
              ))}
              <input
                id={ids.tag}
                className="dform-tag-input"
                value={tagInput}
                onChange={ev => setTagInput(ev.target.value)}
                onKeyDown={ev => { if (ev.key === "Enter" && !isImeComposing(ev)) { ev.preventDefault(); addTag(); } }}
                placeholder="输入后回车"
              />
            </div>
          </section>
        )}

        <section className="dossier-section">
          <div className="ws-section-label">{isEvent ? "相关档案" : "关联关系"}</div>
          <div className="dform-links">
            {links.map((l, i) => {
              const t = bid[l.id];
              const curType = l.type || LIB_relType(l).id;
              const tDef = LIB_REL_TYPES.find(x => x.id === curType) || LIB_relType(l);
              return (
                <div key={l.id} className={`dform-link ${libAccClass(t && t.accent)}`}>
                  <LibGlyph entry={t} glyph={t ? t.glyph : "?"} size="sm" />
                  <span className="dform-link-name">{t ? t.name : "已删除的档案"}</span>
                  {!isEvent && (
                    <>
                      <span className={`dform-link-typewrap acc-${tDef.accent}`}>
                        <span className="dform-link-typedot" aria-hidden="true" />
                        <select className="dform-link-type" value={curType} onChange={ev => setLinkType(i, ev.target.value)} aria-label={`与「${t ? t.name : ""}」的关系类型`}>
                          {!LIB_REL_TYPES.some(rt => rt.id === curType) && <option value={curType}>{tDef.label}</option>}
                          {LIB_REL_TYPES.map(rt => <option key={rt.id} value={rt.id}>{rt.label}</option>)}
                        </select>
                      </span>
                      <input
                        className="dform-link-rel"
                        value={l.rel || ""}
                        onChange={ev => setLinkRel(i, ev.target.value)}
                        placeholder="关系标签，可不填"
                        aria-label={`与「${t ? t.name : ""}」的关系标签`}
                      />
                    </>
                  )}
                  <button type="button" className="dform-link-del" onClick={() => delLink(i)} aria-label="移除这条关联" title="移除这条关联"><I.X size={12} /></button>
                </div>
              );
            })}
            {links.length === 0 && (
              <div className="dform-link-empty">{isEvent ? "还没有关联到人物或设定。" : "还没有关联。从下方添加，让它和别的档案连起来。"}</div>
            )}
          </div>
          <div className="dform-link-add">
            {!isEvent && (
              <select className="dform-link-type-add" value={addType} onChange={ev => setAddType(ev.target.value)} aria-label="新关联的关系类型">
                {LIB_REL_TYPES.map(rt => <option key={rt.id} value={rt.id}>{rt.label}</option>)}
              </select>
            )}
            <select id={ids.link} className="dform-link-select" value={addId} onChange={ev => setAddId(ev.target.value)} aria-label="要关联的档案">
              <option value="">选择要关联的档案…</option>
              {LIB_CATS.filter(c => c.id !== "events").map(c => {
                const opts = linkOptions.filter(x => x.cat === c.id);
                if (!opts.length) return null;
                return (
                  <optgroup key={c.id} label={c.label}>
                    {opts.map(x => <option key={x.id} value={x.id}>{x.name}</option>)}
                  </optgroup>
                );
              })}
            </select>
            <button type="button" className="btn btn-ghost btn-sm" disabled={!addId} onClick={addLink}><I.Plus size={13} /> 添加关联</button>
          </div>
        </section>
      </div>

      <footer className="dossier-foot">
        <button type="button" className="btn btn-accent btn-sm" disabled={!nameOk || saving} onClick={save}>
          <I.Save size={13} /> {saving ? "保存中…" : "保存"}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" disabled={saving} onClick={onCancel}>取消</button>
        <span className="spacer" />
        {!nameOk && <span className="dform-hint is-warn">名称不能为空</span>}
      </footer>
    </div>
  );
}

/* ---- 新建档案 · 轻量创建面板 ----
   onCreate(cat, name, { kind }) 返回 Promise（新条目的服务端 id 或 null）；在途时按钮禁用，失败留在原地。 */
function DossierCreate({ onCreate, onCancel }) {
  const [cat, setCat] = useEdSt(LIB_CATS[0].id);
  const [name, setName] = useEdSt("");
  const [kind, setKind] = useEdSt("location");
  const [busy, setBusy] = useEdSt(false);
  const uid = useEdId();
  const meta = LIB_CATS.find(c => c.id === cat) || LIB_CATS[0];
  const ok = name.trim().length > 0 && !busy;
  const submit = async () => {
    if (!ok) return;
    setBusy(true);
    try {
      await onCreate(cat, name.trim(), { kind });
    } finally {
      setBusy(false);
    }
  };
  const placeholder = cat === "people" ? "例如：一位新角色的名字" : cat === "events" ? "例如：大火那一夜" : "例如：旧城区的钟楼";

  return (
    <div className={`${libAccClass(meta.accent)} dcreate`}>
      <div className="dcreate-head">
        <h2 className="dcreate-title">新建一份档案</h2>
        <p className="dcreate-sub">先选类别、起个名字；建好后直接进入编辑，再补简述、关键信息与关联。</p>
      </div>

      <div className="dcreate-field">
        <div className="dcreate-label" id={`${uid}-cat`}>类别</div>
        <Segmented label="类别" value={cat} onChange={setCat} options={LIB_CATS.map(c => ({ value: c.id, label: c.label }))} />
      </div>

      {cat === "world" && (
        <div className="dcreate-field">
          <div className="dcreate-label">类型</div>
          <Segmented label="类型" value={kind} onChange={setKind} options={LIB_KIND_OPTIONS} />
        </div>
      )}

      <div className="dcreate-field">
        <label className="dcreate-label" htmlFor={`${uid}-name`}>名称</label>
        <input
          id={`${uid}-name`}
          className="dform-input dcreate-name-input"
          value={name}
          autoFocus
          onChange={ev => setName(ev.target.value)}
          onKeyDown={ev => { if (ev.key === "Enter" && !isImeComposing(ev)) submit(); }}
          placeholder={placeholder}
        />
      </div>

      <div className="dcreate-foot">
        <button type="button" className="btn btn-accent btn-sm" disabled={!ok} onClick={submit}>
          <I.Check size={13} /> {busy ? "正在创建…" : "创建并编辑"}
        </button>
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={onCancel}>取消</button>
      </div>
    </div>
  );
}

/* ---- 实时合并视图（写作器等模块共用） ----
   FE-ALIGN P6：LIB_ENTRIES 已按当前作品从后端装载，门控恒开
   （per-work 隔离由 API 保证）；LIB_live() 直接返回当前缓存，
   供正文实体高亮 / @提及等运行时消费，保证与资料库页面同源。 */
const LIB_seedOn = () => true;
function LIB_live() {
  const entries = (LIB_ENTRIES || []).slice();
  const byId = entries.reduce((m, e) => { m[e.id] = e; return m; }, {});
  return { entries, byId };
}

Object.assign(window, { LIB_loadEdits, LIB_persist, LIB_applyEdit, DossierEdit, DossierCreate, LIB_loadAdds, LIB_persistAdds, LIB_newEntry, LIB_seedOn, LIB_live });

/* ESM 导出；上面的 window.* 赋值是写作台等过渡期模块还在读的契约，别删 */
export { LIB_loadEdits, LIB_migrateLegacy, LIB_persist, LIB_applyEdit, DossierEdit, DossierCreate, LIB_loadAdds, LIB_persistAdds, LIB_newEntry, LIB_seedOn, LIB_live, LIB_deleteEntry, LIB_createEntry };
