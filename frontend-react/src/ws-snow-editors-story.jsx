import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { chapterActRuns } from "./ws-snow-chapters-model.js";
import { chapterNoInTitle, isAutoChapterTitle } from "./labels/catalog.js";
import { S2AutoText } from "./ws-snow-fields.jsx";
import { S2ExpansionRow } from "./ws-snow-editor-parts.jsx";
import { countChars } from "./lib/text.js";
import { S2_AUD_FIELDS, S2_BEATS, S2_SYN_BEATS } from "./ws-snow-model.js";

/* ==========================================================
   雪花编辑器 · 情节轨（2026-09-29 从 ws-snow-scaffolds.jsx 拆出）
   ----------------------------------------------------------
   01 读者定位、03 一段话概括（五句 + 道德前提翻转）、05 一页梗概、07 长篇大纲（五段展开 + 章节表的只读镜像）。
   选哪个编辑器在 ws-snow-scaffolds.jsx 的 S2StepEditor。
   ========================================================== */

/* ---- 01 读者定位：类型 / 读者画像 / 核心快感 / 来源 / 反向定位 ---- */
const S2_AUD_GENRES = ["文学悬疑", "言情", "硬核推理", "科幻", "奇幻", "历史", "青春", "惊悚"];
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

/* ---- 07 长篇大纲：五段展开 + 章节表的只读镜像（章是列完场之后的包装决定，阶段 K / V；章表只读，重评 R11）---- */
const S2_ACTS = [
  { act: 1, label: "第一幕", desc: "铺垫 → 灾难一", tone: "slate" },
  { act: 2, label: "第二幕", desc: "灾难二（中点翻转）", tone: "gold" },
  { act: 3, label: "第三幕", desc: "灾难三 → 收尾", tone: "crimson" },
];
/* 07 的章表是分章结果的只读镜像（重评 R11，批准 #18a）：章名、章摘要、章界、灾难标记都只在分章面板里改
   （确认写入 / 只保存章表），章名也能在章节编排里改——07 这一份由服务端写，本机从不上行它。以前这里是第二个
   编辑入口：「添加章节」点出来的占位行、改的章名一保存就当整张章表同步回去，另一台电脑或一直开着的旧标签页里的旧章表
   会删章、把场退回「未分章」。现在每一章一行只读（章号 · 章名 · 章摘要 · 灾难标记），行尾的「改名」打开分章面板
   并把焦点放在这一章的章名框上（onRenameChapter({ rowUid, index })）。 */
export function S2ChapterOutline({ scaffold, onScaffold, refs, onOpenChapterPlan, onRenameChapter, catalogHasChapters }) {
  const chapters = scaffold.chapters || [];
  /* 阶段 D：书里的第 6 步——05 的每一段再扩成约一页（五段展开）；章节表在它下面，是分章结果的镜像。 */
  const expansions = scaffold.expansions || {};
  const syn05 = ((refs && refs.synopsis) || {}).paras || {};
  const setExp = (f, v) => onScaffold(s => ({ ...s, expansions: { ...(s.expansions || {}), [f]: v } }));
  const expFilled = S2_SYN_BEATS.filter(b => (expansions[b.f] || "").trim()).length;
  const spineHits = chapters.filter(c => c.spine).length;
  // 还用着系统起的章名（空、「第 N 章」、带「待补」「未命名」占位标记的）的章：与 AI 起章名、后端同一条规则
  const unnamed = chapters.filter(c => isAutoChapterTitle(c.title)).length;
  /* 章表按数组顺序显示（= 章序 = 分章面板看到的顺序），幕只是分段标签：幕交错时就是看得见的两段 */
  const runs = chapterActRuns(chapters);
  const rename = (c) => {
    if (onRenameChapter) onRenameChapter({ rowUid: c.row_uid || "", index: c.index });
    else if (onOpenChapterPlan) onOpenChapterPlan();
  };
  /* 整理成章之后的第二动线：去 AI 起草台（它的左栏就是全书书脊，与目录同源）。只有目录里真的有章时才出现
     （catalogHasChapters 由视图随 ws:catalog-changed 传下来——编辑器是 memo 的，不能在渲染里自己读目录）。 */
  const goDraft = async () => {
    try { if (WsCatalog && WsCatalog.refresh) await WsCatalog.refresh(); } catch (e) {}
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
        <span className="sf-chapters-rule">这里只读，是分章的结果。章是列完场之后的包装决定：09 列好后用「整理章节结构」按场景分章；章名、章摘要、章界与灾难标记都在分章面板里改（每一章行尾的「改名」直达那一章），章名也能在章节编排里改。</span>
      </div>
      <div className="sf-scene-stats">
        {chapters.length ? (
          <>
            <span className="sf-sstat"><b>{chapters.length}</b> 章</span>
            <span className="sf-sstat tone-gold"><b>{spineHits}</b> 章带灾难标记</span>
            {unnamed > 0 && <span className="sf-sstat tone-gold" data-testid="snow-outline-unnamed"><b>{unnamed}</b> 章还没起名</span>}
          </>
        ) : (
          <span className="sf-sstat" data-testid="snow-outline-empty">章表空着，列完场再分章</span>
        )}
        <span className="sf-sstat-spacer" />
        <button className="btn btn-quiet btn-sm" data-testid="snow-materialize" onClick={() => onOpenChapterPlan && onOpenChapterPlan()}
          title="打开分章面板：还没分过章就按 09 的场景分章，分过就把现有的分章摆出来；拆章、并章、挪章界、改章名都在那里——「确认写入」写进目录，「只保存章表」先存下章表">
          <I.Layout size={13} /> 在分章面板里整理
        </button>
        {catalogHasChapters && (
          <button className="btn btn-quiet btn-sm" data-testid="snow-go-draft" onClick={goDraft} title="AI 起草台的书脊上已经是目录里这一版的章与场">
            <I.Play size={13} /> 去 AI 起草台
          </button>
        )}
      </div>
      {runs.map(run => {
        const a = S2_ACTS.find(x => x.act === run.act) || S2_ACTS[0];
        return (
          <div key={`${run.act}-${run.chapters[0].index}`} className={`sf-act tone-${a.tone}`}>
            <div className="sf-act-head">
              <span className="sf-act-bar" aria-hidden="true" />
              <span className="sf-act-label">{a.label}</span>
              <span className="sf-act-desc">{a.desc}</span>
              <span className="sf-act-count">{run.chapters.length} 章</span>
            </div>
            <ul className="sf-ch-list">
              {run.chapters.map(c => {
                const no = `第 ${c.index + 1} 章`;
                const title = String(c.title || "").trim();
                const summary = String(c.summary || "").trim();
                return (
                  <li key={c.row_uid || c.id || c.index} className={`sf-ch-row ${c.spine ? "is-spine" : ""}`} data-testid={`snow-outline-chapter-${c.index}`}>
                    {/* 章号与分章面板同一条规则（chapterNoInTitle）：章名就是「第 N 章」这种占位、或空着时，左边这一格留空——
                        不把章号和它自己的占位名并排写两遍。格子本身留着，各行的章名才对得齐。 */}
                    {chapterNoInTitle(title, c.index)
                      ? <span className="sf-ch-no" aria-hidden="true" />
                      : <span className="sf-ch-no" title="章序跟着章表的先后走">{no}</span>}
                    <div className="sf-ch-body">
                      <span className={`sf-ch-title ${title ? "" : "is-unnamed"}`}>{title || `${no}（未命名）`}</span>
                      <span className={`sf-ch-sum ${summary ? "" : "is-empty"}`}>{summary || "还没有章摘要"}</span>
                    </div>
                    {c.spine
                      ? <span className="sf-ch-spine" title="这一章收束在这个灾难上">{c.spine}</span>
                      : <span className="sf-ch-spine is-none" aria-hidden="true" />}
                    <button type="button" className="btn btn-quiet btn-xs sf-ch-rename" data-testid={`snow-outline-rename-${c.index}`}
                      onClick={() => rename(c)} aria-label={`改${no}的章名`}
                      title="打开分章面板，光标落在这一章的章名上（章摘要、章界也在那里改）">
                      <I.Pen size={12} /> 改名
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}
