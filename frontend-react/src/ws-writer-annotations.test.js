import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  wrAnnoAnchor, wrAnnoAnchoredIds, wrAnnoApply, wrAnnoLoad, wrAnnoLocate, wrAnnoMark, wrAnnoRefresh,
  wrAnnoRetitle, wrAnnoSave, wrAnnoUnmark,
} from "./ws-writer-annotations.js";
import { wrSerializeManuscript } from "./ws-writer-manuscript.js";

const KEY = "wr-anno:scene-1::work-1";

function editor(html) {
  const el = document.createElement("div");
  el.innerHTML = html;
  document.body.appendChild(el);
  return el;
}

function rangeFor(root, text) {
  const all = root.textContent;
  const at = all.indexOf(text);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let seen = 0;
  let node;
  let started = false;
  while ((node = walker.nextNode())) {
    const len = node.nodeValue.length;
    if (!started && at < seen + len) { range.setStart(node, at - seen); started = true; }
    if (started && at + text.length <= seen + len) { range.setEnd(node, at + text.length - seen); break; }
    seen += len;
  }
  return range;
}

beforeEach(() => { window.localStorage.clear(); });
afterEach(() => { document.body.innerHTML = ""; vi.restoreAllMocks(); });

describe("批注存储：本机浏览器里按场一份清单", () => {
  it("存进去读得回来；清单空了就删键", () => {
    const item = { id: "a1", quote: "潮水退了", prefix: "夜里", suffix: "。", note: "这里再慢一点", createdAt: 1, updatedAt: 2 };
    expect(wrAnnoSave(KEY, [item])).toBe(true);
    expect(wrAnnoLoad(KEY)).toEqual([item]);
    expect(wrAnnoSave(KEY, [])).toBe(true);
    expect(window.localStorage.getItem(KEY)).toBeNull();
  });

  it("坏数据、重复 id、没有引文的条目不进清单；读写失败时不抛", () => {
    window.localStorage.setItem(KEY, JSON.stringify({ v: 1, items: [
      { id: "a1", quote: "有引文", note: "留下" },
      { id: "a1", quote: "重复的 id", note: "丢掉" },
      { id: "bad id!", quote: "id 不合法" },
      { id: "a2", quote: "   " },
      null,
    ] }));
    expect(wrAnnoLoad(KEY).map((item) => item.note)).toEqual(["留下"]);
    window.localStorage.setItem(KEY, "{not json");
    expect(wrAnnoLoad(KEY)).toEqual([]);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    expect(wrAnnoSave(KEY, [{ id: "a3", quote: "字" }])).toBe(false);
    expect(wrAnnoLoad(null)).toEqual([]);
  });

  it("超过每场 200 条时整份拒存、返回 false：不截掉末尾那条新加的还报成功", () => {
    const items = Array.from({ length: 200 }, (_, i) => ({ id: `n${i}`, quote: `第${i}句`, note: "旧" }));
    expect(wrAnnoSave(KEY, items)).toBe(true);
    expect(wrAnnoSave(KEY, [...items, { id: "n200", quote: "第二百零一句", note: "新" }])).toBe(false);
    const stored = wrAnnoLoad(KEY);
    expect(stored).toHaveLength(200);
    expect(stored.some((item) => item.id === "n200")).toBe(false);
  });
});

describe("批注锚定：引文 + 前后文", () => {
  it("选区取出引文和前后文，跨实体高亮拆开的文本节点也算得对", () => {
    const root = editor('<p>门外<span class="wr-entity">风声</span>很大，她没有回头。</p>');
    const anchor = wrAnnoAnchor(root, rangeFor(root, "风声很大"));
    expect(anchor).toMatchObject({ quote: "风声很大", prefix: "门外", suffix: "，她没有回头。" });
  });

  it("同一句话出现两次时，按前后文挑对的那一处", () => {
    const text = "甲说：好。乙说：好。";
    const second = wrAnnoLocate(text, { quote: "好", prefix: "乙说：", suffix: "。" });
    expect(second.start).toBe(text.lastIndexOf("好"));
    const first = wrAnnoLocate(text, { quote: "好", prefix: "甲说：", suffix: "。乙" });
    expect(first.start).toBe(text.indexOf("好"));
    expect(wrAnnoLocate(text, { quote: "不在正文里" })).toBeNull();
  });

  it("载入正文后按清单重新标出；找不到原文的批注不标，但也不丢", () => {
    const root = editor("<p>第一段写雨。</p><p>第二段写灯。</p>");
    const list = [
      { id: "a1", quote: "写灯", prefix: "第二段", suffix: "。", note: "灯要更暗" },
      { id: "a2", quote: "早已删掉的一句", prefix: "", suffix: "", note: "旧批注" },
    ];
    const anchored = wrAnnoApply(root, list);
    expect([...anchored]).toEqual(["a1"]);
    const mark = root.querySelector('mark.wr-anno[data-anno-id="a1"]');
    expect(mark.textContent).toBe("写灯");
    expect(mark.getAttribute("title")).toBe("灯要更暗");
    expect([...wrAnnoAnchoredIds(root)]).toEqual(["a1"]);
  });

  it("跨段落的批注逐段包，不会把段落搬进 <mark> 里", () => {
    const root = editor("<p>第一段结尾</p><p>第二段开头</p>");
    const marks = wrAnnoMark(root, { id: "a1", note: "" }, 3, 8);
    expect(marks.map((mark) => mark.textContent)).toEqual(["结尾", "第二段"]);
    expect(root.querySelectorAll("p > mark.wr-anno")).toHaveLength(2);
    expect(root.children).toHaveLength(2);
  });

  it("作者改了批注的那段字：保存前按现存标注刷新引文与前后文；没变就返回原数组", () => {
    const root = editor("<p>她把钥匙放回抽屉。</p>");
    const list = [{ id: "a1", quote: "钥匙", prefix: "她把", suffix: "放回抽屉。", note: "伏笔" }];
    wrAnnoApply(root, list);
    expect(wrAnnoRefresh(root, list)).toBe(list);
    root.querySelector("mark.wr-anno").firstChild.nodeValue = "那把旧钥匙";
    const next = wrAnnoRefresh(root, list);
    expect(next[0]).toMatchObject({ quote: "那把旧钥匙", prefix: "她把", note: "伏笔" });
    // 改后的锚点能在重新载入的正文里找到
    const reloaded = editor(wrSerializeManuscript(root));
    expect([...wrAnnoApply(reloaded, next)]).toEqual(["a1"]);
  });

  it("标注不进存盘正文；拆标注、改提示都只动标注本身", () => {
    const root = editor("<p>一行字。</p>");
    wrAnnoApply(root, [{ id: "a1", quote: "一行", prefix: "", suffix: "字。", note: "旧" }]);
    wrAnnoRetitle(root, "a1", "新");
    expect(root.querySelector("mark.wr-anno").getAttribute("title")).toBe("新");
    expect(wrSerializeManuscript(root)).toBe("<p>一行字。</p>");
    wrAnnoUnmark(root, "a1");
    expect(root.innerHTML).toBe("<p>一行字。</p>");
  });
});
