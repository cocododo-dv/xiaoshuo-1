import { wrRangeForOffsets, wrRangeForText } from "./ws-writer-manuscript.js";
import { MANUSCRIPT_BLOCK_SELECTOR, unwrapNode } from "./manuscript-html.js";

/* ==========================================================
   深改诊断在编辑器里的标注（2026-09-29 从 ws-deep.jsx 拆出）
   ----------------------------------------------------------
   wrDeepMark / wrDeepUnmark：按发现的段落序号 + 证据文字给句子包 <mark class="wr-dx">（整段就给段落
   加 .wr-dx-para）；wrDxRangeFor：一条发现在编辑器里的 Range。纯 DOM，不读 store、不写 window。
   ========================================================== */

/* 类名写全，设计守卫按字面找引用 */
const WR_DX_SEV_CLASS = { blocking: "sev-blocking", revision: "sev-revision", taste: "sev-taste", info: "sev-info" };
const sevClass = (sev) => WR_DX_SEV_CLASS[sev] || WR_DX_SEV_CLASS.info;

/* ---- 编辑器内标注 ---- */
function wrDeepUnmark(el) {
  if (!el) return;
  el.querySelectorAll("mark.wr-dx").forEach((mk) => {
    const parent = mk.parentNode;
    unwrapNode(mk);
    parent.normalize();
  });
  el.querySelectorAll(".wr-dx-para").forEach((p) => {
    p.classList.remove("wr-dx-para", "is-active");
    Object.values(WR_DX_SEV_CLASS).forEach((name) => p.classList.remove(name));
    p.removeAttribute("data-dx");
  });
}

/* 一条发现在编辑器里的 Range：先按段落序号 + 偏移，再按证据文字在那一段里找，最后全文找
   （作者在前面加了段、改了几个字，标注仍落在那句话上）。返回 { block, range }；找不到给 null。 */
function wrDxRangeFor(el, finding) {
  const ev = finding && finding.evidence;
  if (!el || !ev) return null;
  const blocks = Array.from(el.querySelectorAll(MANUSCRIPT_BLOCK_SELECTOR));
  let block = Number.isInteger(ev.paragraph_index) ? blocks[ev.paragraph_index] : null;
  let range = null;
  if (block) {
    if (Number.isFinite(ev.start) && Number.isFinite(ev.end) && ev.end > ev.start) {
      range = wrRangeForOffsets(block, ev.start, ev.end);
      if (range && ev.excerpt && range.toString() !== ev.excerpt) range = null;
    }
    if (!range && ev.excerpt) range = wrRangeForText(block, ev.excerpt);
  }
  if (!range && ev.excerpt) {
    for (const candidate of blocks) {
      const hit = wrRangeForText(candidate, ev.excerpt);
      if (hit) { block = candidate; range = hit; break; }
    }
  }
  return range ? { block, range } : null;
}

function wrDeepMark(el, findings, activeKey) {
  if (!el) return;
  wrDeepUnmark(el);
  /* 选中的跨段发现：另一段的那句也标出来（同一条 data-dx，淡一层） */
  const activeFinding = (findings || []).find((f) => f.signal_id === activeKey && !f.ignored);
  if (activeFinding && activeFinding.related && activeFinding.related.excerpt && !activeFinding.related.stale) {
    const hit = wrDxRangeFor(el, { evidence: activeFinding.related });
    if (hit && hit.range.toString().trim() !== (hit.block.textContent || "").trim()) {
      try {
        const mk = document.createElement("mark");
        mk.className = "wr-dx is-related";
        mk.setAttribute("data-dx", activeFinding.signal_id);
        mk.appendChild(hit.range.extractContents());
        hit.range.insertNode(mk);
      } catch (e) { /* 标不上就只标焦点 */ }
    }
  }
  (findings || []).forEach((f) => {
    if (f.ignored || !f.evidence) return;
    const hit = wrDxRangeFor(el, f);
    if (!hit) return;
    const { block, range } = hit;
    const active = f.signal_id === activeKey;
    const markParagraph = () => {
      block.classList.add("wr-dx-para", sevClass(f.severity));
      if (active) block.classList.add("is-active");
      block.setAttribute("data-dx", f.signal_id);
    };
    const whole = range.toString().trim() === (block.textContent || "").trim();
    if (whole) { markParagraph(); return; }
    try {
      const mk = document.createElement("mark");
      mk.className = `wr-dx ${sevClass(f.severity)}${active ? " is-active" : ""}`;
      mk.setAttribute("data-dx", f.signal_id);
      /* extract + insert 比 surroundContents 稳：证据跨过实体高亮的 span 也能包起来 */
      mk.appendChild(range.extractContents());
      range.insertNode(mk);
    } catch (e) {
      markParagraph();
    }
  });
}

export { WR_DX_SEV_CLASS, sevClass, wrDeepMark, wrDeepUnmark, wrDxRangeFor };
