/* 正文 HTML 小工具的唯一住处（2026-09-29 前端共享层）：消毒、转义、拆空壳标记、旧占位句、
   HTML → 段落 / 纯文本，以及编辑器「一段」的选择器。纯函数模块，不读 store、不写 window；
   用到 document 的函数在没有 DOM 的环境里各自退回。 */

import { countChars } from "./lib/text.js";

const ALLOWED_MANUSCRIPT_TAGS = new Set([
  "P", "BR", "DIV", "SPAN", "STRONG", "B", "EM", "I", "U", "S", "STRIKE",
  "BLOCKQUOTE", "UL", "OL", "LI", "PRE", "CODE", "H1", "H2", "H3", "H4",
  "H5", "H6", "MARK", "SUB", "SUP",
]);

const DROP_WITH_CONTENT = new Set([
  "SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "SVG", "MATH", "TEMPLATE", "NOSCRIPT",
]);

const DROP_EMPTY = new Set([
  "IMG", "AUDIO", "VIDEO", "SOURCE", "TRACK", "LINK", "META", "BASE", "INPUT",
]);

/* 编辑器里「一段」是什么：后端 manuscript_html.manuscript_paragraphs 按同一条规则数段（契约），
   诊断发现的 paragraph_index 就是这个选择器在编辑器里命中的第几个。 */
export const MANUSCRIPT_BLOCK_SELECTOR = "p, blockquote";

/* 完整转义（& < > " '）：结果放进元素内容或属性值都安全。成稿导出与纯文本转正文用它。 */
export function escapeManuscriptText(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* 只转义 & < >：只放进元素内容（<p>…</p>）的纯文本用，引号原样保留，不能放进属性值。
   AI 起草台拼段落、写作台续写候选用它（与 escapeManuscriptText 的结果字面不同，所以分两个名字）。 */
export function escapeHtmlText(value) {
  return String(value == null ? "" : value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function sanitizeManuscriptHTML(value) {
  const raw = String(value == null ? "" : value);
  if (!raw || !raw.includes("<")) return raw;
  if (typeof document === "undefined") return escapeManuscriptText(raw);

  const template = document.createElement("template");
  template.innerHTML = raw;
  const clean = (parent) => {
    Array.from(parent.childNodes).forEach((node) => {
      if (node.nodeType !== 1) return;
      const tag = node.tagName;
      if (DROP_WITH_CONTENT.has(tag) || DROP_EMPTY.has(tag)) {
        node.remove();
        return;
      }
      clean(node);
      if (!ALLOWED_MANUSCRIPT_TAGS.has(tag)) {
        node.replaceWith(...Array.from(node.childNodes));
        return;
      }
      Array.from(node.attributes).forEach(attr => node.removeAttribute(attr.name));
    });
  };
  clean(template.content);
  return template.innerHTML;
}

/* 拆掉一个元素，把它的子节点原位留下；已经不在树上（没有父节点）的什么都不做。 */
export function unwrapNode(node) {
  const parent = node && node.parentNode;
  if (!parent) return;
  while (node.firstChild) parent.insertBefore(node.firstChild, node);
  parent.removeChild(node);
}

/* 就地拆掉 root 里所有 span / mark。它们在正文里没有别的来源：消毒后不带任何属性，
   只剩「一层空壳」（旧草稿每存一次包一层）或「一块黄底」，拆掉不丢任何字。 */
export function unwrapInline(root) {
  Array.from(root.querySelectorAll("span, mark")).forEach((node) => unwrapNode(node));
}

/* 旧版本写作台把空白页提示当成一段正文种进编辑器，有的草稿里还留着这句开场。
   主页、AI 起草台和写作台（WR_LEGACY_PLACEHOLDER 是它的转出名）都用这一份。 */
export const LEGACY_DRAFT_PLACEHOLDER = "在这里开始写这一场……";

/* 只认「开头」的那句：跳过空段，第一段整段是占位就删段，作者接着占位往下写的只删这几个字。
   正文后面恰好出现这句话，是作者的字，不动。就地改 root；写作台存盘 / 载入与这里的去占位同一条规则。 */
export function stripLeadingPlaceholder(root) {
  for (const block of Array.from(root.childNodes)) {
    const text = (block.textContent || "").trim();
    if (!text) continue;
    if (!text.startsWith(LEGACY_DRAFT_PLACEHOLDER)) return;
    if (text === LEGACY_DRAFT_PLACEHOLDER) { block.parentNode.removeChild(block); return; }
    if (block.nodeType === 3) { block.nodeValue = block.nodeValue.trimStart().slice(LEGACY_DRAFT_PLACEHOLDER.length); return; }
    const walker = root.ownerDocument.createTreeWalker(block, 4 /* NodeFilter.SHOW_TEXT */);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.nodeValue.trim()) continue;
      const lead = node.nodeValue.trimStart();
      if (lead.startsWith(LEGACY_DRAFT_PLACEHOLDER)) node.nodeValue = lead.slice(LEGACY_DRAFT_PLACEHOLDER.length);
      return;
    }
    return;
  }
}

/* 旧草稿 HTML → 去掉开头那句占位后的 HTML。文字里根本没有占位时原样返回（不重新序列化）。
   旧草稿每存一次包一层空 <span>，占位句可能被拆在几个文本节点里（「<span>在这里开始写</span>这一场……」）；
   和写作台载入时（wrPrepareLoadedHTML）一样，先拆掉 span / mark、合并相邻文本节点再判断。
   结果只拿来显示和判断「有没有作者的字」，拆掉这两种空壳不丢任何东西。 */
export function stripLegacyDraftPlaceholder(html) {
  const raw = String(html == null ? "" : html);
  if (!raw.includes("<") && !raw.includes("&")) {
    const lead = raw.trimStart();
    return lead.startsWith(LEGACY_DRAFT_PLACEHOLDER) ? lead.slice(LEGACY_DRAFT_PLACEHOLDER.length) : raw;
  }
  if (typeof document === "undefined") return raw;
  // template 里的内容是惰性的：不执行脚本、不加载图片
  const template = document.createElement("template");
  template.innerHTML = raw;
  const root = template.content;
  if (!(root.textContent || "").includes(LEGACY_DRAFT_PLACEHOLDER)) return raw;
  unwrapInline(root);
  root.normalize();
  stripLeadingPlaceholder(root);
  return template.innerHTML;
}

/* 这份草稿里有没有作者写下的字：消毒、去掉开头的旧占位，剩下的不是空白就算有。 */
export function hasAuthorText(html) {
  const raw = String(html == null ? "" : html);
  let text;
  if (typeof document === "undefined") {
    text = raw.replace(/<[^>]+>/g, "").trimStart();
    if (text.startsWith(LEGACY_DRAFT_PLACEHOLDER)) text = text.slice(LEGACY_DRAFT_PLACEHOLDER.length);
  } else {
    const template = document.createElement("template");
    template.innerHTML = stripLegacyDraftPlaceholder(sanitizeManuscriptHTML(raw));
    text = template.content.textContent || "";
  }
  return countChars(text) > 0;
}

export function manuscriptToDocHTML(value) {
  const content = String(value == null ? "" : value);
  if (!content) return "";
  if (/<\w+[^>]*>/.test(content)) return sanitizeManuscriptHTML(content);
  return content
    .split(/\n+/)
    .map(line => line.trim())
    .filter(Boolean)
    .map(line => `<p>${escapeManuscriptText(line)}</p>`)
    .join("");
}

/* 正文 HTML（或旧版纯文本）→ 段落文字数组：消毒后取 p / li 的文字；一个段落元素都没有时按换行切整段文字。
   纯文本（不带标签）直接按换行切。成稿中心「对比」与恢复中心的句级 diff 用它。 */
export function htmlToParagraphs(raw) {
  if (!raw) return [];
  if (!/<\w+[^>]*>/.test(raw)) return String(raw).split(/\n+/).map(x => x.trim()).filter(Boolean);
  const div = document.createElement("div");
  div.innerHTML = sanitizeManuscriptHTML(raw);
  let paras = Array.from(div.querySelectorAll("p, li")).map(p => (p.textContent || "").trim()).filter(Boolean);
  if (!paras.length) {
    const t = (div.textContent || "").trim();
    paras = t ? t.split(/\n+/).map(x => x.trim()).filter(Boolean) : [];
  }
  return paras;
}

/* 正文 HTML → 去掉首尾空白的纯文字（先消毒）。恢复中心的预览与「复制文字」用它。 */
export function htmlToPlainText(html) {
  const node = document.createElement("div");
  node.innerHTML = sanitizeManuscriptHTML(html || "");
  return (node.textContent || "").trim();
}
