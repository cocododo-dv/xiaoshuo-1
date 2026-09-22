import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { wsConfirm, wsToast } from "./ws-notify.jsx";
import { EmptyState, Spinner, Tag } from "./ws-ui.jsx";
import { SCENE_STATE_META, chapterLabel, chapterStateMeta, chapterOwnTitle } from "./ws-labels.js";

const SCENE_DONE = SCENE_STATE_META.done.label;
import { manuArchivedParas, manuDramaOf } from "./ws-manuscripts-compile.js";

/* ==========================================================
   成稿中心阅读器的内容：正文、结构（戏剧卡 + 场景拼接 + 场景三问）、
   页脚那句「卡在哪、下一步是什么」，以及章节阶段标签。
   ========================================================== */

/* 章节阶段标签（已定稿 / 审阅中 / 草稿 / 写作中 …，与主页同一套词） */
function ManuState({ stage }) {
  const m = chapterStateMeta(stage);
  return <Tag tone={m.tone} dot>{m.label}</Tag>;
}

/* 页脚左边一句话：这一章现在卡在哪、下一步是什么（按钮在右边，每个阶段只有一个主动作）。
   缺场的提示只在这里说一次——正文里的缺场位置由各场占位标出。 */
function ManuFootNote({ picked, canonical, canonicalComplete, blockReason, scenesArchived }) {
  let text;
  if (picked.stage === "approved") {
    text = canonicalComplete
      ? <span><I.Lock size={12} className="ms-foot-lock" /> 终稿已锁定，不能直接修改；要改先「重新打开」。</span>
      : blockReason;
  } else if (canonical && canonical.status === "error") {
    text = "没能核验服务端正文，送审、批准与导出先暂停。";
  } else if (!canonical || canonical.status === "idle" || canonical.status === "loading") {
    text = "正在核验服务端正文…";
  } else if (!canonicalComplete) {
    text = scenesArchived ? `各场都${SCENE_DONE}。${blockReason}` : blockReason;
  } else if (picked.stage === "review") {
    text = `${picked.scenes} 场都${SCENE_DONE}。通读无误后批准为终稿，本章就汇入整书。`;
  } else {
    text = `${picked.scenes} 场都${SCENE_DONE}，可以送入审阅。`;
  }
  return <div className="ms-foot-note">{text}</div>;
}

function ManuRead({ picked, body, loadState, onRetry }) {
  if (loadState && loadState.status === "error") {
    return (
      <div className="ms-empty" role="alert">
        <EmptyState icon="AlertTriangle" title="服务端正文加载失败"
          actions={<button className="btn btn-ghost btn-sm" type="button" data-testid="manuscript-retry" onClick={onRetry}><I.Refresh size={13} /> 重试加载</button>}>
          {(loadState.error && loadState.error.message) || "暂时无法核验本章权威稿。"}
        </EmptyState>
      </div>
    );
  }
  if (!body) {
    const loading = loadState && (loadState.status === "idle" || loadState.status === "loading");
    return (
      <div className="ms-empty" role="status">
        {loading ? <><Spinner size={18} /><div>正在从服务端核验本章正文…</div></> : <EmptyState icon="BookOpen" title="本章正文还没有归档" compact />}
      </div>
    );
  }
  return (
    <article className="ms-read">
      {/* 章名就是「第 N 章」「未命名」这种占位时，正文上方只写一遍章号 */}
      {chapterOwnTitle(picked) && (
        <div className="ms-read-chno">{chapterLabel(picked, { withTitle: false })}</div>
      )}
      <h2 className="ms-read-htitle text-serif">{chapterOwnTitle(picked) || chapterLabel(picked, { withTitle: false })}</h2>
      {body.scenes.map((s, i) => (
        <div key={s.sceneId || i} className={`ms-scene ${s.missing ? "is-missing" : ""}`}>
          <header className="ms-scene-head">
            <span className="ms-scene-idx">第 {Number(s.idx)} 场</span>
            <span className="ms-scene-title">{s.title}</span>
          </header>
          {s.missing
            ? <div className="ms-scene-missing"><I.Clock size={14} /> 这一场尚无服务端归档正文</div>
            : s.paras.map((p, j) => <p key={j} className="ms-scene-p">{p}</p>)}
        </div>
      ))}
      {body.complete && <div className="ms-read-end">— 章节结束 —</div>}
    </article>
  );
}

/* 阶段 D：成稿后的场景三问（Ingermanson 的 Yes / No / Maybe 分诊）——准定稿评审随评审记录给出，
   目录按场透出（catalog story_check）。只是提示，不阻断任何流转。
   判定用中文说；原著的 Yes / No / Maybe 留在悬停提示里。 */
const MS_STORY_VERDICT = { yes: ["成立", "Yes"], no: ["不成立", "No"], maybe: ["能修", "Maybe"] };

function ManuStoryCheck({ check, sceneId, sid, go }) {
  if (!check) return <span className="ms-story-check is-none" />;
  const verdict = MS_STORY_VERDICT[check.verdict] || ["未判", "—"];
  const mark = (flag) => (flag === true ? "✓" : flag === false ? "✗" : "?");
  const title = `成稿后场景三问（准定稿评审）：坩埚可辨 ${mark(check.crucible_identified)} · 三拍落地 ${mark(check.shape_landed)} · 判定「${verdict[0]}」（${verdict[1]}）${check.note ? "\n" + check.note : ""}`;
  /* 阶段 R：原著的七步救治从这里回路——Maybe / No 都回第 10 步改形态与三拍再重写；No 还可以标记待删（回收站可恢复，不真删） */
  const backToPlan = () => {
    if (!go || !sceneId) return;
    // 视图意图在构思视图就绪后依次派发：先切到第 10 步，再选中这一场（ws-snow 自己处理挂载竞态）
    go("snowflake", [{ type: "ws:snow-step", detail: "planning" }, { type: "ws:snow-scene", detail: sceneId }]);
  };
  const markCut = async () => {
    if (!sid) return;
    const ok = await wsConfirm({
      title: "把这一场标为待删？",
      body: "它会进回收站，随时可以恢复。原著的做法是先标待删、下一稿再决定，不真删。",
      confirmLabel: "移入回收站",
      tone: "danger",
    });
    if (!ok) return;
    if (WsCatalog.removeScenes([sid])) wsToast({ message: "这一场已移入回收站。", tone: "neutral" });
  };
  return (
    <span className={`ms-story-check is-${check.verdict || "none"}`} title={title} data-testid="ms-story-check">
      <b>{verdict[0]}</b> 坩埚 {mark(check.crucible_identified)} 三拍 {mark(check.shape_landed)}
      {go && sceneId && check.verdict !== "yes" && (
        <button type="button" className="ms-story-check-act" data-testid="ms-story-check-plan" onClick={backToPlan} title="回第 10 步：先定这一场是主动还是反应场，写下三拍与坩埚，再重写、再评">回第 10 步</button>
      )}
      {sid && check.verdict === "no" && (
        <button type="button" className="ms-story-check-act" data-testid="ms-story-check-cut" onClick={markCut} title="标记待删：送进回收站，可恢复">标待删</button>
      )}
    </span>
  );
}

const DRAMA_FIELDS = [["promise", "核心承诺"], ["thrust", "主线推进"], ["turn", "人物变化"], ["after", "结尾余味"]];

/* 场景拼接的一行：目录场景优先（带状态 / 字数 / 场景三问）；目录没有场景时按服务端归档行列出 */
function structureRows(chapter, body, canonical) {
  const scenes = (chapter && chapter.scenes) || [];
  if (scenes.length) {
    return scenes.map((s, i) => {
      const paras = manuArchivedParas(canonical, s);
      return {
        key: s.sid || s.backendId || i,
        idx: String(i + 1),
        title: s.title,
        meta: paras ? `${paras.join("").length} 字 · ${SCENE_DONE}` : (typeof s.words === "number" && s.words > 0 ? `${s.words.toLocaleString()} 字` : "未展开"),
        done: !!paras || s.state === "done",
        check: s.storyCheck || null,
        sceneId: s.backendId || "",
        sid: s.sid || "",
      };
    });
  }
  return body ? body.scenes.map((s, i) => ({
    key: s.sceneId || i, idx: String(Number(s.idx)), title: s.title, meta: `${s.paras.join("").length} 字 · ${SCENE_DONE}`, done: true,
  })) : [];
}

function ManuStructure({ body, chapter, canonical, go }) {
  const drama = (body && body.drama) || manuDramaOf(chapter);
  const rows = structureRows(chapter, body, canonical);
  return (
    <div className="ms-struct">
      {drama && (
        <div className="card">
          <div className="card-head"><div className="card-title">戏剧卡</div></div>
          <div className="ms-struct-grid">
            {DRAMA_FIELDS.map(([key, label]) => (
              <div key={key}><div className="ms-struct-k">{label}</div><div className="ms-struct-v">{drama[key] || "—"}</div></div>
            ))}
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head"><div className="card-title">场景拼接</div><span className="card-sub">{rows.length} 场</span></div>
        <ul className="ms-struct-scenes">
          {rows.map((s) => (
            <li key={s.key} className={s.done ? "" : "is-ghost"}>
              <span className="ms-scene-idx">第 {s.idx} 场</span>
              <span className={s.done ? "ms-struct-title is-done" : "ms-struct-title"}>{s.title}</span>
              <ManuStoryCheck check={s.check} sceneId={s.sceneId} sid={s.sid} go={go} />
              <span className="text-muted text-sm">{s.meta}</span>
              {s.done ? <I.Check size={13} className="ms-struct-done" aria-label={SCENE_DONE} /> : <I.Dot size={13} aria-hidden="true" />}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export { ManuFootNote, ManuRead, ManuState, ManuStructure };
