import React from "react";
import { I } from "./icons.jsx";
import { rvPush } from "./ws-review.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { Notice, SectionLabel, Segmented, Spinner, Tabs, Tag, onRadioGroupKeyDown, radioTabIndex } from "./ws-ui.jsx";
import {
  SR_DRAFT_MODE_DEFAULT, SR_DRAFT_MODES, SR_SCOPE_NAME, SR_SCOPE_TONE, SR_STRATEGIES,
  buildDimOptions, computeIntensityReadout, computeResynthState, findShadowedBinding, srBindingActive,
  srDraftModeLabel, srStrategyCode, srStrategyName,
} from "./ws-styleref-model.js";
import {
  srAddBannedTerm, srAppliedOtherBookTitle, srInjectionPreview, srLoadBannedTerms, srLoadInjectionLayers,
  srLoadProjectTargets, srLoadTaskDefaults, srRemoveBannedTerm, srUnbind,
} from "./ws-styleref-store.js";
import { SrStageEmpty, srActiveWork, srNotify, srWorkTitle, useSrDeep, useSrStore } from "./ws-styleref-ui.jsx";
import { SrBundleReal, SrLayersReal, SrSampleWindows } from "./ws-styleref-inject.jsx";

/* ==========================================================
   风格参考 · 注入应用
   左：当前应用（这份画像已绑定到哪里）+ 注入设置（策略与维度 / 叠加层 / 样例窗口 / 禁用词）；
   右：应用动作（作用域、提示、「应用 · 进审核」，粘在顶部）+ 起草时实际带上的内容。
   「应用」只在待办里生成一条决策（rvPush，effect=bind_style_profile），批准后才绑定。
   ========================================================== */

/* 任务只保留唯一有生产消费方的 scene_generation；后端 /injection/task-defaults
   仍会返回多项，前端按 SR_TASKS 过滤只展示这一项（project_init / fine_tuning /
   key_chapter 在生成链路里没有调用点，2026-09 W7 移除）。只有一项，就不画成可选卡片。 */
export const SR_TASKS = [
  { id: "scene_generation", name: "场景生成", def: "mixed", refresh: 0 },
];
const SR_TASK_TYPE = "scene_generation";

const SR_SCOPE_OPTIONS = [
  { value: "project", label: "项目" },
  { value: "scene", label: "场景" },
  { value: "character", label: "角色" },
];

const SR_NO_TARGETS = { scene: [], character: [] };

/* 当前作品的场景 / 角色（场景级、角色级绑定选目标用）：直接取后端，不依赖目录缓存的状态。 */
function useSrScopeTargets(projectId, enabled) {
  const [targets, setTargets] = React.useState(SR_NO_TARGETS);
  React.useEffect(() => {
    if (!enabled || !projectId) { setTargets(SR_NO_TARGETS); return undefined; }
    let alive = true;
    srLoadProjectTargets(projectId).then(({ chapters, characters }) => {
      if (!alive) return;
      const scene = [];
      chapters.forEach((c) => (c.scenes || []).forEach((s) => {
        if (s && s.scene_id) scene.push({ id: s.scene_id, label: `${c.no ? `第 ${c.no} 章 · ` : ""}${s.title || "未命名场景"}` });
      }));
      const character = characters
        .map((c) => ({ id: c.character_id || c.id, label: c.name || c.display_name || "未命名角色" }))
        .filter((c) => c.id);
      setTargets({ scene, character });
    }).catch(() => { if (alive) setTargets(SR_NO_TARGETS); });
    return () => { alive = false; };
  }, [enabled, projectId]);
  return targets;
}

/* 真注入预览（dryrun，不写盘，debounce 350 ms）；nonce 变了强制刷新（禁用词增删后）。
   一次预览要一两秒、强度越高越慢：设置变了以后，旧请求晚到的结果一律丢掉，
   否则滑块拖到 10 而读数还在说 100 的篇幅。 */
function useSrInjectionPreview({ profileId, strategy, intensity, selectedDims, nonce }) {
  const [preview, setPreview] = React.useState(null);
  const [error, setError] = React.useState(null);
  React.useEffect(() => {
    if (!profileId) { setPreview(null); setError(null); return undefined; }
    let stale = false;
    const timer = setTimeout(() => {
      srInjectionPreview(profileId, {
        strategy, task_type: SR_TASK_TYPE, intensity,
        sub_dimensions: selectedDims,
        include_positive: true, include_forbidden: true, include_metric: strategy !== "C",
      }).then((r) => { if (!stale) { setPreview(r); setError(null); } })
        .catch((e) => { if (!stale) { setPreview(null); setError((e && e.message) || "注入预览失败"); } });
    }, 350);
    return () => { stale = true; clearTimeout(timer); };
  }, [profileId, strategy, intensity, selectedDims, nonce]);
  return { preview, error };
}

/* 画像级禁用词：列表（null = 还在读）、输入框草稿、增、删；增删成功后 onChanged（预览要刷新）。
   草稿放在这里（注入应用页一级），切页签回来不丢。 */
function useSrBannedTerms(profileId, onChanged) {
  const [terms, setTerms] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [input, setInput] = React.useState("");
  const [scope, setScope] = React.useState("generation");
  /* 换了画像后，上一份画像的禁用词晚到也不能写进来：只认当前画像的最近一次读取 */
  const latest = React.useRef(0);
  const load = React.useCallback(async () => {
    if (!profileId) return;
    const ticket = ++latest.current;
    let list;
    try { list = await srLoadBannedTerms(profileId); } catch { list = []; }
    if (ticket === latest.current) setTerms(list);
  }, [profileId]);
  React.useEffect(() => {
    if (profileId) load();
    else { latest.current += 1; setTerms(null); }
  }, [profileId, load]);
  const mutate = async (action, failText) => {
    if (busy) return false;
    setBusy(true);
    try {
      await action();
      await load();
      onChanged(); // 生成期禁用词进红线段，预览需刷新
      return true;
    } catch (e) {
      srNotify(failText + ((e && e.message) || e));
      return false;
    } finally { setBusy(false); }
  };
  const add = async () => {
    const term = input.trim();
    if (!term) return;
    if (await mutate(() => srAddBannedTerm(profileId, term, scope), "添加禁用词失败：")) setInput("");
  };
  return {
    terms, busy, input, setInput, scope, setScope, add,
    remove: (termId) => mutate(() => srRemoveBannedTerm(termId), "删除禁用词失败："),
  };
}

/* 任务默认表（真源 /injection/task-defaults：默认策略 + 续写刷新周期；失败回退静态值） */
function useSrTaskDefaults(enabled) {
  const [tasks, setTasks] = React.useState(null);
  React.useEffect(() => {
    if (!enabled) { setTasks(null); return undefined; }
    let alive = true;
    srLoadTaskDefaults().then((list) => { if (alive) setTasks(list); }).catch(() => { /* 静态默认兜底 */ });
    return () => { alive = false; };
  }, [enabled]);
  return SR_TASKS.map((t) => {
    const d = (tasks || []).find((x) => x.task_type === t.id);
    return d ? { ...t, def: d.default_strategy, refresh: d.refresh_every_chars || 0 } : t;
  });
}

/* 叠加注入层（真源 /injection/layers：命中层 + 预算分配）。只在「叠加层」页签打开时读；
   上下文带本画像已绑定的场景 / 角色 id，使 scene / character 层能在预览中亮起。 */
function useSrLayerStack({ enabled, projectId, bindings }) {
  const [stack, setStack] = React.useState(null);
  const [error, setError] = React.useState(null);
  /* 绑定换了（解绑一个、批准另一个，条数可能不变）就重读：按绑定 id 而不是条数 */
  const bindingKey = bindings.map((b) => b.binding_id).join("|");
  React.useEffect(() => {
    if (!enabled || !projectId) { setStack(null); setError(null); return undefined; }
    let alive = true;
    const sceneBinding = bindings.find((b) => b.scope === "scene" && b.scope_ref_id);
    srLoadInjectionLayers({
      projectId,
      taskType: SR_TASK_TYPE,
      sceneId: sceneBinding ? sceneBinding.scope_ref_id : null,
      characterIds: bindings.filter((b) => b.scope === "character" && b.scope_ref_id).map((b) => b.scope_ref_id),
    }).then((r) => { if (alive) { setStack(r); setError(null); } })
      .catch((e) => { if (alive) { setStack(null); setError((e && e.message) || "叠层加载失败"); } });
    return () => { alive = false; };
  }, [enabled, projectId, bindingKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return { stack, error };
}

export function SrApply({ go, book }) {
  const deep = useSrDeep(book);
  /* 书库附加事实（当前作品在用哪本书）更新时重渲 */
  useSrStore("books");
  const [sub, setSub] = React.useState("strategy");
  const [strategy, setStrategy] = React.useState("mixed");
  const [applied, setApplied] = React.useState(null); // 已创建的审核条目描述
  const [intensity, setIntensity] = React.useState(100); // 2026-09-12 风格直起：新绑定默认强度拉满
  const [draftMode, setDraftMode] = React.useState(SR_DRAFT_MODE_DEFAULT);
  const [scope, setScope] = React.useState("project");
  const [scopeRefId, setScopeRefId] = React.useState(null); // 场景 / 角色级绑定的目标 id
  const [unbindBusy, setUnbindBusy] = React.useState(null);
  const [previewNonce, setPreviewNonce] = React.useState(0);
  const formRef = React.useRef(null);

  const profileId = (deep && deep.profileId) || null;
  const hasProfile = !!profileId;
  const profile = (deep && deep.profile) || null;
  const bindings = ((deep && deep.bindings) || []).filter(srBindingActive);
  const profileInactive = hasProfile && profile && profile.status !== "active";
  const profileResynth = computeResynthState(deep);
  const profileTitle = (profile && profile.title) || `《${book.title}》风格画像`;
  const work = srActiveWork();
  const workId = work ? work.id : null;
  const workTitle = (work && work.title) || "当前作品";

  /* 注入维度：按画像 profile_json.sub_dimensions 动态生成（缺失回退 input_assessment） */
  const dimOptions = buildDimOptions(profile, deep && deep.book);
  const availableDims = dimOptions.available;
  const [selectedDims, setSelectedDims] = React.useState(() => availableDims);

  const targets = useSrScopeTargets(workId, hasProfile);
  const { preview, error: previewErr } = useSrInjectionPreview({ profileId, strategy, intensity, selectedDims, nonce: previewNonce });
  const banned = useSrBannedTerms(profileId, () => setPreviewNonce((n) => n + 1));
  const task = useSrTaskDefaults(hasProfile)[0];
  const layers = useSrLayerStack({ enabled: hasProfile && sub === "layers", projectId: workId, bindings });

  /* 绑定目标的名字：作品名 / 场景标题 / 角色名；认不出就退回原始 id（title 里也能看到） */
  const targetLabel = (scopeName, refId) => {
    if (!refId) return scopeName === "global" ? "全部作品" : "—";
    if (scopeName === "project") { const t = srWorkTitle(refId); return t ? `《${t}》` : refId; }
    const opt = (targets[scopeName] || []).find((o) => o.id === refId);
    return opt ? opt.label : refId;
  };

  /* 表单跟着所选作用域的现有绑定走：有绑定就把它的配置填进来（修改而不是另起一份），
     离开有绑定的作用域回到默认；没有绑定的作用域之间切换不动作者的改动 */
  const shadowRefId = scope === "project" ? workId : scopeRefId;
  const shadowed = hasProfile && (scope === "project" || !!scopeRefId) ? findShadowedBinding(bindings, scope, shadowRefId) : null;
  const seedKey = `${profileId || ""}|${shadowed ? shadowed.binding_id : "none"}|${availableDims.join("|")}`;
  const seededFromBinding = React.useRef(false);
  React.useEffect(() => {
    if (shadowed) {
      const cfg = shadowed.config_json || {};
      const n = Number(cfg.intensity);
      setStrategy(shadowed.strategy || "mixed");
      setIntensity(Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : 100);
      setDraftMode(cfg.draft_mode === "neutral_first" ? "neutral_first" : SR_DRAFT_MODE_DEFAULT);
      const dims = Array.isArray(cfg.sub_dimensions) ? cfg.sub_dimensions.filter((p) => availableDims.includes(p)) : null;
      setSelectedDims(dims && dims.length ? dims : availableDims);
      seededFromBinding.current = true;
    } else {
      if (seededFromBinding.current) {
        setStrategy("mixed"); setIntensity(100); setDraftMode(SR_DRAFT_MODE_DEFAULT);
        seededFromBinding.current = false;
      }
      setSelectedDims(availableDims);
    }
  }, [seedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  /* 改了任何一项，上一次「已进入审核」就不再代表当前表单 */
  React.useEffect(() => { setApplied(null); }, [strategy, intensity, draftMode, scope, scopeRefId, selectedDims]);

  /* 这部作品现在是不是在用另一本书的画像：项目级只取最新的一份作底，批准后会换成这本 */
  const otherApplied = hasProfile && scope === "project" ? srAppliedOtherBookTitle(book.id) : null;

  const changeScope = (next) => { setScope(next); setScopeRefId(null); };
  const editBinding = (b) => {
    setScope(b.scope === "global" ? "project" : b.scope);
    setScopeRefId(b.scope === "project" || b.scope === "global" ? null : b.scope_ref_id);
    setSub("strategy");
    if (formRef.current && formRef.current.scrollIntoView) formRef.current.scrollIntoView({ block: "start", behavior: "smooth" });
  };
  const unbind = async (b) => {
    if (unbindBusy) return;
    const target = targetLabel(b.scope, b.scope_ref_id);
    const ok = await wsConfirm({
      title: `解除「${profileTitle}」在${SR_SCOPE_NAME[b.scope] || b.scope}${target}上的应用？`,
      body: `解除后，${target}起草时不再带这本书的风格。想再用时要重新应用，并在待办里再批准一次。`,
      confirmLabel: "解除应用",
      tone: "danger",
    });
    if (!ok) return;
    setUnbindBusy(b.binding_id);
    try {
      await srUnbind(b.binding_id, book.id);
      srNotify(`已解除${target}上的应用`, "neutral");
    } catch (e) {
      srNotify("解除失败：" + ((e && e.message) || e));
    } finally { setUnbindBusy(null); }
  };

  const onApply = () => {
    if (!rvPush || !hasProfile) return;
    const selOpt = scope !== "project" ? (targets[scope] || []).find((o) => o.id === scopeRefId) : null;
    const selLabel = selOpt ? selOpt.label : (scopeRefId || "");
    const scopeName = scope === "project" ? `项目《${workTitle}》`
      : scope === "scene" ? `场景 ${selLabel}` : `角色 ${selLabel}`;
    // scope_ref_id：项目级用 project_id，场景 / 角色级用所选目标 id（显式传，不靠后端回退）
    const effScopeRefId = scope === "project" ? workId : scopeRefId;
    rvPush({
      kind: "decision", priority: 1,
      title: `参考画像「${profileTitle}」应用到${scopeName}`,
      where: "风格参考 · 注入应用", source: "风格参考",
      detail: `策略 ${strategy === "mixed" ? "A+B 混合" : strategy} · 强度 ${intensity}% · ${selectedDims.length} 维 · 起草 ${srDraftModeLabel(draftMode)}。批准后画像绑定到该范围、作为生成期默认润色基线，可随时回风格参考解绑。`,
      dedupe_key: `style-apply:${profileId}:${scope}:${effScopeRefId || "_"}:${strategy}`,
      actions: [
        { label: "批准应用", intent: "primary", op: "resolve",
          effect: {
            type: "bind_style_profile",
            profile_id: profileId,
            scope, scope_ref_id: effScopeRefId, task_type: SR_TASK_TYPE, strategy, intensity,
            sub_dimensions: selectedDims,
            include_positive: true, include_forbidden: true, include_metric: strategy !== "C",
            draft_mode: draftMode,
          } },
        { label: "回风格参考调整", intent: "ghost", op: "nav", to: "styleref" },
        { label: "丢弃", intent: "quiet", op: "resolve" },
      ],
    });
    setApplied(scopeName);
  };

  /* 还没有画像：注入应用无对象，给真实空态引导 */
  if (!hasProfile) {
    return (
      <SrStageEmpty icon="Sliders" title="还没有可应用的画像" actionLabel="去维度矩阵" onAction={() => go && go("matrix")}>
        应用的是「风格画像」：先在「维度矩阵」完成抽取并合成画像，再回到这里选策略和强度，应用到项目、场景或角色。
      </SrStageEmpty>
    );
  }

  const scopeWord = SR_SCOPE_NAME[scope] || scope;
  const scopeTargets = targets[scope] || [];
  const applyDisabled = !!applied || (scope !== "project" && !scopeRefId) || (scope === "project" && !workId);

  return (
    <div className="sr-apply">
      <div className="sr-apply-main">
        <SrCurrentBindings
          bindings={bindings}
          profile={profile}
          profileInactive={profileInactive}
          targetLabel={targetLabel}
          unbindBusy={unbindBusy}
          onEdit={editBinding}
          onUnbind={unbind}
        />

        <div ref={formRef} className="sr-apply-form">
          <Tabs
            label="注入设置"
            idPrefix="sr-apply"
            className="sr-apply-tabs"
            value={sub}
            onChange={setSub}
            tabs={[
              { id: "strategy", label: "策略与维度", icon: <I.Sliders size={13} /> },
              { id: "layers", label: "叠加层", icon: <I.Layers size={13} /> },
              { id: "fewshot", label: "样例窗口", icon: <I.Quote size={13} /> },
              { id: "banned", label: "禁用词", icon: <I.Ban size={13} />, count: banned.terms ? banned.terms.length : null },
            ]}
          />

          {sub === "strategy" && (
            <div className="sr-apply-panel" role="tabpanel" id="sr-apply-panel-strategy" aria-labelledby="sr-apply-tab-strategy">
              <SrStrategyCard task={task} strategy={strategy} onStrategy={setStrategy} />
              <SrIntensityCard intensity={intensity} onIntensity={setIntensity} readout={computeIntensityReadout(preview && preview.stats)} previewErr={previewErr} />
              <SrDraftModeCard draftMode={draftMode} onDraftMode={setDraftMode} />
              <SrDimsCard dimOptions={dimOptions} selectedDims={selectedDims} onSelectedDims={setSelectedDims} />
            </div>
          )}

          {sub === "layers" && (
            <div className="sr-apply-panel" role="tabpanel" id="sr-apply-panel-layers" aria-labelledby="sr-apply-tab-layers">
              <SrLayersReal stack={layers.stack} err={layers.error} bindings={bindings} targetLabel={targetLabel} />
            </div>
          )}

          {sub === "fewshot" && (
            <div className="sr-apply-panel" role="tabpanel" id="sr-apply-panel-fewshot" aria-labelledby="sr-apply-tab-fewshot">
              <SrSampleWindows preview={preview} previewErr={previewErr} strategy={strategy} />
            </div>
          )}

          {sub === "banned" && (
            <div className="sr-apply-panel" role="tabpanel" id="sr-apply-panel-banned" aria-labelledby="sr-apply-tab-banned">
              <SrBannedCard banned={banned} />
            </div>
          )}
        </div>
      </div>

      {/* 右：应用动作在最上，注入预览在下 */}
      <aside className="sr-apply-side">
        <div className="card sr-apply-action">
          <SectionLabel icon="GitBranch">应用到</SectionLabel>
          <Segmented label="应用范围" className="sr-scope" block value={scope} onChange={changeScope} options={SR_SCOPE_OPTIONS} />
          {scope === "project" ? (
            <p className="sr-apply-target">{workId ? <>应用到《{workTitle}》整部作品</> : "还没有打开作品"}</p>
          ) : (
            <label className="sr-scope-target">
              <span className="ws-sr-only">{`选择${scopeWord}`}</span>
              <select className="select" value={scopeRefId || ""} onChange={(e) => setScopeRefId(e.target.value || null)}>
                <option value="">{`选择${scopeWord}…`}</option>
                {scopeTargets.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
              {scopeTargets.length === 0 && (
                <span className="sr-side-note">
                  {scope === "scene" ? "当前作品还没有场景：先在构思或章节编排里建场景。" : "当前作品还没有角色：先在构思里补充角色。"}
                </span>
              )}
            </label>
          )}
          {shadowed && !applied && (
            <Notice tone="info" className="sr-apply-note" testId="sr-apply-shadow">
              将替换当前绑定：<b>{profileTitle}</b>（{srStrategyCode(shadowed.strategy)}{shadowed.scope_ref_id ? ` · ${targetLabel(shadowed.scope, shadowed.scope_ref_id)}` : ""}），改成下面的设置。
            </Notice>
          )}
          {otherApplied && !shadowed && !applied && (
            <Notice tone="info" className="sr-apply-note">
              《{workTitle}》现在用的是《{otherApplied}》的风格，批准后改用这本。
            </Notice>
          )}
          {profileInactive && !applied && (
            <Notice tone="warn" className="sr-apply-note" testId="sr-apply-inactive">
              {profileResynth.reason === "stale" ? "画像已失效，应用前请先重新合成。" : "画像还没激活：批准绑定后会自动激活。"}
            </Notice>
          )}
          <button type="button" className="btn btn-accent btn-lg sr-apply-btn" disabled={applyDisabled} onClick={onApply}>
            <I.Check size={15} /> {applied ? "已进入审核" : `应用到${scopeWord} · 进审核`}
          </button>
          {applied ? (
            <p className="sr-apply-done">已为{applied}创建审核条目：<a href="#review">去待办批准</a></p>
          ) : (
            <p className="sr-side-note">应用会在「待办」里生成一条决策，批准后才在起草时生效。</p>
          )}
        </div>

        <div className="card-flat sr-bundle">
          <SectionLabel icon="FileText" aside={srStrategyCode(strategy)}>起草时实际带上的内容</SectionLabel>
          <SrBundleReal preview={preview} previewErr={previewErr} />
        </div>
      </aside>
    </div>
  );
}

/* 当前应用：这份画像已经批准绑定到的地方，每行可「修改」（把表单切到这个作用域）或「解除应用」。 */
function SrCurrentBindings({ bindings, profile, profileInactive, targetLabel, unbindBusy, onEdit, onUnbind }) {
  return (
    <div className="card sr-current">
      <div className="card-head">
        <div><div className="card-title">当前应用</div><div className="card-sub">这份画像已经批准绑定到的地方；起草这些地方时会带上这本书的风格</div></div>
      </div>
      {profileInactive && bindings.length > 0 && (
        <Notice tone="warn" testId="sr-bindings-inactive" className="sr-current-note">
          画像已失效（{profile.coverage_json && profile.coverage_json.stale ? "参与合成的观察变了" : "还没激活"}），注入不会生效：回「风格画像」重新合成后再应用。
        </Notice>
      )}
      <ul className="sr-bindings">
        {bindings.length === 0 && (
          <li className="sr-bindings-empty">还没有应用到任何地方。在右侧选好范围后「应用」，到待办里批准后出现在这里。</li>
        )}
        {bindings.map((b) => {
          const cfg = b.config_json || {};
          const dims = Array.isArray(cfg.sub_dimensions) ? cfg.sub_dimensions.length : null;
          const inten = Number.isFinite(Number(cfg.intensity)) ? Number(cfg.intensity) : null;
          return (
            <li key={b.binding_id} className="sr-binding">
              <div className="sr-binding-main">
                <Tag tone={SR_SCOPE_TONE[b.scope] || "neutral"} dot>{SR_SCOPE_NAME[b.scope] || b.scope}</Tag>
                <span className="sr-binding-target" title={b.scope_ref_id || undefined}>{targetLabel(b.scope, b.scope_ref_id)} · {srStrategyCode(b.strategy)}</span>
              </div>
              <div className="sr-binding-facts">
                <span>{srStrategyName(b.strategy)}</span>
                {inten != null && <span>强度 {inten}%</span>}
                {dims != null && <span>{dims} / 16 维</span>}
                <Tag testId="sr-binding-draft-mode" title="起草方式">{srDraftModeLabel(cfg.draft_mode)}</Tag>
              </div>
              <div className="sr-binding-actions">
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => onEdit(b)}>修改</button>
                <button type="button" className="btn btn-quiet btn-sm" disabled={unbindBusy === b.binding_id} onClick={() => onUnbind(b)}>
                  {unbindBusy === b.binding_id ? <Spinner size={12} /> : null} 解除应用
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function SrStrategyCard({ task, strategy, onStrategy }) {
  const stratItems = SR_STRATEGIES.map((s) => ({ value: s.id }));
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">注入策略</div><div className="card-sub">决定起草时怎样把这本书的风格交给模型</div></div></div>
      <div className="sr-task-row">
        <div className="sr-task">
          <span className="sr-task-name">{task.name}</span>
          <span className="sr-task-def">默认：{srStrategyName(task.def)}{task.refresh > 0 ? ` · 每 ${task.refresh} 字刷新` : ""}</span>
        </div>
      </div>
      {/* 单选组的键盘约定与 Segmented 相同：整组一个 Tab 停靠点（选中的那张），←→ 换策略 */}
      <div className="sr-strat-row" role="radiogroup" aria-label="注入策略"
        onKeyDown={(e) => onRadioGroupKeyDown(e, { items: stratItems, value: strategy, onChange: onStrategy })}>
        {SR_STRATEGIES.map((s) => {
          const active = strategy === s.id;
          return (
            <button key={s.id} type="button" role="radio" aria-checked={active} tabIndex={radioTabIndex(stratItems, strategy, s.id)}
              className={`sr-strat ${active ? "is-active" : ""}`} onClick={() => onStrategy(s.id)}>
              <span className="sr-strat-badge">{s.code}</span>
              <span className="sr-strat-title">{s.name}</span>
              <span className="sr-strat-desc">{s.desc}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* 强度读数只来自预览端点的 stats；没有 stats 就显示「预览中…」，没有本地公式 */
function SrIntensityCard({ intensity, onIntensity, readout, previewErr }) {
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">风格强度</div><div className="card-sub">同时决定规则的篇幅和样例窗口的数量（0 最轻，100 最强）</div></div>
        <span className="sr-intensity-val tab-num">{intensity}%</span>
      </div>
      <input type="range" min="0" max="100" value={intensity} onChange={(e) => onIntensity(parseInt(e.target.value, 10))} className="sr-range" aria-label="风格强度" aria-valuetext={`${intensity}%`} />
      <div className="sr-intensity-ticks" aria-hidden="true"><span>轻</span><span>中</span><span>强</span></div>
      <div className="sr-intensity-readout" data-testid="sr-intensity-readout" role="status">
        <I.Sparkles size={13} />
        {readout
          ? <span>当前注入：<b>{readout.text}</b> · 防抄袭红线固定保留</span>
          : <span>{previewErr ? "预览失败，读数不可用" : "预览中…"}</span>}
      </div>
    </div>
  );
}

function SrDraftModeCard({ draftMode, onDraftMode }) {
  return (
    <div className="card">
      <div className="card-head"><div><div className="card-title">起草方式</div><div className="card-sub">有绑定时首稿由谁定调：直接用参考作者的手笔，还是先出中性稿再上风格</div></div>
        <Tag testId="sr-draft-mode-current">{srDraftModeLabel(draftMode)}</Tag>
      </div>
      <fieldset className="sr-policy-list" data-testid="sr-draft-mode">
        <legend className="sr-policy-legend">首稿怎么写</legend>
        {SR_DRAFT_MODES.map((m) => (
          <label key={m.id} className={`sr-policy ${draftMode === m.id ? "is-selected" : ""}`}>
            <input type="radio" name="sr-draft-mode" value={m.id} checked={draftMode === m.id} onChange={() => onDraftMode(m.id)} />
            <span className="sr-policy-mark" aria-hidden="true" />
            <span className="sr-policy-copy">
              <span className="sr-policy-title">{m.label}{m.badge ? <em>{m.badge}</em> : null}</span>
              <span className="sr-policy-detail">{m.detail}</span>
            </span>
          </label>
        ))}
      </fieldset>
    </div>
  );
}

function SrDimsCard({ dimOptions, selectedDims, onSelectedDims }) {
  const available = dimOptions.available;
  const all = available.length > 0 && selectedDims.length === available.length;
  const toggle = (path) => onSelectedDims((prev) => (prev.includes(path) ? prev.filter((p) => p !== path) : [...prev, path]));
  return (
    <div className="card">
      <div className="card-head">
        <div><div className="card-title">注入维度</div><div className="card-sub">勾选参与注入的维度（{selectedDims.length} / {available.length} 可用已选{dimOptions.source === "profile" ? "，按画像覆盖的维度" : dimOptions.source === "input_assessment" ? "，按语料评估" : ""}）</div></div>
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => onSelectedDims(selectedDims.length === available.length ? [] : available)}>{all ? "全不选" : "全选"}</button>
      </div>
      <div className="sr-dimselect" data-testid="sr-dimselect">
        {dimOptions.layers.map((l) => (
          <div key={l.id} className="sr-ds-layer">
            <div className="sr-ds-layer-name">{l.name}{l.skipped ? <span className="sr-ds-skip">语料不足</span> : null}</div>
            <div className="sr-ds-cells">
              {l.subs.map((s) => {
                const disabled = !s.available;
                const on = selectedDims.includes(s.path);
                const title = disabled
                  ? (dimOptions.source === "profile" ? "画像没有覆盖这个维度" : "这一层语料不足，已跳过")
                  : (s.conf ? `${s.obs} 观察 · ${s.fp} 禁忌 · ${s.q} 引文` : undefined);
                return (
                  <button key={s.id} type="button" className={`sr-ds-cell ${on ? "is-on" : ""} ${disabled ? "is-disabled" : ""}`}
                    data-dim={s.path} title={title} aria-pressed={on}
                    onClick={() => !disabled && toggle(s.path)} disabled={disabled}>
                    {on && <I.Check size={11} />}{s.name}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* 禁用词：「生成时禁用」写进红线段；「抽取时跳过」在下次重跑抽取时滤掉含这个词的段落 */
function SrBannedCard({ banned }) {
  return (
    <div className="card">
      <div className="card-head">
        <div><div className="card-title">禁用词</div><div className="card-sub">「生成时禁用」写进红线段；「抽取时跳过」在下次重跑抽取时滤掉含这个词的段落</div></div>
      </div>
      <div className="sr-banned-add">
        <input className="input" placeholder="添加禁用词…" aria-label="新的禁用词" value={banned.input} disabled={banned.busy}
          onChange={(e) => banned.setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !isImeComposing(e)) banned.add(); }} />
        <Segmented
          label="禁用词作用"
          value={banned.scope}
          onChange={banned.setScope}
          options={[{ value: "generation", label: "生成时禁用" }, { value: "extraction", label: "抽取时跳过" }]}
        />
        <button type="button" className="btn btn-primary btn-sm" disabled={banned.busy || !banned.input.trim()} onClick={banned.add}><I.Plus size={13} /> 添加</button>
      </div>
      <ul className="sr-banned-list">
        {(banned.terms || []).map((b) => (
          <li key={b.term_id} className="sr-banned-item">
            <Tag tone={b.scope === "generation" ? "accent" : "info"}>{b.scope === "generation" ? "生成时禁用" : "抽取时跳过"}</Tag>
            <span className="sr-banned-term text-serif">{b.term}</span>
            {b.replacement_hint && <span className="sr-banned-hint">改用：{b.replacement_hint}</span>}
            {b.source === "preset" && <span className="sr-banned-preset">预置</span>}
            {b.source !== "preset" && (
              <button type="button" className="btn btn-quiet btn-sm btn-icon" aria-label={`删除禁用词「${b.term}」`} title="删除" disabled={banned.busy} onClick={() => banned.remove(b.term_id)}><I.X size={13} /></button>
            )}
          </li>
        ))}
      </ul>
      {banned.terms && banned.terms.length === 0 && <p className="sr-ov-text sr-ov-muted">还没有禁用词。</p>}
      {!banned.terms && <p className="sr-ov-text sr-ov-muted">正在读取…</p>}
    </div>
  );
}
