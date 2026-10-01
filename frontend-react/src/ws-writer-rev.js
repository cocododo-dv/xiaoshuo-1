import { unwrapNode } from "./manuscript-html.js";
import { wrPositionAt } from "./ws-writer-manuscript.js";

/* ==========================================================
   写作台 · AI 改写换进正文与「还原原文」（重评 R12 / 批准 #20b，2026-10）
   ----------------------------------------------------------
   选区改写按段换回：选中的几段（wrSelectionSegments）换成候选的几段——候选的第一段接在起始段选区之前那一截后面，
   最后一段后面接上结束段选区之后那一截，中间的各成一段；每一段改写包在 span.wr-rev 里（界面标记，落盘前拆掉）。
   过去整段改写塞进一个 <span>：跨段的选区被并成一段、改好的字落在段落外面，「还原原文」也拼不回原来的几段。
   · wrRevSplice(editor, seg, paragraphs)：换进去，返回 { caret }（光标落在最后一段改写之后）。换下来的那几段原样
     （同一批节点，连同实体高亮、批注划线，以及段与段之间的空白）留在模块里的 WeakMap 上，按这一组的 span 找——
     组件重新挂载也找得到；正文整篇重载（换场、冲突换稿）后 span 不在了，记录随之作废。
     选区首尾的空白不算改写的字（留在原处）；边界正好夹着软换行（<br>）之类没有字的节点时，它跟着留下的那一截。
     第二段起是新开的段：段落种类（p / blockquote）与段首缩进跟原来对应的那一行（送去改写的第 i 行，见
     wrSelectionSegments 的 lines；最后一段跟结束段）——不拿中间的空段、零散的 <br> 当样子。
     起始段 / 结束段里被选区切开的实体高亮（名字只剩半个）拆掉；被切开的上一次改写的标记也拆掉（那一处的字被这一次
     改写换掉了一部分，要还原它得先还原这一次）。
   · 同一段里先后改写几处（复核 Q3b-R1）：落在这一次选区之外、整个儿留在起始段前半截 / 结束段后半截里的上一次改写，
     标记是同一个活节点——换段时把它挪进新段，换下来的原段里留一份一模一样的替身（记在这一组的 swaps 上）。
     所以先改的那一处照样能单独还原、单独保留，不必先还原后改的：每一组的段落按它的 span 现在在哪一段算，不记死。
     这一组原样还原时把挪走的标记换回原段里的替身位置，原来那几段连同上一次改写的标记原样回来。
   · wrRevGroup(span)：这一组的原文 / 现在的改写（各按段），弹层用；不是这一次打开里换进来的返回 null。
   · wrRevRevert(span)：还原原文。没动过：原来那几段原样放回（innerHTML 逐字相同）。改写之后起始段选区之前、
     结束段选区之后那两截改过（作者写了字，或那里的另一处改写还原 / 保留了）：那两截用现在的，选中的字按原来的段落拼回。
     改写的那几段本身改过，或几段改写不再前后相连（中间插了段、并成了一段、被后来的改写换掉了一部分）：不还原，
     返回 { ok: false, reason: "edited" }（「改写后又改过这几段，不能整段还原；可在版本历史里找回」）。
   · wrRevAccept(span)：保留改写，拆掉这一组的标记。
   DOM 工具 + 模块内一张 WeakMap；不读 store、不写 window。
   ========================================================== */

const GROUPS = new WeakMap();   // span.wr-rev → 这一组的记录
const CUT_MARKERS = "span.wr-entity, span.wr-rev";
const SLOT = "data-wr-rev-slot"; // 换段那一刻给替身做的临时记号（副本里认出它、换成活的标记），用完即摘

/* 换进去的段落外壳：同一种段落（p / blockquote / div），带原来的属性，去掉只给界面用的标记 */
const SHELL_UI_CLASSES = ["is-active", "is-fresh", "is-merge", "wr-dx-para", "wr-anno-flash", "wr-entity-flash"];
function shellOf(block) {
  const shell = block.cloneNode(false);
  SHELL_UI_CLASSES.forEach((name) => shell.classList.remove(name));
  if (!shell.classList.length) shell.removeAttribute("class");
  shell.removeAttribute("data-dx");
  return shell;
}

function ancestorsWithin(block, node) {
  const chain = [];
  for (let at = node && node.nodeType === 1 ? node : node && node.parentNode; at && at !== block; at = at.parentNode) chain.unshift(at);
  return chain;
}

/* 副本里被边界切开的标记：沿边界那一侧的祖先链（副本的右边界看最右一路，左边界看最左一路）逐层对照原节点，
   字比原来少的就是被切开的（两边都找完再拆：拆了一边，另一边的那一路就对不上了） */
function cutClones(fragment, block, position, side) {
  let clone = side === "right" ? fragment.lastChild : fragment.firstChild;
  const cut = [];
  for (const original of ancestorsWithin(block, position.node)) {
    if (!clone || clone.nodeType !== 1) break;
    if (original.matches(CUT_MARKERS) && clone.textContent !== original.textContent) cut.push(clone);
    clone = side === "right" ? clone.lastChild : clone.firstChild;
  }
  return cut;
}

/* 段落 block 里整个儿落在 [from, to)（拼接文字的偏移，to 为空 = 到段尾）里、还有记录的改写标记（最外层的那些）*/
function recordedSpansWithin(block, from, to) {
  const candidates = Array.from(block.querySelectorAll("span.wr-rev")).filter((span) => GROUPS.has(span));
  if (!candidates.length) return [];
  const starts = new Map();
  let total = 0;
  const walker = block.ownerDocument.createTreeWalker(block, 4 /* NodeFilter.SHOW_TEXT */);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    starts.set(node, total);
    total += node.nodeValue.length;
  }
  const limit = to == null ? total : to;
  return candidates.filter((span) => {
    const outer = span.parentElement && span.parentElement.closest("span.wr-rev");
    if (outer && block.contains(outer) && GROUPS.has(outer)) return false; // 套在另一处改写里：跟着外面那一处走
    const length = (span.textContent || "").length;
    const firstText = span.ownerDocument.createTreeWalker(span, 4).nextNode();
    if (!length || !firstText) return false;
    const start = starts.get(firstText);
    return start >= from && start + length <= limit;
  });
}

/* 留在副本里的上一次改写：原节点先换成一模一样的替身（带临时记号），副本里认出替身的副本、换回活的那个——
   活的标记跟着进新段，原段里留下替身（这一组原样还原时再换回来） */
function standIns(block, from, to) {
  return recordedSpansWithin(block, from, to).map((live, i) => {
    const stand = live.cloneNode(true);
    stand.setAttribute(SLOT, String(i));
    live.replaceWith(stand);
    return { live, stand };
  });
}
function seat(fragment, swaps) {
  const seated = [];
  Array.from(fragment.querySelectorAll(`[${SLOT}]`)).forEach((clone) => {
    const swap = swaps[Number(clone.getAttribute(SLOT))];
    if (swap && !seated.includes(swap)) {
      clone.replaceWith(swap.live);
      seated.push(swap);
    } else {
      unwrapNode(clone);
    }
  });
  swaps.forEach((swap) => {
    swap.stand.removeAttribute(SLOT);
    if (!seated.includes(swap)) swap.stand.replaceWith(swap.live); // 没进副本（不该发生）：原样放回
  });
  return seated;
}

/* 留下来的那两截副本：诊断高亮拆掉，切出来的空壳（零个字的内联元素）去掉 */
function tidy(fragment) {
  Array.from(fragment.querySelectorAll("mark.wr-dx")).forEach((node) => unwrapNode(node));
  Array.from(fragment.querySelectorAll("span, mark, em, i, strong, b, u, s, sub, sup")).forEach((node) => {
    if (!node.textContent && !node.querySelector("br") && !GROUPS.has(node)) node.remove();
  });
  return fragment;
}

/* 段落 block 里 [from, to) 那一截的副本（拼接文字的偏移；from 为空 = 从段首，to 为空 = 到段尾）。
   边界正好落在两个文本节点之间时，夹在中间的非文字节点（软换行 <br>、空的格式标签）算哪一边由 startNext / endNext 定：
   留着不动的那两截（keep）尽量大、被换掉 / 拼回的字尽量小。被切开的标记拆掉；整个儿在这一截里、还有记录的改写标记
   换成活的（swaps：原段里留下的替身）；没有记录的改写标记只剩字。tidyUp：顺手做 tidy。 */
function clonePart(block, from, to, { startNext = true, endNext = false, tidyUp = false } = {}) {
  const doc = block.ownerDocument;
  const swaps = standIns(block, from || 0, to);
  const range = doc.createRange();
  range.selectNodeContents(block);
  const start = from == null ? null : wrPositionAt(block, from, startNext);
  const end = to == null ? null : wrPositionAt(block, to, endNext);
  if (start) range.setStart(start.node, start.offset);
  if (end) range.setEnd(end.node, end.offset);
  let fragment = range.cloneContents();
  /* 这一截整个儿落在某几层标签里（加粗的一句中间、一个名字里）时，副本不带外面那几层：照原样包回去，
     两边的祖先链才从段落的子节点对得上 */
  ancestorsWithin(block, range.commonAncestorContainer).reverse().forEach((element) => {
    const wrap = element.cloneNode(false);
    wrap.appendChild(fragment);
    fragment = doc.createDocumentFragment();
    fragment.appendChild(wrap);
  });
  new Set([
    ...(start ? cutClones(fragment, block, start, "left") : []),
    ...(end ? cutClones(fragment, block, end, "right") : []),
  ]).forEach((node) => unwrapNode(node));
  Array.from(fragment.querySelectorAll("span.wr-rev")).forEach((node) => {
    if (!node.hasAttribute(SLOT)) unwrapNode(node);
  });
  const seated = seat(fragment, swaps);
  if (tidyUp) tidy(fragment);
  return { fragment, swaps: seated };
}

/* 起始段里选区之前、结束段里选区之后那两截（留着不动）；还原时拼回的选中的字 */
const keptHead = (block, offset) => clonePart(block, null, offset, { endNext: true, tidyUp: true });
const keptTail = (block, offset) => clonePart(block, offset, null, { startNext: false, tidyUp: true });
const selectedPart = (block, from, to) => clonePart(block, from, to, { startNext: true, endNext: false });

function leadingSpace(text) { return (/^\s*/.exec(String(text || "")) || [""])[0]; }
function trailingSpace(text) { return (/\s*$/.exec(String(text || "")) || [""])[0]; }

/* 一段里某个节点之前 / 之后的兄弟节点（这一组的 span 是段落的直接子节点） */
function siblingsBefore(node) {
  const out = [];
  for (let at = node.previousSibling; at; at = at.previousSibling) out.unshift(at);
  return out;
}
function siblingsAfter(node) {
  const out = [];
  for (let at = node.nextSibling; at; at = at.nextSibling) out.push(at);
  return out;
}
function htmlOf(nodes, doc) {
  const box = doc.createElement("div");
  nodes.forEach((node) => box.appendChild(node.cloneNode(true)));
  return box.innerHTML;
}

/* 紧跟在某个节点后面的光标（节点随后被拆包 / 合并时，活动 Range 会跟着挪到那段字后面） */
export function wrCaretAfter(node) {
  if (!node || !node.parentNode) return null;
  const range = node.ownerDocument.createRange();
  range.setStartAfter(node);
  range.collapse(true);
  return range;
}

function caretAt(block, offset) {
  const at = wrPositionAt(block, offset, false);
  if (!at) return null;
  const range = block.ownerDocument.createRange();
  range.setStart(at.node, at.offset);
  range.collapse(true);
  return range;
}

/* 编辑器根上从 first 到 last（含）的全部节点：段落之外还有段与段之间的空白文本，原样还原时一起放回 */
function rootRun(first, last) {
  const run = [];
  for (let at = first; at; at = at.nextSibling) {
    run.push(at);
    if (at === last) break;
  }
  return run;
}

/* 送去改写的各行，连同它在哪一段：{ block, text }（wrSelectionSegments 的 lines；老调用方没有 lines 时一段算一行） */
function sourceLines(seg) {
  return seg.segments.flatMap((segment) => (Array.isArray(segment.lines) ? segment.lines : [segment.text])
    .filter((text) => String(text || "").trim())
    .map((text) => ({ block: segment.block, text })));
}

/* 把候选的几段换进去。seg：wrSelectionSegments 当下的结果（调用方已核对它和送去改写的是同一段字）；
   paragraphs：候选的各段（至少一段）。返回 { caret, spans }。 */
export function wrRevSplice(editor, seg, paragraphs) {
  const doc = editor.ownerDocument;
  const originals = seg.blocks.slice();
  const first = originals[0];
  const last = originals[originals.length - 1];
  const run = rootRun(first, last);
  /* 选区首尾的空白不算改写的字：留在原处（改写回来的每一段都去掉了首尾空白） */
  const headAt = seg.head.offset + leadingSpace(seg.segments[0].text).length;
  const tailAt = seg.tail.offset - trailingSpace(seg.segments[seg.segments.length - 1].text).length;
  const head = keptHead(first, headAt);
  const tail = keptTail(last, tailAt);
  const count = paragraphs.length;
  /* 第二段起是新开的段，样子跟原来对应的那一行：中间的第 i 段对第 i 行（行比段少时对最后一行），最后一段对最后一行
     （它接着结束段的后半截）。段首缩进（全角空格之类）原本在选区里，改写时被修掉了，照那一行的补上；
     第一行从起始段中间选起，它的缩进是起始段自己的。 */
  const lines = sourceLines(seg);
  const lineFor = (i) => Math.min(i === count - 1 ? lines.length - 1 : i, lines.length - 1);
  const indentOf = (k) => leadingSpace(k === 0 ? first.textContent : lines[k].text);
  const spans = paragraphs.map((text) => {
    const span = doc.createElement("span");
    span.className = "wr-rev";
    span.textContent = text;
    return span;
  });
  const blocks = spans.map((span, i) => {
    const k = lineFor(i);
    const block = shellOf(i === 0 ? first : i === count - 1 ? last : lines[k].block);
    if (i === 0) block.appendChild(head.fragment);
    else if (indentOf(k)) block.appendChild(doc.createTextNode(indentOf(k)));
    block.appendChild(span);
    if (i === count - 1) block.appendChild(tail.fragment);
    return block;
  });
  const record = {
    editor,
    originals,
    run,
    spans,
    inserted: paragraphs.slice(),
    original: seg.paragraphs.map((text) => text.trim()),
    headAt,
    tailAt,
    swaps: [...head.swaps, ...tail.swaps],
    /* 换进去那一刻各段 span 前后的样子：还原时据此判断作者之后改没改过 */
    before: spans.map((span) => htmlOf(siblingsBefore(span), doc)),
    after: spans.map((span) => htmlOf(siblingsAfter(span), doc)),
  };
  first.before(...blocks);
  run.forEach((node) => node.remove());
  spans.forEach((span) => GROUPS.set(span, record));
  return { caret: wrCaretAfter(spans[count - 1]), spans };
}

/* 这一组的各段现在在哪：span 的父节点，必须是编辑器的顶层段落 */
function blockOf(record, span) {
  const block = span.parentNode;
  return block && block.parentNode === record.editor ? block : null;
}

/* 这一组现在还是换进去时的样子吗：改写的那几段本身没被改、段落还连在一起、中间的段落除了改写（和段首缩进）没有别的字。
   起始段选区之前那一截、结束段选区之后那一截可以改过（还原时保留）。 */
function groupIntact(record) {
  const { spans, inserted, editor } = record;
  const doc = editor.ownerDocument;
  let previous = null;
  for (let i = 0; i < spans.length; i += 1) {
    const span = spans[i];
    const block = editor.contains(span) ? blockOf(record, span) : null;
    if (!block || span.textContent !== inserted[i]) return false;
    if (i > 0 && block.previousElementSibling !== previous) return false;
    if (i > 0 && htmlOf(siblingsBefore(span), doc) !== record.before[i]) return false;
    if (i < spans.length - 1 && htmlOf(siblingsAfter(span), doc) !== record.after[i]) return false;
    previous = block;
  }
  return true;
}

export function wrRevGroup(span) {
  const record = span ? GROUPS.get(span) : null;
  if (!record) return null;
  return {
    original: record.original.slice(),
    now: record.spans.map((item) => item.textContent || ""),
    intact: groupIntact(record),
  };
}

function forget(record) {
  record.spans.forEach((span) => GROUPS.delete(span));
}

/* 还原原文。返回 { ok: true, caret }，或 { ok: false, reason: "edited" | "lost" } */
export function wrRevRevert(span) {
  const record = span ? GROUPS.get(span) : null;
  if (!record) return { ok: false, reason: "lost" };
  if (!groupIntact(record)) return { ok: false, reason: "edited" };
  const { spans, originals, run, editor } = record;
  const doc = editor.ownerDocument;
  const blocks = spans.map((item) => blockOf(record, item));
  const headNow = siblingsBefore(spans[0]);
  const tailNow = siblingsAfter(spans[spans.length - 1]);
  /* 没动过 = 那两截还是换进去时的样子，挪进来的上一次改写也还是那几个节点（样子一样、换了节点的不算） */
  const untouched = htmlOf(headNow, doc) === record.before[0] && htmlOf(tailNow, doc) === record.after[spans.length - 1]
    && record.swaps.every(({ live }) => blocks.some((block) => block.contains(live)));
  const lastOriginal = originals[originals.length - 1];
  let caret;
  if (untouched) {
    /* 原来那几段原样放回（同一批节点）；挪进新段的上一次改写换回原段里替身的位置 */
    record.swaps.forEach(({ live, stand }) => stand.replaceWith(live));
    blocks[0].before(...run);
    blocks.forEach((block) => block.remove());
    caret = caretAt(lastOriginal, record.tailAt);
  } else if (originals.length === 1) {
    /* 选区前后那两截改过：那两截用现在的（连同其中别处的改写标记），选中的字按原样拼回 */
    const block = shellOf(originals[0]);
    headNow.forEach((node) => block.appendChild(node));
    block.appendChild(selectedPart(originals[0], record.headAt, record.tailAt).fragment);
    const caretOffset = (block.textContent || "").length;
    tailNow.forEach((node) => block.appendChild(node));
    blocks[0].before(block);
    blocks.forEach((item) => item.remove());
    caret = caretAt(block, caretOffset);
  } else {
    const firstBlock = shellOf(originals[0]);
    headNow.forEach((node) => firstBlock.appendChild(node));
    firstBlock.appendChild(selectedPart(originals[0], record.headAt, null).fragment);
    const lastBlock = shellOf(lastOriginal);
    lastBlock.appendChild(selectedPart(lastOriginal, null, record.tailAt).fragment);
    const caretOffset = (lastBlock.textContent || "").length;
    tailNow.forEach((node) => lastBlock.appendChild(node));
    blocks[0].before(firstBlock, ...run.slice(1, -1), lastBlock);
    blocks.forEach((item) => item.remove());
    caret = caretAt(lastBlock, caretOffset);
  }
  forget(record);
  return { ok: true, caret };
}

/* 保留改写：拆掉这一组的标记，光标落在最后一段改写之后 */
export function wrRevAccept(span) {
  const record = span ? GROUPS.get(span) : null;
  const spans = record ? record.spans : (span ? [span] : []);
  const caret = wrCaretAfter(spans[spans.length - 1]);
  spans.forEach((item) => {
    const parent = item.parentNode;
    if (!parent) return;
    unwrapNode(item);
    if (parent.normalize) parent.normalize();
  });
  if (record) forget(record);
  return caret;
}
