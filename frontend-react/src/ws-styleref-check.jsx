import React from "react";
import { I } from "./icons.jsx";
import { Notice, Segmented, Spinner } from "./ws-ui.jsx";
import { fidErrorInfo, fidJobView, fidReadingStates } from "./ws-fidelity-model.js";
import { fidCheck, fidResumeCheck, fidStartCheck, useFidelityStore } from "./ws-fidelity-store.js";
import {
  FidelityCopyLine, FidelityDimensionTable, FidelityErrorLine, FidelityGaps, FidelityHeadline, FidelityJudgeLine,
} from "./ws-fidelity-ui.jsx";
import { srAppliedToWork, srFormatDuration, srFormatWhen, srIsLegacyGlobalBinding, srSceneLabel, srSceneOptions } from "./ws-styleref-model.js";
import { srActivityTrack, srLoadProjectBinding, srLoadRuntime, srLoadWorkScenes, srProjectBinding, srRuntime } from "./ws-styleref-store.js";
import { SrProgressBar, SrStageEmpty, srActiveWork, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 第四步「对照检查」：拿一段文字（或当前作品的一场）对照这本书的作者，看像不像。
   一次检查是后台的一个作业（POST /api/v2/style-reference/checks，进度也在「参考书活动」里）：
   · 先在本机量一遍（句子、词汇、标点、视角、节奏、信息密度、对白这些测得出的习惯）→ 在作者自己的段落里排第几位；
   · 再请模型对着原文样例和文风卡按 16 维打分，每维一句说明；
   · 顺带查照搬（只给计数，从不给参考原文）。
   贴的文字只对照这份画像；选一场时，这部作品用着这本书就按它现在的设置（重点 / 不学）查，没用就按这份画像查。
   结果与进度记在模块里（离开再回来还在），刷新页面就没了——一场的检查结果另外记在那一场的读数里（起草台可看）。
   ========================================================== */

const CHECK_MAX_CHARS = 60000;

export function srCheckKey(bookId) {
  return `book:${bookId}`;
}

export function SrCheck({ book, go, onAction }) {
  useSrStore("detail", "books");
  useFidelityStore();
  const profileId = book.profile ? book.profile.profile_id : null;
  const work = srActiveWork();
  const workId = work ? work.id : null;
  const workTitle = (work && work.title) || "当前作品";
  const key = srCheckKey(book.id);
  const entry = fidCheck(key);
  const lastTarget = (entry && entry.target) || {};
  /* 检查记录按书记，不按作品：上一次查的那一场属于别的作品时（换过作品）不能把它恢复成这部作品的选择——
     下拉框里没有它、按钮却能点，一点就查了别的作品的一场 */
  const lastSceneHere = lastTarget.sceneId && (!lastTarget.projectId || lastTarget.projectId === workId) ? lastTarget.sceneId : "";
  const [mode, setMode] = React.useState(lastTarget.sceneId ? "scene" : "text");
  const [text, setText] = React.useState(lastTarget.text || "");
  const [sceneId, setSceneId] = React.useState(lastSceneHere);
  const [chapters, setChapters] = React.useState(null);

  React.useEffect(() => { srLoadRuntime(); }, []);
  React.useEffect(() => { if (workId) srLoadProjectBinding(workId); }, [workId]);
  React.useEffect(() => { fidResumeCheck(key); }, [key]);
  React.useEffect(() => {
    let alive = true;
    setChapters(null);
    if (!workId) return undefined;
    srLoadWorkScenes(workId)
      .then((list) => { if (alive) setChapters(list); })
      .catch(() => { if (alive) setChapters([]); });
    return () => { alive = false; };
  }, [workId]);
  /* 选中的场不在这部作品的场景里（换了作品、场被删了）：清掉，不留一个看不见的选择 */
  const sceneKnown = !!sceneId && srSceneOptions(chapters).some((group) => group.scenes.some((scene) => scene.value === sceneId));
  React.useEffect(() => {
    if (chapters && sceneId && !sceneKnown) setSceneId("");
  }, [chapters, sceneId, sceneKnown]);

  if (!profileId) {
    return (
      <SrStageEmpty icon="Target" title="还没有可以对照的文风" actionLabel="去学习文风" onAction={() => go && go("learn")} testId="sr-check-empty">
        对照检查拿学出来的文风画像当尺子：先在「学习文风」里学一次。
      </SrStageEmpty>
    );
  }

  const runtime = srRuntime();
  const noLlm = runtime.phase === "ready" && !!runtime.data && runtime.data.llm_enabled === false;
  const busy = !!entry && (entry.phase === "starting" || entry.phase === "running");
  /* 作品在用这本书：作品层的应用，或旧版全局应用（这部作品自己没有应用时它生效）——都按作品现在的设置（重点 / 不学）
     查（传 project_id，后端按作品现解析的策略来）；只传 profile_id 会按默认维度状态查 */
  const workBinding = workId ? (srProjectBinding(workId) || {}).data : null;
  const legacyGlobalHere = !!(workBinding && srIsLegacyGlobalBinding(workBinding.binding) && workBinding.binding.profile_id === profileId);
  const applied = srAppliedToWork(book, workId) || legacyGlobalHere;
  const groups = srSceneOptions(chapters);
  const count = Array.from(text.trim()).length;
  const ready = mode === "text" ? count > 0 && count <= CHECK_MAX_CHARS : sceneKnown;

  const start = async (target = null) => {
    if (busy) return;
    const body = target || (mode === "text"
      ? { text: text.trim(), profileId }
      : (applied ? { sceneId, projectId: workId } : { sceneId, profileId, projectId: workId }));
    const next = await fidStartCheck(key, body);
    if (next && next.jobId) srActivityTrack(next.jobId, { kind: "check", book_id: book.id, title: book.title });
  };
  const retry = () => { if (entry && entry.target) start(entry.target); };
  const act = (action) => {
    if (!action) return;
    if (action.type === "retry") retry();
    else if (onAction) onAction(action);
  };

  const describe = (target) => {
    if (!target) return "";
    if (target.sceneId) {
      return srSceneLabel(chapters, target.sceneId)
        || (target.projectId && target.projectId !== workId ? "另一部作品的一场" : "当前作品的一场");
    }
    return `贴进来的 ${Array.from(String(target.text || "")).length.toLocaleString("zh-CN")} 字`;
  };

  return (
    <div className="sr-check" data-testid="sr-check">
      <div className="card sr-check-form" data-testid="sr-check-form">
        <div className="card-head">
          <div>
            <div className="card-title">对照检查</div>
            <div className="card-sub">
              拿一段文字对照《{book.title}》的作者：先量句子、标点、对白这些测得出的习惯，看它在作者自己的段落里排第几位；再请模型对着原文样例按 16 个维度打分；顺带查有没有照搬原文。
            </div>
          </div>
        </div>

        {noLlm && (
          <Notice
            tone="warn"
            testId="sr-check-no-llm"
            actions={onAction ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => onAction({ type: "settings" })}>去设置模型</button> : null}
          >
            还没有接入模型：对照检查要由模型对着原文样例评审。
          </Notice>
        )}

        <Segmented
          label="检查什么"
          value={mode}
          onChange={setMode}
          className="sr-check-mode"
          options={[
            { value: "text", label: "贴一段文字", testId: "sr-check-mode-text" },
            { value: "scene", label: `《${workTitle}》的一场`, disabled: !workId, testId: "sr-check-mode-scene" },
          ]}
        />

        {mode === "text" ? (
          <div className="sr-check-input">
            <label className="ws-sr-only" htmlFor="sr-check-text">要检查的文字</label>
            <textarea
              id="sr-check-text"
              className="textarea sr-check-textarea"
              rows={9}
              value={text}
              aria-invalid={count > CHECK_MAX_CHARS ? "true" : undefined}
              placeholder="贴一段你写的文字……一整场（上千字）量得最准。"
              data-testid="sr-check-text"
              onChange={(e) => setText(e.target.value)}
            />
            <div className="sr-check-count" aria-live="polite">
              <span className="tab-num">{count.toLocaleString("zh-CN")} 字</span>
              {count > CHECK_MAX_CHARS && <span className="is-over">一次最多查 {CHECK_MAX_CHARS.toLocaleString("zh-CN")} 字，请分段</span>}
              <span>只对照这份文风，不记到哪部作品上。</span>
            </div>
          </div>
        ) : (
          <div className="sr-check-input">
            {chapters == null ? (
              <p className="sr-ov-text sr-ov-muted"><Spinner size={12} /> 正在读取《{workTitle}》的场景…</p>
            ) : !groups.length ? (
              <p className="sr-ov-text sr-ov-muted" data-testid="sr-check-no-scenes">《{workTitle}》还没有场景：先在构思或章节编排里建场景。</p>
            ) : (
              <>
                <label className="ws-sr-only" htmlFor="sr-check-scene">选一场</label>
                <select id="sr-check-scene" className="select" data-testid="sr-check-scene" value={sceneId} onChange={(e) => setSceneId(e.target.value)}>
                  <option value="">选一场…</option>
                  {groups.map((group) => (
                    <optgroup key={group.label} label={group.label}>
                      {group.scenes.map((scene) => <option key={scene.value} value={scene.value}>{scene.label}</option>)}
                    </optgroup>
                  ))}
                </select>
                <p className="sr-ov-hint">
                  查这一场现在的正文：有终稿查终稿，没有就查最新的草稿或你写的稿子。
                  {applied ? `按《${workTitle}》现在用这本书的设置（重点 / 不学）来查。` : `《${workTitle}》没有用这本书，这次按这份文风画像查。`}
                </p>
              </>
            )}
          </div>
        )}

        <div className="sr-check-foot">
          <button type="button" className="btn btn-accent" data-testid="sr-check-start" disabled={busy || !ready || noLlm} onClick={() => start()}>
            {busy ? <><Spinner size={13} /> 正在检查…</> : <><I.Target size={14} /> 开始对照检查</>}
          </button>
          <span className="sr-check-hint">一次检查：本机量一遍（几秒）+ 模型评审一次（几十秒到几分钟）。</span>
        </div>
      </div>

      {entry && <SrCheckOutcome entry={entry} describe={describe} onAction={act} />}
    </div>
  );
}

function SrCheckOutcome({ entry, describe, onAction }) {
  const what = describe(entry.target);
  if (entry.phase === "starting" || entry.phase === "running") {
    const view = fidJobView(entry.job);
    return (
      <div className="card sr-check-running" data-testid="sr-check-running">
        <div className="sr-check-running-head"><Spinner size={13} /> 正在对照检查{what ? `：${what}` : ""}</div>
        <SrProgressBar percent={view.percent} label="对照检查进度" />
        <div className="sr-activity-meta">
          {[view.label || "排队中", view.elapsed != null && view.elapsed > 0 ? `已用 ${srFormatDuration(view.elapsed)}` : null].filter(Boolean).join(" · ")}
        </div>
        <p className="sr-ov-hint">可以离开这一页，检查在后台接着做，进度也在「参考书活动」里。</p>
      </div>
    );
  }
  if (entry.phase === "failed") {
    return <FidelityErrorLine info={fidErrorInfo(entry.error)} onAction={onAction} testId="sr-check-error" />;
  }
  const reading = entry.reading;
  if (!reading) return null;
  const finished = entry.job && (entry.job.finished_at || entry.job.created_at);
  return (
    <div className="sr-check-result" data-testid="sr-check-result">
      <div className="card">
        <div className="card-head">
          <div>
            <div className="card-title">像不像</div>
            <div className="card-sub">{[what, finished ? `检查于 ${srFormatWhen(finished)}` : null].filter(Boolean).join(" · ")}</div>
          </div>
          <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-check-again" onClick={() => onAction({ type: "retry" })}>
            <I.Refresh size={13} /> 再查一次
          </button>
        </div>
        <FidelityHeadline reading={reading} testId="sr-check-headline" />
        <div className="sr-check-lines">
          <FidelityJudgeLine judge={reading.judge} testId="sr-check-judge" />
          <FidelityCopyLine reading={reading} testId="sr-check-copy" />
        </div>
      </div>
      <div className="card">
        <FidelityGaps reading={reading} testId="sr-check-gaps" />
      </div>
      <div className="card">
        <div className="card-head"><div><div className="card-title">按维度</div><div className="card-sub">测得的几维和评审打的分；标「重点」「不学」的维按作品的设置算。</div></div></div>
        <FidelityDimensionTable reading={reading} judge={reading.judge} states={fidReadingStates(reading)} testId="sr-check-dims" />
      </div>
    </div>
  );
}
