import React from "react";
import { emit } from "./lib/events.js";
import {
  WR_ANNO_MAX_ITEMS, WR_ANNO_MAX_QUOTE,
  wrAnnoAnchor, wrAnnoId, wrAnnoLoad, wrAnnoMark, wrAnnoRetitle, wrAnnoSave, wrAnnoUnmark,
} from "./ws-writer-annotations.js";
import { wrCaretAfter } from "./ws-writer-rev.js";

/* ==========================================================
   选区工具条 · 批注（2026-09-30 从 ws-writer-inline.jsx 拆出，审计 F03-18）
   ----------------------------------------------------------
   useInlineAnnotation({ editorRef, sceneId, annoKey, selection, disabled })：
   批注存在本机浏览器（ws-writer-annotations.js），不进正文、不触发正文保存。
   批注开始时就记下它属于哪一场、存到哪个键，保存 / 删除都写回那里，不跟着当前场走。
   · start()：给选区标上一条新批注（还没存），返回弹层的位置；openAt(mark)：点开正文里已有的一条。
   · save() → { close, caret }：写满一场的上限（full）或写不进本机存储（storage）时不关弹层、说原因；
     空批注按删除处理。remove() / cancel() → 光标该落的位置（新批注取消时拆掉它的标注）。
   · dropUnsaved()：换场时拆掉还没存的那条新批注的标注；reset()：弹层收起时清空。
   ESM 模块，不写 window。
   ========================================================== */

const { useRef, useState } = React;

function announceAnnotations(sceneId) {
  emit("ws:anno-change", { sid: sceneId });
}

export function useInlineAnnotation({ editorRef, sceneId, annoKey, selection, disabled }) {
  const [text, setText] = useState("");
  const [isNew, setIsNew] = useState(false);
  const [saveError, setSaveError] = useState(null); // null | "storage"（写不进本机存储）| "full"（这一场满额）
  const annoRef = useRef(null);   // 正在看 / 写的批注：{ id, anchor?（新建时）, key（存储键）, sid（所属场景）}

  /* 这条批注最后一个标注后面的光标（标注随后被拆掉时，活动 Range 跟着落到那段字后面） */
  const caretAfter = (id) => {
    const ed = editorRef.current;
    if (!ed || !id) return null;
    const marks = Array.from(ed.querySelectorAll("mark.wr-anno")).filter((mark) => mark.getAttribute("data-anno-id") === id);
    return wrCaretAfter(marks[marks.length - 1]);
  };

  const start = () => {
    const ed = editorRef.current;
    const range = selection.rangeRef.current;
    if (!ed || !range || disabled) return null;
    const anchor = wrAnnoAnchor(ed, range);
    if (!anchor || anchor.quote.length > WR_ANNO_MAX_QUOTE) return null;
    const id = wrAnnoId();
    const marks = wrAnnoMark(ed, { id, note: "" }, anchor.start, anchor.end);
    if (!marks.length) return null;
    annoRef.current = { id, anchor, key: annoKey, sid: sceneId };
    setText(""); setIsNew(true); setSaveError(null);
    const r = marks[0].getBoundingClientRect();
    const sel = window.getSelection(); if (sel) sel.removeAllRanges();
    return { top: r.top, bottom: r.bottom, left: r.left + r.width / 2 };
  };

  const openAt = (mark, id) => {
    const item = wrAnnoLoad(annoKey).find((anno) => anno.id === id);
    annoRef.current = { id, key: annoKey, sid: sceneId };
    setText(item ? item.note : "");
    setIsNew(false);
    setSaveError(null);
    const r = mark.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left + r.width / 2 };
  };

  const remove = () => {
    const current = annoRef.current;
    const caret = current ? caretAfter(current.id) : null;
    if (current) {
      const key = current.key || annoKey;
      wrAnnoUnmark(editorRef.current, current.id);
      const list = wrAnnoLoad(key);
      if (list.some((anno) => anno.id === current.id)) wrAnnoSave(key, list.filter((anno) => anno.id !== current.id));
      announceAnnotations(current.sid || sceneId);
    }
    return caret;
  };

  const save = () => {
    const current = annoRef.current;
    const note = text.trim();
    const ed = editorRef.current;
    if (!current || !ed) return { close: true, caret: null };
    if (!note) return { close: true, caret: remove() };
    const key = current.key || annoKey;
    const list = wrAnnoLoad(key);
    const now = Date.now();
    const existing = list.find((anno) => anno.id === current.id);
    if (!existing && !current.anchor) return { close: true, caret: null }; // 点开的旧批注已经在别处删掉了：没有引文可以重建
    if (!existing && list.length >= WR_ANNO_MAX_ITEMS) { setSaveError("full"); return { close: false }; }
    const next = existing
      ? list.map((anno) => (anno.id === current.id ? { ...anno, note, updatedAt: now } : anno))
      : [...list, { id: current.id, quote: current.anchor.quote, prefix: current.anchor.prefix, suffix: current.anchor.suffix, note, createdAt: now, updatedAt: now }];
    if (!wrAnnoSave(key, next)) { setSaveError("storage"); return { close: false }; }
    wrAnnoRetitle(ed, current.id, note);
    announceAnnotations(current.sid || sceneId);
    return { close: true, caret: caretAfter(current.id) };
  };

  const cancel = () => {
    const caret = annoRef.current ? caretAfter(annoRef.current.id) : null;
    if (isNew && annoRef.current) wrAnnoUnmark(editorRef.current, annoRef.current.id);
    return caret;
  };

  return {
    text, setText, isNew, saveError,
    start, openAt, save, remove, cancel,
    dropUnsaved: () => { if (isNew && annoRef.current) wrAnnoUnmark(editorRef.current, annoRef.current.id); },
    reset: () => { setText(""); setSaveError(null); annoRef.current = null; },
  };
}
