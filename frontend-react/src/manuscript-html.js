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

export function escapeManuscriptText(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
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

/* 旧版本写作台把空白页提示当成一段正文种进编辑器，有的草稿里还留着这句开场。
   写作台自己的 WR_LEGACY_PLACEHOLDER 在惰性加载的写作台分包里；主页和 AI 起草台用这里这份，
   不为一句话把整个写作台拉进首屏。两处的字必须一模一样（历史上只出现过这一种写法）。 */
export const LEGACY_DRAFT_PLACEHOLDER = "在这里开始写这一场……";

/* 只认「开头」的那句：跳过空段，第一段整段是占位就删段，作者接着占位往下写的只删这几个字。
   正文后面恰好出现这句话，是作者的字，不动。规则与写作台载入时的处理一致。 */
function stripLeadingPlaceholder(root) {
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
  Array.from(root.querySelectorAll("span, mark")).forEach((node) => {
    while (node.firstChild) node.parentNode.insertBefore(node.firstChild, node);
    node.parentNode.removeChild(node);
  });
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
  return text.replace(/\s/g, "").length > 0;
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
