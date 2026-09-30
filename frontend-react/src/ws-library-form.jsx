import React from "react";
import { I } from "./icons.jsx";
import { LIB_CATS, LIB_KIND_OPTIONS } from "./labels/library.js";
import { LIB_REL_TYPES, LIB_chapterLabel, LIB_relType } from "./ws-library-derive.js";
import { isImeComposing } from "./lib/keyboard.js";
import { LibGlyph, libAccClass, libCatLabel } from "./ws-library-parts.jsx";
import { Segmented } from "./ws-ui.jsx";

const { useState, useEffect, useMemo, useId } = React;

/* ==========================================================
   资料 · 编辑 / 新建表单（2026-09-30 从 ws-library-edit.jsx 拆出，审计 F05-08 / F01-18）
   · DossierEdit：一份档案的编辑表单，保存时把整份表单交给 onSave（视图经 store 的 LIB_persist 写后端）；
   · DossierCreate：选类别、起名字，建好后视图直接打开编辑。
   表单只提供后端真能存下的字段——存不下的字段宁可不给，也不让作者白填。纯 ESM，不写 window。
   ========================================================== */

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
  const [form, setForm] = useState(() => libInitialForm(e));
  const [initial] = useState(() => libFormSnapshot(libInitialForm(e)));
  const [tagInput, setTagInput] = useState("");
  const [addId, setAddId] = useState("");
  const [addType, setAddType] = useState("kin");
  const [saving, setSaving] = useState(false);
  const uid = useId();
  const isEvent = e.cat === "events";
  const ents = allEntries || [];
  const bid = byId || {};
  const set = (key, value) => setForm(f => ({ ...f, [key]: value }));

  const dirty = libFormSnapshot(form) !== initial || tagInput.trim() !== "";
  useEffect(() => { if (onDirtyChange) onDirtyChange(dirty); }, [dirty]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (onDirtyChange) onDirtyChange(false); }, []); // eslint-disable-line react-hooks/exhaustive-deps

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

  const chapterOptions = useMemo(() => {
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
  const [cat, setCat] = useState(LIB_CATS[0].id);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("location");
  const [busy, setBusy] = useState(false);
  const uid = useId();
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

export { DossierEdit, DossierCreate };
