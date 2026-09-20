import React from "react";
import { I } from "./icons.jsx";
import { WsChapterPlan } from "./ws-chapter-plan.jsx";

/* global React, I */
/* ==========================================================
   章节编排 · AI 规划 UI（docs/chapter-arrangement-llm-design-2026-07-16.md §7）
   三个挂点：
   · ArrBlueprintCard —— 戏剧卡内的「章节蓝图」折叠区（读 / 改 / 重生成）
   · ArrPlanPanel —— 场景看板下方的「AI 编排」面板（三方向候选 + 一键补全
     + diff 式补丁确认 → plan/apply）
   · ArrAiHealthBlock —— 右栏章节体检的「AI 体检」合流块
   所有 LLM 产物都是咨询式补丁：必须经作者逐条确认，没有静默改卡。
   ========================================================== */

const { useState: useStP, useEffect: useEfP, useSyncExternalStore: useSyncP } = React;

function useChapterPlan(chapterId) {
  useSyncP(WsChapterPlan.subscribe, () => WsChapterPlan.version());
  return WsChapterPlan.snapshot(chapterId);
}

const cpGoSettings = () => { location.hash = "#settings"; };

/* fallback 的 author_action 提示条（LLM 未配置：引导而非阻断） */
function CpActionHint({ action }) {
  if (!action) return null;
  return (
    <div className="arr-locked-note" role="status" style={{ marginTop: 8 }}>
      <I.AlertTriangle size={14} />
      <span>{action.message || action.title || "当前还没有可用的 LLM 运行配置。"}</span>
      <button className="btn btn-ghost btn-sm" onClick={cpGoSettings}>
        {action.primary_button_label || "去系统配置"}
      </button>
    </div>
  );
}

/* 上下文降级提示 chips：缺哪块料，AI 建议就弱哪块 */
const CP_DEGRADED_LABELS = {
  chapter_architecture: "未生成章节蓝图",
  snowflake_canon: "无雪花构思可用",
  narrative_state: "无叙事事件账本",
  foreshadow_debts: "无伏笔台账",
  author_preferences: "无作者偏好档案",
};
function CpDegradedChips({ slots }) {
  const items = (slots || []).map((s) => CP_DEGRADED_LABELS[s]).filter(Boolean);
  if (!items.length) return null;
  return (
    <div className="arr-sync" style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 }}>
      {items.map((label, i) => (
        <span key={i} className="pill pill-slate text-xs"><span className="pill-dot" />{label}</span>
      ))}
    </div>
  );
}

/* ---- 字段中文名 ---- */
const CP_FIELD_LABELS = {
  goal: "目标", conflict: "阻碍", setback: "出口",
  reaction: "反应", dilemma: "困境", decision: "决定",
  pov_character_name: "POV", exit_change: "出口变化", hook: "钩子", title: "标题",
  "drama.promise": "核心承诺", "drama.spine": "主线推进",
  "drama.arc": "人物变化", "drama.problem": "章节问题",
  "drama.aftertaste": "结尾余味", "drama.ending": "结尾效果",
};
const cpFieldLabel = (key) => CP_FIELD_LABELS[key] || key;
const CP_DROP_REASONS = {
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
   ArrBlueprintCard — 章节蓝图（戏剧卡内折叠区）
   ========================================================== */
const CP_BP_FIELDS = [
  { k: "promise", label: "章承诺", hint: "本章向读者兑现什么" },
  { k: "payoff", label: "兑现目标", hint: "承诺落在哪个具体画面/事件" },
  { k: "shift", label: "人物变化", hint: "谁从什么状态到什么状态" },
  { k: "endingQuestion", label: "结尾问题", hint: "读完本章读者带走的问题" },
];

function ArrBlueprintCard({ ch, locked }) {
  const chapterId = ch && ch.backendId;
  const snap = useChapterPlan(chapterId);
  const [open, setOpen] = useStP(false);
  const [draft, setDraft] = useStP(null);   // null = 未进入编辑，直接展示 store 数据
  const busy = snap.action.busy;

  useEfP(() => {
    setDraft(null);
    if (open && chapterId && snap.arch.status === "idle") {
      WsChapterPlan.loadArchitecture(chapterId).catch(() => {});
    }
  }, [chapterId, open]);

  if (!chapterId) return null;
  const arch = snap.arch.data;
  const view = draft || arch || { promise: "", escalation: [], reveals: [], payoff: "", shift: "", endingQuestion: "" };
  const edit = (key, value) => setDraft({ ...view, [key]: value });
  const editLines = (key, value) => setDraft({ ...view, [key]: value.split("\n") });

  const save = async () => {
    if (!draft) return;
    try { await WsChapterPlan.saveArchitecture(chapterId, draft); setDraft(null); }
    catch (e) { window.alert("保存章节蓝图失败：" + ((e && e.message) || "请稍后重试")); }
  };
  const regenerate = async () => {
    if (arch && !window.confirm("重新生成会取代当前蓝图（旧版留档为 superseded）。继续？")) return;
    try { await WsChapterPlan.generateArchitecture(chapterId); setDraft(null); }
    catch (e) { window.alert("生成章节蓝图失败：" + ((e && e.message) || "请稍后重试")); }
  };

  return (
    <div className="arr-dgroup" style={{ marginTop: 10 }}>
      <header className="arr-dgroup-head tone-gold" style={{ cursor: "pointer" }} onClick={() => setOpen(!open)}>
        <I.Layers size={13} />
        <span>章节蓝图</span>
        <span className="arr-field-hint" style={{ fontWeight: 400 }}>
          {arch ? (arch.fromLlm ? "AI 生成 · 可改写" : "作者改写版") : "未生成"} · 会注入本章每一场的 AI 起草上下文
        </span>
        <i className="arr-dgroup-rule" />
        <I.ChevronRight size={13} style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s" }} />
      </header>
      {open && (
        <div style={{ display: "grid", gap: 8, padding: "8px 2px" }}>
          {snap.arch.status === "loading" && <span className="arr-sync">正在读取章节蓝图…</span>}
          {snap.arch.status === "error" && (
            <span className="arr-sync">蓝图读取失败：{snap.arch.error && snap.arch.error.message}</span>
          )}
          {snap.arch.status !== "loading" && (
            <React.Fragment>
              {CP_BP_FIELDS.map((f) => (
                <div className="arr-field" key={f.k}>
                  <header className="arr-field-head">
                    <span className="arr-field-label">{f.label}</span>
                    <span className="arr-field-hint">{f.hint}</span>
                  </header>
                  <textarea className="arr-field-text" rows={2} value={view[f.k] || ""} disabled={locked || busy}
                    aria-label={`章节蓝图 · ${f.label}`} placeholder="待生成…"
                    onChange={(e) => edit(f.k, e.target.value)} />
                </div>
              ))}
              <div className="arr-field">
                <header className="arr-field-head">
                  <span className="arr-field-label">升级路径</span>
                  <span className="arr-field-hint">一行一级；每级引入不同类型的压力</span>
                </header>
                <textarea className="arr-field-text" rows={3} value={(view.escalation || []).join("\n")}
                  disabled={locked || busy} aria-label="章节蓝图 · 升级路径"
                  onChange={(e) => editLines("escalation", e.target.value)} />
              </div>
              <div className="arr-field">
                <header className="arr-field-head">
                  <span className="arr-field-label">揭示计划</span>
                  <span className="arr-field-hint">一行一条；本章亮出哪些牌</span>
                </header>
                <textarea className="arr-field-text" rows={2} value={(view.reveals || []).join("\n")}
                  disabled={locked || busy} aria-label="章节蓝图 · 揭示计划"
                  onChange={(e) => editLines("reveals", e.target.value)} />
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <button className="btn btn-accent btn-sm" disabled={locked || busy || !draft || !(view.promise || "").trim()} onClick={save}>
                  <I.Check size={13} /> 保存为作者版
                </button>
                <button className="btn btn-ghost btn-sm" disabled={locked || busy} onClick={regenerate}>
                  <I.Refresh size={13} /> {busy && snap.action.kind === "arch-generate" ? "生成中…" : arch ? "重新生成" : "AI 生成蓝图"}
                </button>
                {arch && <span className="arr-field-hint">来源：{arch.createdBy || "—"} · {arch.createdAt ? arch.createdAt.slice(0, 10) : ""}</span>}
              </div>
              <CpActionHint action={snap.authorAction} />
            </React.Fragment>
          )}
        </div>
      )}
    </div>
  );
}

/* ==========================================================
   ArrPlanPanel — AI 编排（候选 / 补全 / 补丁确认）
   ========================================================== */

/* 补丁 → 可勾选行的展开：每行 = 一处填空或一张追加卡 */
function cpPatchRows(patch, sceneNameOf) {
  const rows = [];
  Object.entries(patch.drama || {}).forEach(([field, value]) => {
    rows.push({
      key: `drama:${field}`,
      kind: "drama", field, value,
      label: `章节戏剧卡 · ${cpFieldLabel(`drama.${field}`)}`,
    });
  });
  (patch.scenes || []).forEach((item) => {
    Object.entries(item.set || {}).forEach(([field, value]) => {
      rows.push({
        key: `set:${item.scene_id}:${field}`,
        kind: "set", sceneId: item.scene_id, field, value,
        label: `${sceneNameOf(item.scene_id)} · ${cpFieldLabel(field)}`,
      });
    });
  });
  (patch.append_scenes || []).forEach((item, i) => {
    rows.push({
      key: `append:${i}`,
      kind: "append", append: item,
      label: `新场景 · ${item.title}（${item.kind === "reactive" ? "反应" : "主动"}）`,
      value: Object.entries(item.brief || {}).map(([k, v]) => `${cpFieldLabel(k)}：${v}`).join(" / ") || "（三拍待写）",
    });
  });
  return rows;
}

function cpRowsToPatch(rows, checked) {
  const drama = {};
  const sceneMap = {};
  const appends = [];
  rows.forEach((row) => {
    if (!checked[row.key]) return;
    if (row.kind === "drama") {
      drama[row.field] = row.value;
    } else if (row.kind === "set") {
      sceneMap[row.sceneId] = sceneMap[row.sceneId] || { scene_id: row.sceneId, set: {} };
      sceneMap[row.sceneId].set[row.field] = row.value;
    } else {
      appends.push(row.append);
    }
  });
  return { drama, scenes: Object.values(sceneMap), append_scenes: appends };
}

function ArrPlanPanel({ ch, locked }) {
  const chapterId = ch && ch.backendId;
  const snap = useChapterPlan(chapterId);
  const [hint, setHint] = useStP("");
  const [checked, setChecked] = useStP({});
  const busy = snap.action.busy;
  const sceneNameOf = (backendSceneId) => {
    const scene = (ch.scenes || []).find((s) => s.backendId === backendSceneId);
    return scene ? scene.title : backendSceneId;
  };

  useEfP(() => { setChecked({}); }, [chapterId, snap.fill]);

  if (!chapterId) return null;

  const runCandidates = () => WsChapterPlan.requestCandidates(chapterId, hint.trim() || undefined)
    .catch((e) => window.alert("生成编排候选失败：" + ((e && e.message) || "请稍后重试")));
  const runFill = (candidate) => WsChapterPlan.requestFill(chapterId, candidate ? { candidate } : {})
    .then((fill) => {
      if (fill && !fill.offline) {
        const rows = cpPatchRows(fill.patch, sceneNameOf);
        setChecked(Object.fromEntries(rows.map((r) => [r.key, true])));  // 默认全选，作者按行取消
      }
    })
    .catch((e) => window.alert("生成补全建议失败：" + ((e && e.message) || "请稍后重试")));
  const applyChecked = () => {
    const rows = cpPatchRows(snap.fill.patch, sceneNameOf);
    const patch = cpRowsToPatch(rows, checked);
    if (!Object.keys(patch.drama).length && !patch.scenes.length && !patch.append_scenes.length) { window.alert("没有勾选任何改动。"); return; }
    WsChapterPlan.applyPatch(chapterId, patch)
      .catch((e) => window.alert("应用补丁失败：" + ((e && e.message) || "目录未被改动，可重试。")));
  };

  const fill = snap.fill;
  const rows = fill && !fill.offline ? cpPatchRows(fill.patch, sceneNameOf) : [];

  return (
    <section className="card arr-scenes" style={{ marginTop: 12 }}>
      <div className="card-head">
        <div>
          <div className="card-title">AI 编排</div>
          <div className="card-sub">
            {/* 阶段 Z：说它真的能做的事。伏笔台账 / 张力曲线是已经删掉的模块；雪花整理出来的场设计归构思（阶段 Y），这里不往里填。 */}
            {(ch.scenes || []).some((s) => s.design && s.design.owner === "plan")
              ? "基于全书上下文（章节蓝图 / 雪花构思）补这一章的戏剧卡、给你手加的场出主意。雪花整理出来的场，设计在构思第 10 步改——这里不往里填。所有建议须经你逐条确认，绝不覆盖你写过的内容。"
              : "基于全书上下文（章节蓝图 / 雪花构思）给场景卡出主意。所有建议只填空、须经你逐条确认，绝不覆盖你写过的内容。"}
          </div>
        </div>
        <div className="flex gap-2 items-center">
          <input className="arr-gmc-input" style={{ minWidth: 160 }} value={hint} placeholder="方向倾向（可留空）"
            disabled={locked || busy} aria-label="编排方向倾向"
            onChange={(e) => setHint(e.target.value)} />
          <button className="btn btn-ghost btn-sm" disabled={locked || busy} onClick={runCandidates}>
            <I.GitBranch size={13} /> {busy && snap.action.kind === "candidates" ? "构思中…" : "三个方向"}
          </button>
          <button className="btn btn-accent btn-sm" disabled={locked || busy} onClick={() => runFill(null)}>
            <I.Sparkles size={13} /> {busy && snap.action.kind === "fill" ? "补全中…" : "一键补全"}
          </button>
        </div>
      </div>

      <CpActionHint action={snap.authorAction} />

      {/* 三方向候选 */}
      {snap.candidates && (
        <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
          <CpDegradedChips slots={snap.candidates.degraded} />
          {snap.candidates.items.map((cand, i) => (
            <div className="arr-field" key={i}>
              <header className="arr-field-head">
                <span className="arr-field-label">「{cand.label}」</span>
                <button className="btn btn-quiet btn-sm" disabled={locked || busy} onClick={() => runFill(cand)}>
                  采纳 · 生成补丁
                </button>
              </header>
              <div style={{ fontSize: 13, color: "var(--ink-2)", lineHeight: 1.7 }}>
                <div>{cand.rationale}</div>
                {cand.risk ? <div style={{ color: "var(--ink-3)" }}>代价：{cand.risk}</div> : null}
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 4 }}>
                  {(cand.scene_plan || []).map((s, j) => (
                    <span key={j} className={`pill text-xs ${s.kind === "reactive" ? "pill-slate" : "pill-crimson"}`}>
                      <span className="pill-dot" />{s.ref_scene_id ? "改" : "新"}·{s.title}
                    </span>
                  ))}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 补全结果：diff 式逐条确认 */}
      {fill && (
        <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
          <CpDegradedChips slots={fill.degraded} />
          {fill.offline ? (
            <div className="arr-sync">
              LLM 未接入，先给出待补清单：
              <ul style={{ margin: "4px 0 0 18px" }}>
                {fill.gaps.map((g, i) => <li key={i}>{g}</li>)}
              </ul>
            </div>
          ) : rows.length ? (
            <React.Fragment>
              <ul className="arr-scene-list" style={{ margin: 0 }}>
                {rows.map((row) => (
                  <li key={row.key} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "6px 4px", borderBottom: "1px solid var(--line-1)" }}>
                    <input type="checkbox" checked={!!checked[row.key]} disabled={locked || busy}
                      aria-label={`应用 ${row.label}`}
                      onChange={(e) => setChecked({ ...checked, [row.key]: e.target.checked })} />
                    <span style={{ whiteSpace: "nowrap", fontSize: 12, color: "var(--ink-3)" }}>{row.label}</span>
                    <span style={{ fontSize: 13 }}>{row.value}</span>
                  </li>
                ))}
              </ul>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <button className="btn btn-accent btn-sm" disabled={locked || busy} onClick={applyChecked}>
                  <I.Check size={13} /> {busy && snap.action.kind === "apply" ? "写入中…" : "应用勾选的改动"}
                </button>
                <span className="arr-field-hint">写入目录后由后端版本收敛；未勾选与被拒条目不动。</span>
              </div>
            </React.Fragment>
          ) : (
            <span className="arr-sync">场景卡没有可补的空槽——都已填好。</span>
          )}
          {!!(fill.notes || []).length && (
            <div className="arr-sync">
              <b>覆盖型建议（不自动应用）：</b>
              <ul style={{ margin: "4px 0 0 18px" }}>
                {fill.notes.map((n, i) => (
                  <li key={i}>{n.scene_id ? `${sceneNameOf(n.scene_id)} · ` : ""}{n.suggestion}{n.reason ? `（${n.reason}）` : ""}</li>
                ))}
              </ul>
            </div>
          )}
          {!!(fill.dropped || []).length && (
            <div className="arr-sync" style={{ color: "var(--ink-3)" }}>
              已按护栏拒掉 {fill.dropped.length} 条：{fill.dropped.slice(0, 5).map((d, i) => (
                <span key={i}>{cpFieldLabel(d.field)}（{CP_DROP_REASONS[d.reason] || d.reason}）{i < Math.min(fill.dropped.length, 5) - 1 ? "、" : ""}</span>
              ))}{fill.dropped.length > 5 ? " …" : ""}
            </div>
          )}
        </div>
      )}

      {snap.applied && (
        <div className="arr-sync" style={{ marginTop: 8 }} role="status">
          <I.Check size={13} /> 已补全戏剧卡 {snap.applied.drama} 项、写入 {snap.applied.scenes} 张场景卡、追加 {snap.applied.appended} 场
          {snap.applied.skipped.length
            ? `；${snap.applied.skipped.length} 条被跳过（${snap.applied.skipped.some(d => d.reason === "design_owned_by_plan") ? "已有内容，或这一场的设计在构思第 10 步改" : "已有内容"}）`
            : ""}。
        </div>
      )}
    </section>
  );
}

/* ==========================================================
   ArrAiHealthBlock — 右栏「AI 体检」合流块
   ========================================================== */
const CP_FINDING_LABELS = {
  PROMISE_UNGROUNDED: "承诺不落地",
  SCENE_FUNCTION_DUPLICATE: "场景功能重复",
  REACTIVE_MISSING: "缺反应场",
  TENSION_FLAT: "张力不升级",
  FORESHADOW_OVERDUE: "伏笔逾期",
  POV_FATIGUE: "POV 疲劳",
  HANDOFF_MISMATCH: "承接错位",
  EXIT_NO_CHANGE: "结尾无变化",
  BRIEF_INCOMPLETE: "场景卡不完整",
  OTHER: "其他",
};

function ArrAiHealthBlock({ ch, locked }) {
  const chapterId = ch && ch.backendId;
  const snap = useChapterPlan(chapterId);
  if (!chapterId) return null;
  const busy = snap.action.busy;
  const review = snap.review;
  const run = () => WsChapterPlan.requestReview(chapterId)
    .catch((e) => window.alert("AI 体检失败：" + ((e && e.message) || "请稍后重试")));
  const applySuggestion = (finding) => {
    WsChapterPlan.applyPatch(chapterId, finding.suggestion_patch)
      .catch((e) => window.alert("应用建议失败：" + ((e && e.message) || "目录未被改动。")));
  };

  return (
    <div className="ctx-block">
      <div className="ctx-head">
        <I.Microscope size={13} /><span>AI 体检</span>
        <button className="btn btn-quiet btn-sm" style={{ marginLeft: "auto" }} disabled={busy} onClick={run}>
          {busy && snap.action.kind === "review" ? "体检中…" : review ? "重新体检" : "开始体检"}
        </button>
      </div>
      {review ? (
        review.findings.length ? (
          <ul className="arr-checks">
            {review.findings.map((f, i) => (
              <li key={i} className={`arr-check ${f.severity === "warn" ? "is-warn" : ""}`} style={{ alignItems: "start" }}>
                {f.severity === "warn" ? <I.AlertTriangle size={13} /> : <I.Circle size={13} />}
                <span style={{ display: "grid", gap: 2 }}>
                  <span className="arr-check-label">
                    {CP_FINDING_LABELS[f.code] || f.code}
                    {review.source === "fallback" ? "" : " · AI"}
                  </span>
                  <span style={{ fontSize: 12, color: "var(--ink-3)" }}>{f.summary || f.evidence}</span>
                  {f.suggestion_patch && !locked && (
                    <button className="btn btn-quiet btn-sm" style={{ justifySelf: "start" }} disabled={busy}
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
        <p className="arr-sync">用全书上下文（伏笔 / 张力 / 承接 / POV 分布）给本章编排做一次结构体检。</p>
      )}
      <CpActionHint action={snap.authorAction} />
    </div>
  );
}

export { ArrBlueprintCard, ArrPlanPanel, ArrAiHealthBlock, cpPatchRows, cpRowsToPatch };
