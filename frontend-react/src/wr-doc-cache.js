import { manuscriptToDocHTML, sanitizeManuscriptHTML, stripLeadingPlaceholder } from "./manuscript-html.js";
import { wsKey } from "./ws-works.jsx";
import { activeWorkId, storageFailure } from "./wr-recovery-store.js";

/* ==========================================================
   写作台正文的本机这一层（2026-09-30 W1 从 wr-doc-sync.js 拆出）：读缓存 wr-doc:<sid>、未同步标记
   wr-doc-pending:<sid>（都带 ::<作品 id> 后缀）和会话内存里的那一份，再加「两份正文是不是同一段文字」。
   不发请求、不管保存状态（那是 wr-doc-sync.js 的状态机）。scene = { workId, sid }——WrDocs 每一场的状态就是这个形状。
   ESM 模块，不写 window。
   ========================================================== */

const volatileDocs = new Map();   // 作品id::sid → 本次会话里的读缓存（读时优先；配额不足时它是唯一的一份）

function memoryKeyOf(workId, sid) {
  return `${workId}::${sid}`;
}

/* 本机键：当前作品下就是 wsKey 的结果；异步收尾时作品已经换了，按这一场自己的作品拼（绝不写到另一部作品名下） */
function storageKeyFor(workId, sid, base) {
  return workId === activeWorkId() ? wsKey(base + sid) : `${base}${sid}::${workId}`;
}

function pendingRead(m) {
  try { return localStorage.getItem(storageKeyFor(m.workId, m.sid, "wr-doc-pending:")); } catch (e) { return null; }
}
/* 未同步标记：dirty 只在内存，重启浏览器即丢；标记跨会话存活，下次水合据此先留冲突副本，不让服务端旧稿静默盖掉本机较新的稿 */
function pendingWrite(m) {
  try { localStorage.setItem(storageKeyFor(m.workId, m.sid, "wr-doc-pending:"), String(Date.now())); } catch (e) {}
}
function pendingClear(m) {
  try { localStorage.removeItem(storageKeyFor(m.workId, m.sid, "wr-doc-pending:")); } catch (e) {}
}

function readCache(workId, sid) {
  const memoryKey = memoryKeyOf(workId, sid);
  if (volatileDocs.has(memoryKey)) return volatileDocs.get(memoryKey);
  try {
    const value = localStorage.getItem(storageKeyFor(workId, sid, "wr-doc:"));
    return value == null ? null : value;
  } catch (e) {
    return null;
  }
}

/* 写读缓存。消毒只在这里做一次：之后发 PATCH 的就是这里返回的 html。
   durable=false 只写会话内存、本机存储里那一份不动——冲突副本放不进本机存储时，本机稿还留在本机缓存里，
   刷新后能再走一次冲突副本。 */
function cacheWrite(m, html, { durable = true } = {}) {
  const safeHTML = sanitizeManuscriptHTML(html);
  volatileDocs.set(memoryKeyOf(m.workId, m.sid), safeHTML);
  if (!durable) return { ok: false, error: null, html: safeHTML };
  try {
    localStorage.setItem(storageKeyFor(m.workId, m.sid, "wr-doc:"), safeHTML);
    return { ok: true, error: null, html: safeHTML };
  } catch (error) {
    return { ok: false, error: storageFailure(error), html: safeHTML };
  }
}

function cacheRead(sid) {
  return readCache(activeWorkId(), sid);
}

// 恢复中心会同时列出多部作品的记录。查看另一部作品时，差异必须和
// 那部作品自己的缓存比较，不能误拿当前作品的同名 sid 当基线。
function cacheReadForWork(sid, workId) {
  return readCache(workId || activeWorkId(), sid);
}

/* 文本 → 文档 HTML（服务端草稿以 \n 分段；写作器编辑器吃 <p> 段落） */
function toDocHTML(content) {
  return manuscriptToDocHTML(content);
}

/* 断行的块：它们的边界算一次换行（一段套在 <div> 里、用 <br> 分行，和并排的 <p> 是同一段文字） */
const LINE_BREAK_TAGS = new Set(["P", "DIV", "BLOCKQUOTE", "LI", "UL", "OL", "PRE", "H1", "H2", "H3", "H4", "H5", "H6", "BR"]);

/* 格式也是正文：别的设备只改了格式（加了斜体、一段改成引文），也是改了。同一种格式的几种写法算一样
   （<b> 与 <strong>、<i> 与 <em>、<s> 与 <strike>）；span / mark 是空壳和黄底（载入时就拆掉），p / div 是普通的一段，都不算。 */
const FORMAT_OF_TAG = {
  B: "b", STRONG: "b", I: "i", EM: "i", U: "u", S: "s", STRIKE: "s", CODE: "code", SUB: "sub", SUP: "sup",
  BLOCKQUOTE: "#quote", LI: "#li", OL: "#ol", PRE: "#pre",
  H1: "#h1", H2: "#h2", H3: "#h3", H4: "#h4", H5: "#h5", H6: "#h6",
};
const FORMAT_MARK = "\u0001";
const FORMAT_END = "\u0002";

function formatKey(active) {
  return Object.keys(active).filter((kind) => active[kind] > 0).sort().join(",");
}

/* 按文档序收集文字：块的边界记一次换行；每一段有字的文字前面，格式（行内 + 所在的块）和前一段不同时记一个格式标记。
   只看「这几个字是什么格式」，不看标记怎么嵌套、拆成几个节点（<b>甲</b><b>乙</b> 与 <b>甲乙</b> 一样）。 */
function collectText(node, out, state) {
  node.childNodes.forEach((child) => {
    if (child.nodeType === 3) {
      if (/\S/.test(child.nodeValue)) {
        const key = formatKey(state.active);
        if (key !== state.last) { out.push(FORMAT_MARK + key + FORMAT_END); state.last = key; }
      }
      out.push(child.nodeValue);
      return;
    }
    if (child.nodeType !== 1) return;
    const breaks = LINE_BREAK_TAGS.has(child.tagName);
    const format = FORMAT_OF_TAG[child.tagName];
    if (breaks) { out.push("\n"); state.last = ""; }
    if (format) state.active[format] = (state.active[format] || 0) + 1;
    collectText(child, out, state);
    if (format) state.active[format] -= 1;
    if (breaks) { out.push("\n"); state.last = ""; }
  });
}

/* 一行：空白归一；格式标记两边的空白挪到标记外面（「甲 <b>乙</b>」与「甲<b> 乙</b>」一样） */
function normalizeLine(line) {
  return line
    .replace(/\s+/g, " ")
    .replace(/ ?(\u0001[^\u0002]*\u0002) ?/g, (match, mark) => (match.length > mark.length ? " " : "") + mark)
    .trim();
}

/* 一份正文的文字、分段与格式（不看标记怎么写：服务端消毒后的写法可能和本机的不一样；开头的旧占位句不算字） */
function docText(html) {
  const clean = toDocHTML(html == null ? "" : html);
  if (!clean) return "";
  let joined;
  if (typeof document === "undefined") {
    joined = clean.replace(/<[^>]*>/g, "\n");
  } else {
    const template = document.createElement("template");
    template.innerHTML = clean;
    stripLeadingPlaceholder(template.content);
    const out = [];
    collectText(template.content, out, { active: {}, last: "" });
    joined = out.join("");
  }
  return joined.split("\n").map(normalizeLine).filter(Boolean).join("\n");
}

/* 两份正文是不是同一段文字（字、分段、格式都一样）。写作台用它判断「读到的新版本是不是作者正在写的底稿」。 */
function sameManuscriptText(a, b) {
  return docText(a) === docText(b);
}

export {
  cacheRead, cacheReadForWork, cacheWrite, docText, pendingClear, pendingRead, pendingWrite, readCache,
  sameManuscriptText, toDocHTML,
};
