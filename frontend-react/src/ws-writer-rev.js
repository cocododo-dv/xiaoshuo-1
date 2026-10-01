import { unwrapNode } from "./manuscript-html.js";
import { wrPositionAt } from "./ws-writer-manuscript.js";

/* ==========================================================
   写作台 · AI 改写换进正文与「还原原文」（重评 R12 / 批准 #20b，2026-10）
   ----------------------------------------------------------
   选区改写按段换回：选中的几段（wrSelectionSegments）换成候选的几段——候选的第一段接在起始段选区之前那一截后面，
   最后一段后面接上结束段选区之后那一截，中间的各成一段；每一段改写包在 span.wr-rev 里（界面标记，落盘前拆掉）。
   过去整段改写塞进一个 <span>：跨段的选区被并成一段、改好的字落在段落外面，「还原原文」也拼不回原来的几段。
   · wrRevSplice(editor, seg, paragraphs)：换进去，返回 { caret }（光标落在最后一段改写之后）。换下来的那几段原样
     （同一批节点，连同实体高亮、批注划线）留在模块里的 WeakMap 上，按这一组的 span 找——组件重新挂载也找得到；
     正文整篇重载（换场、冲突换稿）后 span 不在了，记录随之作废。
     选区首尾的空白不算改写的字（留在原处）；第二段起是新开的段，段首缩进跟原来对应的那一段。
     起始段 / 结束段里被选区切开的实体高亮（名字只剩半个）拆掉；那两截里上一次改写的标记也拆掉——那一组的段落已经
     换掉了，要还原它得先还原这一次（还原后原来那几段连同它的标记原样回来）。
   · wrRevGroup(span)：这一组的原文 / 现在的改写（各按段），弹层用；不是这一次打开里换进来的返回 null。
   · wrRevRevert(span)：还原原文。没动过：原来那几段原样放回（innerHTML 逐字相同）。改写之后作者只在起始段选区之前、
     结束段选区之后改过字：那两截用现在的，选中的字按原来的段落拼回。改写的那几段本身改过、段落拆开或挪走了：不还原，
     返回 { ok: false, reason: "edited" }（「改写后又改过这几段，不能整段还原；可在版本历史里找回」）。
   · wrRevAccept(span)：保留改写，拆掉这一组的标记。
   DOM 工具 + 模块内一张 WeakMap；不读 store、不写 window。
   ========================================================== */

const GROUPS = new WeakMap();   // span.wr-rev → 这一组的记录
const CUT_MARKERS = "span.wr-entity, span.wr-rev";

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

/* 副本里被边界切开的标记：沿边界那一侧的祖先链（head 是副本的最右一路，tail 是最左一路）逐层对照原节点，
   字比原来少的就是被切开的 */
function unwrapCut(fragment, block, position, side) {
  let clone = side === "head" ? fragment.lastChild : fragment.firstChild;
  const cut = [];
  for (const original of ancestorsWithin(block, position.node)) {
    if (!clone || clone.nodeType !== 1) break;
    if (original.matches(CUT_MARKERS) && clone.textContent !== original.textContent) cut.push(clone);
    clone = side === "head" ? clone.lastChild : clone.firstChild;
  }
  cut.forEach((node) => unwrapNode(node));
}

/* 留下来的那两截副本：上一次改写的标记、诊断高亮拆掉，切出来的空壳（零个字的内联元素）去掉 */
function tidy(fragment) {
  Array.from(fragment.querySelectorAll("span.wr-rev, mark.wr-dx")).forEach((node) => unwrapNode(node));
  Array.from(fragment.querySelectorAll("span, mark, em, i, strong, b, u, s, sub, sup")).forEach((node) => {
    if (!node.textContent && !node.querySelector("br")) node.remove();
  });
  return fragment;
}

/* 段落 block 里 [0, offset) 那一截的副本 */
function cloneHead(block, offset) {
  const doc = block.ownerDocument;
  if (!(offset > 0)) return doc.createDocumentFragment();
  const end = wrPositionAt(block, offset, false);
  const range = doc.createRange();
  range.selectNodeContents(block);
  range.setEnd(end.node, end.offset);
  const fragment = range.cloneContents();
  unwrapCut(fragment, block, end, "head");
  return tidy(fragment);
}

/* 段落 block 里 [offset, 末尾) 那一截的副本 */
function cloneTail(block, offset) {
  const doc = block.ownerDocument;
  const start = wrPositionAt(block, offset, true);
  const range = doc.createRange();
  range.selectNodeContents(block);
  if (start) range.setStart(start.node, start.offset);
  const fragment = range.cloneContents();
  if (start) unwrapCut(fragment, block, start, "tail");
  return tidy(fragment);
}

/* 段落 block 里 [from, to) 那一截的原样副本（to 为空 = 到段尾）：还原时拼回选中的字 */
function cloneMiddle(block, from, to) {
  const doc = block.ownerDocument;
  const range = doc.createRange();
  range.selectNodeContents(block);
  const start = from > 0 ? wrPositionAt(block, from, true) : null;
  const end = to == null ? null : wrPositionAt(block, to, false);
  if (start) range.setStart(start.node, start.offset);
  if (end) range.setEnd(end.node, end.offset);
  return range.cloneContents();
}

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

/* 把候选的几段换进去。seg：wrSelectionSegments 当下的结果（调用方已核对它和送去改写的是同一段字）；
   paragraphs：候选的各段（至少一段）。返回 { caret, spans }。 */
export function wrRevSplice(editor, seg, paragraphs) {
  const doc = editor.ownerDocument;
  const originals = seg.blocks.slice();
  const first = originals[0];
  const last = originals[originals.length - 1];
  /* 选区首尾的空白不算改写的字：留在原处（改写回来的每一段都去掉了首尾空白） */
  const headAt = seg.head.offset + leadingSpace(seg.segments[0].text).length;
  const tailAt = seg.tail.offset - trailingSpace(seg.segments[seg.segments.length - 1].text).length;
  const head = cloneHead(first, headAt);
  const tail = cloneTail(last, tailAt);
  const count = paragraphs.length;
  /* 第二段起是新开的段：段首的缩进（全角空格之类）跟原来对应那一段的走——它们原本在选区里，改写时被修掉了 */
  const indentOf = (i) => leadingSpace(originals[Math.min(i, originals.length - 1)].textContent || "");
  const spans = paragraphs.map((text) => {
    const span = doc.createElement("span");
    span.className = "wr-rev";
    span.textContent = text;
    return span;
  });
  const blocks = spans.map((span, i) => {
    const block = shellOf(i === count - 1 ? last : first);
    if (i === 0) block.appendChild(head);
    else if (indentOf(i)) block.appendChild(doc.createTextNode(indentOf(i)));
    block.appendChild(span);
    if (i === count - 1) block.appendChild(tail);
    return block;
  });
  const record = {
    editor,
    originals,
    blocks,
    spans,
    inserted: paragraphs.slice(),
    original: seg.paragraphs.map((text) => text.trim()),
    headAt,
    tailAt,
    /* 换进去那一刻各段 span 前后的样子：还原时据此判断作者之后改没改过 */
    before: spans.map((span) => htmlOf(siblingsBefore(span), doc)),
    after: spans.map((span) => htmlOf(siblingsAfter(span), doc)),
  };
  first.before(...blocks);
  originals.forEach((block) => block.remove());
  spans.forEach((span) => GROUPS.set(span, record));
  return { caret: wrCaretAfter(spans[count - 1]), spans };
}

/* 这一组现在还是换进去时的样子吗：改写的那几段本身没被改、段落还连在一起、中间的段落除了改写（和段首缩进）没有别的字。
   起始段选区之前那一截、结束段选区之后那一截可以改过（还原时保留）。 */
function groupIntact(record) {
  const { blocks, spans, inserted, editor } = record;
  const doc = editor.ownerDocument;
  for (let i = 0; i < spans.length; i += 1) {
    const span = spans[i];
    const block = blocks[i];
    if (!span.isConnected || span.parentNode !== block || block.parentNode !== editor) return false;
    if (span.textContent !== inserted[i]) return false;
    if (i > 0 && block.previousElementSibling !== blocks[i - 1]) return false;
    if (i > 0 && htmlOf(siblingsBefore(span), doc) !== record.before[i]) return false;
    if (i < spans.length - 1 && htmlOf(siblingsAfter(span), doc) !== record.after[i]) return false;
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
  const { blocks, spans, originals, editor } = record;
  const doc = editor.ownerDocument;
  const headNow = siblingsBefore(spans[0]);
  const tailNow = siblingsAfter(spans[spans.length - 1]);
  const untouched = htmlOf(headNow, doc) === record.before[0] && htmlOf(tailNow, doc) === record.after[spans.length - 1];
  const lastOriginal = originals[originals.length - 1];
  let restored;
  let caret;
  if (untouched) {
    /* 没动过：原来那几段原样放回（同一批节点） */
    restored = originals;
    blocks[0].before(...restored);
    blocks.forEach((block) => block.remove());
    caret = caretAt(lastOriginal, record.tailAt);
  } else if (originals.length === 1) {
    /* 作者改过选区前后那两截：那两截用现在的，选中的字按原样拼回 */
    const block = shellOf(originals[0]);
    headNow.forEach((node) => block.appendChild(node));
    block.appendChild(cloneMiddle(originals[0], record.headAt, record.tailAt));
    const caretOffset = (block.textContent || "").length;
    tailNow.forEach((node) => block.appendChild(node));
    restored = [block];
    blocks[0].before(block);
    blocks.forEach((item) => item.remove());
    caret = caretAt(block, caretOffset);
  } else {
    const firstBlock = shellOf(originals[0]);
    headNow.forEach((node) => firstBlock.appendChild(node));
    firstBlock.appendChild(cloneMiddle(originals[0], record.headAt, null));
    const lastBlock = shellOf(lastOriginal);
    lastBlock.appendChild(cloneMiddle(lastOriginal, 0, record.tailAt));
    tailNow.forEach((node) => lastBlock.appendChild(node));
    restored = [firstBlock, ...originals.slice(1, -1), lastBlock];
    blocks[0].before(...restored);
    blocks.forEach((item) => item.remove());
    caret = caretAt(lastBlock, record.tailAt);
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
