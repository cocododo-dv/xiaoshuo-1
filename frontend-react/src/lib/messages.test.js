import { isChineseMessage } from "./messages.js";

describe("isChineseMessage（后端原话能不能直接给作者看）", () => {
  it("中文说明可以；英文、异常串、夹几个汉字的英文都不行", () => {
    expect(isChineseMessage("这本书正在学习文风：等它完成，或先取消。")).toBe(true);
    expect(isChineseMessage("")).toBe(false);
    expect(isChineseMessage("internal operation failed")).toBe(false);
    expect(isChineseMessage("TypeError: 无法读取属性")).toBe(false);
    expect(isChineseMessage("RuntimeError: relay down")).toBe(false);
    expect(isChineseMessage("model gpt-x failed at 批 3")).toBe(false);
  });
});
