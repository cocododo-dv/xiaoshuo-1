import { describe, expect, it } from "vitest";
import {
  LEGACY_DRAFT_PLACEHOLDER, hasAuthorText, manuscriptToDocHTML, sanitizeManuscriptHTML, stripLegacyDraftPlaceholder,
} from "./manuscript-html.js";


describe("manuscript HTML trust boundary", () => {
  it("removes scripts, event handlers, remote media and embedded documents", () => {
    const dirty = '<p onclick="steal()">正文<img src=x onerror="steal()"></p>'
      + '<script>steal()</script><iframe src="https://evil.invalid"></iframe>';
    expect(sanitizeManuscriptHTML(dirty)).toBe("<p>正文</p>");
  });

  it("keeps the editor formatting vocabulary without user attributes", () => {
    expect(sanitizeManuscriptHTML('<blockquote class="x"><strong style="x">句子</strong><br></blockquote>'))
      .toBe("<blockquote><strong>句子</strong><br></blockquote>");
  });

  it("escapes plain text before wrapping it in paragraphs", () => {
    expect(manuscriptToDocHTML("A & B\n1 < 2"))
      .toBe("<p>A &amp; B</p><p>1 &lt; 2</p>");
  });
});

describe("旧草稿开头的空白页占位（主页 / AI 起草台共用的判定）", () => {
  const P = LEGACY_DRAFT_PLACEHOLDER;

  it("与写作台载入时（wrPrepareLoadedHTML）判的是同一回事：同一份旧草稿，两边剩下的字和「有没有字」一致", async () => {
    // 写作台分包惰性加载，主页 / AI 起草台不引它；这里在测试里把两边放在一起跑，规则一漂就转红
    const { WR_LEGACY_PLACEHOLDER, wrPrepareLoadedHTML } = await import("./ws-writer-manuscript.js");
    expect(P).toBe(WR_LEGACY_PLACEHOLDER);
    const textOf = (html) => {
      const t = document.createElement("template");
      t.innerHTML = html;
      return (t.content.textContent || "").replace(/\s/g, "");
    };
    const drafts = [
      `<p>${P}</p>`,
      `<p>${P}</p><p>门开了。</p>`,
      `<p>${P}门开了。</p>`,
      `<p>门开了。</p><p>${P}</p>`,
      // 旧草稿每存一次包一层空 span：占位被拆在几个文本节点里
      "<p><span>在这里开始写</span>这一场……门开了。</p>",
      "<p><span>在这里开始写</span><span>这一场……</span></p>",
      "<p><span><span>在这里</span></span><mark>开始写这一场</mark>……</p><p>他没回头。</p>",
      `<p><br></p><p><em>${P}</em>他没回头。</p>`,
      "<p>门开了。</p>",
    ];
    for (const html of drafts) {
      const writer = wrPrepareLoadedHTML(html);
      expect([html, textOf(stripLegacyDraftPlaceholder(html))]).toEqual([html, textOf(writer)]);
      expect([html, hasAuthorText(html)]).toEqual([html, writer !== ""]);
    }
  });

  it("占位被旧版空 span 拆开时也认得出来（只看字面会漏掉）", () => {
    expect(stripLegacyDraftPlaceholder("<p><span>在这里开始写</span>这一场……门开了。</p>")).toBe("<p>门开了。</p>");
    expect(hasAuthorText("<p><span>在这里开始写</span><span>这一场……</span></p>")).toBe(false);
    // 拆开的占位在正文后面：照样是作者的字
    expect(hasAuthorText("<p>门开了。</p><p><span>在这里开始写</span>这一场……</p>")).toBe(true);
  });

  it("只有占位（前后空段也算）的草稿没有作者的字", () => {
    expect(hasAuthorText(`<p>${P}</p>`)).toBe(false);
    expect(hasAuthorText(`<p><br></p><p>${P}</p><p>  </p>`)).toBe(false);
    expect(hasAuthorText(P)).toBe(false);
    expect(hasAuthorText("")).toBe(false);
    expect(hasAuthorText(null)).toBe(false);
    expect(hasAuthorText("<p>&nbsp;</p>")).toBe(false);
  });

  it("作者的字排在占位后面、接在占位同一段里，或者正文后文恰好写到这句话，都算有字", () => {
    expect(hasAuthorText(`<p>${P}</p><p>门开了一条缝。</p>`)).toBe(true);
    expect(hasAuthorText(`<p>${P}门开了一条缝。</p>`)).toBe(true);
    // 旧判定只要整份草稿里出现过这句话就当成空稿——这一条会让它转红
    expect(hasAuthorText(`<p>门开了一条缝。</p><p>纸条上写着：${P}</p>`)).toBe(true);
    expect(hasAuthorText(`<p>门开了一条缝。${P}</p>`)).toBe(true);
  });

  it("脚本、样式里的字不算作者的字", () => {
    expect(hasAuthorText("<script>alert(1)</script><style>p{}</style>")).toBe(false);
  });

  it("stripLegacyDraftPlaceholder 只去掉开头那一句，后文原样保留", () => {
    expect(stripLegacyDraftPlaceholder(`<p>${P}</p><p>门开了。</p>`)).toBe("<p>门开了。</p>");
    expect(stripLegacyDraftPlaceholder(`<p>${P}门开了。</p>`)).toBe("<p>门开了。</p>");
    expect(stripLegacyDraftPlaceholder(`<p>门开了。</p><p>${P}</p>`)).toBe(`<p>门开了。</p><p>${P}</p>`);
    expect(stripLegacyDraftPlaceholder(`<p><br></p><p><em>${P}</em>他没回头。</p>`)).toBe("<p><br></p><p><em></em>他没回头。</p>");
    // 没有占位时原样返回，不重新序列化
    expect(stripLegacyDraftPlaceholder("<P>门开了。")).toBe("<P>门开了。");
  });
});
