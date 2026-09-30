import { manuscriptToDocHTML, sanitizeManuscriptHTML, stripLeadingPlaceholder } from "./manuscript-html.js";
import { wsKey } from "./ws-works.jsx";
import { activeWorkId, storageFailure } from "./wr-recovery-store.js";

/* ==========================================================
   写作台正文的本机这一层（2026-09-30 W1 从 wr-doc-sync.js 拆出）：读缓存 wr-doc:<sid>、未同步标记
   wr-doc-pending:<sid>（都带 ::<作品 id> 后缀）和会话内存里的那一份，再加「两份正文是不是同一段文字」。
   不发请求、不管保存状态（那是 wr-doc-sync.js 的状态机）。scene = { workId, sid }——WrDocs 每一场的状态就是这个形状。
   本机存储里的读缓存键，同一浏览器的几个标签页共用一份；会话内存里那一份是这个标签页自己的（读时优先）：
   这一页打开过、写过的场，之后这一页读到的都是它自己的那一份（复核二 W1-R2B-1）。
   ESM 模块，不写 window。
   ========================================================== */

const volatileDocs = new Map();   // 作品id::sid → 这个标签页自己的那一份（读时优先；配额不足时它是唯一的一份）

function memoryKeyOf(workId, sid) {
  return `${workId}::${sid}`;
}

/* 本机键：当前作品下就是 wsKey 的结果；异步收尾时作品已经换了，按这一场自己的作品拼（绝不写到另一部作品名下） */
function storageKeyFor(workId, sid, base) {
  return workId === activeWorkId() ? wsKey(base + sid) : `${base}${sid}::${workId}`;
}

function pendingKeyOf(m) {
  return storageKeyFor(m.workId, m.sid, "wr-doc-pending:");
}

function pendingRead(m) {
  try { return localStorage.getItem(pendingKeyOf(m)); } catch (e) { return null; }
}

/* 草稿 id 只记尾巴（后端的 id 很长，尾巴是随机的那一截）：标记要短，本机存储满了时也写得进去 */
function draftTail(draftId) {
  return String(draftId || "").slice(-12);
}

/* 一段正文的指纹：字数 + 文字（连同分段、空行、格式；写法不同算一样，见 docText）的散列。未同步标记用它记下自己说的是哪一段字 */
function textFingerprint(html) {
  const text = docText(html);
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `${text.length.toString(36)}.${hash.toString(36)}`;
}

/* 未同步标记里记下的：共用读缓存里那一稿写在服务端哪一版上（draft / revision，不知道时都是 null）、是哪一段字（fp，
   没有指纹的旧版标记是 null）、这一场的后端 scene_id（sceneId，不知道 / 旧版标记是 null）、标记是什么时候写的（at，毫秒；
   读不出来是 null）。没有标记 → null */
function pendingBase(m) {
  const raw = pendingRead(m);
  if (!raw) return null;
  const [time, revision, draft, fp, sceneId] = String(raw).split("|");
  const rev = Number(revision);
  const known = !!draft && revision !== "" && Number.isInteger(rev);
  const at = Number(time);
  return {
    draft: known ? draft : null,
    revision: known ? rev : null,
    fp: fp || null,
    sceneId: sceneId || null,
    at: Number.isFinite(at) && at > 0 ? at : null,
  };
}

/* 标记里的后端 scene_id 这一栏（去掉分隔符；不知道是空） */
function sceneField(sceneId) {
  return sceneId ? String(sceneId).replace(/\|/g, "") : "";
}

/* 未同步标记：dirty 只在内存，重启浏览器即丢；标记跨会话存活，下次水合据此先留冲突副本，不让服务端旧稿静默盖掉本机较新的稿。
   值是「时刻|修订号|草稿 id 的尾巴|指纹|后端 scene_id」：共用读缓存里那一稿（html；没给就是眼下存在那里的那一份）是哪一段字、写在服务端
   哪一版上（base { draftId 或 draft, revision }）——下次打开时服务端若还停在那一版、作者又正是在这段字上接着写的，那就是那一版的
   下一稿，照常保存。不知道写在哪一版上（base 为空）就空着，不沿用原来那个标记的：它说的是另一段字（复核四 W1-R4A-2）。
   后端 scene_id 取 m.sceneId（知道时）：乐观新建的场先用临时 sid，刷新之后（或另一个标签页里）目录不再认得那个名字，凭它还能找到
   这一场（复核七 W1-R7A-3）。
   → { ok, error }：本机存储满了写不进去时 ok=false——调用方据此决定这一稿算不算在本机落了地（复核四 W1-R4B-1） */
function pendingWrite(m, base, html) {
  const text = html === undefined ? readStored(m.workId, m.sid) : html;
  const draft = base ? (base.draft || draftTail(base.draftId)) : "";
  const known = !!draft && Number.isInteger(base && base.revision);
  const value = `${Date.now()}|${known ? base.revision : ""}|${known ? draft : ""}|${textFingerprint(text)}|${sceneField(m.sceneId)}`;
  try {
    localStorage.setItem(pendingKeyOf(m), value);
    return { ok: true, error: null };
  } catch (error) {
    return { ok: false, error: storageFailure(error) };
  }
}

/* 标记写下时还不知道这一场的后端 scene_id（乐观新建的场，建场的回包还没回来），之后知道了：补进去，别的不动 */
function pendingStampScene(m, sceneId) {
  const raw = pendingRead(m);
  if (!raw || !sceneId) return;
  const parts = String(raw).split("|");
  if (parts[4]) return;
  while (parts.length < 4) parts.push("");
  parts[4] = sceneField(sceneId);
  try { localStorage.setItem(pendingKeyOf(m), parts.join("|")); } catch (e) { /* 写不进去：标记照旧，只是刷新之后凭它找不到这一场 */ }
}

/* 把未同步标记放回先前那个值（raw 为 null = 先前没有标记）：新的那一稿没写进读缓存，标记还得说那里原有的那一份 */
function pendingRestore(m, raw) {
  try {
    if (raw == null) localStorage.removeItem(pendingKeyOf(m));
    else localStorage.setItem(pendingKeyOf(m), raw);
  } catch (e) { /* 放不回去：标记说的是没写进去的那一稿，下次打开时照样当成没同步上的留一份，不会盖掉谁 */ }
}

function pendingClear(m) {
  try { localStorage.removeItem(pendingKeyOf(m)); } catch (e) {}
}

function readStored(workId, sid) {
  try {
    const value = localStorage.getItem(storageKeyFor(workId, sid, "wr-doc:"));
    return value == null ? null : value;
  } catch (e) {
    return null;
  }
}

/* 这个标签页读这一场：先读自己的那一份（会话内存）；这一页还没碰过这一场时，读本机存储里共用的那一份 */
function readCache(workId, sid) {
  const memoryKey = memoryKeyOf(workId, sid);
  if (volatileDocs.has(memoryKey)) return volatileDocs.get(memoryKey);
  return readStored(workId, sid);
}

/* 本机存储里共用的那一份（不看这一页的会话内存）：未同步标记说的就是它，另一个标签页写进去的字也在这里 */
function readSlot(m) {
  return readStored(m.workId, m.sid);
}

/* 只记进这一页的会话内存（这一页之后读这一场，读到的就是它），本机存储不动 */
function rememberInSession(m, html) {
  volatileDocs.set(memoryKeyOf(m.workId, m.sid), html == null ? null : html);
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

/* 这一场在本机这一层的键：共用读缓存与未同步标记 */
function sceneKeys(workId, sid) {
  return { slot: storageKeyFor(workId, sid, "wr-doc:"), mark: storageKeyFor(workId, sid, "wr-doc-pending:") };
}

/* 这一场换了名字（乐观新建的场：临时 sid → 后端建好之后稳定的 scene_id，见 WrDocs 的 renameMeta）：会话内存里这一页的那一份、
   共用读缓存和未同步标记搬到新名字下。新名字下已经有本机稿（另一个标签页先打开了它）时不盖它：旧名字下只是读缓存（没有
   未同步标记）就扔掉，标着未同步的原样留着；本机存储满了、搬不动时也原样留着。
   → { moved, left }：moved = 本机存储里的那一份跟到了新名字下（或本来就没有）；left = 旧名字下没搬过去、标着未同步的那一份
   （调用方先把它留进同步与恢复），没有就是 null */
function renameSceneKeys(workId, from, to) {
  const fromMemory = memoryKeyOf(workId, from);
  const toMemory = memoryKeyOf(workId, to);
  if (volatileDocs.has(fromMemory)) {
    if (!volatileDocs.has(toMemory)) volatileDocs.set(toMemory, volatileDocs.get(fromMemory));
    volatileDocs.delete(fromMemory);
  }
  const src = sceneKeys(workId, from);
  const dst = sceneKeys(workId, to);
  let slot = null;
  let mark = null;
  try {
    slot = localStorage.getItem(src.slot);
    mark = localStorage.getItem(src.mark);
  } catch (e) {
    return { moved: false, left: null };
  }
  if (slot == null && mark == null) return { moved: true, left: null };
  let free = false;
  try { free = localStorage.getItem(dst.slot) == null && localStorage.getItem(dst.mark) == null; } catch (e) { free = false; }
  if (free) {
    try {
      if (slot != null) localStorage.setItem(dst.slot, slot);
      if (mark != null) localStorage.setItem(dst.mark, mark);
      localStorage.removeItem(src.slot);
      localStorage.removeItem(src.mark);
      return { moved: true, left: null };
    } catch (e) {
      // 写不进去：新名字下退回原来的空，旧名字下的原样留着
      try { localStorage.removeItem(dst.slot); localStorage.removeItem(dst.mark); } catch (err) {}
      return { moved: false, left: mark != null ? slot : null };
    }
  }
  if (mark == null) {
    try { localStorage.removeItem(src.slot); } catch (e) {}
    return { moved: false, left: null };
  }
  return { moved: false, left: slot };
}

/* 扔掉这一场在本机这一层的一切（会话内存、共用读缓存、未同步标记）：它不在目录里了、没同步上的字已经持久地留进了同步与恢复 */
function dropSceneKeys(scene) {
  volatileDocs.delete(memoryKeyOf(scene.workId, scene.sid));
  const keys = sceneKeys(scene.workId, scene.sid);
  try {
    localStorage.removeItem(keys.slot);
    localStorage.removeItem(keys.mark);
  } catch (e) {}
}

/* 这部作品在本机存储里标着未同步的那几场（sid）：上次会话留下的也在内 */
function pendingSids(workId) {
  const prefix = "wr-doc-pending:";
  const suffix = `::${workId}`;
  const sids = [];
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.startsWith(prefix) && key.endsWith(suffix) && key.length > prefix.length + suffix.length) {
        sids.push(key.slice(prefix.length, key.length - suffix.length));
      }
    }
  } catch (e) {}
  return sids;
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

/* 空了一行：没有字的一块（<p><br></p>、<p></p>、只有空格的一段），同一段里紧挨着的第二个 <br>。
   几处连着的算一处，开头、结尾的不算（编辑器末尾常多一个空段）——别的设备只在两段之间加了一个空行，也是改了
   （复核二 W1-R2B-5）。前后端的消毒都原样留着空段，存上去再读回来不会多出、少掉空行。 */
const BLANK_LINE = "\u0003";
const BLOCK_TAGS = new Set(["P", "DIV", "BLOCKQUOTE", "LI", "PRE", "H1", "H2", "H3", "H4", "H5", "H6"]);

/* 排版上的空白：ASCII 空白与不换行空格（编辑器会把连着的、行尾的空格写成 &nbsp;）——归一成一个空格，行首行尾去掉。
   全角空格（U+3000，中文稿的段首缩进）等别的空白是作者敲下的字，原样算：别的设备只加了段首缩进，也是改了（复核二 W1-R2B-5）。 */
const SOFT_SPACE_RUN = /[ \t\n\r\f\v ]+/g;
const SOFT_SPACE_EDGE = /^[ \t\n\r\f\v ]+|[ \t\n\r\f\v ]+$/g;
const VISIBLE = /[^ \t\n\r\f\v ]/;

function formatKey(active) {
  return Object.keys(active).filter((kind) => active[kind] > 0).sort().join(",");
}

/* 按文档序收集文字：块的边界记一次换行；每一段有字的文字前面，格式（行内 + 所在的块）和前一段不同时记一个格式标记。
   只看「这几个字是什么格式」，不看标记怎么嵌套、拆成几个节点（<b>甲</b><b>乙</b> 与 <b>甲乙</b> 一样）。
   state.breaks：这一段里、上一处有字的地方之后已经过了几个 <br>。 */
function collectText(node, out, state) {
  node.childNodes.forEach((child) => {
    if (child.nodeType === 3) {
      if (VISIBLE.test(child.nodeValue)) {
        const key = formatKey(state.active);
        if (key !== state.last) { out.push(FORMAT_MARK + key + FORMAT_END); state.last = key; }
        state.breaks = 0;
      }
      out.push(child.nodeValue);
      return;
    }
    if (child.nodeType !== 1) return;
    if (child.tagName === "BR") {
      if (state.breaks > 0) out.push(`\n${BLANK_LINE}\n`); // 紧挨着上一个 <br>：空了一行
      state.breaks += 1;
      out.push("\n");
      state.last = "";
      return;
    }
    const breaks = LINE_BREAK_TAGS.has(child.tagName);
    const format = FORMAT_OF_TAG[child.tagName];
    if (breaks) { out.push("\n"); state.last = ""; state.breaks = 0; }
    if (format) state.active[format] = (state.active[format] || 0) + 1;
    collectText(child, out, state);
    if (format) state.active[format] -= 1;
    if (breaks) {
      if (BLOCK_TAGS.has(child.tagName) && !VISIBLE.test(child.textContent || "")) out.push(`\n${BLANK_LINE}\n`);
      out.push("\n");
      state.last = "";
      state.breaks = 0;
    }
  });
}

/* 一行：排版上的空白归一；格式标记两边的空白挪到标记外面（「甲 <b>乙</b>」与「甲<b> 乙</b>」一样） */
function normalizeLine(line) {
  return line
    .replace(SOFT_SPACE_RUN, " ")
    .replace(/ ?(\u0001[^\u0002]*\u0002) ?/g, (match, mark) => (match.length > mark.length ? " " : "") + mark)
    .replace(SOFT_SPACE_EDGE, "");
}

/* 一份正文的文字、分段、空行与格式（不看标记怎么写：服务端消毒后的写法可能和本机的不一样；开头的旧占位句不算字） */
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
    collectText(template.content, out, { active: {}, last: "", breaks: 0 });
    joined = out.join("");
  }
  const lines = [];
  joined.split("\n").map(normalizeLine).filter(Boolean).forEach((line) => {
    if (line !== BLANK_LINE) lines.push(line);
    else if (lines.length && lines[lines.length - 1] !== BLANK_LINE) lines.push(line);
  });
  while (lines.length && lines[lines.length - 1] === BLANK_LINE) lines.pop();
  return lines.join("\n");
}

/* 两份正文是不是同一段文字（字、分段、空行、格式都一样）。写作台用它判断「读到的新版本是不是作者正在写的底稿」。 */
function sameManuscriptText(a, b) {
  return docText(a) === docText(b);
}

export {
  cacheRead, cacheReadForWork, cacheWrite, docText, draftTail, dropSceneKeys, pendingBase, pendingClear, pendingRead, pendingRestore,
  pendingSids, pendingStampScene, pendingWrite, readCache, readSlot, rememberInSession, renameSceneKeys, sameManuscriptText,
  textFingerprint, toDocHTML,
};
