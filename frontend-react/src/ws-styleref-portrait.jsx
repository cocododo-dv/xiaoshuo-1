import React from "react";
import { I } from "./icons.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { EmptyState, Notice, Segmented, Spinner, Tag } from "./ws-ui.jsx";
import { styleDimensionLabel } from "./ws-labels.js";
import { SR_DIMENSION_STATES, srDimensionGroups, srFormatWhen, srNormalizeConfig } from "./ws-styleref-model.js";
import {
  srAddBannedTerm, srLoadBannedTerms, srLoadProfile, srLoadProjectBinding, srProfileDetail, srProjectBinding,
  srRemoveBannedTerm, srSetCardLineState, srSetDimensionState,
} from "./ws-styleref-store.js";
import { SrErrorLine, srActiveWork, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";
import { SrDimensionFidelityBody, SrDimensionFidelityChip, SrWorkFidelityCard, useSrWorkFidelity } from "./ws-styleref-fidelity.jsx";

/* ==========================================================
   风格参考 · 文风画像（学习文风的产物；取代旧的维度矩阵 / 风格画像页）
   · 气质在最前（每场都要体现）；
   · 16 维按层分组，每维：名字、一句概括、「通用写法」对「这位作者」、这位作者的写法与「作者不这么写」，
     每句都能 ✓（总带上）/ ✗（不用这句）——改了立即生效，不重新学习、画像不失效；展开看原话（依据）与手法；
   · 每维一个「重点 / 正常 / 不学」：给当前作品设的，写在它的应用上（这本书还没用于当前作品时锁住并说明）；
   · 这本书正用于当前作品时：顶上一张「像不像」卡（终稿几场在作者范围内、近期常见偏差、走势），每一维右边是作品在
     这一维的平均分（测得 / 评审）与「近期常见偏差」（ws-styleref-fidelity.jsx）；
   · 声音习惯、章与场的尺度、本书专名与禁用词。
   ========================================================== */

const SR_LINE_STATE_META = {
  pinned: { label: "总带上", icon: "Check" },
  excluded: { label: "不用这句", icon: "X" },
};

export function SrPortrait({ book, go, onAction }) {
  useSrStore("detail", "books");
  const profileId = book.profile ? book.profile.profile_id : null;
  const work = srActiveWork();
  const workId = work ? work.id : null;
  React.useEffect(() => { if (profileId) srLoadProfile(profileId); }, [profileId]);
  React.useEffect(() => { if (workId) srLoadProjectBinding(workId); }, [workId]);
  const entry = profileId ? srProfileDetail(profileId) : null;
  const profile = entry && entry.data;
  const bindingEntry = workId ? srProjectBinding(workId) : null;
  const binding = bindingEntry && bindingEntry.data && bindingEntry.data.binding;
  const appliedBinding = binding && binding.profile_id === profileId && binding.binding_id ? binding : null;
  const workFid = useSrWorkFidelity(appliedBinding ? workId : null, profileId);

  if (!profileId) {
    return (
      <div className="card sr-portrait-empty" data-testid="sr-portrait-empty">
        <EmptyState icon="Sparkles" title="还没有文风画像" compact>学完之后，这里列出这位作者在 16 个维度上和通用写法不一样的地方。</EmptyState>
      </div>
    );
  }
  if (!profile) {
    if (entry && entry.phase === "error") {
      return (
        <div className="card">
          <SrErrorLine error={entry.error} onAction={onAction} />
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => srLoadProfile(profileId, { force: true })}><I.Refresh size={13} /> 重试</button>
        </div>
      );
    }
    return <div className="card sr-stage-loading-inline"><Spinner size={14} label="正在读取文风画像" /> 正在读取文风画像…</div>;
  }

  const states = appliedBinding ? srNormalizeConfig(appliedBinding.config).dimension_states : null;
  const setDimension = async (dimension, state) => {
    if (!appliedBinding) return;
    try { await srSetDimensionState(workId, appliedBinding.binding_id, dimension, state); }
    catch (e) { srNotifyError(e, "没有保存，请稍后重试。"); }
  };
  const setLine = async (lineId, state) => {
    try { await srSetCardLineState(profileId, lineId, state); }
    catch (e) { srNotifyError(e, "没有保存，请稍后重试。"); }
  };
  const groups = srDimensionGroups(profile.dimensions);
  const evidenceTotal = (profile.dimensions || []).reduce((sum, d) => sum + Number(d.quote_count || d.evidence_count || 0), 0);
  const lineTotal = (profile.dimensions || []).reduce((sum, d) => sum + (d.lines || []).length, 0);

  return (
    <div className="sr-portrait" data-testid="sr-portrait">
      <div className="card sr-portrait-head">
        <div className="card-head">
          <div>
            <div className="card-title">文风画像</div>
            <div className="card-sub">
              {[
                profile.learned_from && profile.learned_from.learned_at ? `学于 ${srFormatWhen(profile.learned_from.learned_at)}` : null,
                profile.version_tag,
                profile.has_card ? `文风卡 ${lineTotal} 句` : null,
                profile.has_card ? `依据 ${evidenceTotal} 条原话` : null,
              ].filter(Boolean).join(" · ")}
            </div>
          </div>
          {profile.needs_relearn && <Tag tone="warn" dot>建议重新学习</Tag>}
        </div>
        {profile.has_card && (
          <p className="sr-ov-hint">「总带上」= 这句每场都带上；「不用这句」= 以后不再用。改了立即生效，不用重新学习。</p>
        )}
      </div>

      {!profile.has_card ? (
        <SrLegacyPortrait profile={profile} />
      ) : (
        <>
          {profile.temperament && profile.temperament.length > 0 && (
            <div className="card sr-temperament" data-testid="sr-temperament">
              <div className="sr-temperament-label">气质 · 每场都要体现</div>
              <ul>{profile.temperament.map((line, i) => <li key={i} className="text-serif">{line}</li>)}</ul>
              {profile.qualitative_summary && <p className="sr-temperament-summary">{profile.qualitative_summary}</p>}
            </div>
          )}

          {appliedBinding && workFid.data && (
            <SrWorkFidelityCard data={workFid.data} workId={workId} workTitle={work.title || "当前作品"} go={go} />
          )}

          {appliedBinding ? (
            <p className="sr-portrait-states-hint" data-testid="sr-states-hint">
              每一维右边的「重点 / 正常 / 不学」是给《{work.title || "当前作品"}》设的，写在它的应用上。
            </p>
          ) : (
            <Notice
              tone="info"
              testId="sr-states-locked"
              actions={go ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => go("apply")}>去用于作品</button> : null}
            >
              {workId ? `这本书还没用于《${work.title || "当前作品"}》：用上之后可以给每一维设「重点 / 不学」。` : "打开一部作品并用上这本书之后，可以给每一维设「重点 / 不学」。"}
            </Notice>
          )}

          {groups.map((group) => (
            <section key={group.layer} className="sr-dim-group" aria-label={`${group.label}层`}>
              <h3 className="sr-dim-group-title">{group.label}层</h3>
              <ul className="sr-dim-list">
                {group.dims.map((dim) => (
                  <SrDimensionRow
                    key={dim.dimension}
                    dim={dim}
                    state={states ? states[dim.dimension] || "normal" : null}
                    onSetState={states ? (state) => setDimension(dim.dimension, state) : null}
                    onLineState={setLine}
                    fidelity={workFid.averages[dim.dimension] || null}
                    gaps={workFid.gapsByDim[dim.dimension] || null}
                    workTitle={(work && work.title) || "当前作品"}
                  />
                ))}
              </ul>
            </section>
          ))}
        </>
      )}

      <SrVoiceAndStructure profile={profile} />
      <SrBannedTerms profile={profile} />
    </div>
  );
}

/* 一维：标题行（名字、概括、计数、作品在这一维的平均分、状态选择、展开）+ 展开后的对照、各句、手法、作品在这一维 */
function SrDimensionRow({ dim, state, onSetState, onLineState, fidelity = null, gaps = null, workTitle = "当前作品" }) {
  const [open, setOpen] = React.useState(false);
  const bodyId = React.useId();
  const doLines = (dim.lines || []).filter((l) => l.kind !== "avoid");
  const avoidLines = (dim.lines || []).filter((l) => l.kind === "avoid");
  const quotes = Number(dim.quote_count || dim.evidence_count || 0);
  const empty = !(dim.lines || []).length;
  const name = dim.label || styleDimensionLabel(dim.dimension);
  return (
    <li className={`sr-dim${open ? " is-open" : ""}${state === "exclude" ? " is-excluded" : ""}${empty ? " is-empty" : ""}`} data-dimension={dim.dimension}>
      <div className="sr-dim-head">
        <button type="button" className="sr-dim-toggle" aria-expanded={open} aria-controls={open ? bodyId : undefined} onClick={() => setOpen((o) => !o)}>
          {open ? <I.ChevronDown size={14} /> : <I.ChevronRight size={14} />}
          <span className="sr-dim-name">{name}</span>
          {state === "emphasize" && <Tag tone="accent">重点</Tag>}
          <span className="sr-dim-summary">{dim.summary || (empty ? "这一维没学出可靠的写法" : doLines[0] && doLines[0].text)}</span>
          <span className="sr-dim-counts tab-num">
            {empty ? "—" : `${doLines.length} 句${avoidLines.length ? ` · ${avoidLines.length} 句不这么写` : ""}${quotes ? ` · 原话 ${quotes}` : ""}`}
          </span>
        </button>
        <SrDimensionFidelityChip fidelity={fidelity} gaps={gaps} />
        {onSetState && (
          <Segmented
            label={`${name}：给当前作品设`}
            size="sm"
            className="sr-dim-state"
            value={state}
            onChange={onSetState}
            options={SR_DIMENSION_STATES.map((s) => ({ value: s.id, label: s.label, title: s.detail, testId: `sr-dim-state-${dim.dimension}-${s.id}` }))}
          />
        )}
      </div>
      {open && (
        <div className="sr-dim-body" id={bodyId}>
          {empty ? (
            <p className="sr-ov-text sr-ov-muted">这一维没学出有依据的写法：起草时这一维靠原文样例带。</p>
          ) : (
            <>
              {dim.model_default && (
                <div className="sr-dim-contrast">
                  <span className="sr-dim-contrast-label">通用写法</span>
                  <span className="sr-dim-contrast-text">{dim.model_default}</span>
                </div>
              )}
              {doLines.length > 0 && (
                <div className="sr-dim-section">
                  <div className="sr-dim-section-label">这位作者</div>
                  <ul className="sr-lines">{doLines.map((line) => <SrCardLine key={line.line_id} line={line} onState={onLineState} />)}</ul>
                </div>
              )}
              {avoidLines.length > 0 && (
                <div className="sr-dim-section">
                  <div className="sr-dim-section-label">作者不这么写</div>
                  <ul className="sr-lines">{avoidLines.map((line) => <SrCardLine key={line.line_id} line={line} onState={onLineState} />)}</ul>
                </div>
              )}
              {dim.devices && dim.devices.length > 0 && (
                <div className="sr-dim-devices"><span className="sr-dim-section-label">手法</span>{dim.devices.map((d) => <Tag key={d}>{d}</Tag>)}</div>
              )}
            </>
          )}
          <SrDimensionFidelityBody fidelity={fidelity} gaps={gaps} workTitle={workTitle} />
        </div>
      )}
    </li>
  );
}

/* 一句：正文、「必须体现」、✓ / ✗（再点一次取消）、原话 */
function SrCardLine({ line, onState }) {
  const [showQuotes, setShowQuotes] = React.useState(false);
  const quotesId = React.useId();
  const toggle = (state) => onState(line.line_id, line.state === state ? null : state);
  return (
    <li className={`sr-line${line.state ? ` is-${line.state}` : ""}`} data-line-id={line.line_id}>
      <div className="sr-line-main">
        <span className="sr-line-text text-serif">{line.text}</span>
        {line.mandatory && <Tag tone="accent" outline title="起草提示里标着「必须」，每场都要体现">必须体现</Tag>}
      </div>
      <div className="sr-line-actions">
        {["pinned", "excluded"].map((state) => {
          const meta = SR_LINE_STATE_META[state];
          const Ic = I[meta.icon];
          return (
            <button
              key={state}
              type="button"
              className={`sr-line-btn is-${state}${line.state === state ? " is-on" : ""}`}
              aria-pressed={line.state === state}
              data-testid={`sr-line-${state}`}
              title={line.state === state ? `取消「${meta.label}」` : meta.label}
              onClick={() => toggle(state)}
            >
              <Ic size={12} /> {meta.label}
            </button>
          );
        })}
        {line.evidence_count > 0 && (
          <button type="button" className="sr-line-quotes-toggle" aria-expanded={showQuotes} aria-controls={showQuotes ? quotesId : undefined} onClick={() => setShowQuotes((v) => !v)}>
            原话 {line.evidence_count}
          </button>
        )}
      </div>
      {showQuotes && (
        <ul className="sr-quotes" id={quotesId}>
          {(line.evidence || []).map((ev) => (
            <li key={ev.quote_id} className="sr-quote">
              {ev.paragraph_index != null && <span className="sr-quote-where tab-num">第 {Number(ev.paragraph_index) + 1} 段</span>}
              <q className="text-serif">{ev.text}</q>
            </li>
          ))}
          {line.evidence_count > (line.evidence || []).length && (
            <li className="sr-quote-more">另有 {line.evidence_count - (line.evidence || []).length} 条</li>
          )}
        </ul>
      )}
    </li>
  );
}

/* 旧版画像：没有文风卡，起草时读的是这些句子（含数字的整句不带） */
function SrLegacyPortrait({ profile }) {
  const legacy = profile.legacy || {};
  return (
    <div className="card" data-testid="sr-portrait-legacy">
      <Notice tone="warn" title="这是旧版画像">还没有按 16 个维度写的文风卡：点上面的「重新学习」换成文风卡。换之前，起草时照旧读下面这些句子。</Notice>
      {legacy.summary && <p className="sr-temperament-summary text-serif">{legacy.summary}</p>}
      {(legacy.groups || []).map((group) => (
        <div key={group.key} className="sr-dim-section">
          <div className="sr-dim-section-label">{group.label}</div>
          <ul className="sr-lines">
            {group.lines.map((line, i) => (
              <li key={i} className={`sr-line${line.dropped_in_drafting ? " is-dropped" : ""}`}>
                <div className="sr-line-main">
                  <span className="sr-line-text text-serif">{line.text}</span>
                  {line.dropped_in_drafting && <Tag tone="neutral" title="含数字的句子起草时整句不带">起草时不带</Tag>}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function SrVoiceAndStructure({ profile }) {
  const habits = (profile.voice && profile.voice.habits) || [];
  const structure = (profile.structure && profile.structure.lines) || [];
  if (!habits.length && !structure.length) return null;
  return (
    <div className="sr-ov-grid">
      {habits.length > 0 && (
        <div className="card" data-testid="sr-voice">
          <div className="card-head"><div><div className="card-title">声音习惯</div><div className="card-sub">从全书测出来的句子习惯（和这位作者自己的常态比）</div></div></div>
          <ul className="sr-plain-list">{habits.map((h, i) => <li key={i}>{h}</li>)}</ul>
        </div>
      )}
      {structure.length > 0 && (
        <div className="card" data-testid="sr-structure">
          <div className="card-head"><div><div className="card-title">章与场的尺度</div><div className="card-sub">规划章节与场景时参考</div></div></div>
          <ul className="sr-plain-list">{structure.map((line, i) => <li key={i}>{line}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

/* 本书专名（学习时识别，起草时不许照搬）与作者自己加的禁用词 */
function SrBannedTerms({ profile }) {
  const [terms, setTerms] = React.useState(null);
  const [input, setInput] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [showProtected, setShowProtected] = React.useState(false);
  const profileId = profile.profile_id;
  const latest = React.useRef(0);
  const load = React.useCallback(async () => {
    const ticket = ++latest.current;
    let list;
    try { list = await srLoadBannedTerms(profileId); } catch (e) { list = []; }
    if (ticket === latest.current) setTerms(list);
  }, [profileId]);
  React.useEffect(() => { load(); }, [load]);
  const protectedTerms = (terms || []).filter((t) => t.source === "protected_auto");
  const own = (terms || []).filter((t) => t.source !== "protected_auto");
  const add = async () => {
    const term = input.trim();
    if (!term || busy) return;
    setBusy(true);
    try { await srAddBannedTerm(profileId, term); setInput(""); await load(); }
    catch (e) { srNotifyError(e, "没有加上，请稍后重试。"); }
    finally { setBusy(false); }
  };
  const remove = async (termId) => {
    if (busy) return;
    setBusy(true);
    try { await srRemoveBannedTerm(termId); await load(); }
    catch (e) { srNotifyError(e, "没有删掉，请稍后重试。"); }
    finally { setBusy(false); }
  };
  return (
    <div className="card" data-testid="sr-banned">
      <div className="card-head"><div><div className="card-title">起草时不许出现的词</div><div className="card-sub">本书专名（人名、地名、组织……）学习时自动识别，起草时一律不许照搬；也可以自己加</div></div></div>
      {terms == null ? (
        <p className="sr-ov-text sr-ov-muted">正在读取…</p>
      ) : (
        <>
          <p className="sr-ov-text">
            本书专名 <b className="tab-num">{protectedTerms.length}</b> 个
            {protectedTerms.length > 0 && (
              <button type="button" className="btn btn-quiet btn-xs" aria-expanded={showProtected} onClick={() => setShowProtected((v) => !v)}>{showProtected ? "收起" : "看看"}</button>
            )}
          </p>
          {showProtected && <div className="sr-term-chips">{protectedTerms.map((t) => <Tag key={t.term_id}>{t.term}</Tag>)}</div>}
          <ul className="sr-banned-list">
            {own.map((t) => (
              <li key={t.term_id} className="sr-banned-item">
                <span className="sr-banned-term text-serif">{t.term}</span>
                {t.scope === "extraction" && <span className="sr-banned-hint">学习时跳过含这个词的段落</span>}
                {t.source === "preset" ? <span className="sr-banned-preset">预置</span> : (
                  <button type="button" className="btn btn-quiet btn-sm btn-icon" aria-label={`删除禁用词「${t.term}」`} title="删除" disabled={busy} onClick={() => remove(t.term_id)}><I.X size={13} /></button>
                )}
              </li>
            ))}
          </ul>
          <div className="sr-banned-add">
            <input
              className="input"
              placeholder="再加一个词…"
              aria-label="新的禁用词"
              value={input}
              disabled={busy}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !isImeComposing(e)) add(); }}
            />
            <button type="button" className="btn btn-ghost btn-sm" disabled={busy || !input.trim()} onClick={add}><I.Plus size={13} /> 加上</button>
          </div>
        </>
      )}
    </div>
  );
}
