import { sanitizeManuscriptHTML } from "./manuscript-html.js";

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

/* 旧版本把这句空白页提示当成一段正文种进编辑器，有的草稿里还留着它 */
export const WR_LEGACY_PLACEHOLDER = "在这里开始写这一场……";

/* 编辑器里一段也没有时放一个空段落：光标落在 <p> 里，敲下的字才有段落样式 */
export const WR_EMPTY_DOC = "<p><br></p>";

/* 只给界面用的 class：落盘前摘掉（消毒器也会剥属性，这里先摘是为了克隆出来的 DOM 干净可测） */
const UI_CLASSES = ["is-active", "is-fresh", "is-merge", "wr-dx-para", "wr-entity-flash", "wr-anno-flash"];
const UI_ATTRIBUTES = ["data-dx", "data-lib-id", "data-note", "data-orig", "title"];

function unwrap(node) {
  const parent = node.parentNode;
  if (!parent) return;
  while (node.firstChild) parent.insertBefore(node.firstChild, node);
  parent.removeChild(node);
}

/* 就地拆掉 root 里所有界面标记。span / mark 在正文里没有别的来源：消毒后它们不带任何属性，
   只剩「一层空壳」或「一块黄底」，所以整类拆掉，而不是只拆带 class 的那几个。 */
export function wrStripUiMarkup(root) {
  if (!root || !root.querySelectorAll) return root;
  Array.from(root.querySelectorAll("span, mark")).forEach(unwrap);
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

/* 去掉开头那句旧占位：整段都是占位就删段；作者接着占位往下写的，只删掉占位这几个字 */
function stripLeadingPlaceholder(root) {
  const blocks = Array.from(root.childNodes);
  for (const block of blocks) {
    const text = block.textContent || "";
    if (!text.trim()) continue;
    if (!text.trim().startsWith(WR_LEGACY_PLACEHOLDER)) return;
    if (text.trim() === WR_LEGACY_PLACEHOLDER) { block.parentNode.removeChild(block); return; }
    if (block.nodeType === 3) { block.nodeValue = text.trimStart().slice(WR_LEGACY_PLACEHOLDER.length); return; }
    const walker = root.ownerDocument.createTreeWalker(block, 4 /* NodeFilter.SHOW_TEXT */);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue.trim()) continue;
      const lead = node.nodeValue.trimStart();
      if (lead.startsWith(WR_LEGACY_PLACEHOLDER)) node.nodeValue = lead.slice(WR_LEGACY_PLACEHOLDER.length);
      return;
    }
    return;
  }
}

function hasText(root) {
  return !!String((root && root.textContent) || "").replace(/\s/g, "");
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
  Array.from(root.querySelectorAll("span, mark")).forEach(unwrap);
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

/* 一段里按拼接文字的偏移 [start, end) 取 Range；越界返回 null。
   偏移与标记怎么包无关（深改高亮、实体高亮拆开的文本节点拼回去是同一串字）。 */
export function wrRangeForOffsets(block, start, end) {
  if (!block || !block.ownerDocument || !(end > start) || start < 0) return null;
  const { nodes, joined } = blockText(block);
  if (!nodes.length || end > joined.length) return null;
  const range = block.ownerDocument.createRange();
  const locate = (offset, preferNext) => {
    for (let i = 0; i < nodes.length; i += 1) {
      const { node: current, start: from } = nodes[i];
      const stop = from + current.nodeValue.length;
      if (offset < stop || (offset === stop && !preferNext) || i === nodes.length - 1) {
        return { node: current, offset: Math.min(offset - from, current.nodeValue.length) };
      }
    }
    return null;
  };
  const from = locate(start, true);
  const to = locate(end, false);
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

/* 本场字数：去掉空白后的字符数（占位不在 DOM 里了，不必再特判） */
export function wrCountText(el) {
  if (!el) return 0;
  const text = el.innerText != null ? el.innerText : el.textContent;
  return String(text || "").replace(/\s/g, "").length;
}
