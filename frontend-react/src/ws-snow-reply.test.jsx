// 教练回复的轻量排版（ws-snow-reply.jsx）：段落 / 粗体 / 斜体 / 列表，其余原样当文字；只产出 React 元素。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { CoachInline, CoachReply, parseCoachInline, parseCoachReply } from "./ws-snow-reply.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mounted = [];
async function render(node) {
  const host = document.createElement("div");
  host.className = "sf-coach-body";
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}
afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
});

// 模型真实回复的形状（内容是合成的）：引子、编号条目之间隔空行、条目下缩进三格的「- 」子条目、收尾一句
const SHAPED = [
  "先说结论：「开场」这一步还差一口气。",
  "",
  "缺口有三处：",
  "",
  "1. **目标**：主角想要什么，还没落到可拍的动作上。",
  "   - *做法*：给他一个今晚必须拿到的东西。",
  "",
  "2. **阻力**：对手只是嘴上反对。",
  "   - *选项甲（稳）*：让对手先动手。",
  "   - *选项乙（险）*：让盟友倒戈。",
  "",
  "3. **挫败**：结尾没有代价。",
  "",
  "改完这三处再「确认本步」。",
].join("\n");

describe("parseCoachReply · 块级", () => {
  it("空行分段；段内换行保留成一行一行", () => {
    expect(parseCoachReply("第一段第一行\n第一段第二行\n\n\n第二段")).toEqual([
      { type: "p", lines: ["第一段第一行", "第一段第二行"] },
      { type: "p", lines: ["第二段"] },
    ]);
    expect(parseCoachReply("")).toEqual([]);
    expect(parseCoachReply(null)).toEqual([]);
    expect(parseCoachReply("一行\r\n两行")).toEqual([{ type: "p", lines: ["一行", "两行"] }]);
  });

  it("编号条目之间隔空行仍是同一张表，缩进的「- 」是那一条下面的子表；表后不缩进的一句是新段落", () => {
    const blocks = parseCoachReply(SHAPED);
    expect(blocks.map(b => b.type)).toEqual(["p", "p", "list", "p"]);
    const list = blocks[2];
    expect(list.ordered).toBe(true);
    expect(list.items.map(it => it.lines[0])).toEqual([
      "**目标**：主角想要什么，还没落到可拍的动作上。",
      "**阻力**：对手只是嘴上反对。",
      "**挫败**：结尾没有代价。",
    ]);
    expect(list.items[0].lists).toHaveLength(1);
    expect(list.items[0].lists[0]).toMatchObject({ ordered: false });
    expect(list.items[1].lists[0].items.map(it => it.lines[0])).toEqual(["*选项甲（稳）*：让对手先动手。", "*选项乙（险）*：让盟友倒戈。"]);
    expect(list.items[2].lists).toEqual([]);
    expect(blocks[3]).toEqual({ type: "p", lines: ["改完这三处再「确认本步」。"] });
  });

  it("列表可以紧跟在一句引子后面（不隔空行）；「1、」也算编号；续行并进上一条", () => {
    const blocks = parseCoachReply("可以这样改：\n1、先抬压力\n2、再给代价\n   代价要落在他在乎的人身上\n收尾一句");
    expect(blocks.map(b => b.type)).toEqual(["p", "list", "p"]);
    expect(blocks[1].items.map(it => it.lines)).toEqual([["先抬压力"], ["再给代价", "代价要落在他在乎的人身上"]]);
    expect(blocks[2].lines).toEqual(["收尾一句"]);
  });

  it("同一层从「- 」换成「1.」是两张表；编号不从 1 起时记下起点，从 0 起也照记", () => {
    const blocks = parseCoachReply("- 甲\n- 乙\n3. 丙\n4. 丁");
    expect(blocks.map(b => [b.type, b.ordered, b.start, b.items.length])).toEqual([["list", false, 1, 2], ["list", true, 3, 2]]);
    expect(parseCoachReply("0. 零\n1. 一")).toMatchObject([{ type: "list", ordered: true, start: 0 }]);
  });

  it("不认的语法原样当文字：标题、引用、分隔线、代码、链接、小数、年份开头", () => {
    const text = "# 标题\n> 引用\n---\n`代码`\n[链接](http://example.test)\n1.5 倍的压力\n2025. 年初\n-5 度\n* \n-";
    const blocks = parseCoachReply(text);
    expect(blocks).toEqual([{ type: "p", lines: text.split("\n").map(l => l.trim()) }]);
  });

  it("「N、」后面紧跟数字是并列不是编号：「04、06 两步」不会把 04 吞进编号", () => {
    for (const line of ["04、06 两步还空着，先补这两步。", "3、4 月之间要落地", "10、11 两场可以合并", "2、 3 场之后再翻"]) {
      expect(parseCoachReply(line)).toEqual([{ type: "p", lines: [line] }]);
    }
    // 真编号不受影响
    expect(parseCoachReply("1、先抬压力\n2、三场之后再翻")[0]).toMatchObject({ type: "list", ordered: true, start: 1 });
  });
});

describe("parseCoachInline · 行内", () => {
  it("**粗体** 与 *斜体*；斜体里可以套粗体；***三星*** 是粗体套斜体", () => {
    expect(parseCoachInline("先**抬压力**再*给代价*")).toEqual([
      "先", { type: "strong", children: ["抬压力"] }, "再", { type: "em", children: ["给代价"] },
    ]);
    expect(parseCoachInline("*甲 **乙** 丙*")).toEqual([
      { type: "em", children: ["甲 ", { type: "strong", children: ["乙"] }, " 丙"] },
    ]);
    // CommonMark：先扣内侧两颗成粗体，外面剩的一颗成斜体
    expect(parseCoachInline("***重***")).toEqual([
      { type: "em", children: [{ type: "strong", children: ["重"] }] },
    ]);
    expect(parseCoachInline("**甲***")).toEqual([{ type: "strong", children: ["甲"] }, "*"]);
    expect(parseCoachInline("***甲*")).toEqual(["**", { type: "em", children: ["甲"] }]);
  });

  it("收尾星号配最近的开头：「5*3」里的乘号留着，后面的 *4* 才是斜体", () => {
    expect(parseCoachInline("价格 5*3=15，面积 *4* 平")).toEqual([
      "价格 5*3=15，面积 ", { type: "em", children: ["4"] }, " 平",
    ]);
    // 「三的倍数」规则：中间的 ** 两边都贴字，不把斜体拦腰截断
    expect(parseCoachInline("*甲**乙*")).toEqual([{ type: "em", children: ["甲**乙"] }]);
    expect(parseCoachInline("**甲*乙*丙**")).toEqual([
      { type: "strong", children: ["甲", { type: "em", children: ["乙"] }, "丙"] },
    ]);
  });

  it("中文标点紧贴星号照样算：「**「目标」**：」是粗体（CommonMark 的标点规则在中文里会失效，这里只看空白）", () => {
    expect(parseCoachInline("先把**「目标」**：写实")).toEqual([
      "先把", { type: "strong", children: ["「目标」"] }, "：写实",
    ]);
  });

  it("不成对、贴着空白、空内容的星号原样保留", () => {
    expect(parseCoachInline("只有一边**没收尾")).toEqual(["只有一边**没收尾"]);
    expect(parseCoachInline("2 * 3 * 4")).toEqual(["2 * 3 * 4"]);
    expect(parseCoachInline("** 两边有空格 **")).toEqual(["** 两边有空格 **"]);
    expect(parseCoachInline("****")).toEqual(["****"]);
    expect(parseCoachInline("*")).toEqual(["*"]);
  });

  it("怪星号的长行也是线性时间：6000 字以内几毫秒，不会卡住输入框", () => {
    // 以前「最早的开头赢 + 每个开头往后重扫」在第一种形状上 5600 字要近 20 秒
    const shapes = [n => "*a **b ".repeat(n), n => "a*b**".repeat(n), n => "**a*".repeat(n), n => "x***y*z**".repeat(n)];
    for (const shape of shapes) {
      const line = shape(Math.ceil(6000 / shape(1).length));
      const t0 = performance.now();
      parseCoachInline(line);
      expect(performance.now() - t0).toBeLessThan(150);
    }
  });

  it("套得太深的强调原样还原成带星号的文字，不爆栈也不丢字", () => {
    const flat = (nodes) => nodes.map(n => (typeof n === "string" ? n
      : (n.type === "strong" ? "**" : "*") + flat(n.children) + (n.type === "strong" ? "**" : "*"))).join("");
    const depth = (nodes) => Math.max(0, ...nodes.map(n => (typeof n === "string" ? 0 : 1 + depth(n.children))));
    const line = "*a ".repeat(5000) + "a* ".repeat(5000);
    const tree = parseCoachInline(line);
    expect(depth(tree)).toBeLessThanOrEqual(6);
    expect(flat(tree)).toBe(line);
  });
});

describe("CoachReply · 渲染", () => {
  it("段落、粗体、斜体、有序表与嵌套无序表都是真元素；星号和减号不再印在页面上", async () => {
    const host = await render(<CoachReply text={SHAPED} />);
    expect(host.querySelectorAll(":scope > p")).toHaveLength(3);
    const ol = host.querySelector(":scope > ol.sf-coach-md-list");
    expect(ol).toBeTruthy();
    expect(ol.hasAttribute("start")).toBe(false);
    expect(ol.querySelectorAll(":scope > li")).toHaveLength(3);
    expect(ol.querySelectorAll(":scope > li > ul.sf-coach-md-list")).toHaveLength(2);
    expect(Array.from(host.querySelectorAll("strong")).map(n => n.textContent)).toEqual(["目标", "阻力", "挫败"]);
    expect(Array.from(host.querySelectorAll("em")).map(n => n.textContent)).toEqual(["做法", "选项甲（稳）", "选项乙（险）"]);
    expect(host.textContent).not.toContain("*");
    expect(host.textContent).not.toContain("- ");
  });

  it("段内换行渲染成 <br>；编号起点落到 start 属性", async () => {
    const host = await render(<CoachReply text={"上一行\n下一行\n\n3. 丙\n4. 丁"} />);
    expect(host.querySelector("p").innerHTML).toBe("上一行<br>下一行");
    expect(host.querySelector("ol").getAttribute("start")).toBe("3");
  });

  it("像 HTML 的文字照样是文字：不生成任何标签，也不执行", async () => {
    window.__coachPwned = 0;
    const evil = '<img src="x" onerror="window.__coachPwned=1"> **<b>粗</b>** <script>window.__coachPwned=2</script>\n- <a href="javascript:alert(1)">链接</a>';
    const host = await render(<CoachReply text={evil} />);
    expect(host.querySelector("img, script, b, a")).toBeNull();
    expect(host.querySelector("strong").textContent).toBe("<b>粗</b>");
    expect(host.textContent).toContain('<img src="x" onerror="window.__coachPwned=1">');
    expect(host.querySelector("li").textContent).toBe('<a href="javascript:alert(1)">链接</a>');
    expect(window.__coachPwned).toBe(0);
    delete window.__coachPwned;
  });

  it("两个组件都是 memo：教练输入框每敲一个字，没变的回复不重新解析", () => {
    const MEMO = Symbol.for("react.memo");
    expect(CoachReply.$$typeof).toBe(MEMO);
    expect(CoachInline.$$typeof).toBe(MEMO);
  });

  it("编号从 0 起时 start 也落到属性上", async () => {
    const host = await render(<CoachReply text={"0. 零\n1. 一"} />);
    expect(host.querySelector("ol").getAttribute("start")).toBe("0");
  });

  it("CoachInline 只做行内：条目文字里的粗体生效，列表记号不当列表", async () => {
    const host = await render(<CoachInline text={"- 把**代价**写实"} />);
    expect(host.querySelector("ul, ol, p")).toBeNull();
    expect(host.querySelector("strong").textContent).toBe("代价");
    expect(host.textContent).toBe("- 把代价写实");
  });
});
