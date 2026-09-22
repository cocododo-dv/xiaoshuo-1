import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { CloseButton, Segmented } from "./ws-ui.jsx";
import {
  ACT_LABEL, applyChapterNames, buildChapterPlanPayload, buildChapterTitlesRequest, chapterActRuns, homeChapterFor,
  isAutoChapterTitle, isNewChapter, mergeChapterIntoPrevious, moveSceneToChapter, rhythmSummary, scaleExplanation,
  shapeDraft, splitChapterAt,
} from "./ws-snow-chapters-model.js";
import { ChapterPlanChapter, ChapterPlanUnassigned, ChapterPlanWarnings } from "./ws-snow-chapters-parts.jsx";

/* ==========================================================
   分章预览面板（P2；2026-09-18 重做）
   ----------------------------------------------------------
   「整理章节结构」先开这个面板 —— 作者按下确认之前就看得见会得到什么：哪一章拿到
   哪几场、哪些场还没分到、哪些章是空的。确认之前什么都不落库。

   这一版的纪律（对应一次真实的「整理出来乱七八糟」）：
   - **章是场景列表上连续的一段**。场的先后只有一个来源——09 场景列表；面板不提供第二套
     「章内手排」，只提供挪章界（首场并入上一章 / 末场移到下一章）、从某一场另起一章、与上一章合并。
   - 每一场带着它在场景列表里的序号和功能标签，作者一眼看得出顺序对不对。
   - 打开时由服务端按现状挑方案（auto）：分过章就原样摆出来；07 里有作者写的章表就把场倒进去；
     什么都没有（或只有几行「（待补）」）就直接按场景分章——三个灾难各自收束一章。
   - 「每章约 N 场」是面板上的一个数，并写明这个数从哪来（参考书章长 / 作品设置 / 默认）。

   分章算法在后端（snowflake_chaptering.py），前端不持有第二套。

   这个文件只管面板的状态与交互（拉预览、换分法、AI 建议 / 起章名、确认写入、关闭前追问）；
   纯模型在 ws-snow-chapters-model.js，一章 / 未分配 / 提醒区的展示件在 ws-snow-chapters-parts.jsx。
   ========================================================== */

const STRATEGIES = [
  { key: "from_scenes", label: "按场景分章", hint: "章是列完场之后的包装决定：三个灾难各自收束一章，其余按「每章约 N 场」顺着场景列表切开" },
  { key: "spine_anchor", label: "倒进 07 章表", needsTable: true, hint: "用 07 里你写的章表：同一个灾难标记的场与章互相锁定，其余按顺序铺开" },
  { key: "even", label: "07 章表 · 均分", needsTable: true, hint: "用 07 里你写的章表：忽略灾难标记，按顺序把场平均分进各章" },
  { key: "keep_current", label: "已保存的分章", needsSaved: true, hint: "上一次确认 / 保存的分章原样摆出来，新加的场跟着它前一场走" },
];

/* 在不关闭的遮罩上按下鼠标（有未确认调整的分章面板、粘了内容的导入框）时，浏览器默认把焦点移到 body：
   之后 Tab 还能被陷阱拉回来，但读屏和键盘用户已经不在对话框里了。挂在对话框的宿主元素上（React 事件
   穿过 portal 冒泡到宿主），只拦遮罩本身的 mousedown 默认动作，不拦冒泡，遮罩自己的关闭判断照常。 */
export function keepFocusOnDialogBackdrop(event) {
  const target = event && event.target;
  if (target && target.classList && target.classList.contains("ws-dialog-scrim")) event.preventDefault();
}

/* 纯模型的这些函数原先就从这里导出（构思的场景表、单测都这样 import），拆文件后照样从这里拿得到 */
export {
  applyChapterNames, buildChapterPlanPayload, buildChapterTitlesRequest, chapterActRuns, homeChapterFor,
  isAutoChapterTitle, mergeChapterIntoPrevious, moveSceneToChapter, rhythmSummary, scaleExplanation, splitChapterAt,
};

/* 这张面板有两扇门：构思页头和章节编排里的同名按钮「整理章节结构」（阶段 Z）。章的结构只有
   这一个编辑器；onGoToScene(sceneId) 让面板里的一场直达构思第 10 步的那一场（由宿主视图决定怎么跳）。 */
export function WsChapterPlanPanel({ onClose, onDone, onGoToStep, onGoToScene }) {
  const [busy, setBusy] = React.useState(true);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const [draft, setDraft] = React.useState(null);
  const [dirty, setDirty] = React.useState(false);
  const [perChapter, setPerChapter] = React.useState("");
  const [materializationGate, setMaterializationGate] = React.useState(null);
  const previewRequestRef = React.useRef(null);
  /* 这种分法刚算出来的样子（服务端预览或 AI 建议）。「撤销调整」回到它——不再请求一次：
     面板是模态的，打开期间预览的来源不会变；AI 建议也不必为了撤销再花一次模型调用。 */
  const pristineRef = React.useRef(null);

  const load = React.useCallback((strategy, options) => {
    const signature = `${strategy}|${JSON.stringify(options || {})}`;
    const inflight = previewRequestRef.current;
    if (inflight && inflight.signature === signature) return inflight.promise;
    const promise = (async () => {
      setBusy(true);
      setError("");
      try {
        if (!window.SnowSync || typeof window.SnowSync.chapterPreview !== "function") {
          throw new Error("雪花同步模块尚未就绪，请刷新页面后重试。");
        }
        const preview = await window.SnowSync.chapterPreview(strategy, options || {});
        const shaped = shapeDraft(preview);
        pristineRef.current = shaped;
        setDraft(shaped);
        setDirty(false);
        if (shaped.scale && shaped.scale.scenes_per_chapter) setPerChapter(String(shaped.scale.scenes_per_chapter));
        setMaterializationGate((preview && preview.materialization_gate) || null);
      } catch (e) {
        setError((e && e.message) || "无法生成分章预览，请稍后重试。");
        setDraft(null);
        setMaterializationGate(null);
      } finally {
        setBusy(false);
      }
    })();
    previewRequestRef.current = { signature, promise };
    promise.finally(() => {
      if (previewRequestRef.current && previewRequestRef.current.promise === promise) {
        previewRequestRef.current = null;
      }
    });
    return promise;
  }, []);

  /* 换分法 / 重新分 / 处置孤儿场时，按下的那个控件在加载中被收起、或选中项还没换过来：加载回来（或作者在
     确认框里说「不」）之后，焦点若掉到了 body、或停在一个没选中的分法上，就交还给触发它的那个控件，
     找不到就交给选中的那种分法——键盘用户按一下方向键不该被甩出这组单选。 */
  const hostRef = React.useRef(null);
  const focusReturnRef = React.useRef(null);
  const noteFocusReturn = () => {
    const active = document.activeElement;
    const testId = active && active.getAttribute ? active.getAttribute("data-testid") : "";
    focusReturnRef.current = testId && active.getAttribute("role") !== "radio" ? testId : "strategy";
  };
  const restoreFocus = (want) => {
    const host = hostRef.current;
    if (!host) return;
    const group = host.querySelector(".sf-chapterplan-strategies");
    const active = document.activeElement;
    const lost = !active || active === document.body
      || (!!group && group.contains(active) && active.getAttribute("aria-checked") !== "true");
    if (!lost) return;
    const back = want && want !== "strategy" ? host.querySelector(`[data-testid="${want}"]`) : null;
    const target = (back && !back.disabled ? back : null)
      || (group && (group.querySelector('[role="radio"][aria-checked="true"]:not([disabled])')
        || group.querySelector('[role="radio"]:not([disabled])')));
    if (target) target.focus();
  };
  React.useEffect(() => {
    if (busy || !focusReturnRef.current) return;
    const want = focusReturnRef.current;
    focusReturnRef.current = null;
    restoreFocus(want);
  }, [busy]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 换方案会丢掉面板里还没确认的手动调整——先问一句。 */
  const switchTo = (strategy, options) => {
    if (busy || saving) return;
    if (dirty && !window.confirm("换一种分法会丢掉你在面板里还没确认的调整（挪章界 / 拆章 / 并章 / 改章名）。继续？")) {
      // 方向键在 onChange 之后才把焦点挪到相邻那一项（它没被选中）：等那一步做完，再交还给仍然选中的分法
      setTimeout(() => restoreFocus("strategy"), 0);
      return;
    }
    noteFocusReturn();
    load(strategy, options);
  };
  /* 撤销调整：丢掉面板里还没确认的挪章界 / 拆章 / 并章 / 改章名，回到这种分法刚算出来的样子。
     分段单选对「点已经选中的那一项」不做反应，所以这里单独给一个入口。 */
  const revert = () => {
    if (!dirty || busy || saving || !pristineRef.current) return;
    if (!window.confirm("撤销面板里还没确认的调整（挪章界 / 拆章 / 并章 / 改章名），回到这种分法刚算出来的样子？")) return;
    setDraft(pristineRef.current);
    setDirty(false);
    setNameNote("");
  };
  const applyScale = () => {
    const n = Math.round(Number(perChapter));
    if (!(n > 0)) return;
    switchTo("from_scenes", { scenesPerChapter: n });
  };

  /* 处置孤儿场（作者从 09 删掉、但目录里已有场景卡的那些场）。处置完必须重拉预览：
     孤儿警告是后端算的，本地删掉那一条会让界面和真相分家。 */
  const [resolving, setResolving] = React.useState("");
  const resolveOrphan = async (scenePlanId, action) => {
    setResolving(scenePlanId);
    setError("");
    try {
      await window.SnowSync.resolveOrphanedScene(scenePlanId, action);
      noteFocusReturn();
      await load(draft ? draft.strategy : "auto");
    } catch (e) {
      setError((e && e.message) || "处置这一场失败，请稍后重试。");
    } finally {
      setResolving("");
    }
  };

  /* 让 AI 建议分章（P3）：结果是另一份候选预览，采纳与否只是本地状态。
     fail-closed —— LLM 没配好就如实报错，不拿规则结果冒充 AI 建议。
     AI 是往**已经存在**的章里分场；面板里还没确认的新章（new:*）它看不见，所以那时不可用。 */
  const [suggesting, setSuggesting] = React.useState(false);
  const hasUnsavedChapters = !!draft && draft.chapters.some(isNewChapter);
  const suggest = async () => {
    if (suggesting || busy || saving || hasUnsavedChapters) return;
    if (dirty && !window.confirm("AI 建议会替换面板里还没确认的调整。继续？")) return;
    setSuggesting(true);
    setError("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.chapterSuggest !== "function") {
        throw new Error("AI 分章能力尚未就绪，请刷新页面后重试。");
      }
      const base = draft && draft.strategy !== "llm_suggested" && draft.strategy !== "from_scenes" ? draft.strategy : "keep_current";
      const suggestion = await window.SnowSync.chapterSuggest(base);
      const shaped = shapeDraft(suggestion);
      pristineRef.current = shaped;
      setDraft(shaped);
      setDirty(false);
      setMaterializationGate((suggestion && suggestion.materialization_gate) || null);
    } catch (e) {
      setError((e && e.message) || "AI 分章建议不可用，请检查模型配置后重试。");
    } finally {
      setSuggesting(false);
    }
  };

  /* AI 起章名（阶段 W）：只给系统起的占位名起名，结果回到面板里由你改、由你确认；不落库。
     fail-closed —— LLM 没配好就如实报错。面板里还没确认的新章也能起（请求带的是面板此刻的章表）。 */
  const [naming, setNaming] = React.useState(false);
  const [nameNote, setNameNote] = React.useState("");
  const draftRef = React.useRef(null);
  draftRef.current = draft;
  const unnamedCount = draft ? draft.chapters.filter(c => c.scenes.length && isAutoChapterTitle(c.title)).length : 0;
  const nameChapters = async () => {
    if (naming || busy || saving || !draft || !unnamedCount) return;
    setNaming(true);
    setError("");
    setNameNote("");
    try {
      if (!window.SnowSync || typeof window.SnowSync.chapterTitles !== "function") {
        throw new Error("AI 起章名尚未就绪，请刷新页面后重试。");
      }
      const result = await window.SnowSync.chapterTitles(buildChapterTitlesRequest(draft));
      // 等模型的这段时间里作者可能还在改章名：基于**此刻**的面板算，而不是点按钮时的那一份
      const { draft: next, applied } = applyChapterNames(draftRef.current || draft, (result && result.titles) || []);
      if (applied) { setDraft(next); setDirty(true); }
      const notice = result && result.notice && result.notice.message;
      setNameNote(notice || (applied ? `AI 起了 ${applied} 个章名 —— 可以直接改，确认写入时一起落库。` : "这一次没有起出新的章名。"));
    } catch (e) {
      setError((e && e.message) || "AI 起章名不可用，请检查模型配置后重试。");
    } finally {
      setNaming(false);
    }
  };

  React.useEffect(() => { load("auto"); }, [load]);

  /* 关闭只有一条路：× / 取消 / Esc / 遮罩都走 WsDialog 的 requestClose，由这里放行或拦下——写入中不关；
     面板里有还没确认的调整时先问一句（以前换方案、AI 建议、去改某一场都会问，偏偏 Esc、点遮罩、× 和
     「取消」一声不响就把调整丢了）。点遮罩更容易是误触，所以有调整时遮罩干脆不关（dismissOnBackdrop）。 */
  const beforeClose = (reason) => {
    if (saving) return false;
    if (!dirty) return true;
    if (reason === "backdrop") return false;
    return window.confirm("面板里还有没确认的调整（挪章界 / 拆章 / 并章 / 改章名），关掉就不保留了。确定关闭？");
  };

  const blockers = ((draft && draft.warnings) || []).filter(w => w.severity === "blocker");
  /* 手动调整之后，后端算的「顺序冲突 / 空章 / 过大章」可能已经不成立——只保留与归属无关的提醒 */
  const STRUCTURAL_KINDS = ["chapter_order_conflict", "empty_chapter", "oversized_chapter", "unassigned_scenes", "spine_not_placed", "spine_off_hinge"];
  const advisories = ((draft && draft.warnings) || []).filter(w => w.severity !== "blocker"
    && !(dirty && STRUCTURAL_KINDS.includes(w.kind)));
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
    if (dirty && !window.confirm("去补这一步会关掉面板，面板里还没确认的调整（挪章界 / 拆章 / 并章 / 改章名）不会保留。继续？")) return;
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
      const payload = buildChapterPlanPayload({ ...draft, chapters: draft.chapters.filter(c => c.scenes.length || !isNewChapter(c)) });
      const result = await window.SnowSync.materialize(null, payload);
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

  const edit = (fn) => { setDraft(d => fn(d)); setDirty(true); };
  /* 拆章 / 并章 / 挪章界之后，按下的那个按钮往往跟着它的场换了位置或消失，焦点会掉到 body。
     焦点丢了就交给动作落点那一章的章名框，键盘用户可以接着起名、接着调整。 */
  const refocusChapter = (chapterIndex) => setTimeout(() => {
    const host = hostRef.current;
    if (!host || chapterIndex < 0) return;
    const focused = document.activeElement;
    if (focused && focused !== document.body && host.contains(focused)) return;
    const input = host.querySelector(`[data-testid="chapter-plan-chapter-${chapterIndex}"] .sf-chapterplan-title`);
    if (input) input.focus();
  }, 0);
  const move = (from, sceneIndex, to) => { edit(d => moveSceneToChapter(d, from, sceneIndex, to)); refocusChapter(to); };
  const split = (chapterIndex, sceneIndex) => { edit(d => splitChapterAt(d, chapterIndex, sceneIndex)); refocusChapter(chapterIndex + 1); };
  const merge = (chapterIndex) => { edit(d => mergeChapterIntoPrevious(d, chapterIndex)); refocusChapter(chapterIndex - 1); };
  const setChapter = (index, field, value) =>
    edit(d => ({ ...d, chapters: d.chapters.map((c, i) => (i === index ? { ...c, [field]: value } : c)) }));

  const actRuns = chapterActRuns(draft ? draft.chapters : []);
  const table = (draft && draft.chapterTable) || null;
  /* 加载中不禁用（见 Segmented 的 busy）：switchTo 在加载中本来就什么也不做 */
  const strategyDisabled = (s) => saving
    || (s.needsTable && table && !table.authored)
    || (s.needsSaved && table && !table.saved);
  const scaleNote = draft && draft.strategy === "from_scenes" ? scaleExplanation(draft.scale) : "";
  /* 去改这一场：面板里还没确认的调整会丢，先问一句 */
  const goToScene = (scene) => {
    if (typeof onGoToScene !== "function" || !scene.sceneId || saving) return;
    if (dirty && !window.confirm("去构思里改这一场会关掉面板，面板里还没确认的调整（挪章界 / 拆章 / 并章 / 改章名）不会保留。继续？")) return;
    onGoToScene(scene.sceneId);
  };

  /* 对话框语义、焦点（移入 / Tab 困住 / 关闭后回到打开它的按钮）、Esc 与叠放次序都交给 WsDialog。
     就地渲染（portal={false}）：两个宿主（构思页、章节编排）的单测都在宿主容器里找它。外面这层
     display:contents 的壳只为接住遮罩上的 mousedown（见 keepFocusOnDialogBackdrop）。 */
  return (
    <div className="sf-chapterplan-host" ref={hostRef} onMouseDown={keepFocusOnDialogBackdrop}>
      <WsDialog portal={false} size="xl" className="sf-chapterplan" scrimClassName="sf-chapterplan-scrim"
        testId="chapter-plan-panel" labelledBy="sf-chapterplan-title" describedBy="sf-chapterplan-desc"
        dismissOnBackdrop={!dirty && !saving} onBeforeClose={beforeClose} onClose={() => onClose()}>
        {({ requestClose }) => (<>
          <header className="ws-dialog-head sf-chapterplan-head">
            <div>
              <h2 className="ws-dialog-title" id="sf-chapterplan-title">整理章节结构</h2>
              <p className="ws-dialog-desc" id="sf-chapterplan-desc">确认之前先看清每一场归哪一章；在这里拆章、并章、挪章界、改章名，确认写入之前什么都不落库。</p>
            </div>
            <CloseButton className="wr-drawer-x" label="关闭" title="关闭（Esc）" disabled={saving} onClick={() => requestClose("close")} />
          </header>

          <div className="sf-chapterplan-bar">
            <span className="text-muted text-sm" id="sf-chapterplan-strategy-label">怎么分</span>
            {/* 四种分法是一组单选（分段控件）：选中的那种不再是和「确认写入」一样的红色实心按钮 */}
            <Segmented
              className="sf-chapterplan-strategies"
              label="怎么分"
              busy={busy}
              value={draft ? draft.strategy : ""}
              onChange={(key) => switchTo(key)}
              options={STRATEGIES.map(s => ({
                value: s.key,
                label: s.label,
                title: s.needsTable && table && !table.authored ? "07 里还没有你写的章表（只有占位行或空着）——按场景分章就好" : s.hint,
                disabled: strategyDisabled(s),
                testId: `chapter-plan-strategy-${s.key}`,
              }))}
            />
            <button className="btn btn-quiet btn-sm" disabled={busy || saving || suggesting || hasUnsavedChapters}
              title={hasUnsavedChapters ? "AI 是往已经确认的章里分场——先确认写入这一版章表，再让它建议" : "让 AI 依据灾难标记与上下游材料给一份分章建议；采纳与否由你决定"}
              onClick={suggest} data-testid="chapter-plan-suggest">
              <I.Wand size={13} className={suggesting ? "sf-spin" : ""} /> {suggesting ? "推演中…" : "AI 建议"}
            </button>
            <button className="btn btn-quiet btn-sm" disabled={busy || saving || naming || !unnamedCount}
              title={unnamedCount
                ? `给还叫「第 N 章」的 ${unnamedCount} 章各起一个名字、写一句章摘要；你起过名字的章不动。结果可以直接改，确认写入时才落库`
                : "每一章都已经有你起的名字了；想让 AI 重起某一章，先把它的章名清空"}
              onClick={nameChapters} data-testid="chapter-plan-name">
              <I.Tag size={13} className={naming ? "sf-spin" : ""} /> {naming ? "起名中…" : "AI 起章名"}
            </button>
            <span style={{ flex: 1 }} />
            {draft && <span className="text-muted text-sm tab-num">{chapterTotal} 章 · {sceneTotal} 场</span>}
          </div>

          {!busy && draft && nameNote && (
            <div className="sf-chapterplan-rationale" data-testid="chapter-plan-name-note">
              <I.Tag size={12} /> <span>{nameNote}</span>
            </div>
          )}

          {!busy && draft && draft.strategy === "from_scenes" && (
            <div className="sf-chapterplan-scale" data-testid="chapter-plan-scale">
              <label className="sf-chapterplan-scale-field">
                每章约
                <input type="number" min="1" max="99" value={perChapter} disabled={saving}
                  onChange={e => setPerChapter(e.target.value)}
                  onKeyDown={e => { if (e.key === "Enter") applyScale(); }}
                  aria-label="每章约几场" data-testid="chapter-plan-per-chapter" />
                场
              </label>
              <button className="btn btn-quiet btn-xs" disabled={busy || saving || !(Number(perChapter) > 0)}
                onClick={applyScale} data-testid="chapter-plan-rescale">
                <I.Refresh size={11} /> 重新分
              </button>
              {scaleNote && <span className="sf-chapterplan-scale-note">{scaleNote}</span>}
              {!!draft.replacesChapterCount && (
                <span className="sf-chapterplan-scale-note">确认后替换现有的 {draft.replacesChapterCount} 章章表（07 的章节表随之更新）</span>
              )}
            </div>
          )}

          {!busy && draft && draft.rhythm && !dirty && (
            <div className="sf-chapterplan-rhythm" data-testid="chapter-plan-rhythm">
              <I.Activity size={12} />
              {rhythmSummary(draft.rhythm).map((item, i) => (
                <span key={i} className="sf-chapterplan-rhythmitem">
                  {item.k && <span className="sf-chapterplan-rhythmk">{item.k}</span>}{item.v}
                </span>
              ))}
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
                      <ChapterPlanChapter key={chapter.rowUid} chapter={chapter} saving={saving}
                        isLastChapter={chapter.index >= draft.chapters.length - 1}
                        onSetField={(field, value) => setChapter(chapter.index, field, value)}
                        onMerge={() => merge(chapter.index)}
                        onMove={(sceneIndex, to) => move(chapter.index, sceneIndex, to)}
                        onSplit={(sceneIndex) => split(chapter.index, sceneIndex)}
                        onGoToScene={typeof onGoToScene === "function" ? goToScene : null} />
                    ))}
                  </div>
                ))}

                {!!draft.unassigned.length && (
                  <ChapterPlanUnassigned scenes={draft.unassigned} canAssign={!!draft.chapters.length}
                    onAssign={(sceneIndex, scene) => move(-1, sceneIndex, homeChapterFor(draft, scene))} />
                )}
              </div>

              <ChapterPlanWarnings blockers={blockers} advisories={advisories}
                gateBlockers={gateBlockers} gateAdvisories={gateAdvisories} unassignedCount={draft.unassigned.length}
                busy={busy} saving={saving} resolving={resolving}
                onResolveOrphan={resolveOrphan} onFixOrder={() => switchTo("from_scenes")}
                onGoToGateItem={typeof onGoToStep === "function" ? goToGateItem : null} />

              {error && <div className="sf-chapterplan-warn tone-rose" role="alert">{error}</div>}

              <footer className="ws-dialog-foot sf-chapterplan-foot">
                {dirty && (
                  <div className="sf-chapterplan-dirtyline">
                    <span className="sf-chapterplan-dirty" role="status">有还没确认的调整</span>
                    <button type="button" className="btn btn-quiet btn-xs" onClick={revert} disabled={busy || saving}
                      title="丢掉面板里还没确认的挪章界 / 拆章 / 并章 / 改章名，回到这种分法刚算出来的样子"
                      data-testid="chapter-plan-revert">撤销调整</button>
                  </div>
                )}
                <button className="btn btn-ghost btn-sm" onClick={() => requestClose("cancel")} disabled={saving}>取消</button>
                <button className="btn btn-accent btn-sm" onClick={confirm} disabled={!canConfirm}
                  data-testid="chapter-plan-confirm">
                  {saving ? "写入中…" : `确认写入 ${chapterTotal} 章 / ${sceneTotal} 场`}
                </button>
              </footer>
            </>
          )}
        </>)}
      </WsDialog>
    </div>
  );
}
