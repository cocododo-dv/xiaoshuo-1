import {
  LEGACY_DRAFT_PLACEHOLDER, MANUSCRIPT_BLOCK_SELECTOR, sanitizeManuscriptHTML, stripLeadingPlaceholder, unwrapInline,
} from "./manuscript-html.js";
import { countChars } from "./lib/text.js";

/* ==========================================================
   写作台正文的序列化边界（2026-09-21）
   ----------------------------------------------------------
   编辑器 DOM 里除了作者的字，还挂着一堆只给界面用的标记：档案实体高亮
   （span.wr-entity）、批注划线（mark.wr-anno）、AI 改写的「可还原」标记
   （span.wr-rev）、深改诊断高亮（mark.wr-dx / .wr-dx-para）、当前段 / 刚采纳 /
   融合草稿的 class。过去这些原样进了 WrDocs.save：消毒器剥掉属性，留下一层层
   没有意义的空 <span>（每存一次包一层），批注剩一块点不开的黄底。
   现在落盘前一律走 wrSerializeManuscript：存下去的只有干净正文。
   载入时 wrPrepareLoadedHTML 把历史遗留的空壳标记拆掉，并去掉旧版本当成正文
   存下去的开场占位句。纯函数模块，不读 store、不写 window。
   ========================================================== */

/* 旧版本把这句空白页提示当成一段正文种进编辑器，有的草稿里还留着它（与主页 / AI 起草台同一份，住在 manuscript-html.js） */
export { LEGACY_DRAFT_PLACEHOLDER as WR_LEGACY_PLACEHOLDER };

/* 编辑器里一段也没有时放一个空段落：光标落在 <p> 里，敲下的字才有段落样式 */
export const WR_EMPTY_DOC = "<p><br></p>";

/* 只给界面用的 class：落盘前摘掉（消毒器也会剥属性，这里先摘是为了克隆出来的 DOM 干净可测） */
const UI_CLASSES = ["is-active", "is-fresh", "is-merge", "wr-dx-para", "wr-entity-flash", "wr-anno-flash"];
const UI_ATTRIBUTES = ["data-dx", "data-lib-id", "data-note", "data-orig", "title"];

/* 就地拆掉 root 里所有界面标记。span / mark 在正文里没有别的来源：消毒后它们不带任何属性，
   只剩「一层空壳」或「一块黄底」，所以整类拆掉，而不是只拆带 class 的那几个。 */
export function wrStripUiMarkup(root) {
  if (!root || !root.querySelectorAll) return root;
  unwrapInline(root);
  Array.from(root.querySelectorAll("[class]")).forEach((node) => {
    UI_CLASSES.forEach((name) => node.classList.remove(name));
    if (!node.classList.length) node.removeAttribute("class");
  });
  UI_ATTRIBUTES.forEach((name) => {
    Array.from(root.querySelectorAll(`[${name}]`)).forEach((node) => node.removeAttribute(name));
  });
  if (root.normalize) root.normalize();
  return root;
}

function hasText(root) {
  return countChars((root && root.textContent) || "") > 0;
}

/* 编辑器 → 存盘 HTML。不改动传入的节点（克隆后处理）；没有一个字时返回空串。 */
export function wrSerializeManuscript(el) {
  if (!el) return "";
  const clone = el.cloneNode(true);
  wrStripUiMarkup(clone);
  stripLeadingPlaceholder(clone);
  if (!hasText(clone)) return "";
  return sanitizeManuscriptHTML(clone.innerHTML);
}

/* 存盘 / 缓存 HTML → 编辑器 HTML：消毒、拆空壳标记、去旧占位。没有一个字时返回空串，
   调用方自己决定放不放空段落（WR_EMPTY_DOC）。 */
export function wrPrepareLoadedHTML(html) {
  const clean = sanitizeManuscriptHTML(html == null ? "" : html);
  if (!clean || typeof document === "undefined") return clean;
  const template = document.createElement("template");
  template.innerHTML = clean;
  const root = template.content;
  unwrapInline(root);
  root.normalize();
  stripLeadingPlaceholder(root);
  if (!hasText(root)) return "";
  const box = document.createElement("div");
  box.appendChild(root);
  return box.innerHTML;
}

function blockText(block) {
  const nodes = [];
  const walker = block.ownerDocument.createTreeWalker(block, 4 /* NodeFilter.SHOW_TEXT */);
  let node;
  let joined = "";
  while ((node = walker.nextNode())) { nodes.push({ node, start: joined.length }); joined += node.nodeValue; }
  return { nodes, joined };
}

/* 在一段（block）里找到 text 的第一次出现，返回覆盖它的 Range；text 为空时选中整段。
   文字可能跨好几个文本节点（实体高亮把人名拆成了单独的 span），所以按拼接后的偏移找。 */
export function wrRangeForText(block, text) {
  if (!block || !block.ownerDocument) return null;
  if (!text) {
    const range = block.ownerDocument.createRange();
    range.selectNodeContents(block);
    return range;
  }
  const at = blockText(block).joined.indexOf(text);
  if (at < 0) return null;
  return wrRangeForOffsets(block, at, at + text.length);
}

function locateIn(nodes, offset, preferNext) {
  for (let i = 0; i < nodes.length; i += 1) {
    const { node: current, start: from } = nodes[i];
    const stop = from + current.nodeValue.length;
    if (offset < stop || (offset === stop && !preferNext) || i === nodes.length - 1) {
      return { node: current, offset: Math.min(offset - from, current.nodeValue.length) };
    }
  }
  return null;
}

/* 一段里按拼接文字的偏移找 DOM 位置 { node, offset }：preferNext 为真时，正好落在两个文本节点之间的偏移取后一个的开头
   （否则取前一个的末尾）。这一段一个字都没有时落在段落本身的开头；越界返回 null。 */
export function wrPositionAt(block, offset, preferNext = false) {
  if (!block || !block.ownerDocument || offset < 0) return null;
  const { nodes, joined } = blockText(block);
  if (offset > joined.length) return null;
  if (!nodes.length) return { node: block, offset: 0 };
  return locateIn(nodes, offset, preferNext);
}

/* 一段里按拼接文字的偏移 [start, end) 取 Range；越界返回 null。
   偏移与标记怎么包无关（深改高亮、实体高亮拆开的文本节点拼回去是同一串字）。 */
export function wrRangeForOffsets(block, start, end) {
  if (!block || !block.ownerDocument || !(end > start) || start < 0) return null;
  const { nodes, joined } = blockText(block);
  if (!nodes.length || end > joined.length) return null;
  const range = block.ownerDocument.createRange();
  const from = locateIn(nodes, start, true);
  const to = locateIn(nodes, end, false);
  if (!from || !to) return null;
  range.setStart(from.node, from.offset);
  range.setEnd(to.node, to.offset);
  return range;
}

/* 选区落在某一段（block）里的那一截：{ start, end, text }（这一段里按拼接文字算的偏移）。
   跨段的选区只取起始段里的部分——深改工具条把它带回起草姿态重新选中；
   过去带的是整个选区的文字（含换行），在单独一段里永远找不到，作者的选区就无声无息地没了。 */
export function wrBlockSlice(block, range) {
  if (!block || !range || !block.ownerDocument) return null;
  const doc = block.ownerDocument;
  const inside = doc.createRange();
  inside.selectNodeContents(block);
  if (block.contains(range.startContainer)) inside.setStart(range.startContainer, range.startOffset);
  if (block.contains(range.endContainer)) inside.setEnd(range.endContainer, range.endOffset);
  const before = doc.createRange();
  before.selectNodeContents(block);
  before.setEnd(inside.startContainer, inside.startOffset);
  const start = before.toString().length;
  const text = inside.toString();
  return { start, end: start + text.length, text };
}

/* ---- 选区按段切（重评 R12 / 批准 #20b：跨段的选区按段改写、按段换回） ----
   编辑器里的「一段」是顶层的 p / blockquote / div（里面不再套段落）；诊断的段号仍按 MANUSCRIPT_BLOCK_SELECTOR 数。 */
const LEAF_BLOCK_TAGS = new Set(["P", "BLOCKQUOTE", "DIV"]);
const NESTED_BLOCK_SELECTOR = "p, div, blockquote, ul, ol, li, pre, h1, h2, h3, h4, h5, h6, table";

function isLeafBlock(node) {
  return !!node && node.nodeType === 1 && LEAF_BLOCK_TAGS.has(node.tagName) && !node.querySelector(NESTED_BLOCK_SELECTOR);
}

/* 选区落在编辑器的哪几段：
   { blocks, segments: [{ block, index, start, end, text }], paragraphs, text, head, tail }
   · blocks：从选区起始段到结束段（含中间整段选中的空段），替换时这几段整体换掉；
   · segments：每段里被选中的那一截（偏移按这一段的拼接文字算；index 是诊断的段号，不是段落元素时为 -1）；
   · paragraphs：选中的字按段分开（空白段不算）——送去改写的就是它们，一段一行（text = paragraphs.join("\n")）；
   · head / tail：起始段里选区之前、结束段里选区之后留着不动的那两截的位置（{ block, offset }）。
   开头 / 结尾那一段里只选中了空白（选区停在下一段的开头、或从上一段的末尾起）不算那一段。
   选区碰到了不在任何段落里的散字、或套着段落 / 列表的结构：返回 { unsupported: true }——没法按段换回。
   选区不在编辑器里、或一个字都没选中：返回 null。 */
export function wrSelectionSegments(editor, range) {
  if (!editor || !range || range.collapsed || !editor.contains(range.commonAncestorContainer)) return null;
  const doc = editor.ownerDocument;
  const touched = [];
  for (const node of Array.from(editor.childNodes)) {
    if (!range.intersectsNode(node)) continue;
    if (node.nodeType === 3) {
      const part = doc.createRange();
      part.selectNodeContents(node);
      if (node === range.startContainer) part.setStart(node, range.startOffset);
      if (node === range.endContainer) part.setEnd(node, range.endOffset);
      if (part.toString().trim()) return { unsupported: true };
      continue;
    }
    if (node.nodeType !== 1) continue;
    touched.push({ block: node, ...wrBlockSlice(node, range) });
  }
  while (touched.length && !touched[0].text.trim()) touched.shift();
  while (touched.length && !touched[touched.length - 1].text.trim()) touched.pop();
  if (!touched.length) return null;
  if (touched.some((item) => !isLeafBlock(item.block))) return { unsupported: true };
  const indexed = Array.from(editor.querySelectorAll(MANUSCRIPT_BLOCK_SELECTOR));
  const segments = touched.map((item) => ({ ...item, index: indexed.indexOf(item.block) }));
  const paragraphs = segments.map((item) => item.text).filter((text) => text.trim());
  const first = segments[0];
  const last = segments[segments.length - 1];
  return {
    blocks: segments.map((item) => item.block),
    segments,
    paragraphs,
    text: paragraphs.join("\n"),
    head: { block: first.block, offset: first.start },
    tail: { block: last.block, offset: last.end },
  };
}

/* 本场字数：lib/text 的 countChars（去掉空白、按码点计，与服务端 count_words 同口径：一个生僻字 / 表情算一个；
   AI 起草台采纳时记的字数也是它，两边对得上）。
   读 textContent 而不是 innerText：innerText 每敲一个字都要对整篇稿子做一次样式和布局计算，
   空白反正要去掉，编辑器里也没有隐藏的子节点。占位不在 DOM 里了，不必再特判。 */
export function wrCountText(el) {
  if (!el) return 0;
  return countChars(el.textContent);
}
