import React from "react";
import { I } from "./icons.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, ProgressBar, RadioCards, Segmented, Spinner, StatTile, Tabs, Tag } from "./ws-ui.jsx";
import { srActivityActive, srActivityView, srFormatWhen, srModelGate } from "./ws-styleref-model.js";
import {
  RATCHET_PLACEMENTS, srRatchetAgreement, srRatchetArmDiff, srRatchetBody, srRatchetDefaultForm, srRatchetPreviewStats,
  srRatchetReadingView, srRatchetRunTag, srRatchetRunTitle, srRatchetSameArms, srRatchetSourceLabel, srRatchetToggleVote,
  srRatchetUsageText, srRatchetVerdictStats, srRatchetVerdictView, srRatchetVoteDone, srRatchetVotesDone,
} from "./ws-styleref-ratchet-model.js";
import {
  srCancelRatchet, srLoadRatchetOverview, srLoadRatchetRun, srRatchetOverview, srRatchetPreview, srRatchetRun,
  srSaveRatchetVotes, srStartRatchet,
} from "./ws-styleref-ratchet-store.js";
import { srActivityFor, srLoadRuntime, srRuntime, srSubscribe } from "./ws-styleref-store.js";
import { SrErrorLine, SrStageEmpty, srNotifyError, srProgressTone, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 ·「配置对比」（风格棘轮的界面；2026-10-18）
   改风格配置之前先验收：从这本书里留出几段原文，提炼出情节要点，两套起草配置（模型 / 样例窗数 / 样例位置）各照着写
   一遍，量谁离原文更近；同时给一份盲读——每段三份文字（原文、A、B）顺序打乱，作者读完选最像 / 最不像，再揭晓。
   · 总览：比什么（两套配置 / 量噪声）+ 两栏配置 + 预估 + 做过的对比 + 噪声记录；
   · 一次比较：先盲读、后看数字（揭晓之前后端不给答案和数字；揭晓后选择锁定）；
   · 在跑的进度也在「参考书活动」里，从那里「打开」一次比较落在这一页（adoptJobId）。
   控件与兄弟页一套：比什么用 RadioCards（同「用于作品」的参考方式）、样例窗数用同一条滑杆（.sr-windows）、选最像 / 最不像
   用 Segmented（同文风画像的「重点 / 正常 / 不学」）、段落用 Tabs、数字用 StatTile、逐段数字用 .fid-table。
   请求与缓存在 ws-styleref-ratchet-store.js，说法在 ws-styleref-ratchet-model.js。
   ========================================================== */

export function SrRatchet({ book, onAction, adoptJobId = null, onAdopted = null }) {
  useSrStore("ratchet", "activity", "detail");
  const profileId = book.profile ? book.profile.profile_id : null;
  const [openJob, setOpenJob] = React.useState(adoptJobId || null);

  React.useEffect(() => { srLoadRuntime(); }, []);
  React.useEffect(() => { if (profileId) srLoadRatchetOverview(profileId, { force: true }); }, [profileId]);
  React.useEffect(() => {
    if (!adoptJobId) return;
    setOpenJob(adoptJobId);
    if (onAdopted) onAdopted(adoptJobId);
  }, [adoptJobId]); // eslint-disable-line react-hooks/exhaustive-deps
  /* 一次比较结束（活动表里看到的）：重读总览；开着的就是它的话连详情一起重读 */
  React.useEffect(() => srSubscribe("finished")((event) => {
    const entry = event && event.detail;
    if (!entry || entry.kind !== "ratchet" || !profileId) return;
    srLoadRatchetOverview(profileId, { force: true });
    if (entry.job_id) srLoadRatchetRun(entry.job_id, { force: true });
  }), [profileId]);

  if (!profileId) {
    return (
      <SrStageEmpty icon="Sliders" title="学完文风之后才能比" testId="sr-ratchet-empty">
        配置对比拿这本书的文风画像起草：先在「学习文风」里学一次。
      </SrStageEmpty>
    );
  }
  if (openJob) {
    return <SrRatchetRun jobId={openJob} profileId={profileId} onBack={() => setOpenJob(null)} />;
  }
  return <SrRatchetHome book={book} profileId={profileId} onAction={onAction} onOpen={setOpenJob} />;
}

/* ---------- 总览：表单、在跑的、历史、噪声 ---------- */

function SrRatchetHome({ book, profileId, onAction, onOpen }) {
  const entry = srRatchetOverview(profileId);
  const overview = entry && entry.data;
  if (!overview) {
    if (entry && entry.phase === "error") {
      return (
        <div className="card">
          <SrErrorLine error={entry.error} onAction={onAction} />
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => srLoadRatchetOverview(profileId, { force: true })}><I.Refresh size={13} /> 重试</button>
        </div>
      );
    }
    return <div className="card sr-stage-loading-inline"><Spinner size={14} label="正在读取配置对比" /> 正在读取配置对比…</div>;
  }
  const live = srActivityFor(book.id, "ratchet");
  const active = live || (overview.active && srActivityActive(overview.active) ? overview.active : null);
  const noise = overview.noise || [];
  return (
    <div className="sr-ratchet" data-testid="sr-ratchet">
      {!overview.available ? (
        <Notice tone="warn" testId="sr-ratchet-unavailable">{overview.reason || "这份画像现在比不了。"}</Notice>
      ) : active ? (
        <SrRatchetActive job={active} profileId={profileId} />
      ) : (
        <SrRatchetForm overview={overview} profileId={profileId} bookId={book.id} onAction={onAction} />
      )}

      <SrRatchetHistory runs={overview.runs || []} onOpen={onOpen} />

      {noise.length > 0 && (
        <div className="card" data-testid="sr-ratchet-noise">
          <div className="card-head">
            <div>
              <div className="card-title">噪声</div>
              <div className="card-sub">同一配置写两份的随机差别。比较时差距要大过它才算数，自动用上。</div>
            </div>
          </div>
          <ul className="sr-ratchet-noise">
            {noise.map((record) => (
              <li key={record.key}>
                <span className="sr-ratchet-noise-what">{record.describe}</span>
                <Tag tone="neutral">平均差 {Number(record.mean || 0).toFixed(3)} · {record.passages} 段</Tag>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function SrRatchetActive({ job, profileId }) {
  const [busy, setBusy] = React.useState(false);
  const view = srActivityView(job);
  const cancel = async () => {
    setBusy(true);
    try { await srCancelRatchet(profileId); } catch (err) { srNotifyError(err); } finally { setBusy(false); }
  };
  return (
    <div className="card" data-testid="sr-ratchet-active">
      <div className="card-head">
        <div>
          <div className="card-title sr-ratchet-running"><Spinner size={14} /> 正在对比</div>
          <div className="card-sub">可以离开这一页：进度也在「参考书活动」里，跑完了在「做过的对比」里打开。</div>
        </div>
        <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-ratchet-cancel" disabled={busy || !!job.cancel_requested} onClick={cancel}>
          <I.X size={13} /> 取消
        </button>
      </div>
      <ProgressBar value={view.percent} label="配置对比的进度" valueText={view.detail} tone={srProgressTone(job.status)} />
      <p className="sr-ov-hint" role="status">{view.detail}</p>
    </div>
  );
}

/* 一套配置的三项：模型、样例窗数（同「用于作品」的滑杆）、样例放在哪。changed：B 栏里和 A 不一样的项标「改」 */
function SrRatchetArmFields({ side, arm, models, windowsMax, changed = [], onChange }) {
  const set = (key, value) => onChange({ ...arm, [key]: value });
  const modelList = models && models.length ? models : null;
  const mark = (key) => (changed.includes(key) ? <Tag tone="accent">改</Tag> : null);
  const windows = Number(arm.windows) || 0;
  const id = `sr-ratchet-${side}`;
  return (
    <div className="sr-ratchet-arm" data-testid={`${id}-arm`}>
      <div className="sr-ratchet-field">
        <label className="label" htmlFor={`${id}-model`}>模型 {mark("model")}</label>
        {modelList ? (
          <select id={`${id}-model`} className="select" value={arm.model} onChange={(e) => set("model", e.target.value)} data-testid={`${id}-model`}>
            {modelList.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        ) : (
          <input id={`${id}-model`} className="input" value={arm.model} onChange={(e) => set("model", e.target.value)} placeholder="风格稿路由的模型" data-testid={`${id}-model`} />
        )}
      </div>
      <div className="sr-windows">
        <div className="sr-windows-head">
          <label className="sr-windows-label" htmlFor={`${id}-windows`}>样例窗数 {mark("windows")}</label>
          <span className="sr-windows-value tab-num">{windows ? `${windows} 窗` : "不带样例"}</span>
        </div>
        <input
          id={`${id}-windows`}
          type="range"
          className="sr-range"
          min={0}
          max={windowsMax}
          step={1}
          value={windows}
          aria-valuetext={windows ? `${windows} 窗` : "不带样例"}
          data-testid={`${id}-windows`}
          onChange={(e) => set("windows", parseInt(e.target.value, 10))}
        />
      </div>
      <div className="sr-ratchet-field">
        <span className="label">样例放在 {mark("placement")}</span>
        <Segmented size="sm" label={`配置 ${side.toUpperCase()} 的样例位置`} value={arm.placement} options={RATCHET_PLACEMENTS} onChange={(v) => set("placement", v)} />
      </div>
    </div>
  );
}

function SrRatchetForm({ overview, profileId, bookId, onAction }) {
  const current = overview.current;
  const [form, setForm] = React.useState(() => srRatchetDefaultForm(current, { passages: overview.passages_default }));
  const [preview, setPreview] = React.useState({ phase: "idle", data: null, error: null });
  const [starting, setStarting] = React.useState(false);
  const runtime = srRuntime();
  const gate = srModelGate(runtime.phase === "ready" ? runtime.data : null, { purpose: "ratchet" });
  const same = srRatchetSameArms(form);
  const body = srRatchetBody(form);
  const bodyKey = JSON.stringify(body);
  const noNoise = !(overview.noise && overview.noise.length);

  /* 表单停手 400 ms 后重算预估（只读、不建作业） */
  React.useEffect(() => {
    if (same) { setPreview({ phase: "idle", data: null, error: null }); return undefined; }
    let alive = true;
    setPreview((p) => ({ ...p, phase: "loading" }));
    const timer = setTimeout(() => {
      srRatchetPreview(profileId, body)
        .then((data) => { if (alive) setPreview({ phase: "ready", data, error: null }); })
        .catch((error) => { if (alive) setPreview({ phase: "error", data: null, error }); });
    }, 400);
    return () => { alive = false; clearTimeout(timer); };
  }, [profileId, bodyKey, same]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    setStarting(true);
    try { await srStartRatchet(profileId, body, { bookId }); }
    catch (err) { srNotifyError(err); }
    finally { setStarting(false); }
  };
  const stats = preview.phase === "ready" ? srRatchetPreviewStats(preview.data) : [];
  return (
    <div className="card sr-ratchet-form" data-testid="sr-ratchet-form">
      <div className="card-head">
        <div>
          <div className="card-title">配置对比</div>
          <div className="card-sub">
            改风格配置之前先验收：从书里留出几段原文（起草时不给模型看），两套配置照着同样的情节各写一遍，量谁离原文更近，再给你盲读。
          </div>
        </div>
      </div>

      <RadioCards
        label="比什么"
        value={form.aa ? "aa" : "compare"}
        onChange={(v) => setForm({ ...form, aa: v === "aa" })}
        testId="sr-ratchet-mode"
        items={[
          { value: "compare", label: "两套配置比一比", detail: "A 是现在的配置，B 改一两项：看换了以后是更像了，还是只是不一样了。", testId: "sr-ratchet-mode-compare" },
          {
            value: "aa", label: "量噪声（A/A）", badge: noNoise ? "先做这个" : null, testId: "sr-ratchet-mode-aa",
            detail: "同一套配置写两份，量出随机差别有多大；之后的比较，差距要大过它才算数。",
          },
        ]}
      />

      <div className="sr-ratchet-arms">
        <section className="sr-ratchet-side" aria-label="配置 A">
          <div className="sr-ratchet-side-title">配置 A <span>{current && current.from_binding ? "默认 = 现在用的" : "默认 = 缺省设置"}</span></div>
          <SrRatchetArmFields side="a" arm={form.a} models={overview.models} windowsMax={overview.windows_max} onChange={(a) => setForm({ ...form, a })} />
        </section>
        <section className="sr-ratchet-side" aria-label="配置 B">
          <div className="sr-ratchet-side-title">配置 B <span>{form.aa ? "= A，另写一份" : "改一两项来比"}</span></div>
          {form.aa ? (
            <p className="sr-ov-text">和 A 完全一样，只是另写一份。两份之间的差就是噪声。</p>
          ) : (
            <SrRatchetArmFields
              side="b"
              arm={form.b}
              models={overview.models}
              windowsMax={overview.windows_max}
              changed={srRatchetArmDiff(form)}
              onChange={(b) => setForm({ ...form, b })}
            />
          )}
        </section>
      </div>

      <div className="sr-ratchet-field">
        <span className="label">留出几段原文</span>
        <Segmented
          size="sm"
          label="留出几段原文"
          value={Number(form.passages)}
          options={(overview.passage_choices || [3, 4, 5, 6]).map((n) => ({ value: n, label: `${n} 段` }))}
          onChange={(n) => setForm({ ...form, passages: n })}
        />
        <span className="sr-ratchet-muted">段越多越稳，也越费；同样的段数每次留出的是同一组原文，写过的稿子不再写。</span>
      </div>

      {same && <Notice tone="warn" testId="sr-ratchet-same">两套配置一模一样：改一下 B，或者选「量噪声」。</Notice>}
      {gate && (
        <Notice tone="warn" testId="sr-ratchet-no-llm" actions={onAction ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => onAction(gate.action)}>{gate.action.label}</button> : null}>
          {gate.text}
        </Notice>
      )}

      {!same && (
        <div className="sr-ratchet-stats" data-testid="sr-ratchet-preview" role="status" aria-live="polite">
          {preview.phase === "error" ? <SrErrorLine error={preview.error} onAction={onAction} />
            : stats.length ? stats.map((s) => <StatTile key={s.key} label={s.label} value={s.value} unit={s.unit} hint={s.hint} testId={`sr-ratchet-preview-${s.key}`} />)
              : <span className="sr-ratchet-muted"><Spinner size={12} /> 正在估算要调用几次模型…</span>}
        </div>
      )}

      <div className="sr-check-foot">
        <span className="sr-check-hint">
          {preview.phase === "ready" && preview.data && !preview.data.calls
            ? "都写过了：不调用模型，直接出结论。"
            : "跑完在下面「做过的对比」里打开，先盲读，再看结论。"}
        </span>
        <button
          type="button"
          className="btn btn-accent"
          data-testid="sr-ratchet-start"
          disabled={same || starting || !!gate || preview.phase === "error"}
          onClick={start}
        >
          <I.Play size={13} /> {form.aa ? "开始量噪声" : "开始对比"}
        </button>
      </div>
    </div>
  );
}

function SrRatchetHistory({ runs, onOpen }) {
  if (!runs.length) return null;
  return (
    <div className="card" data-testid="sr-ratchet-history">
      <div className="card-head">
        <div>
          <div className="card-title">做过的对比</div>
          <div className="card-sub">打开一次，先盲读再揭晓；揭晓之前这里不显示结论。</div>
        </div>
      </div>
      <ul className="sr-ratchet-runs">
        {runs.map((run) => {
          const tag = srRatchetRunTag(run);
          const arms = run.arms || {};
          const openable = run.status === "succeeded" || run.status === "failed";
          const detail = arms.A ? `A：${arms.A.describe}\nB：${arms.B.describe}` : undefined;
          return (
            <li key={run.job_id}>
              <button type="button" className="sr-ratchet-run" disabled={!openable} onClick={() => onOpen(run.job_id)} title={detail} data-testid="sr-ratchet-run">
                <span className="sr-ratchet-run-main">
                  <span className="sr-ratchet-run-title">{arms.A ? srRatchetRunTitle(arms) : (run.error && run.error.message) || "配置对比"}</span>
                  <span className="sr-ratchet-run-meta">
                    {srFormatWhen(run.finished_at || run.created_at)}{run.passages ? ` · ${run.passages} 段` : ""}
                  </span>
                </span>
                <Tag tone={tag.tone} dot>{tag.label}</Tag>
                {openable && <I.ChevronRight size={14} className="sr-ratchet-run-go" aria-hidden="true" />}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ---------- 一次比较：盲读、揭晓、数字 ---------- */

function SrRatchetRun({ jobId, profileId, onBack }) {
  const entry = srRatchetRun(jobId);
  const [index, setIndex] = React.useState(0);
  const [saving, setSaving] = React.useState(false);
  React.useEffect(() => { srLoadRatchetRun(jobId, { force: true }); }, [jobId]);
  const back = (
    <button type="button" className="btn btn-quiet btn-sm sr-ratchet-back" onClick={onBack} data-testid="sr-ratchet-back">
      <I.ChevronLeft size={13} /> 全部对比
    </button>
  );
  const data = entry && entry.data;
  if (!data) {
    return (
      <div className="sr-ratchet">
        {back}
        {entry && entry.phase === "error"
          ? <div className="card"><SrErrorLine error={entry.error} /></div>
          : <div className="card sr-stage-loading-inline"><Spinner size={14} label="正在读取这次对比" /> 正在读取这次对比…</div>}
      </div>
    );
  }
  const job = data.job || {};
  if (job.status !== "succeeded") {
    return (
      <div className="sr-ratchet" data-testid="sr-ratchet-run">
        {back}
        <Notice tone={job.status === "failed" ? "danger" : "info"} testId="sr-ratchet-run-unfinished">
          {job.status === "failed" ? `这次对比没有完成：${(job.error && job.error.message) || "原因不明"}。重新开一次，写过的稿子不会重写。`
            : job.status === "cancelled" ? "这次对比取消了。" : "还在跑：跑完了再来读。"}
        </Notice>
      </div>
    );
  }
  const reading = data.reading || [];
  const votes = data.votes || {};
  const revealed = !!data.revealed;
  const summary = data.summary || {};
  const arms = summary.arms || {};
  const done = srRatchetVotesDone(reading, votes);
  const item = reading[Math.min(index, reading.length - 1)] || null;

  const save = async (next, { reveal = false } = {}) => {
    setSaving(true);
    try { await srSaveRatchetVotes(jobId, next, { reveal, profileId }); }
    catch (err) { srNotifyError(err, "没有保存，请再选一次。"); }
    finally { setSaving(false); }
  };
  const vote = (kind, label) => {
    if (!item || revealed) return;
    const key = String(item.index);
    const nextVote = srRatchetToggleVote(votes[key], kind, label);
    const next = { ...votes };
    if (nextVote.best || nextVote.worst) next[key] = nextVote; else delete next[key];
    save(next);
    /* 这一段两项都选好了：自动翻到下一段没读完的 */
    if (srRatchetVoteDone(nextVote)) {
      const after = reading.findIndex((r, i) => i > index && !srRatchetVoteDone(next[String(r.index)]));
      if (after >= 0) setIndex(after);
    }
  };
  const revealNow = async () => {
    if (done < reading.length) {
      const ok = await wsConfirm({
        title: "不读完就看结论？",
        body: `还有 ${reading.length - done} 段没选。揭晓之后答案和数字都摆出来，盲读的选择就不能再改了。`,
        confirmLabel: "揭晓",
        cancelLabel: "接着读",
      });
      if (!ok) return;
    }
    save(votes, { reveal: true });
  };

  return (
    <div className="sr-ratchet" data-testid="sr-ratchet-run">
      {back}
      <div className="card">
        <div className="card-head">
          <div>
            <div className="card-title">{srRatchetRunTitle(arms)}</div>
            <div className="card-sub">{srFormatWhen(job.finished_at)} · {reading.length} 段 · {srRatchetUsageText(summary.usage)}</div>
          </div>
          {revealed ? <Tag tone="ok" dot>已揭晓</Tag> : <Tag tone="info" dot testId="sr-ratchet-progress">已读 {done}/{reading.length}</Tag>}
        </div>
        {arms.A && (
          <dl className="sr-ratchet-armlist">
            <dt>A</dt><dd>{arms.A.describe}</dd>
            <dt>B</dt><dd>{arms.B.describe}</dd>
          </dl>
        )}
      </div>

      {revealed ? <SrRatchetVerdict summary={summary} entry={data.entry} /> : (
        <Notice tone="info" testId="sr-ratchet-blind-hint" title="先盲读，再看数字">
          每段三份文字是同一段情节：一份原文，A 和 B 各一份，顺序打乱。每段选出最像原作者的和最不像的；数字会影响判断，揭晓之前不显示。
        </Notice>
      )}

      {item && (
        <SrRatchetPassage
          item={item}
          index={index}
          reading={reading}
          votes={votes}
          revealed={revealed}
          saving={saving}
          onIndex={setIndex}
          onVote={vote}
        />
      )}

      {!revealed && (
        <div className="card sr-check-foot sr-ratchet-reveal">
          <span className="sr-check-hint">
            {done < reading.length ? `还有 ${reading.length - done} 段没选完。` : "都读完了：揭晓看看答案和数字。"}
          </span>
          <button type="button" className={`btn ${done < reading.length ? "btn-ghost" : "btn-accent"}`} disabled={saving} onClick={revealNow} data-testid="sr-ratchet-reveal">
            <I.Eye size={13} /> {done < reading.length ? "不读了，直接揭晓" : "揭晓"}
          </button>
        </div>
      )}

      {revealed && <SrRatchetRows summary={summary} reading={reading} votes={votes} />}
    </div>
  );
}

function SrRatchetVerdict({ summary, entry }) {
  const verdict = srRatchetVerdictView(summary);
  const stats = srRatchetVerdictStats(summary);
  const readingView = srRatchetReadingView(entry && entry.reading, { sameConfig: !!summary.same_config });
  const agreement = srRatchetAgreement(summary, entry && entry.reading);
  return (
    <div className="card" data-testid="sr-ratchet-verdict">
      <div className="card-head">
        <div>
          <div className="card-title">{verdict ? verdict.title : "结论"}</div>
          {verdict && <div className="card-sub">{verdict.text}</div>}
        </div>
      </div>
      <div className="sr-ratchet-stats">
        {stats.map((s) => <StatTile key={s.key} label={s.label} value={s.value} unit={s.unit} hint={s.hint} tone={s.tone} />)}
      </div>
      {readingView && <p className="sr-ov-text sr-ratchet-readout" data-testid="sr-ratchet-readout">{readingView.text}</p>}
      {agreement && <Notice tone={agreement.tone} testId="sr-ratchet-agreement">{agreement.text}</Notice>}
    </div>
  );
}

function SrRatchetPassage({ item, index, reading, votes, revealed, saving, onIndex, onVote }) {
  const vote = votes[String(item.index)] || {};
  const key = item.key || null;
  const tabs = reading.map((r, i) => ({
    id: i,
    label: `第 ${r.index} 段`,
    icon: srRatchetVoteDone(votes[String(r.index)]) ? <I.Check size={12} /> : null,
  }));
  return (
    <section className="card sr-ratchet-passage" aria-label={`第 ${item.index} 段`} data-testid="sr-ratchet-passage">
      <Tabs label="段落" value={index} tabs={tabs} onChange={onIndex} />
      <div className="sr-ratchet-passage-meta">
        <span className="sr-ratchet-muted">参考书第 {item.chapter_no ?? "?"} 章 · 原文约 {item.chars} 字</span>
        <details className="sr-ratchet-beats">
          <summary>情节要点（两份仿写都照这个写）</summary>
          <ol>{(item.beats || []).map((beat, i) => <li key={i}>{beat}</li>)}</ol>
        </details>
      </div>
      <div className="sr-ratchet-texts">
        {(item.texts || []).map((t) => {
          const source = key ? key[t.label] : null;
          const picked = vote.best === t.label ? "best" : vote.worst === t.label ? "worst" : null;
          return (
            <article key={t.label} className={`sr-ratchet-text${picked ? ` is-${picked}` : ""}`} data-label={t.label}>
              <header className="sr-ratchet-text-head">
                <span className="sr-ratchet-text-label">{t.label}</span>
                {source && <Tag tone={source === "original" ? "ok" : "neutral"} testId="sr-ratchet-source">{srRatchetSourceLabel(source)}</Tag>}
                <Segmented
                  size="sm"
                  className="sr-ratchet-vote"
                  label={`${t.label}：像不像原作者`}
                  value={picked}
                  onChange={(kind) => onVote(kind, t.label)}
                  options={[
                    { value: "best", label: "最像", testId: "sr-ratchet-best", disabled: revealed || saving },
                    { value: "worst", label: "最不像", testId: "sr-ratchet-worst", disabled: revealed || saving },
                  ]}
                />
              </header>
              <div className="sr-ratchet-prose text-serif">
                {String(t.text || "").split(/\n+/).filter(Boolean).map((para, i) => <p key={i}>{para}</p>)}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function SrRatchetRows({ summary, reading, votes }) {
  const rows = summary.rows || [];
  if (!rows.length) return null;
  const byWindow = new Map(reading.map((r) => [r.window_no, r]));
  const fmt = (n) => Number(n || 0).toFixed(3);
  return (
    <div className="card" data-testid="sr-ratchet-rows">
      <div className="card-head">
        <div>
          <div className="card-title">逐段数字</div>
          <div className="card-sub">「离原文」越小越像：比的是句长、标点、对白、节奏这些测得出的习惯，按这本书的分布加权。</div>
        </div>
      </div>
      <div className="fid-table-scroll" role="region" aria-label="逐段数字" tabIndex={0}>
        <table className="fid-table">
          <thead>
            <tr>
              <th scope="col">段</th><th scope="col">原文</th><th scope="col">A 离原文</th><th scope="col">B 离原文</th>
              <th scope="col">更近</th><th scope="col">你选的最像</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const item = byWindow.get(row.window_no);
              const vote = item ? votes[String(item.index)] : null;
              const source = vote && vote.best && item && item.key ? item.key[vote.best] : null;
              const nearer = row.b.to_original < row.a.to_original ? "B" : row.a.to_original < row.b.to_original ? "A" : null;
              return (
                <tr key={row.window_no}>
                  <td className="tab-num">{item ? item.index : "?"}</td>
                  <td className="tab-num">第 {row.chapter_no ?? "?"} 章 · {row.chars} 字</td>
                  <td className="tab-num">{fmt(row.a.to_original)} <span className="sr-ratchet-muted">{row.a.chars} 字</span></td>
                  <td className="tab-num">{fmt(row.b.to_original)} <span className="sr-ratchet-muted">{row.b.chars} 字</span></td>
                  <td>{nearer ? <Tag tone={nearer === "B" ? "ok" : "warn"}>{nearer}</Tag> : "—"}</td>
                  <td>{source ? <Tag tone={source === "original" ? "ok" : "neutral"}>{srRatchetSourceLabel(source)}</Tag> : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
