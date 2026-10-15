import React from "react";
import { I } from "./icons.jsx";
import { WsAuthorAi } from "./ws-author-ai-store.js";
import { Notice, Tag } from "./ws-ui.jsx";
import { wsConfirm } from "./ws-notify.jsx";

/* ==========================================================
   章节编排 · 章节蓝图卡（store 是 ws-author-ai-store.js 的 WsAuthorAi）
   章节详情里的「章节蓝图」：起草时本章每一场都读这一份（后端 bundle 读的章架构）；还没有时，第一次起草前场景运行
   按场景卡与戏剧卡现做一份。作者可以读、改写（保存成「我的版本」，构思 / 参考书再改也不作废），也可以让 AI 按全书
   上下文（邻章交接、章结构邻域、人物站位、戏剧卡、雪花构思）重新生成。
   没有可用模型时后端拒绝（409 + author_action），这里只给「去系统配置」——不拿规则算的东西冒充 AI 结果。
   失败不弹浏览器对话框：store 把错误挂在 action.error 上，这里就地说清是哪一步没成。

   2026-10（round 2 · B2-5）：以前这里是「AI 编排」卡（蓝图 / 方向 / 补全三个页签）外加右栏的「AI 体检」。方向、补全
   与体检一次都没用过，删了：出编排方向、补场景卡归构思 09 / 10 步，整章挑毛病归成稿中心的「AI 通读本章」。
   ========================================================== */

const { useState, useEffect, useSyncExternalStore } = React;

function useAuthorAi(chapterId) {
  useSyncExternalStore(WsAuthorAi.subscribe, () => WsAuthorAi.version());
  return WsAuthorAi.snapshot(chapterId);
}

/* 没有可用模型时后端给的 author_action：引导而非阻断（去系统配置由宿主的 goView 决定怎么走） */
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

/* 失败说哪一步没成 */
const ARR_AI_FAILED = {
  "arch-save": "保存章节蓝图没有成功",
  "arch-generate": "生成章节蓝图没有成功",
};
function ArrAiError({ snap }) {
  const err = snap.action && snap.action.error;
  const kind = snap.action && snap.action.kind;
  if (!err) return null;
  return (
    <Notice tone="danger" className="arr-ai-note">
      {ARR_AI_FAILED[kind] || "请求没有成功"}：{err.message || "请稍后重试"}
    </Notice>
  );
}

const ARR_BP_FIELDS = [
  { k: "promise", label: "章承诺", hint: "本章向读者兑现什么" },
  { k: "payoff", label: "兑现目标", hint: "承诺落在哪个具体画面 / 事件" },
  { k: "shift", label: "人物变化", hint: "谁从什么状态到什么状态" },
  { k: "endingQuestion", label: "结尾问题", hint: "读完本章读者带走的问题" },
];

const ARR_BP_EMPTY = { promise: "", escalation: [], reveals: [], payoff: "", shift: "", endingQuestion: "" };

/* ==========================================================
   ArrBlueprintCard —「章节蓝图」卡（读 / 改写 / AI 生成）
   手上的草稿记着是哪一章的：换章即清（以前开合折叠区也会清，没保存的改动悄悄没了）。
   ========================================================== */
function ArrBlueprintCard({ ch, locked, sectionRef, onConfigureModel }) {
  const chapterId = ch && ch.backendId;
  const snap = useAuthorAi(chapterId);
  const [draftState, setDraftState] = useState(() => ({ id: chapterId, view: null }));
  const draft = draftState.id === chapterId ? draftState.view : null;
  const setDraft = (view) => setDraftState({ id: chapterId, view });
  const busy = snap.action.busy;
  const status = snap.arch.status;

  /* 打开这一章时读一次；上次读失败就再读一次。状态在 effect 里现读：状态变化不重新触发——失败后等作者再打开这一章，
     不在后台反复重试 */
  useEffect(() => {
    const current = chapterId ? WsAuthorAi.snapshot(chapterId).arch.status : null;
    if (current === "idle" || current === "error") {
      WsAuthorAi.loadArchitecture(chapterId).catch(() => {});
    }
  }, [chapterId]);

  if (!chapterId) return null;
  const arch = snap.arch.data;
  const view = draft || arch || ARR_BP_EMPTY;
  const edit = (key, value) => setDraft({ ...view, [key]: value });
  const editLines = (key, value) => setDraft({ ...view, [key]: value.split("\n") });

  const save = async () => {
    if (!draft) return;
    try { await WsAuthorAi.saveArchitecture(chapterId, draft); setDraft(null); } catch (e) { /* 错误在下方就地显示 */ }
  };
  const regenerate = async () => {
    if (arch && !(await wsConfirm({
      title: "重新生成章节蓝图？",
      body: "新蓝图会取代现在这一版（旧版留档，但这里不能再切回去）。",
      confirmLabel: "重新生成",
    }))) return;
    try { const next = await WsAuthorAi.generateArchitecture(chapterId); if (next) setDraft(null); } catch (e) { /* 同上 */ }
  };
  const idPrefix = `arr-bp-${ch.id}`;

  return (
    <section className="card arr-ai" ref={sectionRef} aria-labelledby={`${idPrefix}-title`}>
      <div className="card-head">
        <div>
          <h2 className="card-title" id={`${idPrefix}-title`}>章节蓝图</h2>
          <div className="card-sub">
            起草时本章每一场都读这一份；还没有时，第一次起草前会按场景卡和章的说明现做一份。改了章的说明，这里会提示过时。可以直接改写，也可以让 AI 按全书上下文重新生成。
          </div>
        </div>
      </div>

      <div className="arr-ai-pane">
        <p className="arr-sync">
          {arch ? (arch.fromLlm ? "AI 生成的蓝图，可以直接改写。" : "你改写过的蓝图：构思或参考书再改也不会作废它。") : "这一章还没有蓝图。"}
          {draft ? <Tag tone="warn" className="arr-ai-unsaved">有未保存的改动</Tag> : null}
        </p>
        {status === "loading" && <p className="arr-sync" role="status">正在读取章节蓝图…</p>}
        {arch && arch.designChanged && (
          <Notice tone="warn" className="arr-ai-note" title="设计改过，蓝图可能过时" testId="arr-ai-design-changed">
            {arch.designChanged.message || "这份蓝图写好之后，构思或参考书又改过：蓝图照旧保留、照旧用于起草；需要时改写或重新生成。"}
          </Notice>
        )}
        {status === "error" && (
          <Notice tone="danger" className="arr-ai-note">章节蓝图读取失败：{(snap.arch.error && snap.arch.error.message) || "请稍后再打开这一章"}</Notice>
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
        <ArrAiError snap={snap} />
      </div>

      <ArrAiActionHint action={snap.authorAction} onConfigureModel={onConfigureModel} />
    </section>
  );
}

export { ArrBlueprintCard };
