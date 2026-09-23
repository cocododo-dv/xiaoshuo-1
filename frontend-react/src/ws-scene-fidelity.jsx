import React from "react";
import { I } from "./icons.jsx";
import { Spinner, Tag } from "./ws-ui.jsx";
import {
  fidErrorInfo, fidJobView, fidJudgeView, fidPatchView, fidRankText, fidReadingStates, fidStyleStepView, fidVerdict,
  fidWeakestDims,
} from "./ws-fidelity-model.js";
import { fidCheck, fidLoadScene, fidResumeCheck, fidScene, fidStartCheck, useFidelityStore } from "./ws-fidelity-store.js";
import { FidelityDimensionTable, FidelityErrorLine, FidelityGaps, FidelityMeter } from "./ws-fidelity-ui.jsx";

const { useEffect } = React;

/* ==========================================================
   AI 起草台 — 证据栏里的「像不像」（2026-09-23 风格参考 v3 · P6b）
   这一场这次运行（工作台 generation_summary.style_fidelity）一步步量出来的：
   首稿排第几位 → 风格步做了什么（在作者范围内没有再改 / 按几个维度定向修改并采用 / 改了没有更像、保留首稿）
   → 修改稿 → 补丁留没留（没有更像就退回）→ 终稿；参考评审分最低的几维与说明；和作者不一样的地方。
   本地没有这次运行的记录时，用这一场的读数接口（GET /api/v1/scenes/{id}/style-fidelity）补。
   「对照检查」对这一场现在的正文跑一次检查作业（风格参考同一个作业），结果记在这一场的读数里。
   数字的意思：把参考作者自己书里的段落按像不像作者本人排成 100 位（第 1 位最像），稿子排第几位。
   ========================================================== */

export function sceneCheckKey(sceneId) {
  return `scene:${sceneId}`;
}

/* 这次运行的一串（或读数接口补出来的一串）：{ first, step, revision, patch, final, judge } */
export function sceneFidelityChain(run, scene) {
  if (run) {
    return {
      first: run.first_draft || null,
      step: run.style_step || null,
      revision: run.revision || null,
      patch: run.patch || null,
      final: run.final || ((scene && scene.readings && scene.readings.final) || null),
      judge: run.judge || (scene && scene.judge) || null,
    };
  }
  const readings = (scene && scene.readings) || {};
  const decisions = Array.isArray(scene && scene.decisions) ? scene.decisions : [];
  const step = decisions.find((d) => d && d.kind === "style_step") || null;
  let patch = decisions.find((d) => d && d.kind === "patch") || null;
  // 读数接口给的是每个阶段最新的一条：只认与最新风格步同一次运行的修改稿 / 补丁，不把上一次运行的混进来
  if (patch && step && patch.bundle_id && step.bundle_id && patch.bundle_id !== step.bundle_id) patch = null;
  const revision = step && step.revision_reading_id && readings.revision && readings.revision.reading_id === step.revision_reading_id
    ? readings.revision : null;
  return {
    first: readings.first_draft || null,
    step,
    revision,
    patch,
    final: readings.final || null,
    judge: (scene && scene.judge) || null,
  };
}

function ReadingRow({ label, reading, testId }) {
  if (!reading) return null;
  const verdict = fidVerdict(reading);
  return (
    <div className="scn2-fid-row" data-testid={testId}>
      <dt>{label}</dt>
      <dd>
        <span className="scn2-fid-rank tab-num">{fidRankText(reading.percentile)}</span>
        {verdict && <Tag tone={verdict.tone}>{verdict.label}</Tag>}
      </dd>
    </div>
  );
}

function DecisionRow({ label, view, testId }) {
  if (!view) return null;
  return (
    <div className="scn2-fid-row" data-testid={testId}>
      <dt>{label}</dt>
      <dd className="scn2-fid-decision" data-tone={view.tone}>{view.text}</dd>
    </div>
  );
}

function CheckBlock({ sceneId, bound, manual, go }) {
  const key = sceneCheckKey(sceneId);
  const entry = fidCheck(key);
  const busy = !!entry && (entry.phase === "starting" || entry.phase === "running");
  const start = () => { if (!busy) fidStartCheck(key, { sceneId }); };
  const onAction = (action) => {
    if (!action) return;
    if (action.type === "retry") start();
    else if (action.type === "settings") { if (go) go("settings", { type: "ws:settings-tab", detail: "ai" }); }
    else if (go) go("styleref");
  };
  const view = busy ? fidJobView(entry.job) : null;
  const judge = manual ? fidJudgeView(manual.judge) : null;
  return (
    <div className="scn2-fid-check" data-testid="scene-fidelity-check">
      <div className="scn2-fid-check-head">
        <span className="scn2-fid-sub">对照检查</span>
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          data-testid="scene-fidelity-check-run"
          disabled={busy || !bound}
          title={bound ? "对这一场现在的正文量一次、请模型对着原文样例评审一次" : "这一场现在没有用参考书的文风"}
          onClick={start}
        >
          {busy ? <><Spinner size={11} /> 检查中…</> : <><I.Target size={12} /> 对照检查</>}
        </button>
      </div>
      {busy && <p className="scn2-fid-note" role="status">{view.label || "排队中"}：检查这一场现在的正文（终稿 → 最新的草稿 → 你写的稿子）。</p>}
      {!busy && entry && entry.phase === "failed" && (
        <FidelityErrorLine info={fidErrorInfo(entry.error)} onAction={onAction} testId="scene-fidelity-check-error" />
      )}
      {manual ? (
        <div className="scn2-fid-manual" data-testid="scene-fidelity-manual">
          <ReadingRow label="结果" reading={manual} />
          {judge && judge.overall != null && (
            <div className="scn2-fid-row"><dt>评审</dt><dd><FidelityMeter score={judge.overall} label="参考评审总分" /></dd></div>
          )}
          {judge && judge.summary && <p className="scn2-fid-note">{judge.summary}</p>}
        </div>
      ) : (!busy && !(entry && entry.phase === "failed") && (
        <p className="scn2-fid-note">{bound ? "还没对这一场做过对照检查。" : "这一场现在没有用参考书的文风，没法对照。"}</p>
      ))}
    </div>
  );
}

export function SceneFidelityPanel({ sceneId, runFidelity = null, go = null }) {
  useFidelityStore();
  const runKey = runFidelity
    ? [runFidelity.first_draft, runFidelity.revision, runFidelity.final].map((r) => (r && r.reading_id) || "").join("|")
    : "";
  useEffect(() => { if (sceneId) fidLoadScene(sceneId, { force: true }); }, [sceneId, runKey]);
  useEffect(() => { if (sceneId) fidResumeCheck(sceneCheckKey(sceneId)); }, [sceneId]);
  if (!sceneId) return null;
  const entry = fidScene(sceneId);
  const scene = entry && entry.data;
  const chain = sceneFidelityChain(runFidelity, scene);
  const bound = scene ? !!scene.bound : !!runFidelity;
  const manual = (scene && scene.readings && scene.readings.manual) || null;
  // 没绑定的场景没有「参考评审」可言：润色口径的软 QC 顺手给的分不是像不像，不画「参考评审总分」
  const judgeSource = bound ? chain.judge : null;
  const hasChain = !!(chain.first || chain.step || chain.revision || chain.patch || chain.final || judgeSource);
  if (!hasChain && !manual && !bound) return null;

  const stepView = fidStyleStepView(chain.step);
  const patchView = fidPatchView(chain.patch);
  const weakest = fidWeakestDims(judgeSource, 3);
  const judge = fidJudgeView(judgeSource);
  const latest = chain.final || chain.revision || chain.first;
  const latestLabel = chain.final ? "终稿" : chain.revision ? "修改稿" : "首稿";

  return (
    <section className="scn2-evi-block scn2-fid" data-testid="scene-fidelity" aria-label="像不像">
      <h3 className="scn2-evi-h" title="把参考作者自己书里的段落按像不像作者本人排成 100 位（第 1 位最像），看稿子排第几位">
        <I.Target size={13} /> 像不像
      </h3>
      {hasChain ? (
        <dl className="scn2-fid-steps">
          <ReadingRow label="首稿" reading={chain.first} testId="scene-fidelity-first" />
          <DecisionRow label="风格步" view={stepView} testId="scene-fidelity-step" />
          <ReadingRow label="修改稿" reading={chain.revision} testId="scene-fidelity-revision" />
          <DecisionRow label="补丁" view={patchView} testId="scene-fidelity-patch" />
          <ReadingRow label="终稿" reading={chain.final} testId="scene-fidelity-final" />
        </dl>
      ) : (
        <p className="scn2-fid-note">这一场还没有读数：起草时会先量首稿；也可以现在对照检查现有的正文。</p>
      )}
      {judge && (
        <div className="scn2-fid-judge" data-testid="scene-fidelity-judge">
          <div className="scn2-fid-row"><dt>评审</dt><dd>{judge.overall != null ? <FidelityMeter score={judge.overall} label="参考评审总分" /> : "—"}</dd></div>
          {weakest.length > 0 && (
            <ul className="scn2-fid-weak" aria-label="评审分最低的几维">
              {weakest.map((dim) => (
                <li key={dim.dimension}>
                  <span className="scn2-fid-weak-name">{dim.label}</span>
                  <FidelityMeter score={dim.score} label={dim.label} />
                  {dim.note && <span className="scn2-fid-weak-note">{dim.note}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {latest && <FidelityGaps reading={latest} limit={4} title={`和作者不一样的地方（${latestLabel}）`} testId="scene-fidelity-gaps" />}
      {latest && (judge || latest.dimension_scores) && (
        <details className="scn2-fid-dims">
          <summary>16 个维度都看</summary>
          <FidelityDimensionTable reading={latest} judge={judgeSource} states={fidReadingStates(latest)} />
        </details>
      )}
      <CheckBlock sceneId={sceneId} bound={bound} manual={manual} go={go} />
    </section>
  );
}
