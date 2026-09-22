import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { chapterActRuns, chapterNoInTitle } from "./ws-snow-chapters-model.js";
import { S2AutoText } from "./ws-snow-fields.jsx";
import { S2SceneList } from "./ws-snow-scene-list.jsx";
import { S2ScenePlan } from "./ws-snow-scene-plan.jsx";
import { S2_SPINE_OPTS, s2BusyOn } from "./ws-snow-model.js";

/* ==========================================================
   雪花十步的编辑器（编辑页）
   ----------------------------------------------------------
   02 一句话概括是自由文本（带长度尺）；其余九步是结构化脚手架：01 读者定位、03 五句骨架、
   04 角色摘要表、05 一页梗概、06 / 08 按角色分栏的深档、07 五段展开 + 章节表；09 在
   ws-snow-scene-list.jsx，10 在 ws-snow-scene-plan.jsx。S2StepEditor 按步骤挑编辑器，并对无关的重渲染（同步状态、健康、回执）免疫。
   ========================================================== */

/* 编辑页的编辑器：有脚手架的步骤用脚手架（旧版遗留的自由草稿在上方明示），否则是自由文本。
   memo：只有这一步的内容、引用的上游脚手架或 AI 忙态变了才重渲染（回调都是稳定引用）。 */
export const S2StepEditor = React.memo(function S2StepEditor({ step, data, draft, setDraft, scaffold, onScaffold, refs, go, ai, onOpenChapterPlan, catalogHasChapters }) {
  if (!data.scaffold) {
    return <S2Edit draft={draft} setDraft={setDraft} stepName={step.name} target={data.target} meter={data.meter} />;
  }
  return (
    <React.Fragment>
      {draft.trim() ? <S2DraftOverride draft={draft} setDraft={setDraft} stepName={step.name} /> : null}
      <S2Scaffold kind={data.scaffold.type} scaffold={scaffold} onScaffold={onScaffold} refs={refs} go={go} ai={ai} onOpenChapterPlan={onOpenChapterPlan} catalogHasChapters={catalogHasChapters} />
    </React.Fragment>
  );
});

/* ====== Freeform editor (+ optional word meter) ====== */
function S2Edit({ draft, setDraft, stepName, target, meter }) {
  // 字数只报一处、只有一个口径：有长度尺就看尺子，没有才在工具行里报（以前工具行「目标约 60」、尺子「/ 42」、页头「≤25 词」三个数并存）
  const len = draft.replace(/\s+/g, "").length;
  return (
    <div className="edit-pane">
      {meter
        ? <S2Meter len={len} target={meter.target} note={meter.note} />
        : <div className="edit-toolbar"><div className="text-muted text-sm">{len} 字{target ? ` · 建议约 ${target} 字` : ""}</div></div>}
      <textarea className="edit-text" aria-label={stepName} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={`在这里写「${stepName}」…`} />
    </div>
  );
}

function S2Meter({ len, target, note }) {
  const pct = Math.min(100, (len / target) * 100);
  const over = len > target;
  return (
    <div className={`sf-meter ${over ? "is-over" : ""}`}>
      <div className="sf-meter-track"><div className="sf-meter-fill" style={{ width: pct + "%" }} /><div className="sf-meter-cap" style={{ left: "100%" }} /></div>
      <div className="sf-meter-foot">
        <span className="sf-meter-count">{len} / {target} 字{over ? "，偏长，再砍一刀" : ""}</span>
        <span className="sf-meter-note">{note}</span>
      </div>
    </div>
  );
}

/* 旧版「仅作草稿」留下的自由草稿，在有脚手架的步骤上可见可编可退——
   它会优先于脚手架参与评分 / 引用 / 导出，所以必须明示，不能藏在水面下。
   阶段 U 起不再有新入口写它（方向一律「按此生成本步」进脚手架）；清掉即回到脚手架。 */
function S2DraftOverride({ draft, setDraft, stepName }) {
  const clear = () => {
    if (!window.confirm(`清除这段自由草稿？本步将回到结构化脚手架作为唯一内容源。`)) return;
    setDraft("");
  };
  return (
    <div className="sf-dov">
      <div className="sf-dov-head">
        <span className="sf-dov-tag"><I.Wand size={12} /> 自由草稿（旧版采纳候选所得）</span>
        <span className="sf-dov-note">只要这段非空，本步的评分、引用与导出都优先用它，而非下方脚手架。</span>
        <button className="btn btn-quiet btn-sm" onClick={clear} title="清除草稿，回到脚手架"><I.X size={12} /> 清除草稿</button>
      </div>
      <textarea className="sf-dov-text" rows={4} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={`「${stepName}」的自由草稿…`} />
    </div>
  );
}

/* ====== Structured scaffolds ====== */
/* 脚手架上方原来各有一条「说明」横幅，大多是在复述右栏的「本步任务」；现在只留那些说出数据规则的话
   （名册归 04 管、章表可以留空）。 */
function S2Scaffold({ kind, scaffold, onScaffold, refs, go, ai, onOpenChapterPlan, catalogHasChapters }) {
  return (
    <div className="edit-pane">
      {kind === "beats" && <S2Beats scaffold={scaffold} onScaffold={onScaffold} />}
      {kind === "audience" && <S2Audience scaffold={scaffold} onScaffold={onScaffold} />}
      {kind === "charsheet" && <S2CharSheet scaffold={scaffold} onScaffold={onScaffold} ai={ai} />}
      {kind === "synopsisbeats" && <S2SynopsisBeats scaffold={scaffold} onScaffold={onScaffold} refs={refs} />}
      {kind === "chapters" && <S2ChapterOutline scaffold={scaffold} onScaffold={onScaffold} refs={refs} onOpenChapterPlan={onOpenChapterPlan} catalogHasChapters={catalogHasChapters} />}
      {kind === "backstory" && <S2CharDeep scaffold={scaffold} onScaffold={onScaffold} ai={ai} fields={S2_BACKSTORY_FIELDS} roster={(refs && refs.characters) || null} go={go} />}
      {kind === "profile" && <S2CharDeep scaffold={scaffold} onScaffold={onScaffold} ai={ai} fields={S2_PROFILE_FIELDS} roster={(refs && refs.characters) || null} go={go} />}
      {kind === "scenelist" && <S2SceneList scaffold={scaffold} onScaffold={onScaffold} refs={refs} onOpenChapterPlan={onOpenChapterPlan} />}
      {kind === "scene" && <S2ScenePlan scaffold={scaffold} onScaffold={onScaffold} refs={refs} go={go} ai={ai} />}
    </div>
  );
}

/* ---- 03 一段话概括：五句骨架 + 中点的道德前提翻转 ---- */
const S2_BEATS = [
  { f: "setup",      label: "铺垫",   act: "开场",      desc: "交代背景，引入 1–2 位主角" },
  { f: "d1",         label: "灾难一", act: "第一幕末",  desc: "逼主角入局、做出承诺", tone: "crimson" },
  { f: "d2",         label: "灾难二", act: "第二幕中点", desc: "道德前提翻转：错误信念 → 正确信念", tone: "gold", flip: true },
  { f: "d3",         label: "灾难三", act: "第二幕末",  desc: "逼主角（与反派）走向终局", tone: "crimson" },
  { f: "resolution", label: "结局",   act: "第三幕",    desc: "终极对决 + 收束（喜 / 悲 / 苦甜）" },
];
function S2Beats({ scaffold, onScaffold }) {
  return (
    <div className="sf-scaffold sf-beats">
      {S2_BEATS.map((b, i) => (
        <div key={b.f} className={`sf-beat ${b.tone ? `tone-${b.tone}` : ""}`}>
          <div className="sf-beat-side">
            <span className="sf-beat-idx">{i + 1}</span>
            <span className="sf-beat-act">{b.act}</span>
          </div>
          <div className="sf-beat-main">
            <label className="sf-beat-label" htmlFor={`sf-beat-${b.f}`}>{b.label}<span className="sf-beat-desc">{b.desc}</span></label>
            <S2AutoText id={`sf-beat-${b.f}`} className="sf-beat-text" minRows={2} value={scaffold[b.f] || ""}
              onChange={(e) => onScaffold(s => ({ ...s, [b.f]: e.target.value }))} placeholder={`写「${b.label}」…`} />
            {b.flip && (
              <div className="sf-premise-flip">
                <span className="sf-pf-tag">道德前提</span>
                <input className="sf-pf-input is-false" aria-label="错误信念" value={scaffold.premiseF || ""} onChange={(e) => onScaffold(s => ({ ...s, premiseF: e.target.value }))} placeholder="错误信念…" />
                <I.ChevronRight size={13} />
                <input className="sf-pf-input is-true" aria-label="正确信念" value={scaffold.premiseT || ""} onChange={(e) => onScaffold(s => ({ ...s, premiseT: e.target.value }))} placeholder="正确信念…" />
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

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
function S2CharSheet({ scaffold, onScaffold, ai }) {
  const ids = Object.keys(scaffold.chars);
  const sel = scaffold.chars[scaffold.sel] ? scaffold.sel : ids[0];
  const ch = scaffold.chars[sel] || {};
  const setField = (f, v) => onScaffold(s => ({ ...s, chars: { ...s.chars, [sel]: { ...s.chars[sel], [f]: v } } }));
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
      <div className="sf-char-tabs">
        {ids.map(id => {
          const c = scaffold.chars[id];
          return (
            <button key={id} className={`sf-char-tab ${sel === id ? "is-sel" : ""}`} onClick={() => onScaffold(s => ({ ...s, sel: id }))}>
              <span className="sf-char-av text-serif">{(c.name || "?")[0]}</span>
              <span className="sf-char-tab-body"><span className="sf-char-tab-name">{c.name || "未命名"}</span><span className="sf-char-tab-role">{c.role}</span></span>
            </button>
          );
        })}
        <button className="sf-char-add" onClick={addChar} title="添加角色（06/08 名册同步继承）"><I.Plus size={15} /></button>
      </div>
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
        {ai && (
          <button className="btn btn-quiet btn-sm" disabled={ai.structBusy} onClick={() => ai.onFillChar(sel, ch.name)}
            title="只让 AI 补全当前选中的这个角色——其余角色保持不动（依据上游材料，与其他角色保持一致）">
            <I.Wand size={13} className={s2BusyOn(ai, "fill_char", sel) ? "sf-spin" : ""} /> {s2BusyOn(ai, "fill_char", sel) ? "生成中…" : "AI 补全此角色"}
          </button>
        )}
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
const S2_BACKSTORY_FIELDS = [
  { f: "belief",   label: "信念起点",   hint: "故事开始前她相信什么？怎么形成的？" },
  { f: "wound",    label: "第一道裂缝", hint: "哪件事第一次动摇了她——她的旧伤" },
  { f: "desire",   label: "内心渴望",   hint: "她真正渴望的是什么？为何渴望" },
  { f: "fear",     label: "隐秘恐惧",   hint: "最怕被人发现什么——故事将击中的靶心" },
  { f: "relation", label: "关系与行为", hint: "与其他角色的纠葛；压力下她会怎么做" },
  // 阶段 D：书里的第 5 步——从每个角色的视角把整本书讲一遍（规范值是 synopsis 里的第六个前缀行「视角故事：」）
  { f: "povstory",  label: "视角故事",   hint: "从她的视角把整个故事讲一遍：她看见什么、以为什么、要什么、付出什么——半页到一页", rows: 5, accent: true },
];
const S2_PROFILE_FIELDS = [
  { f: "physical",      label: "生理",       hint: "外貌、习惯、标志性细节" },
  { f: "psych",         label: "心理",       hint: "核心恐惧、渴望、创伤" },
  { f: "environment",   label: "环境",       hint: "家庭、工作、人际" },
  { f: "personality",   label: "性格",       hint: "口头禅、矛盾面" },
  { f: "contradiction", label: "内在矛盾",   hint: "嘴上说的 vs 实际做的", accent: true },
  { f: "views",         label: "两个版本的她", hint: "别人眼中的她 ／ 她自己眼中的她", accent: true },
];
const S2_ROLE_TONE = { "主角": "crimson", "对立面": "gold", "次要": "slate", "导师": "slate", "帮手": "sage" };
function S2CharDeep({ scaffold, onScaffold, fields, roster, go, ai }) {
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
        <div className="sf-char-tabs">
          {ids.map(id => {
            const m = rosterChars[id] || (scaffold.chars || {})[id] || {};
            const fc = filledCount(id);
            return (
              <button key={id} className={`sf-char-tab tone-${S2_ROLE_TONE[m.role] || "slate"} ${sel === id ? "is-sel" : ""}`} onClick={() => onScaffold(s => ({ ...s, sel: id }))}>
                <span className="sf-char-av text-serif">{(m.name || "?")[0]}</span>
                <span className="sf-char-tab-body">
                  <span className="sf-char-tab-name">{m.name || "未命名"}{!rosterChars[id] && <em className="sf-char-legacy" title="这个角色不在 04 名册里（历史数据）">·遗留</em>}</span>
                  <span className="sf-char-tab-role">{m.role || "—"} · {fc}/{fields.length}</span>
                </span>
              </button>
            );
          })}
          <button className="sf-char-add" onClick={() => go && go("characters")} title="名册由 04 管理——去 04 添加角色"><I.Plus size={15} /></button>
        </div>
      </div>
      <div className="sf-chardeep-head">
        <span className="sf-chardeep-name is-ro" title="姓名与定位继承自 04 角色摘要表">{meta.name || "未命名"}</span>
        <span className="sf-chardeep-role is-ro">{meta.role || "—"}</span>
        {ai && (
          <button className="btn btn-quiet btn-sm" disabled={ai.structBusy} onClick={() => ai.onFillChar(sel, meta.name)}
            title="只让 AI 补全当前选中的这个角色——其余角色保持不动（依据上游材料，与其他角色保持一致）">
            <I.Wand size={13} className={s2BusyOn(ai, "fill_char", sel) ? "sf-spin" : ""} /> {s2BusyOn(ai, "fill_char", sel) ? "生成中…" : "AI 补全此角色"}
          </button>
        )}
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

/* ---- 01 读者定位：类型 / 读者画像 / 核心快感 / 来源 / 反向定位 ---- */
const S2_AUD_GENRES = ["文学悬疑", "言情", "硬核推理", "科幻", "奇幻", "历史", "青春", "惊悚"];
const S2_AUD_FIELDS = [
  { f: "reader",   label: "读者画像", hint: "谁？年龄、阅读口味、她为何被这种故事吸引", rows: 2 },
  { f: "pleasure", label: "核心快感", hint: "用「她读完会觉得 ___」一句话锁定", rows: 2, accent: true },
  { f: "source",   label: "快感来源", hint: "这种快感具体从哪来——叙述、主题、节奏？", rows: 2 },
  { f: "emotion",  label: "期待读者情绪", hint: "压力升级中，读者持续感到什么——揪心、压迫、向前的拉力？", rows: 2 },
  { f: "stance",   label: "叙述人称与时态", hint: "全书用什么人称、什么时态、视角纪律——如「第三人称限知，过去时，每场固定一个视角人物」；起草时有约束力", rows: 1 },
  { f: "exclude",  label: "反向定位", hint: "「我不为谁写 / 不写什么」——砍掉犹豫", rows: 2, danger: true },
];
function S2Audience({ scaffold, onScaffold }) {
  const set = (f, v) => onScaffold(s => ({ ...s, [f]: v }));
  const filled = ["genre", ...S2_AUD_FIELDS.map(f => f.f)].filter(k => (scaffold[k] || "").trim()).length;
  return (
    <div className="sf-scaffold sf-audience">
      <div className="sf-aud-genre">
        <div className="sf-field-label">类型<span className="sf-field-hint">类型决定读者带着什么期待打开书</span></div>
        <div className="sf-genre-chips">
          {S2_AUD_GENRES.map(g => (
            <button key={g} className={`sf-genre-chip ${scaffold.genre === g ? "is-sel" : ""}`} onClick={() => set("genre", g)}>{g}</button>
          ))}
          <input className="sf-genre-other" value={S2_AUD_GENRES.includes(scaffold.genre) ? "" : (scaffold.genre || "")} onChange={(e) => set("genre", e.target.value)} placeholder="其他…" />
        </div>
      </div>

      <div className="sf-aud-fields">
        {S2_AUD_FIELDS.map(fl => (
          <label key={fl.f} className={`sf-deep-field ${fl.accent ? "is-accent" : ""} ${fl.danger ? "is-danger" : ""}`}>
            <span className="sf-field-label">{fl.label}<span className="sf-field-hint">{fl.hint}</span></span>
            <S2AutoText className="sf-deep-text" minRows={fl.rows} value={scaffold[fl.f] || ""} onChange={(e) => set(fl.f, e.target.value)} placeholder={`写「${fl.label}」…`} />
          </label>
        ))}
      </div>

      <div className="sf-aud-foot">
        <span className={`sf-aud-prog ${filled === S2_AUD_FIELDS.length + 1 ? "is-all" : ""}`}><I.Target size={11} /> 定位完成度 {filled} / {S2_AUD_FIELDS.length + 1}</span>
        {scaffold.genre && scaffold.pleasure ? (
          <span className="sf-aud-seal"><I.Check size={11} /> 已锚定：<b>{scaffold.genre}</b> · 取悦「{(scaffold.pleasure || "").slice(0, 14)}…」的读者</span>
        ) : (
          <span className="sf-aud-seal is-pending">把类型和核心快感都填上，定位才算锚定</span>
        )}
      </div>
    </div>
  );
}

/* ---- 05 一页梗概：五段，每段锚定 03 的一句脊柱节拍（1→5 分形展开可见） ---- */
const S2_SYN_BEATS = [
  { f: "setup",      label: "铺垫",   ref: "setup", tone: "slate",   desc: "世界观与初始处境" },
  { f: "d1",         label: "灾难一", ref: "d1",    tone: "crimson", desc: "触发事件 · 第一幕末" },
  { f: "d2",         label: "灾难二", ref: "d2",    tone: "gold",    desc: "认知翻转 · 中点" },
  { f: "d3",         label: "灾难三", ref: "d3",    tone: "crimson", desc: "升级 · 第二幕末" },
  { f: "resolution", label: "结局",   ref: "resolution", tone: "slate", desc: "高潮走向与收尾" },
];
function S2SynopsisBeats({ scaffold, onScaffold, refs }) {
  const paras = scaffold.paras || {};
  const para03 = (refs && refs.paragraph) || {};
  const setPara = (f, v) => onScaffold(s => ({ ...s, paras: { ...s.paras, [f]: v } }));
  const filled = S2_SYN_BEATS.filter(b => (paras[b.f] || "").trim()).length;
  return (
    <div className="sf-scaffold sf-synopsis">
      <div className="sf-syn-prog">
        <span className="sf-syn-prog-c"><b>{filled}</b> / 5 段已展开</span>
        <div className="sf-syn-track">{S2_SYN_BEATS.map(b => <span key={b.f} className={`sf-syn-tick tone-${b.tone} ${(paras[b.f] || "").trim() ? "is-on" : ""}`} />)}</div>
      </div>
      {S2_SYN_BEATS.map((b, i) => {
        const src = para03[b.ref] || "";
        const expanded = (paras[b.f] || "");
        const grew = expanded.replace(/\s/g, "").length > src.replace(/\s/g, "").length;
        return (
          <div key={b.f} className={`sf-syn-row tone-${b.tone}`}>
            <div className="sf-syn-side">
              <span className="sf-syn-idx">{i + 1}</span>
              <span className="sf-syn-label">{b.label}</span>
              <span className="sf-syn-desc">{b.desc}</span>
            </div>
            <div className="sf-syn-main">
              <div className="sf-syn-src" title="展开自 03 一段话概括的这一句">
                <span className="sf-syn-src-tag"><I.ArrowRight size={10} style={{ transform: "rotate(180deg)" }} /> 展开自 03</span>
                <span className="sf-syn-src-text">{src || <em className="sf-syn-empty">（03 这一拍还没写）</em>}</span>
              </div>
              <S2AutoText className="sf-syn-text" minRows={3} value={expanded} onChange={(e) => setPara(b.f, e.target.value)} aria-label={`${b.label}：扩成一段`} placeholder={`把「${b.label}」扩成一段有画面的梗概…`} />
              {expanded.trim() && (
                <div className={`sf-syn-meta ${grew ? "is-ok" : "is-warn"}`}>
                  {grew ? <><I.Check size={10} /> 已展开（{expanded.replace(/\s/g, "").length} 字 &gt; 源句 {src.replace(/\s/g, "").length}）</> : <><I.AlertTriangle size={10} /> 还没比源句长——再填点画面</>}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ---- 07 长篇大纲：五段展开 + 三幕章节表（章表可留空——章是列完场之后的包装决定，阶段 K / V）---- */
const S2_ACTS = [
  { act: 1, label: "第一幕", desc: "铺垫 → 灾难一", tone: "slate" },
  { act: 2, label: "第二幕", desc: "灾难二（中点翻转）", tone: "gold" },
  { act: 3, label: "第三幕", desc: "灾难三 → 收尾", tone: "crimson" },
];
function S2ChapterOutline({ scaffold, onScaffold, refs, onOpenChapterPlan, catalogHasChapters }) {
  const chapters = scaffold.chapters || [];
  /* 阶段 D：书里的第 6 步——05 的每一段再扩成约一页（五段展开）；章节表在它下面，仍是分章的真相。 */
  const expansions = scaffold.expansions || {};
  const syn05 = ((refs && refs.synopsis) || {}).paras || {};
  const setExp = (f, v) => onScaffold(s => ({ ...s, expansions: { ...(s.expansions || {}), [f]: v } }));
  const expFilled = S2_SYN_BEATS.filter(b => (expansions[b.f] || "").trim()).length;
  const setCh = (id, f, v) => onScaffold(s => ({ ...s, chapters: s.chapters.map(c => c.id === id ? { ...c, [f]: v } : c) }));
  const delCh = (id) => onScaffold(s => ({ ...s, chapters: s.chapters.filter(c => c.id !== id) }));
  /* 章表按数组顺序显示（= 上行的 chapter_seq = 分章面板看到的顺序），幕只是分段标签。以前按幕重新分组显示，
     在第二幕已有章时「添加第一幕章节」会显示在第一幕、却按数组末尾存成最后一章——所见非所存。
     现在新章插在同一幕最后一章之后（这一幕还没有章时，插在它前一幕的最后一章之后）。 */
  const addCh = (act) => onScaffold(s => {
    const list = s.chapters || [];
    const max = list.reduce((m, c) => Math.max(m, parseInt(c.id, 10) || 0), 0);
    const fresh = { id: String(max + 1).padStart(2, "0"), act, title: "（待补）", summary: "", spine: "" };
    let at = -1;
    list.forEach((c, i) => { if ((c.act || 1) <= act) at = i; });
    return { ...s, chapters: [...list.slice(0, at + 1), fresh, ...list.slice(at + 1)] };
  });
  const spineHits = chapters.filter(c => c.spine).length;
  /* 占位章 = 「添加章节」点出来、还什么都没写的行（章名空或「（待补）」，摘要 / 章目标 / 脊柱全空）。
     与后端 is_placeholder_chapter 同一口径：整张表都是占位时，分章面板当它不存在、直接按场景分章。 */
  const isPlaceholder = (c) => (!(c.title || "").trim() || (c.title || "").includes("待补"))
    && !(c.summary || "").trim() && !(c.goal || "").trim() && !(c.spine || "").trim();
  const placeholders = chapters.filter(isPlaceholder).length;
  const runs = chapterActRuns(chapters);
  const lastRunOfAct = {};
  runs.forEach((run, i) => { lastRunOfAct[run.act] = i; });
  const actsWithout = S2_ACTS.filter(a => !chapters.some(c => (c.act || 1) === a.act));
  /* 整理成章之后的第二动线：去 AI 起草台（它的左栏就是全书书脊，与目录同源）。只有目录里真的有章时才出现
     （catalogHasChapters 由视图随 ws:catalog-changed 传下来——编辑器是 memo 的，不能在渲染里自己读目录）。 */
  const goDraft = async () => {
    try { if (WsCatalog && WsCatalog.__refresh) await WsCatalog.__refresh(); } catch (e) {}
    location.hash = "#scene";
  };
  return (
    <div className="sf-scaffold sf-chapters">
      <div className="sf-outline-expand" data-testid="snow-outline-expansions">
        <div className="sf-syn-prog">
          <span className="sf-syn-prog-c"><b>{expFilled}</b> / 5 段已扩成一页</span>
          <span className="sf-syn-prog-note">每段约一页（600–1000 字）：场景、行动与反应、关键对话、情感节点</span>
          <div className="sf-syn-track" aria-hidden="true">{S2_SYN_BEATS.map(b => <span key={b.f} className={`sf-syn-tick tone-${b.tone} ${(expansions[b.f] || "").trim() ? "is-on" : ""}`} />)}</div>
        </div>
        {S2_SYN_BEATS.map((b, i) => {
          const src = syn05[b.f] || "";
          const expanded = expansions[b.f] || "";
          const len = expanded.replace(/\s/g, "").length;
          const grew = len > src.replace(/\s/g, "").length;
          return (
            <div key={b.f} className={`sf-syn-row tone-${b.tone}`}>
              <div className="sf-syn-side">
                <span className="sf-syn-idx">{i + 1}</span>
                <span className="sf-syn-label">{b.label}</span>
                <span className="sf-syn-desc">{b.desc}</span>
              </div>
              <div className="sf-syn-main">
                <div className="sf-syn-src" title="展开自 05 一页梗概的这一段">
                  <span className="sf-syn-src-tag">展开自 05</span>
                  <span className="sf-syn-src-text">{src || <em className="sf-syn-empty">（05 这一段还没写）</em>}</span>
                </div>
                <S2AutoText className="sf-syn-text" minRows={5} value={expanded} onChange={(e) => setExp(b.f, e.target.value)} aria-label={`${b.label}：扩成约一页`} placeholder={`把「${b.label}」这一段扩成约一页…`} />
                {expanded.trim() && (
                  <div className={`sf-syn-meta ${grew ? "is-ok" : "is-warn"}`}>
                    {grew ? <><I.Check size={10} /> 已展开 {len} 字{len < 300 ? "，离一页还差些" : ""}</> : <><I.AlertTriangle size={10} /> 还没比 05 的源段长，再填画面与行动</>}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="sf-chapters-head">
        <h3 className="sf-chapters-title">章节表</h3>
        <span className="sf-chapters-rule">可以先空着：章是列完场之后的包装决定。09 列好后用「整理章节结构」按场景分章，确认的章表会回填到这里；章的先后与每章有哪几场，都在分章面板里改。</span>
      </div>
      <div className="sf-scene-stats">
        <span className="sf-sstat"><b>{chapters.length}</b> 章</span>
        <span className="sf-sstat tone-gold"><b>{spineHits}</b> 章带灾难标记</span>
        {chapters.length ? (
          <span className={`sf-sstat ${placeholders ? "tone-gold" : "tone-sage"}`}>{placeholders ? <><I.AlertTriangle size={11} /> {placeholders} 章还是占位（分章时不算数，可删）</> : <><I.Check size={11} /> 章表已写</>}</span>
        ) : (
          <span className="sf-sstat">章表空着，列完场再分章</span>
        )}
        <span className="sf-sstat-spacer" />
        <button className="btn btn-quiet btn-sm" data-testid="snow-materialize" onClick={() => onOpenChapterPlan && onOpenChapterPlan()}
          title="打开分章面板：章表空着就按 09 的场景分章，写了章表就把场倒进你的章；拆章、并章、挪章界、改章名都在那里，确认后才写入目录">
          <I.Layout size={13} /> 在分章面板里整理
        </button>
        {catalogHasChapters && (
          <button className="btn btn-quiet btn-sm" data-testid="snow-go-draft" onClick={goDraft} title="AI 起草台的书脊上已经是目录里这一版的章与场">
            <I.Play size={13} /> 去 AI 起草台
          </button>
        )}
      </div>
      {runs.map((run, ri) => {
        const a = S2_ACTS.find(x => x.act === run.act) || S2_ACTS[0];
        return (
          <div key={`${run.act}-${run.chapters[0].id}`} className={`sf-act tone-${a.tone}`}>
            <div className="sf-act-head">
              <span className="sf-act-bar" aria-hidden="true" />
              <span className="sf-act-label">{a.label}</span>
              <span className="sf-act-desc">{a.desc}</span>
              <span className="sf-act-count">{run.chapters.length} 章</span>
            </div>
            <div className="sf-ch-list">
              {run.chapters.map(c => (
                <div key={c.id} className={`sf-ch-row ${c.spine ? "is-spine" : ""} ${isPlaceholder(c) ? "is-ph" : ""}`}>
                  {/* 章号与分章面板同一条规则（chapterNoInTitle）：章名就是「第 N 章」这种占位、或空着
                      （占位提示里带着章号）时，左边这一格留空——不把章号和它自己的占位名并排写两遍。
                      格子本身留着，各行的章名框才对得齐。 */}
                  {chapterNoInTitle(c.title, c.index)
                    ? <span className="sf-ch-no" aria-hidden="true" />
                    : <span className="sf-ch-no" title="章序跟着表里的先后走">第 {c.index + 1} 章</span>}
                  <div className="sf-ch-body">
                    <input className="sc-in sf-ch-title" value={c.title || ""} onChange={(e) => setCh(c.id, "title", e.target.value)} placeholder={`第 ${c.index + 1} 章（未命名）`} aria-label={`第 ${c.index + 1} 章标题`} />
                    <input className="sc-in sf-ch-sum" value={c.summary || ""} onChange={(e) => setCh(c.id, "summary", e.target.value)} placeholder="这一章把局面推到哪——一句话" aria-label={`第 ${c.index + 1} 章摘要`} />
                  </div>
                  <select className="sc-spine sf-ch-spine" value={c.spine || ""} onChange={(e) => setCh(c.id, "spine", e.target.value)} aria-label={`第 ${c.index + 1} 章的灾难标记`} title="这一章收束在哪个灾难上">
                    {S2_SPINE_OPTS.map(o => <option key={o} value={o}>{o || "—"}</option>)}
                  </select>
                  <button className="sc-act sc-act-del" onClick={() => delCh(c.id)} aria-label={`删除第 ${c.index + 1} 章`} title="删除本章"><I.X size={13} /></button>
                </div>
              ))}
              {lastRunOfAct[run.act] === ri && (
                <button className="sf-ch-add" onClick={() => addCh(run.act)}><I.Plus size={13} /> 添加{a.label}章节</button>
              )}
            </div>
          </div>
        );
      })}
      {actsWithout.length > 0 && (
        <div className="sf-ch-addbar">
          {actsWithout.map(a => (
            <button key={a.act} className="sf-ch-add" onClick={() => addCh(a.act)}><I.Plus size={13} /> 添加{a.label}章节</button>
          ))}
        </div>
      )}
    </div>
  );
}
