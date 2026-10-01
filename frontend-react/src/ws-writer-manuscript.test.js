import { describe, expect, it } from "vitest";
import {
  WR_LEGACY_PLACEHOLDER, wrBlockSlice, wrCountText, wrPositionAt, wrPrepareLoadedHTML, wrRangeForOffsets, wrRangeForText,
  wrSelectionSegments, wrSerializeManuscript,
} from "./ws-writer-manuscript.js";

function editor(html) {
  const el = document.createElement("div");
  el.innerHTML = html;
  return el;
}

describe("写作台正文序列化：落盘的只有干净正文", () => {
  it("拆掉实体高亮、批注、改写标记、深改诊断与段落状态 class，文字一个不少", () => {
    const el = editor(
      '<p class="is-active">她看见<span class="wr-entity" data-lib-id="lib-1">沈越</span>，'
      + '<mark class="wr-anno" data-note="这里再冷一点" title="这里再冷一点">风很大</mark>。</p>'
      + '<p class="is-merge"><span class="wr-rev" data-orig="原来的句子">改过的句子</span>收尾。</p>'
      + '<p class="wr-dx-para is-active" data-dx="dump:2"><mark class="wr-dx k-echo is-active" data-dx="echo:2:安静">安静安静</mark>了。</p>'
      + '<p class="is-fresh">刚采纳的一段。</p>',
    );
    expect(wrSerializeManuscript(el)).toBe(
      "<p>她看见沈越，风很大。</p><p>改过的句子收尾。</p><p>安静安静了。</p><p>刚采纳的一段。</p>",
    );
  });

  it("不改动编辑器本身（在克隆上处理）", () => {
    const el = editor('<p class="is-active"><span class="wr-entity" data-lib-id="x">沈越</span>来了</p>');
    wrSerializeManuscript(el);
    expect(el.querySelector("span.wr-entity")).not.toBeNull();
    expect(el.querySelector("p").classList.contains("is-active")).toBe(true);
  });

  it("历史存档里一层层的空 <span> 会被压平，不再每存一次多包一层", () => {
    const el = editor("<p><span><span><span>沈越</span></span></span>的信</p>");
    expect(wrSerializeManuscript(el)).toBe("<p>沈越的信</p>");
  });

  it("空白场（只有空段落）存成空串，不把 <p><br></p> 当正文存下去", () => {
    expect(wrSerializeManuscript(editor("<p><br></p>"))).toBe("");
    expect(wrSerializeManuscript(editor(""))).toBe("");
  });

  it("旧开场占位句不会再被存下去", () => {
    expect(wrSerializeManuscript(editor(`<p>${WR_LEGACY_PLACEHOLDER}</p>`))).toBe("");
    expect(wrSerializeManuscript(editor(`<p>${WR_LEGACY_PLACEHOLDER}</p><p>真正的第一句。</p>`))).toBe("<p>真正的第一句。</p>");
  });
});

describe("写作台载入：拆掉历史遗留的空壳与旧占位", () => {
  it("开头那段旧占位整段去掉；作者接着占位写下去的，只去掉占位几个字", () => {
    expect(wrPrepareLoadedHTML(`<p>${WR_LEGACY_PLACEHOLDER}</p><p>门开了。</p>`)).toBe("<p>门开了。</p>");
    expect(wrPrepareLoadedHTML(`<p>${WR_LEGACY_PLACEHOLDER}门开了。</p>`)).toBe("<p>门开了。</p>");
    expect(wrPrepareLoadedHTML(`<p>${WR_LEGACY_PLACEHOLDER}</p>`)).toBe("");
  });

  it("只去开头的占位：正文中间引用到同一句话不动", () => {
    const html = `<p>开场。</p><p>${WR_LEGACY_PLACEHOLDER}</p>`;
    expect(wrPrepareLoadedHTML(html)).toBe(html);
  });

  it("消毒器剥过属性的空壳 <mark> / <span>（丢了内容的旧批注、旧实体高亮）拆掉，脚本照样清除", () => {
    expect(wrPrepareLoadedHTML('<p><mark>风</mark>很<span>大</span><script>x()</script></p>')).toBe("<p>风很大</p>");
  });

  it("没有一个字时返回空串，由调用方放空段落", () => {
    expect(wrPrepareLoadedHTML("")).toBe("");
    expect(wrPrepareLoadedHTML(null)).toBe("");
    expect(wrPrepareLoadedHTML("<p> </p>")).toBe("");
  });
});

describe("写作台辅助", () => {
  it("字数不含空白", () => {
    expect(wrCountText(editor("<p>一 二\n三</p><p>四</p>"))).toBe(4);
    expect(wrCountText(null)).toBe(0);
    // 按码点计，与服务端 count_words 同口径（UTF-16 的 .length 会把一个生僻字算成两个）
    expect(wrCountText(editor("<p>𠀀字</p>"))).toBe(2);
  });

  it("按拼接后的文字找选区，能跨实体高亮拆开的文本节点", () => {
    const p = editor('<p>她看见<span class="wr-entity">沈越</span>站在门口。</p>').firstChild;
    const range = wrRangeForText(p, "看见沈越站");
    expect(range.toString()).toBe("看见沈越站");
    expect(wrRangeForText(p, "不存在的话")).toBeNull();
    expect(wrRangeForText(p, "").toString()).toBe("她看见沈越站在门口。");
  });

  it("按段内偏移取选区：同一句出现两次时取的是指定的那一处；越界返回 null", () => {
    const p = editor('<p>雨停了。<mark class="wr-dx">雨停了。</mark>天亮了。</p>').firstChild;
    const range = wrRangeForOffsets(p, 4, 8);
    expect(range.toString()).toBe("雨停了。");
    expect(range.startContainer.parentNode.tagName).toBe("MARK");
    expect(wrRangeForOffsets(p, 10, 20)).toBeNull();
    expect(wrRangeForOffsets(p, 3, 3)).toBeNull();
  });

  it("跨段的选区只取起始段里的那一截，带着段内偏移", () => {
    const root = editor("<p>雨停了。雨停了。</p><p>天亮了。</p>");
    const [first, second] = root.querySelectorAll("p");
    const range = document.createRange();
    range.setStart(first.firstChild, 4);
    range.setEnd(second.firstChild, 2);
    expect(range.toString()).toContain("天亮");
    expect(wrBlockSlice(first, range)).toEqual({ start: 4, end: 8, text: "雨停了。" });
    const inside = document.createRange();
    inside.setStart(first.firstChild, 1);
    inside.setEnd(first.firstChild, 3);
    expect(wrBlockSlice(first, inside)).toEqual({ start: 1, end: 3, text: "停了" });
  });
});

/* 选区按段切（重评 R12 / #20b）：送去改写的是选中的那几段、一段一行，替换时按段换回 */
function rangeIn(root, [startBlock, startOffset], [endBlock, endOffset]) {
  const blocks = root.children;
  const range = document.createRange();
  const at = (block, offset) => {
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    let seen = 0;
    let node;
    while ((node = walker.nextNode())) {
      if (offset <= seen + node.nodeValue.length) return [node, offset - seen];
      seen += node.nodeValue.length;
    }
    return [block, block.childNodes.length];
  };
  range.setStart(...at(blocks[startBlock], startOffset));
  range.setEnd(...at(blocks[endBlock], endOffset));
  return range;
}

describe("选区按段切（wrSelectionSegments）", () => {
  it("一段之内：一段，偏移按段内拼接文字算；head / tail 指着选区两头", () => {
    const root = editor("<p>雨城入夜。林昭读信。</p><p>窗外有人。</p>");
    const seg = wrSelectionSegments(root, rangeIn(root, [0, 5], [0, 10]));
    expect(seg.paragraphs).toEqual(["林昭读信。"]);
    expect(seg.text).toBe("林昭读信。");
    expect(seg.blocks).toEqual([root.children[0]]);
    expect(seg.head).toEqual({ block: root.children[0], offset: 5 });
    expect(seg.tail).toEqual({ block: root.children[0], offset: 10 });
    expect(seg.segments[0]).toMatchObject({ index: 0, start: 5, end: 10, text: "林昭读信。" });
  });

  it("跨三段：中间整段选中的空段在 blocks 里（一起换掉），不在送去改写的段落里；段号是诊断的段号", () => {
    const root = editor("<p>第一段开头。第一段后半。</p><p><br></p><p>第三段前半。第三段结尾。</p>");
    const seg = wrSelectionSegments(root, rangeIn(root, [0, 6], [2, 6]));
    expect(seg.blocks).toHaveLength(3);
    expect(seg.paragraphs).toEqual(["第一段后半。", "第三段前半。"]);
    expect(seg.text).toBe("第一段后半。\n第三段前半。");
    expect(seg.segments.map((item) => item.index)).toEqual([0, 1, 2]);
    expect(seg.tail).toEqual({ block: root.children[2], offset: 6 });
  });

  it("选区停在下一段的开头 / 从上一段的末尾起：那一段不算", () => {
    const root = editor("<p>第一段。</p><p>第二段。</p><p>第三段。</p>");
    const range = document.createRange();
    range.setStart(root.children[0].firstChild, 4);       // 第一段的末尾
    range.setEnd(root.children[2].firstChild, 0);         // 第三段的开头
    const seg = wrSelectionSegments(root, range);
    expect(seg.blocks).toEqual([root.children[1]]);
    expect(seg.paragraphs).toEqual(["第二段。"]);
  });

  it("碰到不在段落里的散字、或套着列表的结构：unsupported（没法按段换回）；选区不在编辑器里 / 没选中字：null", () => {
    const loose = editor("<p>第一段。</p>散在根上的字<p>第三段。</p>");
    expect(wrSelectionSegments(loose, rangeIn(loose, [0, 1], [1, 2]))).toEqual({ unsupported: true });
    const list = editor("<p>第一段。</p><ul><li>一条</li></ul>");
    const range = document.createRange();
    range.setStart(list.children[0].firstChild, 1);
    range.setEnd(list.querySelector("li").firstChild, 1);
    expect(wrSelectionSegments(list, range)).toEqual({ unsupported: true });
    // 中间没有字的顶层元素（零散的 <br>）一起换掉，不算「切不开」
    const stray = editor("<p>第一段。</p><br><p>第三段。</p>");
    const range2 = document.createRange();
    range2.setStart(stray.children[0].firstChild, 1);
    range2.setEnd(stray.children[2].firstChild, 2);
    expect(wrSelectionSegments(stray, range2)).toMatchObject({ paragraphs: ["一段。", "第三"] });
    expect(wrSelectionSegments(stray, range2).blocks).toHaveLength(3);
    const other = editor("<p>别处。</p>");
    expect(wrSelectionSegments(loose, rangeIn(other, [0, 0], [0, 2]))).toBeNull();
    const collapsed = document.createRange();
    collapsed.setStart(loose.children[0].firstChild, 1);
    expect(wrSelectionSegments(loose, collapsed)).toBeNull();
  });

  it("wrPositionAt：落在两个文本节点之间时，preferNext 取后一个的开头，否则取前一个的末尾；空段落在段落本身", () => {
    const p = editor('<p>雨城<span class="wr-entity">林昭</span>读信。</p>').firstChild;
    const [rain, nameText] = [p.firstChild, p.querySelector("span").firstChild];
    expect(wrPositionAt(p, 2, false)).toEqual({ node: rain, offset: 2 });
    expect(wrPositionAt(p, 2, true)).toEqual({ node: nameText, offset: 0 });
    expect(wrPositionAt(p, 99, false)).toBeNull();
    const empty = editor("<p><br></p>").firstChild;
    expect(wrPositionAt(empty, 0, false)).toEqual({ node: empty, offset: 0 });
  });
});
