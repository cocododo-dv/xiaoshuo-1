/* ==========================================================
   写作台批注（2026-09-21）
   ----------------------------------------------------------
   批注不是正文：不进草稿、不上服务器。过去它是正文里的 <mark data-note>，
   一落盘就被消毒器剥掉属性——批注内容丢了，只剩一块点不开的黄底。
   现在每场一份批注清单存在本机浏览器（localStorage，键 wr-anno:<场景 id>::<作品 id>，
   与 wr-notes 的缓存是同一种按作品隔离的键），每条按「引文 + 前后各一小段上下文」锚定
   （W3C 文本引文选择器的思路），正文载入后重新标出来；作者改了被批注的那段字，
   保存时锚点跟着更新。正文改得找不到那段话了，批注仍留在清单里标「找不到原文」，不静默丢掉。
   这些键只存在本机，设置页「清除本机缓存」不清它们（ws-settings.jsx 的 wsWorkCachePurgePlan）。

   纯函数 + DOM 模块：不读 store、不写 window。存储键由调用方算好传进来。
   ========================================================== */

export const WR_ANNO_CONTEXT = 24;   // 前后各留多少字做上下文
export const WR_ANNO_KEY_PREFIX = "wr-anno:";
export const WR_ANNO_MAX_ITEMS = 200;   // 每场最多几条
export const WR_ANNO_MAX_NOTE = 2000;   // 一条批注最多几个字（输入框用同一个上限，存的时候不会再被截）
export const WR_ANNO_MAX_QUOTE = 2000;  // 最多圈几个字（截短的引文在正文里找不回来）
const MAX_ITEMS = WR_ANNO_MAX_ITEMS;
const MAX_NOTE = WR_ANNO_MAX_NOTE;
const MAX_QUOTE = WR_ANNO_MAX_QUOTE;
const ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

/* ---------------- 存储 ---------------- */

function normalizeItem(raw) {
  if (!raw || typeof raw !== "object") return null;
  const id = String(raw.id || "");
  const quote = typeof raw.quote === "string" ? raw.quote.slice(0, MAX_QUOTE) : "";
  if (!ID_RE.test(id) || !quote.trim()) return null;
  return {
    id,
    quote,
    prefix: typeof raw.prefix === "string" ? raw.prefix.slice(-WR_ANNO_CONTEXT) : "",
    suffix: typeof raw.suffix === "string" ? raw.suffix.slice(0, WR_ANNO_CONTEXT) : "",
    note: typeof raw.note === "string" ? raw.note.slice(0, MAX_NOTE) : "",
    createdAt: Number(raw.createdAt) || 0,
    updatedAt: Number(raw.updatedAt) || Number(raw.createdAt) || 0,
  };
}

/* 读不到（隐私窗口、存储被禁、内容坏了）就当没有批注——批注只是提醒，不是闸门 */
export function wrAnnoLoad(key) {
  if (!key) return [];
  try {
    const raw = JSON.parse(localStorage.getItem(key));
    const items = raw && Array.isArray(raw.items) ? raw.items : [];
    const seen = new Set();
    return items.map(normalizeItem).filter((item) => {
      if (!item || seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    });
  } catch (e) {
    return [];
  }
}

/* 写不进去（配额满、存储被禁）返回 false，调用方据此提示作者。
   超过每场上限时整份拒存（也返回 false）——过去是截掉末尾，新加的那条被静默丢掉却照样报成功 */
export function wrAnnoSave(key, list) {
  if (!key) return false;
  try {
    const items = (list || []).map(normalizeItem).filter(Boolean);
    if (items.length > MAX_ITEMS) return false;
    if (!items.length) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify({ v: 1, items }));
    return true;
  } catch (e) {
    return false;
  }
}

export function wrAnnoId() {
  return "a" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

/* ---------------- 文本模型 ---------------- */

/* 编辑器里所有文本节点按文档顺序拼起来——与 Range.toString() 同一种「字」的算法 */
function textIndex(root) {
  const doc = root.ownerDocument;
  const walker = doc.createTreeWalker(root, 4 /* NodeFilter.SHOW_TEXT */);
  const nodes = [];
  let text = "";
  let node;
  while ((node = walker.nextNode())) {
    nodes.push({ node, start: text.length });
    text += node.nodeValue;
  }
  return { text, nodes };
}

function offsetOf(root, container, offset) {
  const range = root.ownerDocument.createRange();
  range.setStart(root, 0);
  range.setEnd(container, offset);
  return range.toString().length;
}

function contextOf(text, start, end) {
  return {
    prefix: text.slice(Math.max(0, start - WR_ANNO_CONTEXT), start),
    suffix: text.slice(end, end + WR_ANNO_CONTEXT),
  };
}

/* 选区 → 锚点 { quote, prefix, suffix, start, end }；选区不在 root 里或只有空白时返回 null */
export function wrAnnoAnchor(root, range) {
  if (!root || !range || !root.contains(range.commonAncestorContainer)) return null;
  const { text } = textIndex(root);
  const start = offsetOf(root, range.startContainer, range.startOffset);
  const end = offsetOf(root, range.endContainer, range.endOffset);
  const quote = text.slice(start, end);
  if (!quote.trim()) return null;
  return { quote, ...contextOf(text, start, end), start, end };
}

function sharedTail(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n += 1;
  return n;
}
function sharedHead(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n += 1;
  return n;
}

/* 在全文里找这条批注：引文出现多次时，挑前后文对得最多的那一处；一处都没有返回 null */
export function wrAnnoLocate(text, anno) {
  const quote = anno && anno.quote;
  if (!quote || !text) return null;
  const prefix = anno.prefix || "";
  const suffix = anno.suffix || "";
  let best = null;
  let at = text.indexOf(quote);
  while (at >= 0) {
    const end = at + quote.length;
    const score = sharedTail(text.slice(Math.max(0, at - prefix.length), at), prefix)
      + sharedHead(text.slice(end, end + suffix.length), suffix);
    if (!best || score > best.score) best = { start: at, end, score };
    at = text.indexOf(quote, at + 1);
  }
  return best;
}

/* ---------------- 标注 ---------------- */

function makeMark(doc, anno) {
  const mark = doc.createElement("mark");
  mark.className = "wr-anno";
  mark.setAttribute("data-anno-id", anno.id);
  if (anno.note) mark.setAttribute("title", anno.note);
  return mark;
}

/* 把 [start, end) 这段字逐个文本节点包进 <mark>：跨段落、跨实体高亮也不会把段落搬进 mark 里 */
function wrapText(root, start, end, anno) {
  const doc = root.ownerDocument;
  const marks = [];
  textIndex(root).nodes.forEach(({ node, start: from }) => {
    const length = node.nodeValue.length;
    const to = from + length;
    if (!length || to <= start || from >= end) return;
    // 段落之间的空白文本节点直接挂在编辑器上：不包，免得在块级位置插一个 <mark>
    if (node.parentNode === root && !node.nodeValue.trim()) return;
    let target = node;
    const cutEnd = Math.min(length, end - from);
    const cutStart = Math.max(0, start - from);
    if (cutEnd < length) target.splitText(cutEnd);
    if (cutStart > 0) target = target.splitText(cutStart);
    const mark = makeMark(doc, anno);
    target.parentNode.insertBefore(mark, target);
    mark.appendChild(target);
    marks.push(mark);
  });
  return marks;
}

function marksOf(root, id) {
  if (!root) return [];
  const all = Array.from(root.querySelectorAll("mark.wr-anno"));
  return id ? all.filter((mark) => mark.getAttribute("data-anno-id") === id) : all;
}

/* 拆掉标注（id 为空时拆掉全部），文字原样留下 */
export function wrAnnoUnmark(root, id) {
  marksOf(root, id).forEach((mark) => {
    const parent = mark.parentNode;
    if (!parent) return;
    while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
    parent.removeChild(mark);
    if (parent.normalize) parent.normalize();
  });
}

/* 按锚点标出一条批注，返回生成的 <mark> 们 */
export function wrAnnoMark(root, anno, start, end) {
  if (!root || !anno || !(end > start)) return [];
  return wrapText(root, start, end, anno);
}

/* 清掉旧标注，按清单重新标一遍；返回标上了的批注 id */
export function wrAnnoApply(root, list) {
  const anchored = new Set();
  if (!root) return anchored;
  wrAnnoUnmark(root);
  (list || []).forEach((anno) => {
    const hit = wrAnnoLocate(textIndex(root).text, anno);
    if (hit && wrapText(root, hit.start, hit.end, anno).length) anchored.add(anno.id);
  });
  return anchored;
}

/* 作者改了批注的那段字：按正文里现存的标注重新取引文与前后文。
   正文里已经没有标注的批注保持原样（清单里显示「找不到原文」）。没有变化时返回原数组。 */
export function wrAnnoRefresh(root, list) {
  if (!root || !list || !list.length) return list || [];
  const { text } = textIndex(root);
  let changed = false;
  const next = list.map((anno) => {
    const marks = marksOf(root, anno.id);
    if (!marks.length) return anno;
    const last = marks[marks.length - 1];
    const start = offsetOf(root, marks[0], 0);
    const end = offsetOf(root, last, last.childNodes.length);
    const quote = text.slice(start, end);
    if (!quote.trim()) return anno;
    const { prefix, suffix } = contextOf(text, start, end);
    if (quote === anno.quote && prefix === anno.prefix && suffix === anno.suffix) return anno;
    changed = true;
    return { ...anno, quote, prefix, suffix };
  });
  return changed ? next : list;
}

/* 改了批注内容：标注上的悬停提示跟着换 */
export function wrAnnoRetitle(root, id, note) {
  marksOf(root, id).forEach((mark) => {
    if (note) mark.setAttribute("title", note); else mark.removeAttribute("title");
  });
}

/* 标注里还剩字吗：作者把批注的那段字删光时，浏览器有时会留下一个空的 <mark> */
function markHasText(mark) {
  return !!String(mark.textContent || "").trim();
}

/* 正文里现在标得出来的批注 id（批注页签据此区分「点一下定位」和「找不到原文」）；
   只剩空标注的不算 */
export function wrAnnoAnchoredIds(root) {
  return new Set(marksOf(root).filter(markHasText).map((mark) => mark.getAttribute("data-anno-id")).filter(Boolean));
}

export function wrAnnoFirstMark(root, id) {
  return marksOf(root, id).find(markHasText) || null;
}
