import { describe, expect, it } from "vitest";

import { wrCandSentences, wrPickedParas, wrPlainText } from "./writer-candidates.js";


describe("writer candidate sentence adoption", () => {
  it("decodes escaped prose entities before inserting selected sentences", () => {
    const sentences = wrCandSentences([
      "第一句 A &amp; B。第二句金额 &lt; 100，符号 &gt; 0！",
    ]);

    expect(wrPickedParas(sentences, [1, 0])).toEqual([
      "第一句 A & B。第二句金额 < 100，符号 > 0！",
    ]);
  });

  it("挑中的句子按原来的段落拼回：同一段的连在一起，不同段各成一段，顺序按原文", () => {
    const sentences = wrCandSentences(["她推开门。屋里没人。", "他转身走了。她追出去。", "灯灭了。"]);

    expect(sentences.map((sentence) => sentence.para)).toEqual([0, 0, 1, 1, 2]);
    expect(wrPickedParas(sentences, [4, 0, 2])).toEqual(["她推开门。", "他转身走了。", "灯灭了。"]);
    expect(wrPickedParas(sentences, [1, 0])).toEqual(["她推开门。屋里没人。"]);
  });

  it("keeps marked prose as plain text without leaking markup", () => {
    expect(wrPlainText("<mark>潮水</mark> &amp; 月光")).toBe("潮水 & 月光");
  });
});
