import React from "react";
import { I } from "./icons.jsx";

/* global window */
/* ==========================================================
   分章预览面板（P2）
   ----------------------------------------------------------
   「整理为章节结构」以前是个黑盒：按下去直接落库，而且因为整条管线里没人分章，
   落出来的永远是「全书一章」。现在它先开这个面板 —— 作者按下确认之前就看得见
   会得到什么：哪一章拿到哪几场、哪些场还没分到、哪些章是空的。

   分章算法在后端（snowflake_chaptering.py），前端不持有第二套：以前
   ws-snow.jsx 的 s2MaterializePreview 是唯一分对章的实现，却只在闸门未过时
   才走，做得越完整反而掉进最差的那条路。
   ========================================================== */

const STRATEGIES = [
  { key: "spine_anchor", label: "脊柱锚点", hint: "三个灾难是结构铰链：同标记的场与章互相锁定，其余按顺序均匀铺开" },
  { key: "even", label: "均分", hint: "忽略锚点，按顺序把场平均分进各章" },
  { key: "keep_current", label: "保持现状", hint: "已分好的不动，只给新场找位置" },
];

const ACT_LABEL = { 1: "第一幕", 2: "第二幕", 3: "第三幕" };

/* preview 回包 → 面板可编辑的本地态。改动只在本地，确认前不落库。 */
function shapeDraft(preview) {
  return {
    strategy: (preview && preview.strategy) || "spine_anchor",
    chapters: ((preview && preview.chapters) || []).map(c => ({
      rowUid: c.row_uid,
      title: c.title || "",
      act: c.act || 1,
      spine: c.spine || "",
      chapterGoal: c.chapter_goal || "",
      scenes: (c.scenes || []).map(s => ({
        scenePlanId: s.scene_plan_id,
        title: s.title || "",
        primaryForm: s.primary_form,
        spine: s.spine || "",
        anchored: !!s.anchored,
        planned: !!s.planned,
      })),
    })),
    unassigned: ((preview && preview.unassigned) || []).map(s => ({
      scenePlanId: s.scene_plan_id,
      title: s.title || "",
      primaryForm: "proactive",
      spine: "",
      anchored: false,
      planned: false,
    })),
    warnings: (preview && preview.warnings) || [],
    rhythm: (preview && preview.rhythm) || null,
    rationale: (preview && preview.rationale) || "",
  };
}

/* 节奏体检 → 一行人话。只报结构本身看得出、作者一眼能行动的东西
   （这里还没有正文，谈「张力评分」是空话）。 */
export function rhythmSummary(rhythm) {
  if (!rhythm) return [];
  const lines = [];
  if (rhythm.scene_counts && rhythm.scene_counts.length) {
    lines.push(`每章 ${rhythm.min_scenes}–${rhythm.max_scenes} 场 · 均值 ${rhythm.mean_scenes_per_chapter}`);
  }
  (rhythm.acts || []).forEach(a => {
    lines.push(`第 ${a.act} 幕：${a.chapter_count} 章 / ${a.scene_count} 场`);
  });
  const offHinge = (rhythm.spine_placement || []).filter(s => s.placed && !s.on_hinge).length;
  const missing = (rhythm.spine_placement || []).filter(s => !s.placed).length;
  if (!offHinge && !missing) lines.push("三个灾难都落在幕的铰链上");
  return lines;
}

/* 本地态 → materialize / chapter-plan 的提交载荷。顺序即 scene_seq。 */
export function buildChapterPlanPayload(draft) {
  return {
    chapters: draft.chapters.map(c => ({
      row_uid: c.rowUid,
      title: c.title,
      act: c.act,
      spine: c.spine,
      chapter_goal: c.chapterGoal,
    })),
    assignments: draft.chapters.flatMap((c, ci) =>
      c.scenes.map((s, si) => ({
        scene_plan_id: s.scenePlanId,
        chapter_row_uid: c.rowUid,
        scene_seq: si + 1,
        _chapterIndex: ci,
      })).map(({ _chapterIndex, ...rest }) => rest),
    ),
  };
}

/* 章表 → 幕段（按**数组顺序**切，幕值一变就起新的一段）。

   刻意不按幕重新分组。重新分组会让「看到的顺序」和「保存的顺序」分家：draft.chapters
   可以是交错的（``_derive_from_long_synopsis`` 按段落序给 act，``_sanitize_chapter_items``
   也照收模型给的 act），比如 [A(一幕), B(二幕), C(一幕)]。按幕分组的面板显示
   第一幕: A、C ／ 第二幕: B，作者据此点了确认，而 buildChapterPlanPayload 交的是数组序
   A、B、C，后端 save 按数组序写 chapter_seq —— 目录里是 A、B、C，不是作者确认的 A、C、B。
   「移到下一章」同样按数组序跳，于是那一场跑到了作者眼里另一幕的章里。

   按数组顺序切段则处处一致：显示 = 提交 = 落库。幕交错因此变成看得见的两段「第一幕」，
   作者能发现并去 07 修 —— 藏起来不是帮忙。 */
export function chapterActRuns(chapters) {
  const runs = [];
  (chapters || []).forEach((chapter, index) => {
    const act = chapter.act || 1;
    const last = runs[runs.length - 1];
    if (last && last.act === act) last.chapters.push({ ...chapter, index });
    else runs.push({ act, chapters: [{ ...chapter, index }] });
  });
  return runs;
}

/* 把一场从 (chapterIndex, sceneIndex) 移到相邻章。chapterIndex = -1 表示「未分配」区。 */
export function moveSceneToChapter(draft, from, sceneIndex, to) {
  const next = {
    ...draft,
    chapters: draft.chapters.map(c => ({ ...c, scenes: c.scenes.slice() })),
    unassigned: draft.unassigned.slice(),
  };
  const source = from < 0 ? next.unassigned : next.chapters[from] && next.chapters[from].scenes;
  if (!source) return draft;
  const [scene] = source.splice(sceneIndex, 1);
  if (!scene) return draft;
  if (to < 0) next.unassigned.push(scene);
  else if (next.chapters[to]) next.chapters[to].scenes.push(scene);
  else return draft;
  return next;
}

export function WsChapterPlanPanel({ onClose, onDone, onGoToStep }) {
  const [busy, setBusy] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const [draft, setDraft] = React.useState(null);
  const [materializationGate, setMaterializationGate] = React.useState(null);
  const previewRequestRef = React.useRef(null);

  const [noChapters, setNoChapters] = React.useState(false);
  const load = React.useCallback((strategy) => {
    const inflight = previewRequestRef.current;
    if (inflight && inflight.strategy === strategy) return inflight.promise;
    const promise = (async () => {
      setBusy(true);
      setError("");
      setNoChapters(false);
      try {
        if (!window.SnowSync || typeof window.SnowSync.chapterPreview !== "function") {
          throw new Error("雪花同步模块尚未就绪，请刷新页面后重试。");
        }
        const preview = await window.SnowSync.chapterPreview(strategy);
        setDraft(shapeDraft(preview));
        setMaterializationGate((preview && preview.materialization_gate) || null);
      } catch (e) {
        // 阶段 K：07 没出章表不是错——章是列完场之后的包装决定，这里直接按场景列表提议
        setNoChapters(!!(e && e.code === "SNOWFLAKE_CHAPTER_PLAN_EMPTY"));
        setError((e && e.message) || "无法生成分章预览，请稍后重试。");
        setDraft(null);
        setMaterializationGate(null);
      } finally {
        setBusy(false);
      }
    })();
    previewRequestRef.current = { strategy, promise };
    promise.finally(() => {
      if (previewRequestRef.current && previewRequestRef.current.promise === promise) {
        previewRequestRef.current = null;
      }
    });
    return promise;
  }, []);

  /* 阶段 K：按场景列表提议章表（三个灾难收束各自的章，章数按作品设置或每章约三场）。
     已有章表时是「重排」，要作者确认——旧章会被替换、归属重排。 */
  const [proposing, setProposing] = React.useState(false);
  const propose = async (replace) => {
    if (proposing || saving) return;
    if (replace && !window.confirm("按场景列表重新提议章表？现有章会被替换，场景归属重排（07 的章表也随之更新）。")) return;
    setProposing(true);
    setError("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.chapterPropose !== "function") {
        throw new Error("雪花同步模块尚未就绪，请刷新页面后重试。");
      }
      await window.SnowSync.chapterPropose(replace ? { replace: true } : {});
      await load("keep_current");
    } catch (e) {
      setError((e && e.message) || "按场景提议章表失败，请稍后重试。");
    } finally {
      setProposing(false);
    }
  };

  /* 处置孤儿场（作者从 09 删掉、但目录里已有场景卡的那些场）。处置完必须重拉预览：
     孤儿警告是后端算的，本地删掉那一条会让界面和真相分家。 */
  const [resolving, setResolving] = React.useState("");
  const resolveOrphan = async (scenePlanId, action) => {
    setResolving(scenePlanId);
    setError("");
    try {
      await window.SnowSync.resolveOrphanedScene(scenePlanId, action);
      await load(draft ? draft.strategy : "spine_anchor");
    } catch (e) {
      setError((e && e.message) || "处置这一场失败，请稍后重试。");
    } finally {
      setResolving("");
    }
  };

  /* 让 AI 建议分章（P3）：结果是另一份候选预览，采纳与否只是本地状态。
     fail-closed —— LLM 没配好就如实报错，不拿规则结果冒充 AI 建议。 */
  const [suggesting, setSuggesting] = React.useState(false);
  const suggest = async () => {
    if (suggesting || busy || saving) return;
    setSuggesting(true);
    setError("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.chapterSuggest !== "function") {
        throw new Error("AI 分章能力尚未就绪，请刷新页面后重试。");
      }
      const suggestion = await window.SnowSync.chapterSuggest(draft ? draft.strategy : "spine_anchor");
      setDraft(shapeDraft(suggestion));
      setMaterializationGate((suggestion && suggestion.materialization_gate) || null);
    } catch (e) {
      setError((e && e.message) || "AI 分章建议不可用，请检查模型配置后重试。");
    } finally {
      setSuggesting(false);
    }
  };

  React.useEffect(() => { load("spine_anchor"); }, [load]);

  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape" && !saving) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [saving, onClose]);

  const blockers = ((draft && draft.warnings) || []).filter(w => w.severity === "blocker");
  const advisories = ((draft && draft.warnings) || []).filter(w => w.severity !== "blocker");
  const sceneTotal = draft ? draft.chapters.reduce((n, c) => n + c.scenes.length, 0) : 0;
  const chapterTotal = draft ? draft.chapters.filter(c => c.scenes.length).length : 0;
  const rawGateItems = Array.isArray(materializationGate && materializationGate.items)
    ? materializationGate.items
    : [];
  /* workspace 的当前真相在确认前必然可能含 chapter_plan_required；当前预览已经提供完整
     chapters + assignments 时，这一项会在 materialize 同一事务先被满足，不能反过来把
     “确认分章”按钮锁死。其他步骤/分诊阻断仍必须提前展示。 */
  const gateItems = rawGateItems.filter(item => !(
    item && item.kind === "chapter_plan_required"
    && draft && !draft.unassigned.length && chapterTotal > 0 && sceneTotal > 0
  ));
  const gateBlockers = gateItems.filter(item => item && item.severity === "blocker");
  const gateAdvisories = gateItems.filter(item => item && item.severity !== "blocker");
  const canConfirm = !!draft && !busy && !saving && !blockers.length && !gateBlockers.length
    && !draft.unassigned.length && sceneTotal > 0;

  const goToGateItem = (item) => {
    const stepKey = (item && item.step_key)
      || (item && item.primary_action && item.primary_action.step_key);
    if (!stepKey || typeof onGoToStep !== "function") return;
    onGoToStep(stepKey);
    onClose();
  };

  const confirm = async () => {
    if (!canConfirm) return;
    setSaving(true);
    setError("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.materialize !== "function") {
        throw new Error("雪花同步模块尚未就绪，请刷新页面后重试。");
      }
      const result = await window.SnowSync.materialize(null, buildChapterPlanPayload(draft));
      onDone(result);
    } catch (e) {
      const freshGate = e && e.details && e.details.materialization_gate;
      if (freshGate) {
        setMaterializationGate(freshGate);
        setError("还有整理前检查没有通过；请按下面的阻断项处理后重试。");
      } else {
        setError((e && e.message) || "写入章节结构失败，请稍后重试。");
      }
      setSaving(false);
    }
  };

  const move = (from, sceneIndex, to) => setDraft(d => moveSceneToChapter(d, from, sceneIndex, to));
  const setChapter = (index, field, value) =>
    setDraft(d => ({ ...d, chapters: d.chapters.map((c, i) => (i === index ? { ...c, [field]: value } : c)) }));

  const actRuns = chapterActRuns(draft ? draft.chapters : []);

  return (
    <div className="sf-sd-scrim" role="dialog" aria-modal="true" aria-label="整理为章节结构" onClick={saving ? undefined : onClose}>
      <div className="sf-sd-card sf-chapterplan" data-testid="chapter-plan-panel" onClick={(e) => e.stopPropagation()}>
        <header className="sf-sd-head">
          <div>
            <div className="sf-sd-sub">整理为章节结构 · 预览</div>
            <div className="sf-sd-title">确认之前，先看清每一场归哪一章</div>
          </div>
          <button className="wr-drawer-x" onClick={onClose} disabled={saving} title="关闭 (Esc)" aria-label="关闭">
            <I.X size={16} />
          </button>
        </header>

        <div className="sf-chapterplan-bar">
          <span className="text-muted text-sm">分章策略</span>
          {STRATEGIES.map(s => (
            <button
              key={s.key}
              className={`btn btn-sm ${draft && draft.strategy === s.key ? "btn-accent" : "btn-quiet"}`}
              title={s.hint}
              disabled={busy || saving}
              onClick={() => load(s.key)}
              data-testid={`chapter-plan-strategy-${s.key}`}
            >
              {s.label}
            </button>
          ))}
          <button className="btn btn-quiet btn-sm" disabled={busy || saving || suggesting}
            title="让 AI 依据脊柱锚点与上下游材料给一份分章建议；采纳与否由你决定"
            onClick={suggest} data-testid="chapter-plan-suggest">
            <I.Wand size={13} className={suggesting ? "sf-spin" : ""} /> {suggesting ? "推演中…" : "AI 建议"}
          </button>
          <button className="btn btn-quiet btn-sm" disabled={busy || saving || proposing}
            title="章是列完场之后的包装决定：按 09 的场景列表重新提议章表——三个灾难各自收束一章，章数按作品设置或每章约三场"
            onClick={() => propose(!noChapters)} data-testid="chapter-plan-propose">
            <I.Layout size={13} className={proposing ? "sf-spin" : ""} /> {proposing ? "提议中…" : (noChapters ? "按场景生成章表" : "按场景重排章表")}
          </button>
          <span style={{ flex: 1 }} />
          {draft && <span className="text-muted text-sm">{chapterTotal} 章 · {sceneTotal} 场</span>}
        </div>

        {!busy && draft && draft.rhythm && (
          <div className="sf-chapterplan-rhythm" data-testid="chapter-plan-rhythm">
            <I.Activity size={12} />
            {rhythmSummary(draft.rhythm).map((line, i) => <span key={i} className="sf-chapterplan-rhythmitem">{line}</span>)}
          </div>
        )}

        {!busy && draft && draft.rationale && (
          <div className="sf-chapterplan-rationale" data-testid="chapter-plan-rationale">
            <I.Wand size={12} /> <span>{draft.rationale}</span>
          </div>
        )}

        {busy && <div className="sf-chapterplan-empty">正在推演分章…</div>}
        {!busy && error && !draft && <div className="sf-chapterplan-empty tone-rose" role="alert">{error}</div>}

        {!busy && draft && (
          <>
            <div className="sf-chapterplan-body">
              {actRuns.map(group => (
                <div key={`act-${group.act}-${group.chapters[0].index}`} className="sf-chapterplan-act">
                  <div className="sf-chapterplan-actlabel">{ACT_LABEL[group.act] || `第 ${group.act} 幕`}</div>
                  {group.chapters.map(chapter => (
                    <div key={chapter.rowUid} className={`sf-chapterplan-chapter ${chapter.scenes.length ? "" : "is-empty"}`}>
                      <div className="sf-chapterplan-chaphead">
                        <input
                          className="sf-chapterplan-title"
                          value={chapter.title}
                          placeholder="（未命名章）"
                          onChange={e => setChapter(chapter.index, "title", e.target.value)}
                          aria-label={`第 ${chapter.index + 1} 章标题`}
                        />
                        {chapter.spine && <span className="sf-chapterplan-spine">{chapter.spine}</span>}
                        <span className="sf-chapterplan-count">{chapter.scenes.length} 场</span>
                      </div>
                      <ul className="sf-chapterplan-scenes">
                        {chapter.scenes.map((scene, si) => (
                          <li key={scene.scenePlanId} className="sf-chapterplan-scene">
                            <span className={`sf-chapterplan-kind is-${scene.primaryForm}`}>
                              {scene.primaryForm === "reactive" ? "反应" : "主动"}
                            </span>
                            <span className="sf-chapterplan-scenetitle">{scene.title}</span>
                            {scene.anchored && <span className="sf-chapterplan-anchor" title="脊柱锚定">●锚定</span>}
                            {!scene.planned && <span className="sf-chapterplan-unplanned" title="第 10 步还没规划三拍">未规划</span>}
                            <span style={{ flex: 1 }} />
                            <button className="btn btn-ghost btn-xs" disabled={chapter.index === 0}
                              title="移到上一章" aria-label="移到上一章"
                              onClick={() => move(chapter.index, si, chapter.index - 1)}>
                              <I.ChevronLeft size={12} />
                            </button>
                            <button className="btn btn-ghost btn-xs" disabled={chapter.index >= draft.chapters.length - 1}
                              title="移到下一章" aria-label="移到下一章"
                              onClick={() => move(chapter.index, si, chapter.index + 1)}>
                              <I.ChevronRight size={12} />
                            </button>
                          </li>
                        ))}
                        {!chapter.scenes.length && <li className="sf-chapterplan-scene is-placeholder">（没有分到场 · 不会写入目录）</li>}
                      </ul>
                    </div>
                  ))}
                </div>
              ))}

              {!!draft.unassigned.length && (
                <div className="sf-chapterplan-chapter is-unassigned">
                  <div className="sf-chapterplan-chaphead">
                    <span className="sf-chapterplan-title as-text">未分配</span>
                    <span className="sf-chapterplan-count">{draft.unassigned.length} 场</span>
                  </div>
                  <ul className="sf-chapterplan-scenes">
                    {draft.unassigned.map((scene, si) => (
                      <li key={scene.scenePlanId} className="sf-chapterplan-scene">
                        <span className="sf-chapterplan-scenetitle">{scene.title}</span>
                        <span style={{ flex: 1 }} />
                        <button className="btn btn-quiet btn-xs" disabled={!draft.chapters.length}
                          onClick={() => move(-1, si, draft.chapters.length - 1)}>归入末章</button>
                        <button className="btn btn-quiet btn-xs" disabled={!draft.chapters.length}
                          onClick={() => move(-1, si, 0)}>归入首章</button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>

            {(blockers.length || advisories.length || gateItems.length || draft.unassigned.length) ? (
              <div className="sf-chapterplan-warnings" data-testid="chapter-plan-warnings">
                {blockers.map((w, i) => (
                  <div key={`b${i}`} className="sf-chapterplan-warn tone-rose">
                    <I.AlertTriangle size={12} /> {w.message}
                    {/* 孤儿场的 blocker 明说「请先决定是一并删除还是保留」——那两个决定
                        必须真的在这里，否则这条警告就是个无解的死结。 */}
                    {w.kind === "orphaned_scene" && w.scene_plan_id ? (
                      <span className="sf-chapterplan-warn-actions">
                        <button className="btn btn-quiet btn-xs" disabled={!!resolving}
                          onClick={() => resolveOrphan(w.scene_plan_id, "keep")}>
                          保留正文
                        </button>
                        <button className="btn btn-quiet btn-xs" disabled={!!resolving}
                          onClick={() => resolveOrphan(w.scene_plan_id, "discard")}>
                          一并删除
                        </button>
                      </span>
                    ) : null}
                  </div>
                ))}
                {!!draft.unassigned.length && (
                  <div className="sf-chapterplan-warn tone-rose">
                    <I.AlertTriangle size={12} /> 还有 {draft.unassigned.length} 场没有分到章 —— 指派完才能写入。
                  </div>
                )}
                {advisories.map((w, i) => (
                  <div key={`w${i}`} className="sf-chapterplan-warn tone-gold">
                    <I.AlertTriangle size={12} /> {w.message}
                  </div>
                ))}
                {gateBlockers.map((item, i) => (
                  <div key={item.id || `gb${i}`} className="sf-chapterplan-warn tone-rose" role="alert"
                    data-testid="materialization-gate-blocker">
                    <I.AlertTriangle size={12} /> {item.message}
                    {typeof onGoToStep === "function" && item.step_key ? (
                      <span className="sf-chapterplan-warn-actions">
                        <button className="btn btn-quiet btn-xs" onClick={() => goToGateItem(item)}>
                          {(item.primary_action && item.primary_action.label) || "去补这一步"}
                        </button>
                      </span>
                    ) : null}
                  </div>
                ))}
                {gateAdvisories.map((item, i) => (
                  <div key={item.id || `gw${i}`} className="sf-chapterplan-warn tone-gold">
                    <I.AlertTriangle size={12} /> {item.message}
                  </div>
                ))}
              </div>
            ) : null}

            {error && <div className="sf-chapterplan-warn tone-rose" role="alert">{error}</div>}

            <footer className="sf-sd-foot">
              <button className="btn btn-ghost btn-sm" onClick={onClose} disabled={saving}>取消</button>
              <button className="btn btn-accent btn-sm" onClick={confirm} disabled={!canConfirm}
                data-testid="chapter-plan-confirm">
                {saving ? "写入中…" : `确认写入 ${chapterTotal} 章 / ${sceneTotal} 场`}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  );
}
