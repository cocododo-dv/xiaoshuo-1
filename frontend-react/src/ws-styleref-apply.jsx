import React from "react";
import { I } from "./icons.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, Spinner, Tag, onRadioGroupKeyDown, radioTabIndex } from "./ws-ui.jsx";
import {
  SR_DRAFT_MODES, SR_REFERENCE_MODES, SR_SAMPLE_WINDOWS_MAX, SR_SAMPLE_WINDOWS_MIN, SR_SEGMENTS_ONLY_LABEL,
  srBindingOwnedByWork, srBindingSummary, srDimensionStatesSummary, srFormatChars, srIsLegacyGlobalBinding,
  srNormalizeConfig, srSettingsEqual,
} from "./ws-styleref-model.js";
import {
  srApplyProfile, srLoadProfileBindings, srLoadProjectBinding, srProfileBindings, srProjectBinding, srScenePreview,
  srUnbind, srUpdateBinding,
} from "./ws-styleref-store.js";
import { SrErrorLine, SrStageEmpty, srActiveWork, srNotify, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";
import { SrScenePreview } from "./ws-styleref-scene-preview.jsx";

/* ==========================================================
   风格参考 · 第三步「用于作品」：把这本书的文风直接用于当前作品（不再经待办）
   · 现在这部作品用的是哪一份：这本 / 另一本（用这本会替换它）/ 旧版的全局应用（只读）/ 没有；
   · 只有这部作品自己的应用（作品层、目标就是它）能在这里保存、解除；旧版的全局应用对所有没有自己应用的作品
     生效，状态卡上只说明、不给解除——要解除在下面「这份文风还用在」里解除（那里解除才是对全部作品）；
     「用于」给这部作品单独建一条；
   · 换回一本以前用过的书：表单按它在这部作品上停用的那条应用的设置填（后端就地重新启用那一行，设置真的还在）；
   · 三个旋钮，每个都会以已知方式改变起草时带上的东西：参考方式（三选一）、样例窗数（0–16，旁边是本场预览
     测出来的真实字数）、起草方式；「重点 / 正常 / 不学」在文风画像里逐维设；一句话汇总按真正生效的参考方式说
     （「起草不发原文」的书只用文风卡）；
   · 用于 / 保存设置直接生效，结果就地说明；可解除；这份画像还用在别的作品 / 某一场 / 某个角色上的也列出来；
   · 下面是「本场预览」：挑当前作品的一场，看它起草时会拿到哪些样例窗与文风卡。
   旧的四种策略、强度滑块、16 个开关、任务类型、待办往返都没有了。
   ========================================================== */

const SR_SCOPE_WORD = { project: "作品", scene: "某一场", character: "某个角色", global: "全部作品（旧）" };

/* 不带场景的预览：只要样例窗数与字数（设置一变 350 ms 后重算；旧请求晚到的结果丢掉） */
function useSampleReadout(profileId, config, enabled) {
  const [readout, setReadout] = React.useState(null);
  const key = `${profileId}|${config.reference_mode}|${config.sample_windows}`;
  React.useEffect(() => {
    if (!enabled || !profileId) { setReadout(null); return undefined; }
    let stale = false;
    setReadout((r) => (r ? { ...r, loading: true } : { loading: true }));
    const timer = setTimeout(() => {
      srScenePreview(profileId, { config })
        .then((data) => {
          if (stale) return;
          const sizes = (data && data.sizes) || {};
          setReadout({ loading: false, windows: Number(sizes.sample_windows || 0), chars: Number(sizes.sample_chars || 0), mode: data && data.reference_mode });
        })
        .catch(() => { if (!stale) setReadout({ loading: false, error: true }); });
    }, 350);
    return () => { stale = true; clearTimeout(timer); };
  }, [key, enabled]); // eslint-disable-line react-hooks/exhaustive-deps
  return readout;
}

export function SrApply({ book, go, onAction }) {
  useSrStore("detail", "books");
  const profileId = book.profile ? book.profile.profile_id : null;
  const work = srActiveWork();
  const workId = work ? work.id : null;
  const workTitle = (work && work.title) || "当前作品";
  React.useEffect(() => { if (workId) srLoadProjectBinding(workId); }, [workId]);
  React.useEffect(() => { if (profileId) srLoadProfileBindings(profileId); }, [profileId]);
  const bindingEntry = workId ? srProjectBinding(workId) : null;
  const current = bindingEntry && bindingEntry.data;
  const binding = current && current.binding;
  /* 只有这部作品自己的应用（作品层、目标就是它）能在这里改、解除；旧版的全局应用对所有没有自己应用的作品生效，
     在这里改它 / 解除它会波及全部作品——只读，要换就给这部作品单独建一条（用于） */
  const legacyGlobal = srIsLegacyGlobalBinding(binding) ? binding : null;
  const legacyThisBook = legacyGlobal && legacyGlobal.profile_id === profileId ? legacyGlobal : null;
  const ownBinding = binding && binding.profile_id === profileId && srBindingOwnedByWork(binding, workId) ? binding : null;
  const otherBinding = binding && !ownBinding && !legacyGlobal ? binding : null;
  const otherTitle = (current && current.book && current.book.title) || "另一本参考书";
  const bindingsEntry = profileId ? srProfileBindings(profileId) : null;
  const profileBindings = (bindingsEntry && bindingsEntry.data) || [];
  /* 这份画像在这部作品上已有、被换下来（停用）的那条应用：换回这本书时按它的设置来（后端就地重新启用那一行） */
  const stored = ownBinding ? null : profileBindings.find((b) => b.profile_id === profileId && srBindingOwnedByWork(b, workId)) || null;
  const bindingsLoading = !ownBinding && !!profileId && (!bindingsEntry || (!bindingsEntry.data && bindingsEntry.phase !== "error"));
  const seed = ownBinding || stored || legacyThisBook;
  const samplesBlocked = book.cloudPolicy === "segments_only";

  const [draft, setDraft] = React.useState(() => srNormalizeConfig(seed ? seed.config : null));
  const [busy, setBusy] = React.useState(null);
  const [error, setError] = React.useState(null);
  const [result, setResult] = React.useState(null);
  const seedKey = `${profileId}|${workId}|${seed ? `${seed.scope}:${seed.binding_id || "pending"}` : "none"}`;
  React.useEffect(() => {
    setDraft(srNormalizeConfig(seed ? seed.config : null));
    setError(null);
  }, [seedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const effectiveDraft = samplesBlocked ? { ...draft, reference_mode: "card_only" } : draft;
  const sendsSamples = effectiveDraft.reference_mode !== "card_only";
  const readout = useSampleReadout(profileId, effectiveDraft, sendsSamples);

  if (!profileId) {
    return (
      <SrStageEmpty icon="Pen" title="还没有可用的文风" actionLabel="去学习文风" onAction={() => go && go("learn")} testId="sr-apply-empty">
        用于作品的是学出来的文风画像：先在「学习文风」里学一次。
      </SrStageEmpty>
    );
  }
  if (!workId) {
    return (
      <div className="card" data-testid="sr-apply-no-work">
        <Notice tone="info">还没有打开作品：先在左侧的作品里打开一部，再回来把这本书用上。</Notice>
      </div>
    );
  }

  const changed = ownBinding ? !srSettingsEqual(ownBinding.config, draft) : true;
  const set = (patch) => { setDraft((d) => ({ ...d, ...patch })); setResult(null); setError(null); };
  const summaryOf = (b) => srBindingSummary(b, { cloudPolicy: book.cloudPolicy });

  const apply = async () => {
    if (busy) return;
    setBusy("apply"); setError(null); setResult(null);
    try {
      const settings = { reference_mode: draft.reference_mode, sample_windows: draft.sample_windows, draft_mode: draft.draft_mode };
      if (ownBinding && ownBinding.binding_id) {
        await srUpdateBinding(ownBinding.binding_id, settings, { projectId: workId });
        setResult({ kind: "saved" });
      } else {
        /* 从旧版全局应用换成这部作品自己的：逐维的「重点 / 不学」一并带过来，这部作品起草时照旧 */
        const config = legacyThisBook && !stored
          ? { ...settings, dimension_states: srNormalizeConfig(legacyThisBook.config).dimension_states }
          : settings;
        const data = await srApplyProfile(profileId, { projectId: workId, config, baseConfig: stored ? stored.config : null });
        setResult({ kind: "applied", replaced: (data && data.replaced) || [], restored: !!stored });
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  const unbind = async (target, label) => {
    if (busy || !target || !target.binding_id) return;
    const ok = await wsConfirm({
      title: `不再把《${book.title}》用于${label}？`,
      body: `解除之后，${label}起草新场景时不再带这本书的文风；已经写好的正文不受影响。以后想用，再点一次「用于」就行。`,
      confirmLabel: "解除",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(`unbind:${target.binding_id}`);
    try {
      await srUnbind(target.binding_id, { projectId: target.scope === "project" ? target.scope_ref_id : null, profileId });
      srNotify(`已解除：${label}不再用《${book.title}》的文风`, "neutral");
      setResult(null);
    } catch (e) {
      srNotifyError(e, "解除没有完成，请稍后重试。");
    } finally {
      setBusy(null);
    }
  };

  const primaryLabel = ownBinding ? (changed ? "保存设置" : "已在用")
    : otherBinding || (legacyGlobal && !legacyThisBook) ? "改用这本"
    : `用于《${workTitle}》`;
  const others = profileBindings.filter((b) => b.status === "active" && !srBindingOwnedByWork(b, workId));
  const projectTitleOf = (projectId) => {
    const hit = (book.appliedProjects || []).find((p) => p && p.project_id === projectId);
    return hit && hit.project_title ? `《${hit.project_title}》` : projectId;
  };

  return (
    <div className="sr-apply">
      <div className="card sr-apply-status" data-testid="sr-apply-status">
        {bindingEntry && bindingEntry.phase === "loading" && !current ? (
          <p className="sr-ov-text sr-ov-muted"><Spinner size={12} /> 正在读取《{workTitle}》现在用的文风…</p>
        ) : ownBinding ? (
          <>
            <div className="sr-apply-status-row">
              <Tag tone="ok" dot>在用</Tag>
              <span className="sr-apply-status-text">《{workTitle}》正在用这本书的文风：{summaryOf(ownBinding)} · {srDimensionStatesSummary(srNormalizeConfig(ownBinding.config).dimension_states)}</span>
            </div>
            <div className="sr-apply-status-actions">
              <button type="button" className="btn btn-quiet btn-sm" data-testid="sr-apply-unbind" disabled={!!busy || ownBinding.pending} onClick={() => unbind(ownBinding, `《${workTitle}》`)}>
                {busy === `unbind:${ownBinding.binding_id}` ? <Spinner size={12} /> : null} 不再用于《{workTitle}》
              </button>
            </div>
          </>
        ) : legacyGlobal ? (
          <div className="sr-apply-status-row">
            <Tag tone={legacyThisBook ? "ok" : "info"} dot>旧版全局应用</Tag>
            <span className="sr-apply-status-text" data-testid="sr-apply-legacy">
              {legacyThisBook
                ? `《${workTitle}》现在沿用一条旧版的「全部作品」应用，用的就是这本书的文风：${summaryOf(legacyThisBook)}。这条旧版应用对所有没有自己应用的作品生效，要解除请到下面「这份文风还用在」里解除（那里解除才是对全部作品）；点「用于《${workTitle}》」给这部作品单独建一条应用（沿用它的设置，可以先在下面改），别的作品照旧。`
                : `《${workTitle}》现在沿用一条旧版的「全部作品」应用，用的是《${otherTitle}》的文风；用这本只在《${workTitle}》上盖过它，别的作品照旧用它。`}
            </span>
          </div>
        ) : otherBinding ? (
          <div className="sr-apply-status-row">
            <Tag tone="info" dot>用着别的书</Tag>
            <span className="sr-apply-status-text" data-testid="sr-apply-other">
              《{workTitle}》现在用的是《{otherTitle}》的文风；用这本会替换它（《{otherTitle}》在《{workTitle}》上的设置保留，以后换回来还在）。
            </span>
          </div>
        ) : (
          <div className="sr-apply-status-row">
            <Tag tone="neutral" dot>没有在用</Tag>
            <span className="sr-apply-status-text">《{workTitle}》现在起草时不带任何参考书的文风。</span>
          </div>
        )}
        {stored && (
          <p className="sr-ov-hint" data-testid="sr-apply-stored">这本书上次用于《{workTitle}》时的设置已经填在下面，用上就按它来（逐维的「重点 / 不学」也还在）。</p>
        )}
      </div>

      <div className="card sr-apply-form" data-testid="sr-apply-form">
        <div className="card-head"><div><div className="card-title">怎么带这本书</div><div className="card-sub">三项设置都会直接改变起草时带上的东西；「重点 / 正常 / 不学」在文风画像里逐维设。</div></div></div>

        <SrRadioCards
          label="参考方式"
          name="sr-reference-mode"
          items={SR_REFERENCE_MODES.map((m) => ({ ...m, disabled: samplesBlocked && m.id !== "card_only" }))}
          value={effectiveDraft.reference_mode}
          onChange={(id) => set({ reference_mode: id })}
          testId="sr-reference-mode"
        />
        {samplesBlocked && (
          <Notice tone="info" testId="sr-apply-segments-only">这本书导入时选了「{SR_SEGMENTS_ONLY_LABEL}」：起草时只送文风卡，带原文样例的两种方式用不了（要用，得换一档范围重新导入）。</Notice>
        )}

        <div className={`sr-windows${sendsSamples ? "" : " is-off"}`}>
          <div className="sr-windows-head">
            <label className="sr-windows-label" htmlFor="sr-sample-windows">样例窗数</label>
            <span className="sr-windows-value tab-num">{sendsSamples ? `${draft.sample_windows} 窗` : "不带样例"}</span>
          </div>
          <input
            id="sr-sample-windows"
            type="range"
            className="sr-range"
            min={SR_SAMPLE_WINDOWS_MIN}
            max={SR_SAMPLE_WINDOWS_MAX}
            step={1}
            value={draft.sample_windows}
            disabled={!sendsSamples}
            aria-valuetext={`${draft.sample_windows} 窗`}
            data-testid="sr-sample-windows"
            onChange={(e) => set({ sample_windows: parseInt(e.target.value, 10) })}
          />
          <p className="sr-windows-readout" data-testid="sr-sample-readout" role="status">
            {!sendsSamples ? "只用文风卡：起草时不发原文段落。"
              : !readout || readout.loading ? "正在按这本书的窗口算…"
              : readout.error ? "读不到预览，字数暂时算不出来。"
              : `每一场起草时约带 ${readout.windows} 窗、${srFormatChars(readout.chars)}原文（每场按那一场挑，场与场不同）。`}
          </p>
        </div>

        <SrRadioCards
          label="起草方式"
          name="sr-draft-mode"
          items={SR_DRAFT_MODES}
          value={draft.draft_mode}
          onChange={(id) => set({ draft_mode: id })}
          testId="sr-draft-mode"
        />

        <div className="sr-apply-foot">
          <button
            type="button"
            className="btn btn-accent"
            data-testid="sr-apply-submit"
            disabled={!!busy || (ownBinding && !changed) || !!(ownBinding && ownBinding.pending) || bindingsLoading}
            title={bindingsLoading ? `正在读取这本书在《${workTitle}》上的旧设置` : undefined}
            onClick={apply}
          >
            {busy === "apply" ? <><Spinner size={13} /> 正在保存…</> : <><I.Check size={14} /> {primaryLabel}</>}
          </button>
          {result && result.kind === "applied" && (
            <span className="sr-apply-result" role="status" data-testid="sr-apply-result">
              已用于《{workTitle}》：之后起草的场景带上这本书的文风
              {result.restored ? "（按这本书上次用于它时的设置）" : ""}
              {result.replaced.length ? `（换下了${result.replaced.map((r) => `《${r.book_title || r.profile_title || "另一本"}》`).join("、")}）` : ""}。
            </span>
          )}
          {result && result.kind === "saved" && (
            <span className="sr-apply-result" role="status" data-testid="sr-apply-result">已保存，之后起草的场景按新设置带。</span>
          )}
        </div>
        <SrErrorLine error={error} onAction={onAction} testId="sr-apply-error" />
      </div>

      {others.length > 0 && (
        <div className="card" data-testid="sr-apply-others">
          <div className="card-head"><div><div className="card-title">这份文风还用在</div><div className="card-sub">场景级 / 角色级的应用在那一场、那个角色上盖过作品层</div></div></div>
          <ul className="sr-others">
            {others.map((b) => {
              const global = srIsLegacyGlobalBinding(b);
              const label = global ? "所有沿用它的作品" : b.scope === "project" ? `作品${projectTitleOf(b.scope_ref_id)}` : SR_SCOPE_WORD[b.scope] || b.scope;
              const target = global ? "没有自己应用的所有作品" : b.scope === "project" ? projectTitleOf(b.scope_ref_id) : b.scope_ref_id;
              return (
                <li key={b.binding_id} className="sr-other" data-binding-scope={b.scope}>
                  <Tag>{SR_SCOPE_WORD[b.scope] || b.scope}</Tag>
                  <span className="sr-other-target" title={b.scope_ref_id || undefined}>{target}</span>
                  <span className="sr-other-config">{summaryOf(b)}</span>
                  <button type="button" className="btn btn-quiet btn-sm" disabled={!!busy} onClick={() => unbind(b, label)}>解除</button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      <SrScenePreview book={book} profileId={profileId} config={effectiveDraft} workId={workId} workTitle={workTitle} />
    </div>
  );
}

/* 卡片式单选组（键盘约定同 Segmented：整组一个 Tab 停靠点，方向键换选项） */
function SrRadioCards({ label, name, items, value, onChange, testId }) {
  const radioItems = items.map((item) => ({ value: item.id, disabled: item.disabled }));
  return (
    <fieldset className="sr-choice" data-testid={testId}>
      <legend className="sr-policy-legend">{label}</legend>
      <div className="sr-choice-row" role="radiogroup" aria-label={label} onKeyDown={(e) => onRadioGroupKeyDown(e, { items: radioItems, value, onChange })}>
        {items.map((item) => {
          const on = item.id === value;
          return (
            <button
              key={item.id}
              type="button"
              role="radio"
              name={name}
              aria-checked={on}
              tabIndex={radioTabIndex(radioItems, value, item.id)}
              disabled={item.disabled}
              data-value={item.id}
              className={`sr-choice-card${on ? " is-active" : ""}`}
              onClick={() => { if (!on) onChange(item.id); }}
            >
              <span className="sr-choice-title">{item.label}{item.badge ? <em>{item.badge}</em> : null}</span>
              <span className="sr-choice-detail">{item.detail}</span>
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}
