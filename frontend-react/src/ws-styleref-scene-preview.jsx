import React from "react";
import { I } from "./icons.jsx";
import { Notice, Spinner, Tag } from "./ws-ui.jsx";
import { paragraphTypeLabel, styleDimensionLabel, styleWindowSlotLabel, windowPositionLabel } from "./ws-labels.js";
import { SR_SEGMENTS_ONLY_LABEL, srFormatChars, srReferenceModeMeta, srSceneOptions } from "./ws-styleref-model.js";
import { srLoadParagraphs, srLoadWorkScenes, srScenePreview } from "./ws-styleref-store.js";
import { SrErrorLine } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 本场预览（取代旧的「示例预览」）：挑当前作品的一场，看它起草时会拿到什么——
   与起草同一套选窗、同一个块次序（后端 POST …/injection-preview，只读，不写冻结行）：
   · 样例窗：第几章 · 章首 / 章末 / 整章、字数、学习作业给它打的场面 / 情绪标签、最能示范的维度（中文名）与一句话梗概，
     可展开读原文；
   · 文风卡、声音习惯、红线三块的全文；
   · 各块多少字；书的原文范围压过设置时（起草不发原文）如实说实际带的是什么。
   用的是「用于作品」页上正在编辑的设置（还没保存也能先看）。
   ========================================================== */

const SR_NOTICE_TEXT = {
  STYLE_REFERENCE_SAMPLES_BLOCKED: "这本书的原文不能发给当前模型：这一场只带文风卡。",
  STYLE_REFERENCE_BOOK_CHANGED: "这本书在学完之后改过，样例窗是按现在的正文挑的；建议重新学习。",
  STYLE_REFERENCE_NO_WINDOWS: "这本书还没有样例窗口（先学习文风）。",
  STYLE_REFERENCE_BOOK_MISSING: "这份文风画像对应的参考书已经不在了。",
};

/* 这一场在章里的位置（挑样例时按它优先挑同位置的片段） */
const SR_SCENE_WHERE = {
  opening: "开章的一场",
  closing: "收章的一场",
  whole: "独占一章",
  middle: "章中的一场",
};

export function SrScenePreview({ book, profileId, config, workId, workTitle }) {
  const [chapters, setChapters] = React.useState(null);
  const [sceneId, setSceneId] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [preview, setPreview] = React.useState(null);
  const [previewKey, setPreviewKey] = React.useState(null);

  React.useEffect(() => {
    let alive = true;
    setChapters(null);
    setSceneId("");
    setPreview(null);
    if (!workId) return undefined;
    srLoadWorkScenes(workId)
      .then((list) => { if (alive) setChapters(list); })
      .catch(() => { if (alive) setChapters([]); });
    return () => { alive = false; };
  }, [workId]);

  const configKey = JSON.stringify([config.reference_mode, config.sample_windows, config.draft_mode, config.dimension_states]);
  const currentKey = `${profileId}|${sceneId}|${configKey}`;
  const outdated = !!preview && previewKey !== currentKey;
  const groups = srSceneOptions(chapters);

  const run = async () => {
    if (!sceneId || busy) return;
    setBusy(true); setError(null);
    try {
      const data = await srScenePreview(profileId, { sceneId, projectId: workId, config });
      setPreview(data);
      setPreviewKey(currentKey);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card sr-scene-preview" data-testid="sr-scene-preview">
      <div className="card-head">
        <div>
          <div className="card-title">本场预览</div>
          <div className="card-sub">挑《{workTitle}》的一场，看它起草时会拿到哪些样例窗和文风卡（按上面正在编辑的设置算，不用先保存）。</div>
        </div>
      </div>
      {chapters == null ? (
        <p className="sr-ov-text sr-ov-muted"><Spinner size={12} /> 正在读取《{workTitle}》的场景…</p>
      ) : !groups.length ? (
        <p className="sr-ov-text sr-ov-muted" data-testid="sr-scene-preview-empty">《{workTitle}》还没有场景：先在构思或章节编排里建场景。</p>
      ) : (
        <div className="sr-scene-pick">
          <label className="ws-sr-only" htmlFor="sr-scene-select">选一场</label>
          <select id="sr-scene-select" className="select" data-testid="sr-scene-select" value={sceneId} onChange={(e) => setSceneId(e.target.value)}>
            <option value="">选一场…</option>
            {groups.map((group) => (
              <optgroup key={group.label} label={group.label}>
                {group.scenes.map((scene) => <option key={scene.value} value={scene.value}>{scene.label}</option>)}
              </optgroup>
            ))}
          </select>
          <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-scene-preview-run" disabled={!sceneId || busy} onClick={run}>
            {busy ? <><Spinner size={12} /> 正在挑…</> : <><I.Eye size={13} /> 看这一场</>}
          </button>
        </div>
      )}
      <SrErrorLine error={error} testId="sr-scene-preview-error" />
      {preview && <SrScenePreviewResult book={book} preview={preview} outdated={outdated} />}
    </div>
  );
}

function SrScenePreviewResult({ book, preview, outdated }) {
  const sizes = preview.sizes || {};
  const scene = preview.scene || {};
  const where = SR_SCENE_WHERE[scene.position] || "章中的一场";
  const modeChanged = preview.requested_reference_mode && preview.reference_mode && preview.requested_reference_mode !== preview.reference_mode;
  const notices = (preview.notices || []).map((code) => SR_NOTICE_TEXT[code]).filter(Boolean);
  return (
    <div className="sr-scene-result" data-testid="sr-scene-preview-result">
      {outdated && <Notice tone="info">设置改过了：再点一次「看这一场」按新设置算。</Notice>}
      <dl className="sr-calib">
        <div><dt>这一场</dt><dd>{where}{(scene.situation_tags || []).length ? ` · ${scene.situation_tags.join("、")}` : ""}</dd></div>
        <div><dt>实际参考方式</dt><dd>{srReferenceModeMeta(preview.reference_mode).label}</dd></div>
        <div><dt>样例</dt><dd className="tab-num">{sizes.sample_windows || 0} 窗 · {srFormatChars(sizes.sample_chars || 0)}</dd></div>
        <div><dt>文风卡</dt><dd className="tab-num">{sizes.card_lines || 0} 句 · {srFormatChars(sizes.card_chars || 0)}</dd></div>
        <div className="is-wide"><dt>一共</dt><dd className="tab-num">约 {srFormatChars(sizes.total_chars || 0)}（样例在提示末尾，文风卡与声音在前面）</dd></div>
      </dl>
      {modeChanged && (
        <Notice tone="info" testId="sr-scene-preview-mode">这本书导入时选了「{SR_SEGMENTS_ONLY_LABEL}」：设置是「{srReferenceModeMeta(preview.requested_reference_mode).label}」，实际只带文风卡。</Notice>
      )}
      {notices.map((text) => <Notice key={text} tone="warn">{text}</Notice>)}
      {(preview.windows || []).length > 0 && (
        <ol className="sr-window-list" data-testid="sr-scene-windows">
          {preview.windows.map((w) => <SrWindowItem key={w.window_no} book={book} window={w} />)}
        </ol>
      )}
      <SrBlock label="文风卡" text={preview.blocks && preview.blocks.card} testId="sr-block-card" />
      <SrBlock label="声音习惯" text={preview.blocks && preview.blocks.voice} testId="sr-block-voice" />
      <SrBlock label="防照搬红线（固定带上）" text={preview.blocks && preview.blocks.red_line} testId="sr-block-red-line" />
    </div>
  );
}

function SrWindowItem({ book, window: w }) {
  const [open, setOpen] = React.useState(false);
  const [text, setText] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const position = windowPositionLabel(w.position);
  const tags = [...(w.situations || []), ...(w.moods || [])];
  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && text == null && !loading) {
      setLoading(true);
      try {
        const data = await srLoadParagraphs(book.id, w.start, w.end);
        setText(data.paragraphs.map((p) => p.text).join("\n"));
      } catch (e) {
        setText("");
      } finally {
        setLoading(false);
      }
    }
  };
  return (
    <li className="sr-window" data-window-no={w.window_no}>
      <button type="button" className="sr-window-head" aria-expanded={open} onClick={toggle}>
        <span className="sr-window-meta">
          {w.chapter ? `第 ${w.chapter} 章` : `片段 ${w.window_no}`}{position ? ` · ${position}` : ""}
        </span>
        <span className="sr-window-size tab-num">{w.paragraphs ? `${w.paragraphs} 段 · ` : ""}{Number(w.chars || 0).toLocaleString()} 字</span>
        {w.gist && <span className="sr-window-gist text-serif">{w.gist}</span>}
        <span className="sr-window-tags">
          {styleWindowSlotLabel(w.slot) && <Tag outline>{styleWindowSlotLabel(w.slot)}</Tag>}
          {tags.map((t) => <Tag key={t}>{t}</Tag>)}
          {(w.dimensions || []).map((d) => <Tag key={`d-${d}`} tone="accent" outline>{styleDimensionLabel(d)}</Tag>)}
          {w.paragraph_type && <Tag outline>{paragraphTypeLabel(w.paragraph_type)}为主</Tag>}
        </span>
      </button>
      {open && (
        <div className="sr-window-body">
          {loading ? <p className="sr-ov-text sr-ov-muted"><Spinner size={12} /> 正在读原文…</p>
            : text ? <p className="sr-window-text text-serif">{text}</p>
            : <p className="sr-ov-text sr-ov-muted">读不到这一段原文。</p>}
        </div>
      )}
    </li>
  );
}

function SrBlock({ label, text, testId }) {
  const body = String(text || "").trim();
  if (!body) return null;
  return (
    <details className="sr-block" data-testid={testId}>
      <summary><span className="sr-block-name">{label}</span><span className="sr-block-chars tab-num">{body.length.toLocaleString()} 字</span></summary>
      <pre className="sr-block-text">{body}</pre>
    </details>
  );
}
