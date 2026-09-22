import React from "react";
import { I } from "./icons.jsx";
import { navigateWithViewIntent } from "./ws-view-intents.js";
import { useWrEvent } from "./ws-writer-hooks.js";
import { isImeComposing } from "./ws-dialog.jsx";

/* ==========================================================
   档案实体 — 正文里已登记的人物 / 地点 / 术语（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   · wrHighlightEntities(root)：把正文中档案名标成 span.wr-entity（落盘前由
     wrSerializeManuscript 拆掉，存下去的只有字）。
   · useWrEntities：悬停看档案摘要、点击直达档案；从档案「在正文中定位」跳来时滚动并闪一下。
   · useWrMention + WrMentionPicker：正文里敲 @ 唤出档案选择器，插入一处引用。
   档案数据运行时读 window.LIB_*（资料库的过渡全局，只读）。ESM 模块，不写 window。
   ========================================================== */

const { useCallback, useEffect, useRef, useState } = React;

function libLive() {
  return window.LIB_live ? window.LIB_live() : { entries: window.LIB_ENTRIES || [], byId: window.LIB_BY_ID || {} };
}
function libCategories() {
  return (window.LIB_CATS || []).reduce((map, cat) => { map[cat.id] = cat; return map; }, {});
}

/* 与资料库同源：种子按作品门控 + 用户新建 + 编辑覆盖；只认两个字以上的档案名 */
function entityNameIndex() {
  const idOf = {};
  libLive().entries.forEach((entry) => {
    if (entry.name && entry.name.length >= 2 && !(entry.name in idOf)) idOf[entry.name] = entry.id;
  });
  return idOf;
}

export function wrHighlightEntities(root) {
  if (!root) return;
  const idOf = entityNameIndex();
  const names = Object.keys(idOf).sort((a, b) => b.length - a.length);
  if (!names.length) return;
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const rx = new RegExp("(" + names.map(esc).join("|") + ")", "g");
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      let parent = node.parentElement;
      while (parent && parent !== root) {
        if (parent.classList && (parent.classList.contains("wr-entity") || parent.classList.contains("wr-anno"))) return NodeFilter.FILTER_REJECT;
        parent = parent.parentElement;
      }
      rx.lastIndex = 0;
      return rx.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const targets = [];
  let node;
  while ((node = walker.nextNode())) targets.push(node);
  targets.forEach((textNode) => {
    rx.lastIndex = 0;
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    let match;
    while ((match = rx.exec(text))) {
      if (match.index > last) frag.appendChild(document.createTextNode(text.slice(last, match.index)));
      const span = document.createElement("span");
      span.className = "wr-entity";
      span.setAttribute("data-lib-id", idOf[match[0]]);
      span.textContent = match[0];
      frag.appendChild(span);
      last = match.index + match[0].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode.replaceChild(frag, textNode);
  });
}

/* 悬停卡 / 点击直达档案 / 反向定位。
   onNeedScene：要定位的实体不在当前这一场时，切到「现在在写的那一场」再找（载入后由 locatePending 接着定位）。 */
export function useWrEntities({ editorRef, scrollRef, onNeedScene }) {
  const [entityPop, setEntityPop] = useState(null);
  const pendingRef = useRef(null);
  const needScene = useWrEvent(() => { if (onNeedScene) onNeedScene(); });

  const locateEntity = useCallback((id) => {
    const el = editorRef.current;
    const scroller = scrollRef.current;
    if (!el || !scroller) return false;
    const span = el.querySelector('.wr-entity[data-lib-id="' + id + '"]');
    if (!span) return false;
    const r = span.getBoundingClientRect();
    const sr = scroller.getBoundingClientRect();
    scroller.scrollTo({ top: Math.max(0, scroller.scrollTop + (r.top - sr.top) - sr.height / 2 + r.height / 2), behavior: "smooth" });
    span.classList.add("wr-entity-flash");
    setTimeout(() => span.classList.remove("wr-entity-flash"), 1500);
    return true;
  }, [editorRef, scrollRef]);

  /* 反向链路：从档案「在正文中定位」跳来 */
  useEffect(() => {
    const onLocate = (e) => {
      const id = e.detail;
      if (!id || locateEntity(id)) return;
      pendingRef.current = id;
      needScene();
    };
    window.addEventListener("ws:writer-locate", onLocate);
    return () => window.removeEventListener("ws:writer-locate", onLocate);
  }, [locateEntity, needScene]);

  /* 正文重新载入后接着定位：返回取消函数 */
  const locatePending = useCallback(() => {
    const id = pendingRef.current;
    if (!id) return null;
    pendingRef.current = null;
    const frame = requestAnimationFrame(() => locateEntity(id));
    return () => cancelAnimationFrame(frame);
  }, [locateEntity]);

  const openDossier = useCallback((id) => {
    if (!id) return;
    setEntityPop(null);
    navigateWithViewIntent("library", "ws:lib-open", id);
  }, []);

  const onEditorOver = useCallback((e) => {
    const span = e.target.closest && e.target.closest(".wr-entity");
    if (!span) return;
    const r = span.getBoundingClientRect();
    setEntityPop({ id: span.getAttribute("data-lib-id"), x: r.left + r.width / 2, top: r.top, bottom: r.bottom });
  }, []);
  const onEditorOut = useCallback((e) => {
    const span = e.target.closest && e.target.closest(".wr-entity");
    if (!span) return;
    const to = e.relatedTarget;
    if (!to || !to.closest || !to.closest(".wr-entity")) setEntityPop(null);
  }, []);
  /* 批注标注叠在实体上时，批注的点击（捕获阶段先处理并 preventDefault）优先 */
  const onEditorClick = useCallback((e) => {
    if (e.defaultPrevented) return;
    const span = e.target.closest && e.target.closest(".wr-entity");
    if (span) { e.preventDefault(); openDossier(span.getAttribute("data-lib-id")); }
  }, [openDossier]);

  return { entityPop, locatePending, openDossier, onEditorOver, onEditorOut, onEditorClick };
}

/* ---- 档案悬停卡 ---- */
export function WrEntityPop({ pop }) {
  if (!pop) return null;
  const entry = libLive().byId[pop.id];
  if (!entry) return null;
  const cats = libCategories();
  const above = pop.top > 180;
  const style = {
    left: pop.x,
    top: above ? pop.top - 10 : pop.bottom + 10,
    transform: above ? "translate(-50%, -100%)" : "translate(-50%, 0)",
  };
  return (
    <div className={`wr-entpop acc-${entry.accent}`} style={style}>
      <div className="wr-entpop-head">
        <span className="wr-entpop-glyph">{entry.glyph}</span>
        <div className="wr-entpop-main">
          <div className="wr-entpop-name">{entry.name}</div>
          <div className="wr-entpop-kind">{(cats[entry.cat] || {}).label} · {entry.kind}</div>
        </div>
      </div>
      {entry.summary && <div className="wr-entpop-sum">{entry.summary}</div>}
      <div className="wr-entpop-foot"><I.BookOpen size={12} /> 点击打开档案</div>
    </div>
  );
}

/* ---- @ 唤档案：检测、筛选、插入引用 ---- */
export function useWrMention({ editorRef, onInserted }) {
  const [mention, setMention] = useState(null);     /* { query, x, y } */
  const [mentionIdx, setMentionIdx] = useState(0);
  const ctxRef = useRef(null);
  const inserted = useWrEvent(() => { if (onInserted) onInserted(); });

  const closeMention = useCallback(() => { ctxRef.current = null; setMention(null); }, []);
  const detectMention = useCallback(() => {
    const sel = window.getSelection();
    const node = sel && sel.rangeCount ? sel.anchorNode : null;
    const match = node && node.nodeType === 3 ? /@([^@\s]{0,12})$/.exec(node.nodeValue.slice(0, sel.anchorOffset)) : null;
    if (!match) { closeMention(); return; }
    ctxRef.current = { node, start: match.index, end: sel.anchorOffset };
    const rect = sel.getRangeAt(0).cloneRange().getBoundingClientRect();
    setMention({ query: match[1], x: rect.left || rect.right, y: rect.bottom || rect.top });
    setMentionIdx(0);
  }, [closeMention]);

  const mentionList = mention
    ? (libLive().entries || []).filter((entry) => {
        const q = (mention.query || "").toLowerCase();
        if (!q) return true;
        return (entry.name + " " + (entry.summary || "") + " " + (entry.kind || "") + " " + (entry.tags || []).join(" ")).toLowerCase().includes(q);
      }).slice(0, 8)
    : [];

  const insertMention = (entry) => {
    const ctx = ctxRef.current;
    const el = editorRef.current;
    if (!ctx || !el) return;
    const range = document.createRange();
    range.setStart(ctx.node, ctx.start);
    range.setEnd(ctx.node, ctx.end);
    range.deleteContents();
    const span = document.createElement("span");
    span.className = "wr-entity";
    span.setAttribute("data-lib-id", entry.id);
    span.textContent = entry.name;
    range.insertNode(span);
    const space = document.createTextNode(" ");
    span.after(space);
    const caret = document.createRange();
    caret.setStartAfter(space);
    caret.collapse(true);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(caret);
    closeMention();
    inserted();
    el.focus();
  };

  /* 选择器开着时接管方向键 / 回车 / Esc；返回 true 表示这一下已经处理掉了 */
  const onMentionKeyDown = (e) => {
    if (!mention) return false;
    // 输入法组字时的方向键 / 回车是在挑候选字，不是在挑档案条目
    if (isImeComposing(e)) return false;
    if (e.key === "ArrowDown") { e.preventDefault(); setMentionIdx((i) => Math.min(Math.max(0, mentionList.length - 1), i + 1)); return true; }
    if (e.key === "ArrowUp") { e.preventDefault(); setMentionIdx((i) => Math.max(0, i - 1)); return true; }
    if (e.key === "Enter" && mentionList[mentionIdx]) { e.preventDefault(); insertMention(mentionList[mentionIdx]); return true; }
    if (e.key === "Escape") { e.preventDefault(); closeMention(); return true; }
    return false;
  };

  return { mention, mentionIdx, setMentionIdx, mentionList, detectMention, insertMention, onMentionKeyDown };
}

export function WrMentionPicker({ mention, list, idx, onPick, onHover }) {
  if (!mention) return null;
  const cats = libCategories();
  const flip = mention.y > (typeof window !== "undefined" ? window.innerHeight - 300 : 9999);
  const style = { left: mention.x, top: flip ? mention.y - 26 : mention.y + 6, transform: flip ? "translateY(-100%)" : "none" };
  return (
    <div className="wr-mention" style={style}>
      <div className="wr-mention-head"><I.Library size={12} /> 插入档案引用{mention.query ? <span className="wr-mention-q">「{mention.query}」</span> : null}</div>
      {list.length === 0 ? (
        <div className="wr-mention-empty">没有匹配的档案</div>
      ) : (
        <ul className="wr-mention-list">
          {list.map((entry, i) => (
            <li key={entry.id}
              className={`wr-mention-item acc-${entry.accent} ${i === idx ? "is-sel" : ""}`}
              onMouseEnter={() => onHover(i)}
              onMouseDown={(ev) => { ev.preventDefault(); onPick(entry); }}>
              <span className="wr-mention-glyph">{entry.glyph}</span>
              <span className="wr-mention-main">
                <span className="wr-mention-name">{entry.name}</span>
                <span className="wr-mention-sub">{(cats[entry.cat] || {}).label} · {entry.summary || entry.kind}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="wr-mention-foot"><kbd className="wr-kbd">↑↓</kbd>选择 <kbd className="wr-kbd">⏎</kbd>插入 <kbd className="wr-kbd">Esc</kbd>取消</div>
    </div>
  );
}
