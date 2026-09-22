import React from "react";

/* ==========================================================
   教练回复的轻量排版（只认几样，其余一律原样当文字）
   ----------------------------------------------------------
   教练（snowflake_workspace_assistant）的回复常带 markdown：**要点**、*小标签*、
   「1. …」编号条目，条目下面再缩进一层「- …」。过去整段塞进一个 <p>，星号和减号原样印在页面上，
   换行也被折成一行。这里只认：
     · 段落（空行分段；段内换行保留为换行）
     · **粗体**、*斜体*（CommonMark 的定界符栈：收尾星号配最近的开头星号；星号内侧不能是空白，所以「2 * 3 * 4」不是斜体）
     · 无序列表（- / * / + / •）与有序列表（1. / 1) / 1、），按缩进嵌套；条目之间隔空行仍是同一张表
       （「04、06 两步还空着」里的 、 后面紧跟数字，是并列，不是编号）
   标题、引用、代码、表格、链接、HTML 都不认，原样当文字显示。
   安全：只产出 React 元素和字符串子节点，从不走 innerHTML——回复里像 HTML 的文字（<img onerror=…>）照样是文字。
   纯 ESM，不写 window。
   ========================================================== */

const UL_RE = /^(\s*)[-*+•]\s+(.*)$/;
/* 「N、」后面紧跟数字不算编号：教练常说「04、06 两步」「3、4 月之间」，当成列表会把前一个数吞进编号里 */
const OL_RE = /^(\s*)(\d{1,3})(?:[.)]\s+|、(?!\s*\d)\s*)(.*)$/;
/* 比上一层条目多缩进 2 格以上才算下一层（模型常用 2 / 3 / 4 格） */
const NEST_INDENT = 2;

const indentOf = (line) => {
  let n = 0;
  for (const ch of line) {
    if (ch === " ") n += 1;
    else if (ch === "\t") n += 4;
    else break;
  }
  return n;
};
const isBlank = (line) => !line || !line.trim();

function listItemOf(line) {
  const ol = OL_RE.exec(line);
  if (ol && ol[3].trim()) return { indent: indentOf(ol[1]), ordered: true, num: Number(ol[2]), text: ol[3].trim() };
  const ul = UL_RE.exec(line);
  if (ul && ul[2].trim()) return { indent: indentOf(ul[1]), ordered: false, num: 1, text: ul[2].trim() };
  return null;
}

/* 从 start 起的一段列表：条目、续行（比首条缩进更深的非条目行）、条目之间的空行。
   遇到不缩进的普通行就结束——列表后面紧跟的一句总结是新段落，不是最后一条的续行。 */
function parseList(lines, start) {
  const baseIndent = listItemOf(lines[start]).indent;
  const top = [];
  const stack = [];
  let last = null;
  let i = start;
  for (; i < lines.length; i += 1) {
    const line = lines[i];
    if (isBlank(line)) {
      let j = i + 1;
      while (j < lines.length && isBlank(lines[j])) j += 1;
      if (j >= lines.length) break;
      const next = lines[j];
      if (!listItemOf(next) && indentOf(next) <= baseIndent) break;
      continue;
    }
    const item = listItemOf(line);
    if (!item) {
      if (indentOf(line) <= baseIndent) break;
      if (last) last.lines.push(line.trim());
      continue;
    }
    const node = { lines: [item.text], lists: [] };
    while (stack.length > 1 && item.indent < stack[stack.length - 1].indent) stack.pop();
    const level = stack[stack.length - 1];
    if (level && level.list.items.length && item.indent >= level.indent + NEST_INDENT) {
      const owner = level.list.items[level.list.items.length - 1];
      const prev = owner.lists[owner.lists.length - 1];
      const list = prev && prev.ordered === item.ordered ? prev : { ordered: item.ordered, start: item.num, items: [] };
      if (list !== prev) owner.lists.push(list);
      stack.push({ indent: item.indent, list, container: owner.lists });
    } else if (!level || level.list.ordered !== item.ordered) {
      // 同一层换了列表种类（- 之后接 1.）：另起一张表，和 CommonMark 一样
      const list = { ordered: item.ordered, start: item.num, items: [] };
      const container = level ? level.container : top;
      container.push(list);
      if (level) level.list = list;
      else stack.push({ indent: item.indent, list, container });
    }
    stack[stack.length - 1].list.items.push(node);
    last = node;
  }
  return { lists: top, next: i };
}

/* 块级：[{ type: "p", lines: [..] } | { type: "list", ordered, start, items: [{ lines, lists }] }] */
export function parseCoachReply(text) {
  const lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let para = [];
  const flush = () => { if (para.length) blocks.push({ type: "p", lines: para }); para = []; };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) { flush(); i += 1; continue; }
    if (listItemOf(line)) {
      flush();
      const { lists, next } = parseList(lines, i);
      lists.forEach(list => blocks.push({ type: "list", ...list }));
      i = next;
      continue;
    }
    para.push(line.trim());
    i += 1;
  }
  flush();
  return blocks;
}

const isSpace = (ch) => ch === undefined || /\s/.test(ch);

/* ---------- 行内：CommonMark 的定界符栈（process emphasis），只认星号 ----------
   先把一行切成文字节点和「星号串」节点，每个星号串记下能不能开头（后一个字不是空白）、能不能收尾（前一个字不是空白）。
   CommonMark 还要看两边的标点，中文里「**「开场」**：」这种写法就会失效，所以这里只看空白。
   然后从左往右找收尾串，往回配最近的能开头的串：
     · 两边都至少两颗 → 粗体，否则斜体，用掉的星号从内侧扣
     · 「三的倍数」规则：一边既能开头又能收尾时，两串原长之和是 3 的倍数（且不都是 3 的倍数）就不配，
       所以「*甲**乙*」是一整段斜体，不是在 ** 处断开
     · 配不上的收尾串记下往回找的下界（openers_bottom），同一类收尾串下次不再往下翻——整趟是线性的
   以前的写法是「最早的开头星号赢」且每个开头都往后重扫，「价格 5*3=15，面积 *4*」会吞掉乘号，
   一行怪星号能把输入框卡住好几秒。节点用双向链表，套一层强调只改几个指针。 */
function tokenizeInline(src) {
  const head = { next: null };
  let tail = head;
  const append = (node) => { node.prev = tail === head ? null : tail; tail.next = node; tail = node; return node; };
  const delims = [];
  let textStart = 0;
  let i = src.indexOf("*");
  while (i >= 0) {
    let j = i;
    while (src[j] === "*") j += 1;
    if (i > textStart) append({ kind: "text", text: src.slice(textStart, i), next: null });
    const node = append({ kind: "text", text: src.slice(i, j), next: null });
    const canOpen = !isSpace(src[j]);
    const canClose = !isSpace(src[i - 1]);
    if (canOpen || canClose) delims.push({ node, count: j - i, orig: j - i, canOpen, canClose, prev: null, next: null });
    textStart = j;
    i = src.indexOf("*", j);
  }
  if (textStart < src.length) append({ kind: "text", text: src.slice(textStart), next: null });
  for (let k = 0; k < delims.length; k += 1) {
    delims[k].prev = delims[k - 1] || null;
    delims[k].next = delims[k + 1] || null;
  }
  return { first: head.next, delims: delims[0] || null };
}

function removeDelim(d) {
  if (d.prev) d.prev.next = d.next;
  if (d.next) d.next.prev = d.prev;
}

function processEmphasis(firstDelim) {
  const bottom = {};
  let closer = firstDelim;
  while (closer) {
    if (!closer.canClose) { closer = closer.next; continue; }
    const key = (closer.canOpen ? 3 : 0) + (closer.orig % 3);
    let opener = closer.prev;
    const floor = bottom[key] === undefined ? null : bottom[key];
    while (opener && opener !== floor) {
      const oddMatch = (closer.canOpen || opener.canClose)
        && (opener.orig + closer.orig) % 3 === 0
        && !(opener.orig % 3 === 0 && closer.orig % 3 === 0);
      if (opener.canOpen && !oddMatch) break;
      opener = opener.prev;
    }
    if (!opener || opener === floor) {
      bottom[key] = closer.prev;
      const next = closer.next;
      if (!closer.canOpen) removeDelim(closer);
      closer = next;
      continue;
    }
    const use = closer.count >= 2 && opener.count >= 2 ? 2 : 1;
    opener.count -= use;
    closer.count -= use;
    const o = opener.node;
    const c = closer.node;
    o.text = o.text.slice(use);
    c.text = c.text.slice(use);
    // 开头串和收尾串之间的节点整段挂到新的强调节点下面
    const wrap = { kind: use === 2 ? "strong" : "em", first: null, next: c, prev: o };
    if (o.next !== c) {
      wrap.first = o.next;
      wrap.first.prev = null;
      c.prev.next = null;
    }
    o.next = wrap;
    c.prev = wrap;
    // 夹在中间的定界符都作废，留作文字
    opener.next = closer;
    closer.prev = opener;
    if (opener.count === 0) removeDelim(opener);
    if (closer.count === 0) {
      const next = closer.next;
      removeDelim(closer);
      closer = next;
    }
  }
}

/* 强调最多套这么多层；再往里的原样还原成带星号的文字。
   真实回复最多套两三层（***甲***），但「*a *a … a* a*」这种行能套上万层，递归转换和渲染都会爆栈。 */
const MAX_NEST = 6;

/* 一棵强调子树还原成源文字（显式栈，不递归） */
function literalOf(first) {
  let s = "";
  const stack = [{ n: first, close: "" }];
  while (stack.length) {
    const top = stack[stack.length - 1];
    const n = top.n;
    if (!n) { s += top.close; stack.pop(); continue; }
    top.n = n.next;
    if (n.kind === "text") s += n.text;
    else {
      const d = n.kind === "strong" ? "**" : "*";
      s += d;
      stack.push({ n: n.first, close: d });
    }
  }
  return s;
}

/* 链表 → [string | { type, children }]；相邻文字合并，扣光的星号串丢掉 */
function toTree(first, depth = 0) {
  const out = [];
  const pushText = (text) => {
    if (!text) return;
    if (typeof out[out.length - 1] === "string") out[out.length - 1] += text;
    else out.push(text);
  };
  for (let n = first; n; n = n.next) {
    if (n.kind === "text") pushText(n.text);
    else if (depth >= MAX_NEST) {
      const d = n.kind === "strong" ? "**" : "*";
      pushText(d + literalOf(n.first) + d);
    } else out.push({ type: n.kind, children: toTree(n.first, depth + 1) });
  }
  return out;
}

/* 行内：[string | { type: "strong" | "em", children: [...] }] */
export function parseCoachInline(text) {
  const src = String(text == null ? "" : text);
  if (!src.includes("*")) return src ? [src] : [];
  const { first, delims } = tokenizeInline(src);
  processEmphasis(delims);
  return toTree(first);
}

function renderInline(nodes) {
  return nodes.map((n, i) => {
    if (typeof n === "string") return <React.Fragment key={i}>{n}</React.Fragment>;
    const Tag = n.type === "strong" ? "strong" : "em";
    return <Tag key={i}>{renderInline(n.children)}</Tag>;
  });
}

/* 一行一行：段内换行保留为 <br /> */
function renderLines(lines) {
  return lines.map((line, i) => (
    <React.Fragment key={i}>
      {i > 0 && <br />}
      {renderInline(parseCoachInline(line))}
    </React.Fragment>
  ));
}

function renderList(list, key) {
  const Tag = list.ordered ? "ol" : "ul";
  return (
    <Tag key={key} className="sf-coach-md-list" start={list.ordered && list.start !== 1 ? list.start : undefined}>
      {list.items.map((item, i) => (
        <li key={i}>
          {renderLines(item.lines)}
          {item.lists.map((sub, j) => renderList(sub, j))}
        </li>
      ))}
    </Tag>
  );
}

/* 两个组件都 memo：教练日志在 S2Coach 里，输入框每敲一个字整页重渲染一遍，没变的回复不该每次重新解析 */

/* 一段只有行内排版的文字（教练的建议条目） */
export const CoachInline = React.memo(function CoachInline({ text }) {
  return <React.Fragment>{renderInline(parseCoachInline(text))}</React.Fragment>;
});

/* 教练回复：直接产出块级元素，落在 .sf-coach-body 里（段落沿用 .sf-coach-body p，块间距沿用它的 gap） */
export const CoachReply = React.memo(function CoachReply({ text }) {
  const blocks = parseCoachReply(text);
  return (
    <React.Fragment>
      {blocks.map((b, i) => (b.type === "p"
        ? <p key={i}>{renderLines(b.lines)}</p>
        : renderList(b, i)))}
    </React.Fragment>
  );
});
