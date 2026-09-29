import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { chapterActRuns } from "./ws-snow-chapters-model.js";
import { chapterNoInTitle, isPlaceholderChapterRow } from "./labels/catalog.js";
import { S2AutoText } from "./ws-snow-fields.jsx";
import { S2ExpansionRow } from "./ws-snow-editor-parts.jsx";
import { S2_SPINE_OPTS } from "./ws-snow-model.js";
import { countChars } from "./lib/text.js";

/* ==========================================================
   雪花编辑器 · 情节轨（2026-09-29 从 ws-snow-scaffolds.jsx 拆出）
   ----------------------------------------------------------
   01 读者定位、03 一段话概括（五句 + 道德前提翻转）、05 一页梗概、07 长篇大纲（五段展开 + 章节表）。
   选哪个编辑器在 ws-snow-scaffolds.jsx 的 S2StepEditor。
   ========================================================== */

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
export function S2Audience({ scaffold, onScaffold }) {
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

/* ---- 03 一段话概括：五句骨架 + 中点的道德前提翻转 ---- */
const S2_BEATS = [
  { f: "setup",      label: "铺垫",   act: "开场",      desc: "交代背景，引入 1–2 位主角" },
  { f: "d1",         label: "灾难一", act: "第一幕末",  desc: "逼主角入局、做出承诺", tone: "crimson" },
  { f: "d2",         label: "灾难二", act: "第二幕中点", desc: "道德前提翻转：错误信念 → 正确信念", tone: "gold", flip: true },
  { f: "d3",         label: "灾难三", act: "第二幕末",  desc: "逼主角（与反派）走向终局", tone: "crimson" },
  { f: "resolution", label: "结局",   act: "第三幕",    desc: "终极对决 + 收束（喜 / 悲 / 苦甜）" },
];
export function S2Beats({ scaffold, onScaffold }) {
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

/* ---- 05 一页梗概：五段，每段锚定 03 的一句脊柱节拍（1→5 分形展开可见） ---- */
const S2_SYN_BEATS = [
  { f: "setup",      label: "铺垫",   ref: "setup", tone: "slate",   desc: "世界观与初始处境" },
  { f: "d1",         label: "灾难一", ref: "d1",    tone: "crimson", desc: "触发事件 · 第一幕末" },
  { f: "d2",         label: "灾难二", ref: "d2",    tone: "gold",    desc: "认知翻转 · 中点" },
  { f: "d3",         label: "灾难三", ref: "d3",    tone: "crimson", desc: "升级 · 第二幕末" },
  { f: "resolution", label: "结局",   ref: "resolution", tone: "slate", desc: "高潮走向与收尾" },
];
export function S2SynopsisBeats({ scaffold, onScaffold, refs }) {
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
        const grew = countChars(expanded) > countChars(src);
        return (
          <S2ExpansionRow key={b.f} beat={b} index={i} value={expanded} onChange={(v) => setPara(b.f, v)} minRows={3}
            srcTitle="展开自 03 一段话概括的这一句" srcTag={<><I.ArrowRight size={10} style={{ transform: "rotate(180deg)" }} /> 展开自 03</>}
            src={src} emptyText="（03 这一拍还没写）" ariaLabel={`${b.label}：扩成一段`} placeholder={`把「${b.label}」扩成一段有画面的梗概…`}
            meta={grew ? <><I.Check size={10} /> 已展开（{countChars(expanded)} 字 &gt; 源句 {countChars(src)}）</> : <><I.AlertTriangle size={10} /> 还没比源句长——再填点画面</>} grew={grew} />
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
export function S2ChapterOutline({ scaffold, onScaffold, refs, onOpenChapterPlan, catalogHasChapters }) {
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
  /* 占位章 = 「添加章节」点出来、还什么都没写的行（isPlaceholderChapterRow，与后端 is_placeholder_chapter
     同一口径）：整张表都是占位时，分章面板当它不存在、直接按场景分章。 */
  const isPlaceholder = isPlaceholderChapterRow;
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
          const len = countChars(expanded);
          const grew = len > countChars(src);
          return (
            <S2ExpansionRow key={b.f} beat={b} index={i} value={expanded} onChange={(v) => setExp(b.f, v)} minRows={5}
              srcTitle="展开自 05 一页梗概的这一段" srcTag="展开自 05"
              src={src} emptyText="（05 这一段还没写）" ariaLabel={`${b.label}：扩成约一页`} placeholder={`把「${b.label}」这一段扩成约一页…`}
              meta={grew ? <><I.Check size={10} /> 已展开 {len} 字{len < 300 ? "，离一页还差些" : ""}</> : <><I.AlertTriangle size={10} /> 还没比 05 的源段长，再填画面与行动</>} grew={grew} />
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
