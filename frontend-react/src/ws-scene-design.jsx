import React from "react";
import { I } from "./icons.jsx";

/* ==========================================================
   场景设计卡 — 写作台与 AI 起草台共用的同一张卡（阶段 X「一条书脊」）
   ----------------------------------------------------------
   构思（雪花）里给一场定下的东西——坩埚、POV、时间、地点、出场、三拍、后续三拍、
   离场变化、钩子、读者情绪、必须包含 / 隐瞒、代价、篇幅、呈现方式、破例理由，以及它所在
   那一章的章摘要 / 章目标 / 脊柱标记——过去只留在雪花那一侧：目录只把三拍交给台子，
   写作台的「场景定位」读的还是**章**上的 POV / 时间 / 地点（雪花的章上没有这些，于是全是「—」）。
   现在目录载荷带着整张卡，两张台子渲染的是同一个模型：在哪张台子上看到的都是同一场。

   纯展示 + 一个纯函数模型；不读 store、不写 window。
   ========================================================== */

const BEATS_PROACTIVE = [["goal", "目标"], ["conflict", "冲突"], ["setback", "挫折"]];
const BEATS_REACTIVE = [["reaction", "反应"], ["dilemma", "两难"], ["decision", "决定"]];
const LENGTH_BAND_LABEL = { short: "短", medium: "中", long: "长" };
/* 系统占位（「（本场目标待规划）」「（待规划）」「（待补）」）不是作者写的内容：卡上按「没填」处理 */
const PLACEHOLDER_TEXT = /^（[^）]*待(规划|补)[^）]*）$/;

function sdText(value) {
  const text = String(value == null ? "" : value).trim();
  return text && !PLACEHOLDER_TEXT.test(text) ? text : "";
}

function sdLengthLabel(band) {
  const raw = String(band || "").trim();
  if (!raw) return "";
  if (LENGTH_BAND_LABEL[raw]) return `篇幅 ${LENGTH_BAND_LABEL[raw]}`;
  const range = /^(\d+)\s*[-–~]\s*(\d+)$/.exec(raw);
  return range ? `${range[1]}–${range[2]} 字` : raw;
}

/* 目录命中（WsCatalog.sceneById 的返回）→ 设计卡模型。纯函数，可单测。 */
function sceneDesignModel(hit) {
  if (!hit || !hit.scene) return null;
  const { chapter, scene, index } = hit;
  const design = scene.design || {};
  const reactive = scene.kind === "反应";
  const primaryKeys = reactive ? BEATS_REACTIVE : BEATS_PROACTIVE;
  const values = [scene.goal, scene.obstacle, scene.turn];
  const beats = primaryKeys.map(([key, label], i) => ({ key, label, text: sdText(values[i]) }));
  const followupSource = design.followup || {};
  const followup = (reactive ? BEATS_PROACTIVE : BEATS_REACTIVE)
    .map(([key, label]) => ({ key, label, text: sdText(followupSource[key]) }))
    .filter(beat => beat.text);
  const cast = (design.cast || []).map(c => c.name).filter(Boolean);
  const chapterModel = chapter ? {
    n: chapter.n || "",
    title: chapter.title || "",
    origin: chapter.origin || "manual",
    summary: sdText(chapter.summary),
    goal: sdText(chapter.goal),
    spine: chapter.spine || "",
    sceneCount: (chapter.scenes || []).length,
  } : null;
  const facts = [
    { k: "POV", v: scene.povName || (chapter && chapter.pov) || "" },
    { k: "时间", v: design.storyTime || (chapter && chapter.time) || "" },
    { k: "地点", v: design.location || (chapter && chapter.place) || "" },
    { k: "出场", v: cast.join("、") },
  ];
  const summary = sdText(scene.summary);
  return {
    sid: scene.sid,
    backendId: scene.backendId || "",
    origin: design.origin === "snowflake" ? "snowflake" : "manual",
    stamp: chapter ? `CH ${chapter.n} · SC ${String((index || 0) + 1).padStart(2, "0")}` : "",
    kind: reactive ? "反应" : "主动",
    reactive,
    title: scene.title || "",
    // 题名就是整句摘要时不重复显示
    summary: summary && summary !== (scene.title || "") ? summary : "",
    beats,
    beatsFilled: beats.filter(beat => beat.text).length,
    followup,
    facts,
    crucible: sdText(design.crucible),
    exitChange: sdText(scene.exitChange),
    hook: sdText(scene.hook),
    readerEmotion: sdText(design.readerEmotion),
    mustInclude: sdText(design.mustInclude),
    mustWithhold: sdText(design.mustWithhold),
    cost: sdText(design.cost),
    lengthLabel: sdLengthLabel(design.lengthBand),
    summaryMode: design.renderingMode === "summary",
    exceptionReason: sdText(design.exceptionReason),
    chapterLast: !!design.chapterLast,
    deskEdited: !!design.deskEdited,
    chapter: chapterModel,
  };
}

function SdRow({ k, v, tone }) {
  if (!v) return null;
  return (
    <div className={`sdc-row ${tone ? "tone-" + tone : ""}`}>
      <span className="sdc-row-k">{k}</span>
      <span className="sdc-row-v">{v}</span>
    </div>
  );
}

/* 正文上方那张（compact）可以收起：雪花的三拍常常很长，写的时候不该总隔在作者和纸之间。
   收起 / 展开是界面偏好（localStorage），不是内容。 */
const SDC_COLLAPSED_LS = "ws_scene_design_collapsed_v1";
function sdLoadCollapsed() {
  try { return localStorage.getItem(SDC_COLLAPSED_LS) === "1"; } catch (e) { return false; }
}

/* sync = { pending: bool, busy: bool, onSync: fn }（这张卡落后于已确认的构思时才给）
   onEditPlan：回构思第 10 步改这一场；onEditCard：去章节编排改这张卡。variant="compact" 只给三拍 + 事实行。 */
function SceneDesignCard({ model, variant = "full", sync, onEditPlan, onEditCard }) {
  const [collapsed, setCollapsed] = React.useState(sdLoadCollapsed);
  if (!model) return null;
  const fromPlan = model.origin === "snowflake";
  const facts = model.facts.filter(f => f.v);
  const compact = variant === "compact";
  const folded = compact && collapsed;
  const toggle = () => setCollapsed((value) => {
    const next = !value;
    try { localStorage.setItem(SDC_COLLAPSED_LS, next ? "1" : "0"); } catch (e) {}
    return next;
  });
  return (
    <section className={`sdc ${compact ? "is-compact" : ""} ${folded ? "is-collapsed" : ""}`} data-testid="scene-design-card" data-origin={model.origin}>
      <header className="sdc-head">
        <span className={`sdc-kind ${model.reactive ? "is-react" : ""}`}>{model.kind}场景</span>
        {model.summaryMode && <span className="sdc-chip" title="构思里定的呈现方式：两三段叙述性概述，不逐拍展开">概述</span>}
        {model.lengthLabel && <span className="sdc-chip">{model.lengthLabel}</span>}
        {model.chapterLast && <span className="sdc-chip" title="本章最后一场">章末</span>}
        <span className="sdc-src">{fromPlan ? "本场的设计 · 来自构思" : "本场的卡 · 来自章节编排"}</span>
        {fromPlan && onEditPlan && (
          <button type="button" className="sdc-link" data-testid="scene-design-edit-plan" onClick={onEditPlan}
            title="回构思第 10 步改这一场的设计；确认那一步之后，这张卡自动跟上">在构思里改 ↗</button>
        )}
        {!fromPlan && onEditCard && (
          <button type="button" className="sdc-link" data-testid="scene-design-edit-card" onClick={onEditCard}
            title="到章节编排修改这张卡">编辑卡 ↗</button>
        )}
        {compact && (
          <button type="button" className="sdc-toggle" data-testid="scene-design-toggle" aria-expanded={!folded} onClick={toggle}
            title={folded ? "展开这一场的设计卡" : "收起设计卡，只留这一行"}>{folded ? "展开" : "收起"}</button>
        )}
      </header>

      {sync && sync.pending && (
        <div className="sdc-sync" data-testid="scene-design-sync" role="status">
          <I.Refresh size={12} />
          <span>{model.deskEdited
            ? "构思里这一场已经更新；你在台子上也改过这张卡——同步会以构思为准。"
            : "构思里这一场已经更新，这张卡还是旧的。"}</span>
          <button type="button" className="sdc-sync-btn" disabled={!!sync.busy} onClick={sync.onSync}>
            {sync.busy ? "同步中…" : "同步这一场"}
          </button>
        </div>
      )}

      {folded ? null : (<>
      {!compact && model.summary && <p className="sdc-summary">{model.summary}</p>}
      {model.crucible && <SdRow k="坩埚" v={model.crucible} tone="crimson" />}

      {facts.length > 0 && (
        <ul className="sdc-facts">
          {facts.map(f => (<li key={f.k}><span>{f.k}</span><strong>{f.v}</strong></li>))}
        </ul>
      )}

      <div className="sdc-beats">
        {model.beats.map(beat => (
          <div className="sdc-beat" key={beat.key}>
            <span className="sdc-beat-k">{beat.label}</span>
            <span className={`sdc-beat-v ${beat.text ? "" : "is-empty"}`}>{beat.text || "（待规划）"}</span>
          </div>
        ))}
      </div>
      {model.followup.length > 0 && (
        <div className="sdc-beats is-followup" title="这一场接着另一组三拍——在主三拍之后按顺序发生，本场落在最后一拍上">
          {model.followup.map(beat => (
            <div className="sdc-beat" key={beat.key}>
              <span className="sdc-beat-k">接 · {beat.label}</span>
              <span className="sdc-beat-v">{beat.text}</span>
            </div>
          ))}
        </div>
      )}
      {model.exceptionReason && <SdRow k="破例" v={model.exceptionReason} tone="gold" />}

      {!compact && (
        <>
          <SdRow k="离场变化" v={model.exitChange} />
          <SdRow k="钩子" v={model.hook} />
          <SdRow k="读者感受" v={model.readerEmotion} />
          <SdRow k="代价" v={model.cost} />
          <SdRow k="必须包含" v={model.mustInclude} />
          <SdRow k="必须隐瞒" v={model.mustWithhold} />
        </>
      )}

      {!compact && model.chapter && (model.chapter.summary || model.chapter.goal || model.chapter.spine) && (
        <footer className="sdc-chapter">
          <div className="sdc-chapter-head">
            <span className="sdc-chapter-n">第 {model.chapter.n} 章</span>
            <span className="sdc-chapter-t">{model.chapter.title}</span>
            {model.chapter.spine && <span className="sdc-chip tone-gold"><I.Activity size={9} /> {model.chapter.spine}</span>}
          </div>
          {model.chapter.summary && <p className="sdc-chapter-sum">{model.chapter.summary}</p>}
          {model.chapter.goal && model.chapter.goal !== model.chapter.summary && (
            <p className="sdc-chapter-sum"><b>章目标</b>　{model.chapter.goal}</p>
          )}
        </footer>
      )}
      </>)}
    </section>
  );
}

/* 回构思第 10 步并对准这一场（与成稿中心「回第 10 步」同一组意图） */
function planIntentsForScene(backendSceneId) {
  return [
    { type: "ws:snow-step", detail: "planning" },
    ...(backendSceneId ? [{ type: "ws:snow-scene", detail: backendSceneId }] : []),
  ];
}

export { SceneDesignCard, planIntentsForScene, sceneDesignModel, sdLengthLabel };
