import React from "react";
import { I } from "./icons.jsx";
import { S2AutoText } from "./ws-snow-fields.jsx";
import { S2CharFillButton, S2CharTabs } from "./ws-snow-editor-parts.jsx";

/* ==========================================================
   雪花编辑器 · 角色轨（2026-09-29 从 ws-snow-scaffolds.jsx 拆出）
   ----------------------------------------------------------
   04 角色摘要表（名册的唯一真相源）、06 角色背景与 08 角色全档案（按角色分栏的深档，名册继承 04）。
   ========================================================== */

/* ---- 04 角色摘要表：名册的唯一真相源 ---- */
const S2_CHAR_FIELDS = [
  { f: "role",     label: "角色",          hint: "主角 / 对立面 / 导师 / 帮手…", short: true },
  { f: "goal",     label: "目标（具体）",  hint: "这个故事里她要的、看得见的东西" },
  { f: "ambition", label: "抱负（抽象）",  hint: "她对人生说不出口的渴望" },
  { f: "conflict", label: "阻碍",          hint: "什么挡在她和目标之间" },
  // 阶段 D：价值观按书里的句式一行一条（2–3 条，互相有张力）；脚手架仍存一个字符串，换行分隔
  { f: "values",   label: "价值观",        hint: "「没有什么比 ___ 更重要」写 2–3 条，互相有张力——主角和对手这句话必须冲突", kind: "values", prefix: "没有什么比", suffix: "更重要", wide: true },
  { f: "epiphany", label: "顿悟",          hint: "故事结束时她学到什么（反派常无）" },
  // 阶段 D：书里的角色表还有两栏——这个角色自己的一句话 / 一段话故事线（规范键 one_sentence_summary / one_paragraph_summary）
  { f: "storyline",      label: "一句话故事线", hint: "她自己的故事，一句话：要什么、谁挡着、代价是什么", wide: true },
  { f: "storyline_para", label: "一段话故事线", hint: "扩成一段：她怎样进入故事、三次灾难怎样打在她身上、她的结局，以及她能生出哪些场景", rows: 3, wide: true },
];
/* 价值观列表：一行一条「没有什么比 ___ 更重要」。脚手架里仍是一个字符串（换行分隔），
   canonFromFE 上行时才拆成数组并补全句式——旧缓存里的单行字符串自然成为第一条。 */
function S2ValuesList({ value, prefix, suffix, onChange }) {
  const rows = String(value || "").split("\n");
  const setLine = (i, v) => { const next = [...rows]; next[i] = v.replace(/\n/g, " "); onChange(next.join("\n")); };
  const addLine = () => onChange([...rows, ""].join("\n"));
  const delLine = (i) => { const next = rows.filter((_, j) => j !== i); onChange((next.length ? next : [""]).join("\n")); };
  return (
    <div className="sf-values">
      {rows.map((line, i) => (
        <span key={i} className="sf-field-affix sf-values-row">
          <span className="sf-affix">{prefix}</span>
          <input className="sf-field-input" value={line} placeholder={i === 0 ? "真相" : "…与上一条有张力"} onChange={(e) => setLine(i, e.target.value)} />
          <span className="sf-affix">{suffix}</span>
          {rows.length > 1 && <button type="button" className="sf-values-del" onClick={() => delLine(i)} title="删除这条价值观"><I.X size={12} /></button>}
        </span>
      ))}
      <button type="button" className="sf-values-add" onClick={addLine} title="价值观要互相有张力——主角和对手的这句话必须冲突"><I.Plus size={12} /> 再加一条</button>
    </div>
  );
}
export function S2CharSheet({ scaffold, onScaffold, ai }) {
  const ids = Object.keys(scaffold.chars);
  const sel = scaffold.chars[scaffold.sel] ? scaffold.sel : ids[0];
  const ch = scaffold.chars[sel] || {};
  const setField = (f, v) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...s.chars[sel], [f]: v } } }));
  const selectChar = (id) => onScaffold(s => ({ ...s, sel: id }));
  const addChar = () => onScaffold(s => {
    let n = 1; while (s.chars["c" + n]) n++;
    const id = "c" + n;
    return { ...s, sel: id, chars: { ...s.chars, [id]: { name: "新角色", role: "次要", goal: "", ambition: "", values: "", conflict: "", epiphany: "", storyline: "", storyline_para: "" } } };
  });
  const delChar = () => {
    if (ids.length <= 1) { window.alert("至少保留一个角色。"); return; }
    if (!window.confirm(`删除角色「${ch.name || "未命名"}」？06 / 08 中她的深档字段会保留但不再展示。`)) return;
    onScaffold(s => {
      const chars = { ...s.chars }; delete chars[sel];
      return { ...s, sel: Object.keys(chars)[0], chars };
    });
  };
  return (
    <div className="sf-scaffold sf-charsheet">
      <p className="sf-scaffold-rule"><I.Users size={13} /> 全书的角色名册只在这里增删、改名；06 角色背景与 08 角色全档案都继承这份名册。</p>
      <S2CharTabs sel={sel} onSelect={selectChar} onAdd={addChar} addTitle="添加角色（06/08 名册同步继承）"
        tabs={ids.map(id => ({ id, name: scaffold.chars[id].name, sub: scaffold.chars[id].role }))} />
      {/* 阶段 L：全书主角——每一场的挫折 / 胜利以此人衡量；双主角时由作者定，不再猜「第一个主角」 */}
      <label className="sf-field is-short sf-char-protagonist" data-testid="snow-protagonist">
        <span className="sf-field-label">全书主角<span className="sf-field-hint">每场的挫败以此人衡量；双主角时选结局归属的那一个</span></span>
        <select className="sf-field-input" value={scaffold.protagonist || ""} onChange={(e) => onScaffold(s => ({ ...s, protagonist: e.target.value }))}>
          <option value="">（按定位自动：第一个「主角」）</option>
          {ids.map(id => <option key={id} value={id}>{(scaffold.chars[id] || {}).name || id}</option>)}
        </select>
      </label>
      <div className="sf-chardeep-head">
        <input className="sf-chardeep-name" value={ch.name || ""} placeholder="角色名"
          onChange={(e) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...s.chars[sel], name: e.target.value } } }))} />
        <S2CharFillButton ai={ai} id={sel} name={ch.name} />
        <button className="btn btn-quiet btn-sm" onClick={delChar} title="删除这个角色"><I.X size={13} /> 删除角色</button>
      </div>
      <div className="sf-fields">
        {S2_CHAR_FIELDS.map(fl => {
          const Wrap = fl.kind === "values" ? "div" : "label";
          return (
            <Wrap key={fl.f} className={`sf-field ${fl.short ? "is-short" : ""} ${fl.wide ? "is-wide" : ""}`}>
              <span className="sf-field-label">{fl.label}<span className="sf-field-hint">{fl.hint}</span></span>
              {fl.kind === "values" ? (
                <S2ValuesList value={ch[fl.f] || ""} prefix={fl.prefix} suffix={fl.suffix} onChange={(v) => setField(fl.f, v)} />
              ) : fl.rows ? (
                <S2AutoText className="sf-field-input sf-field-text" minRows={fl.rows} value={ch[fl.f] || ""} onChange={(e) => setField(fl.f, e.target.value)} placeholder={`写「${fl.label}」…`} />
              ) : (
                <input className="sf-field-input" value={ch[fl.f] || ""} onChange={(e) => setField(fl.f, e.target.value)} />
              )}
            </Wrap>
          );
        })}
      </div>
    </div>
  );
}

/* ---- 06 角色背景 / 08 角色全档案：按角色分栏的深档编辑器（共用） ---- */
export const S2_BACKSTORY_FIELDS = [
  { f: "belief",   label: "信念起点",   hint: "故事开始前她相信什么？怎么形成的？" },
  { f: "wound",    label: "第一道裂缝", hint: "哪件事第一次动摇了她——她的旧伤" },
  { f: "desire",   label: "内心渴望",   hint: "她真正渴望的是什么？为何渴望" },
  { f: "fear",     label: "隐秘恐惧",   hint: "最怕被人发现什么——故事将击中的靶心" },
  { f: "relation", label: "关系与行为", hint: "与其他角色的纠葛；压力下她会怎么做" },
  // 阶段 D：书里的第 5 步——从每个角色的视角把整本书讲一遍（规范值是 synopsis 里的第六个前缀行「视角故事：」）
  { f: "povstory",  label: "视角故事",   hint: "从她的视角把整个故事讲一遍：她看见什么、以为什么、要什么、付出什么——半页到一页", rows: 5, accent: true },
];
export const S2_PROFILE_FIELDS = [
  { f: "physical",      label: "生理",       hint: "外貌、习惯、标志性细节" },
  { f: "psych",         label: "心理",       hint: "核心恐惧、渴望、创伤" },
  { f: "environment",   label: "环境",       hint: "家庭、工作、人际" },
  { f: "personality",   label: "性格",       hint: "口头禅、矛盾面" },
  { f: "contradiction", label: "内在矛盾",   hint: "嘴上说的 vs 实际做的", accent: true },
  { f: "views",         label: "两个版本的她", hint: "别人眼中的她 ／ 她自己眼中的她", accent: true },
];
const S2_ROLE_TONE = { "主角": "crimson", "对立面": "gold", "次要": "slate", "导师": "slate", "帮手": "sage" };
export function S2CharDeep({ scaffold, onScaffold, fields, roster, go, ai }) {
  /* 名册的唯一真相源是 04 角色摘要表；本步只存自己这一层的深档字段。
     （本地遗留的、不在 04 名册里的角色仍展示，但标记出来） */
  const rosterChars = (roster && roster.chars) || {};
  const rosterIds = Object.keys(rosterChars);
  const localIds = Object.keys(scaffold.chars || {});
  const legacyIds = localIds.filter(id => !rosterChars[id] && fields.some(fl => ((scaffold.chars[id] || {})[fl.f] || "").trim()));
  const ids = [...rosterIds, ...legacyIds];
  const sel = ids.includes(scaffold.sel) ? scaffold.sel : ids[0];
  const ch = (scaffold.chars || {})[sel] || {};
  const meta = rosterChars[sel] || ch; // name/role 优先取 04
  const setField = (f, v) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...(s.chars[sel] || {}), [f]: v } } }));
  const filledCount = (id) => fields.filter(fl => (((scaffold.chars || {})[id] || {})[fl.f] || "").trim()).length;
  if (!ids.length) {
    return (
      <div className="sf-scaffold sf-chardeep">
        <div className="sf-plan-empty">
          <I.Users size={20} />
          <div>
            <div className="fw-600">名册还是空的</div>
            <div className="text-muted text-sm">角色名册由 04 角色摘要表统一管理——先去那里立人。</div>
          </div>
          <button className="btn btn-primary btn-sm" onClick={() => go && go("characters")}>去 04 · 角色摘要表</button>
        </div>
      </div>
    );
  }
  return (
    <div className="sf-scaffold sf-chardeep">
      <div className="sf-roster">
        <div className="sf-roster-lead"><I.Users size={12} /> 角色花名册 · {ids.length} 人<span className="sf-roster-src">名册与姓名由 04 统一管理</span></div>
        <S2CharTabs sel={sel} onSelect={(id) => onScaffold(s => ({ ...s, sel: id }))} onAdd={() => go && go("characters")} addTitle="名册由 04 管理——去 04 添加角色"
          tabs={ids.map(id => {
            const m = rosterChars[id] || (scaffold.chars || {})[id] || {};
            return { id, name: m.name, tone: S2_ROLE_TONE[m.role] || "slate", legacy: !rosterChars[id], sub: `${m.role || "—"} · ${filledCount(id)}/${fields.length}` };
          })} />
      </div>
      <div className="sf-chardeep-head">
        <span className="sf-chardeep-name is-ro" title="姓名与定位继承自 04 角色摘要表">{meta.name || "未命名"}</span>
        <span className="sf-chardeep-role is-ro">{meta.role || "—"}</span>
        <S2CharFillButton ai={ai} id={sel} name={meta.name} />
        <button className="sf-lineage" onClick={() => go && go("characters")} title="改名 / 改定位 / 增删角色，都在 04">
          <I.ArrowRight size={11} style={{ transform: "rotate(180deg)" }} /> 名册管理在 04
        </button>
      </div>
      <div className="sf-deep-fields">
        {fields.map(fl => (
          <label key={fl.f} className={`sf-deep-field ${fl.accent ? "is-accent" : ""}`}>
            <span className="sf-field-label">{fl.label}<span className="sf-field-hint">{fl.hint}</span></span>
            <S2AutoText className="sf-deep-text" minRows={fl.rows || 2} value={ch[fl.f] || ""} onChange={(e) => setField(fl.f, e.target.value)} placeholder={`写「${fl.label}」…`} />
          </label>
        ))}
      </div>
    </div>
  );
}
