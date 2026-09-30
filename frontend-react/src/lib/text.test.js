import { describe, expect, it } from "vitest";
import { countChars } from "./text.js";

describe("countChars：与后端 count_words 同一口径（去空白后的 code point 数）", () => {
  it("中文按字、标点也算一个，空白不算；null / undefined 是空串", () => {
    expect(countChars("她回到雨城。")).toBe(6);
    expect(countChars("  潮水\n退去\t，  ")).toBe(5);
    expect(countChars(null)).toBe(0);
    expect(countChars(undefined)).toBe(0);
    expect(countChars(0)).toBe(1);
  });

  it("两个 UTF-16 码元的字（扩展区汉字、emoji）只算一个（复核 Q3-R6）", () => {
    expect(countChars("𠀀")).toBe(1);
    expect(countChars("他笑了😀")).toBe(4);
    expect(countChars("𠮷野家 🚢")).toBe(4);
  });
});
