import React from "react";
import { I } from "./icons.jsx";
import { WsChapterPlan, cpFieldLabel, cpPatchRows, cpRowsToPatch } from "./ws-chapter-plan.jsx";
import { Notice, Tabs, Tag } from "./ws-ui.jsx";
import { wsConfirm } from "./ws-notify.jsx";

/* ==========================================================
   章节编排 · AI 编排 UI（docs/chapter-arrangement-llm-design-2026-07-16.md §7）
   「计划」在章节编排里有三种意思，名字分开：这里是 AI 编排（store 是 WsChapterPlan）；
   「整理章节结构」是构思的分章面板（WsChapterPlanPanel）；构思条（ArrPlanStrip）说这一章在构思里是什么。
   两个挂点：
   · ArrAiArrange —— 章节详情里唯一的「AI 编排」卡，三个页签：
       蓝图（ArrAiBlueprint：读 / 改 / 重生成）· 方向（三个编排方向）· 补全（一键补全 + 逐条确认的补丁 → plan/apply）
   · ArrAiHealth —— 右栏「章节体检」里的 AI 体检（规则体检免费兜底，AI 补结构性判断）
   所有 LLM 产物都是咨询式补丁：必须经作者逐条确认，没有静默改卡。
   失败不弹浏览器对话框：store 把错误挂在 action.error 上，这里就地说清是哪一步没成。
   ========================================================== */

const { useState: useStP, useEffect: useEfP, useRef: useRefP, useSyncExternalStore: useSyncP } = React;

function useChapterPlan(chapterId) {
  useSyncP(WsChapterPlan.subscribe, () => WsChapterPlan.version());
  return WsChapterPlan.snapshot(chapterId);
}

/* LLM 未配置时后端给的 author_action：引导而非阻断（去系统配置由宿主的 goView 决定怎么走） */
function ArrAiActionHint({ action, onConfigureModel }) {
  if (!action) return null;
  return (
    <Notice tone="warn" className="arr-ai-note"
      actions={onConfigureModel
        ? <button type="button" className="btn btn-ghost btn-sm" onClick={onConfigureModel}>{action.primary_button_label || "去系统配置"}</button>
        : null}>
      {action.message || action.title || "当前还没有可用的 AI 模型配置。"}
    </Notice>
  );
}

/* 失败说哪一步没成、目录动没动。kinds = 这一处关心的动作。 */
const ARR_AI_FAILED = {
  "arch-save": "保存章节蓝图没有成功",
  "arch-generate": "生成章节蓝图没有成功",
  candidates: "生成编排方向没有成功",
  fill: "生成补全建议没有成功",
  apply: "写入改动没有成功，目录没有被改动",
  review: "AI 体检没有成功",
};
function ArrAiError({ snap, kinds }) {
  const err = snap.action && snap.action.error;
  const kind = snap.action && snap.action.kind;
  if (!err || !kinds.includes(kind)) return null;
  return (
    <Notice tone="danger" className="arr-ai-note">
      {ARR_AI_FAILED[kind] || "请求没有成功"}：{err.message || "请稍后重试"}
    </Notice>
  );
}

/* 上下文降级提示：缺哪块料，AI 建议就弱哪块 */
const ARR_AI_DEGRADED = {
  chapter_architecture: "未生成章节蓝图",
  snowflake_canon: "无雪花构思可用",
  narrative_state: "无叙事事件账本",
  author_preferences: "无作者偏好档案",
};
function ArrAiDegraded({ slots }) {
  const items = (slots || []).map((s) => ARR_AI_DEGRADED[s]).filter(Boolean);
  if (!items.length) return null;
  return (
    <div className="arr-ai-chips" aria-label="这次建议缺了哪些上下文">
      {items.map((label, i) => <Tag key={i} tone="neutral" dot>{label}</Tag>)}
    </div>
  );
}

const ARR_AI_DROP_REASONS = {
  field_not_empty: "已有内容，不覆盖",
  field_not_allowed: "不允许 AI 改动",
  unknown_scene: "场景不存在",
  empty_value: "空建议",
  append_cap_reached: "追加数量达上限",
  title_required: "缺标题",
  // 阶段 Y：雪花整理出来的场，设计在构思第 10 步改——章节规划 AI 不往里填
  design_owned_by_plan: "这一场的设计在构思第 10 步改",
};

/* ==========================================================
   蓝图页签 — 章节蓝图（会注入本章每一场的 AI 起草上下文）
   草稿由 ArrAiArrange 持有：切页签、换到别的卡片区都不丢；只有换章才清。
   ========================================================== */
const ARR_BP_FIELDS = [
  { k: "promise", label: "章承诺", hint: "本章向读者兑现什么" },
  { k: "payoff", label: "兑现目标", hint: "承诺落在哪个具体画面 / 事件" },
  { k: "shift", label: "人物变化", hint: "谁从什么状态到什么状态" },
  { k: "endingQuestion", label: "结尾问题", hint: "读完本章读者带走的问题" },
];

function ArrAiBlueprint({ ch, locked, active, draft, setDraft }) {
  const chapterId = ch && ch.backendId;
  const snap = useChapterPlan(chapterId);
  const busy = snap.action.busy;
  const status = snap.arch.status;

  /* 打开蓝图页签时读一次；上次读失败就再读一次（以前只在 idle 时读，失败后要刷新整页才会重试） */
  useEfP(() => {
    if (active && chapterId && (status === "idle" || status === "error")) {
      WsChapterPlan.loadArchitecture(chapterId).catch(() => {});
    }
    // status 变化不重新触发：失败后等作者再次打开页签，不在后台反复重试
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chapterId, active]);

  if (!chapterId) return null;
  const arch = snap.arch.data;
  const view = draft || arch || { promise: "", escalation: [], reveals: [], payoff: "", shift: "", endingQuestion: "" };
  const edit = (key, value) => setDraft({ ...view, [key]: value });
  const editLines = (key, value) => setDraft({ ...view, [key]: value.split("\n") });

  const save = async () => {
    if (!draft) return;
    try { await WsChapterPlan.saveArchitecture(chapterId, draft); setDraft(null); } catch (e) { /* 错误在下方就地显示 */ }
  };
  const regenerate = async () => {
    if (arch && !(await wsConfirm({
      title: "重新生成章节蓝图？",
      body: "新蓝图会取代现在这一版（旧版留档，但这里不能再切回去）。",
      confirmLabel: "重新生成",
    }))) return;
    try { const next = await WsChapterPlan.generateArchitecture(chapterId); if (next) setDraft(null); } catch (e) { /* 同上 */ }
  };

  return (
    <div className="arr-ai-pane">
      <p className="arr-sync">
        {arch ? (arch.fromLlm ? "AI 生成的蓝图，可以直接改写。" : "你改写过的蓝图。") : "这一章还没有蓝图。"}
        保存后会进入本章每一场的 AI 起草上下文。
        {draft ? <Tag tone="warn" className="arr-ai-unsaved">有未保存的改动</Tag> : null}
      </p>
      {status === "loading" && <p className="arr-sync" role="status">正在读取章节蓝图…</p>}
      {status === "error" && (
        <Notice tone="danger" className="arr-ai-note">章节蓝图读取失败：{(snap.arch.error && snap.arch.error.message) || "请稍后再打开这个页签"}</Notice>
      )}
      {status !== "loading" && (
        <>
          <div className="arr-bp-grid">
            {ARR_BP_FIELDS.map((f) => (
              <div className="arr-field" key={f.k}>
                <header className="arr-field-head">
                  <span className="arr-field-label">{f.label}</span>
                  <span className="arr-field-hint">{f.hint}</span>
                </header>
                <textarea className="arr-field-text" rows={2} value={view[f.k] || ""} disabled={locked || busy}
                  aria-label={`章节蓝图 · ${f.label}`} placeholder="还没写"
                  onChange={(e) => edit(f.k, e.target.value)} />
              </div>
            ))}
            <div className="arr-field">
              <header className="arr-field-head">
                <span className="arr-field-label">升级路径</span>
                <span className="arr-field-hint">一行一级，每级换一种压力</span>
              </header>
              <textarea className="arr-field-text" rows={3} value={(view.escalation || []).join("\n")}
                disabled={locked || busy} aria-label="章节蓝图 · 升级路径"
                onChange={(e) => editLines("escalation", e.target.value)} />
            </div>
            <div className="arr-field">
              <header className="arr-field-head">
                <span className="arr-field-label">揭示计划</span>
                <span className="arr-field-hint">一行一条，本章亮出哪些牌</span>
              </header>
              <textarea className="arr-field-text" rows={3} value={(view.reveals || []).join("\n")}
                disabled={locked || busy} aria-label="章节蓝图 · 揭示计划"
                onChange={(e) => editLines("reveals", e.target.value)} />
            </div>
          </div>
          <div className="arr-ai-actions">
            <button type="button" className="btn btn-accent btn-sm" disabled={locked || busy || !draft || !(view.promise || "").trim()} onClick={save}>
              <I.Check size={13} /> 保存我的版本
            </button>
            {draft ? (
              <button type="button" className="btn btn-quiet btn-sm" disabled={busy} onClick={() => setDraft(null)}>放弃改动</button>
            ) : null}
            <button type="button" className="btn btn-ghost btn-sm" disabled={locked || busy || !!draft}
              title={draft ? "先保存或放弃手上的改动，再让 AI 重写" : undefined}
              onClick={regenerate}>
              <I.Refresh size={13} /> {busy && snap.action.kind === "arch-generate" ? "生成中…" : arch ? "重新生成" : "AI 生成蓝图"}
            </button>
            {arch && arch.createdAt ? <span className="arr-field-hint">{arch.createdAt.slice(0, 10)} 的版本</span> : null}
          </div>
        </>
      )}
    </div>
  );
}

const ARR_AI_TABS = [
  { id: "blueprint", label: "蓝图" },
  { id: "directions", label: "方向" },
  { id: "fill", label: "补全" },
];

/* ==========================================================
   ArrAiArrange — 「AI 编排」卡（蓝图 / 方向 / 补全）
   ========================================================== */
function ArrAiArrange({ ch, locked, sectionRef, onConfigureModel }) {
  const chapterId = ch && ch.backendId;
  const snap = useChapterPlan(chapterId);
  const [tab, setTab] = useStP("blueprint");
  const [hint, setHint] = useStP("");
  const [checked, setChecked] = useStP({});
  const [bpDraft, setBpDraft] = useStP(null);
  const lastChapter = useRefP(chapterId);
  const busy = snap.action.busy;
  const sceneNameOf = (backendSceneId) => {
    const scene = (ch.scenes || []).find((s) => s.backendId === backendSceneId);
    return scene ? scene.title : "已不在本章的场";
  };

  /* 换章才清蓝图草稿（以前开合折叠区也会清，没保存的改动悄悄没了） */
  useEfP(() => {
    if (lastChapter.current !== chapterId) { lastChapter.current = chapterId; setBpDraft(null); }
  }, [chapterId]);
  useEfP(() => { setChecked({}); }, [chapterId, snap.fill]);

  if (!chapterId) return null;

  const runCandidates = () => WsChapterPlan.requestCandidates(chapterId, hint.trim() || undefined).catch(() => {});
  const runFill = (candidate) => {
    setTab("fill");
    return WsChapterPlan.requestFill(chapterId, candidate ? { candidate } : {})
      .then((fill) => {
        if (fill && !fill.offline) {
          const rows = cpPatchRows(fill.patch, sceneNameOf);
          setChecked(Object.fromEntries(rows.map((r) => [r.key, true])));  // 默认全选，作者按行取消
        }
      })
      .catch(() => {});
  };
  const fill = snap.fill;
  const rows = fill && !fill.offline ? cpPatchRows(fill.patch, sceneNameOf) : [];
  const checkedCount = rows.filter((row) => checked[row.key]).length;
  const applyChecked = () => {
    const patch = cpRowsToPatch(rows, checked);
    if (!Object.keys(patch.drama).length && !patch.scenes.length && !patch.append_scenes.length) return;
    WsChapterPlan.applyPatch(chapterId, patch).catch(() => {});
  };
  const planOwnedScenes = (ch.scenes || []).some((s) => s.design && s.design.owner === "plan");
  const tabs = ARR_AI_TABS.map((t) => ({
    ...t,
    label: t.id === "blueprint" && bpDraft ? "蓝图 · 未保存" : t.label,
    count: t.id === "directions" && snap.candidates ? snap.candidates.items.length
      : t.id === "fill" && rows.length ? rows.length : undefined,
  }));
  const idPrefix = `arr-ai-${ch.id}`;

  return (
    <section className="card arr-ai" ref={sectionRef} aria-labelledby={`${idPrefix}-title`}>
      <div className="card-head">
        <div>
          <h2 className="card-title" id={`${idPrefix}-title`}>AI 编排</h2>
          <div className="card-sub">
            {planOwnedScenes
              ? "用全书上下文补这一章的戏剧卡、给你手加的场出主意；雪花整理出来的场设计在构思第 10 步改，这里不往里填。每条建议都要你勾选才写入，不覆盖你写过的内容。"
              : "用全书上下文给这一章的戏剧卡和场景卡出主意。每条建议都要你勾选才写入，不覆盖你写过的内容。"}
          </div>
        </div>
      </div>

      <Tabs value={tab} tabs={tabs} onChange={setTab} label="AI 编排" idPrefix={idPrefix} className="arr-ai-tabs" />

      <div role="tabpanel" id={`${idPrefix}-panel-blueprint`} aria-labelledby={`${idPrefix}-tab-blueprint`} hidden={tab !== "blueprint"}>
        <ArrAiBlueprint ch={ch} locked={locked} active={tab === "blueprint"} draft={bpDraft} setDraft={setBpDraft} />
        <ArrAiError snap={snap} kinds={["arch-save", "arch-generate"]} />
      </div>

      <div role="tabpanel" id={`${idPrefix}-panel-directions`} aria-labelledby={`${idPrefix}-tab-directions`} hidden={tab !== "directions"}>
        <div className="arr-ai-pane">
          <p className="arr-sync">先要三个不同的编排方向，挑一个再生成补丁。可以写一句你想往哪边走。</p>
          <div className="arr-ai-actions">
            <input className="input arr-ai-hint" value={hint} placeholder="方向倾向（可留空）"
              disabled={locked || busy} aria-label="编排方向倾向"
              onChange={(e) => setHint(e.target.value)} />
            <button type="button" className="btn btn-ghost btn-sm" disabled={locked || busy} onClick={runCandidates}>
              <I.GitBranch size={13} /> {busy && snap.action.kind === "candidates" ? "构思中…" : snap.candidates ? "换三个方向" : "给三个方向"}
            </button>
          </div>
          <ArrAiError snap={snap} kinds={["candidates"]} />
          {snap.candidates && (
            <div className="arr-ai-list">
              <ArrAiDegraded slots={snap.candidates.degraded} />
              {!snap.candidates.items.length && <p className="arr-sync">这次没有给出方向，换一句倾向再试。</p>}
              {snap.candidates.items.map((cand, i) => (
                <div className="arr-field arr-ai-cand" key={i}>
                  <header className="arr-field-head">
                    <span className="arr-field-label">「{cand.label}」</span>
                    <button type="button" className="btn btn-quiet btn-sm" disabled={locked || busy} onClick={() => runFill(cand)}>
                      按这个方向补全
                    </button>
                  </header>
                  <div className="arr-ai-cand-body">
                    <div>{cand.rationale}</div>
                    {cand.risk ? <div className="arr-ai-cand-risk">代价：{cand.risk}</div> : null}
                    <div className="arr-ai-chips">
                      {(cand.scene_plan || []).map((s, j) => (
                        <Tag key={j} tone={s.kind === "reactive" ? "info" : "accent"} dot>{s.ref_scene_id ? "改" : "新"}·{s.title}</Tag>
                      ))}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div role="tabpanel" id={`${idPrefix}-panel-fill`} aria-labelledby={`${idPrefix}-tab-fill`} hidden={tab !== "fill"}>
        <div className="arr-ai-pane">
          <div className="arr-ai-actions">
            <button type="button" className="btn btn-accent btn-sm" disabled={locked || busy} onClick={() => runFill(null)}>
              <I.Sparkles size={13} /> {busy && snap.action.kind === "fill" ? "补全中…" : "一键补全"}
            </button>
            <span className="arr-field-hint">只填空着的格子；写入前逐条勾选。</span>
          </div>
          <ArrAiError snap={snap} kinds={["fill", "apply"]} />
          {fill && (
            <div className="arr-ai-list">
              <ArrAiDegraded slots={fill.degraded} />
              {fill.offline ? (
                <div className="arr-sync">
                  AI 还没接上，先给出待补清单：
                  <ul className="arr-ai-bullets">
                    {fill.gaps.map((g, i) => <li key={i}>{g}</li>)}
                  </ul>
                </div>
              ) : rows.length ? (
                <>
                  <ul className="arr-ai-rows">
                    {rows.map((row) => (
                      <li key={row.key}>
                        <label className="arr-ai-row">
                          <input type="checkbox" checked={!!checked[row.key]} disabled={locked || busy}
                            onChange={(e) => setChecked({ ...checked, [row.key]: e.target.checked })} />
                          <span className="arr-ai-row-label">{row.label}</span>
                          <span className="arr-ai-row-value">{row.value}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                  <div className="arr-ai-actions">
                    <button type="button" className="btn btn-accent btn-sm" disabled={locked || busy || !checkedCount} onClick={applyChecked}>
                      <I.Check size={13} /> {busy && snap.action.kind === "apply" ? "写入中…" : `写入勾选的 ${checkedCount} 条`}
                    </button>
                    <span className="arr-field-hint">没勾的和被拒的条目不动。</span>
                  </div>
                </>
              ) : (
                <p className="arr-sync">场景卡没有可补的空格，都已填好。</p>
              )}
              {!!(fill.notes || []).length && (
                <div className="arr-sync">
                  <b>会覆盖已有内容的建议（不自动写入）：</b>
                  <ul className="arr-ai-bullets">
                    {fill.notes.map((n, i) => (
                      <li key={i}>{n.scene_id ? `${sceneNameOf(n.scene_id)} · ` : ""}{n.suggestion}{n.reason ? `（${n.reason}）` : ""}</li>
                    ))}
                  </ul>
                </div>
              )}
              {!!(fill.dropped || []).length && (
                <p className="arr-sync">
                  按护栏拒掉 {fill.dropped.length} 条：{fill.dropped.slice(0, 5).map((d, i) => (
                    <span key={i}>{cpFieldLabel(d.field)}（{ARR_AI_DROP_REASONS[d.reason] || "不符合护栏"}）{i < Math.min(fill.dropped.length, 5) - 1 ? "、" : ""}</span>
                  ))}{fill.dropped.length > 5 ? " …" : ""}
                </p>
              )}
            </div>
          )}
          {snap.applied && (
            <Notice tone="ok" className="arr-ai-note">
              已补全戏剧卡 {snap.applied.drama} 项、写入 {snap.applied.scenes} 张场景卡、追加 {snap.applied.appended} 场
              {snap.applied.skipped.length
                ? `；${snap.applied.skipped.length} 条被跳过（${snap.applied.skipped.some((d) => d.reason === "design_owned_by_plan") ? "已有内容，或这一场的设计在构思第 10 步改" : "已有内容"}）`
                : ""}。
            </Notice>
          )}
        </div>
      </div>

      <ArrAiActionHint action={snap.authorAction} onConfigureModel={onConfigureModel} />
    </section>
  );
}

/* ==========================================================
   ArrAiHealth — 右栏「章节体检」里的 AI 体检
   ========================================================== */
const ARR_AI_FINDINGS = {
  PROMISE_UNGROUNDED: "承诺不落地",
  SCENE_FUNCTION_DUPLICATE: "场景功能重复",
  REACTIVE_MISSING: "缺反应场",
  TENSION_FLAT: "张力不升级",
  FORESHADOW_OVERDUE: "伏笔逾期",
  POV_FATIGUE: "视角疲劳",
  HANDOFF_MISMATCH: "承接错位",
  EXIT_NO_CHANGE: "结尾无变化",
  BRIEF_INCOMPLETE: "场景卡不完整",
  OTHER: "其他",
};

function ArrAiHealth({ ch, locked, onConfigureModel }) {
  const chapterId = ch && ch.backendId;
  const snap = useChapterPlan(chapterId);
  if (!chapterId) return null;
  const busy = snap.action.busy;
  const review = snap.review;
  const run = () => WsChapterPlan.requestReview(chapterId).catch(() => {});
  const applySuggestion = (finding) => {
    WsChapterPlan.applyPatch(chapterId, finding.suggestion_patch).catch(() => {});
  };

  return (
    <div className="arr-ai-health">
      <div className="arr-ai-health-head">
        <span className="arr-ai-health-title"><I.Microscope size={13} /> AI 体检</span>
        <button type="button" className="btn btn-quiet btn-xs" disabled={busy} onClick={run}>
          {busy && snap.action.kind === "review" ? "体检中…" : review ? "重新体检" : "开始体检"}
        </button>
      </div>
      {review ? (
        review.findings.length ? (
          <ul className="arr-checks arr-ai-findings">
            {review.findings.map((f, i) => (
              <li key={i} className={`arr-check ${f.severity === "warn" ? "is-warn" : ""}`}>
                {f.severity === "warn" ? <I.AlertTriangle size={13} /> : <I.Circle size={13} />}
                <span className="arr-ai-finding">
                  <span className="arr-check-label">
                    {ARR_AI_FINDINGS[f.code] || ARR_AI_FINDINGS.OTHER}
                    {review.source === "fallback" ? "" : " · AI"}
                  </span>
                  <span className="arr-ai-finding-text">{f.summary || f.evidence}</span>
                  {f.suggestion_patch && !locked && (
                    <button type="button" className="btn btn-quiet btn-xs arr-ai-finding-apply" disabled={busy}
                      onClick={() => applySuggestion(f)}>应用建议</button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="arr-sync">没有发现结构性问题。</p>
        )
      ) : (
        <p className="arr-sync">用全书上下文看这一章：和上一章接不接得上、视角分布、场景卡写全了没有。</p>
      )}
      <ArrAiError snap={snap} kinds={["review", "apply"]} />
      <ArrAiActionHint action={snap.authorAction} onConfigureModel={onConfigureModel} />
    </div>
  );
}

export { ArrAiArrange, ArrAiBlueprint, ArrAiHealth };
