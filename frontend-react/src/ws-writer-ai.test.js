import { describe, expect, it } from "vitest";
import {
  wrAiError, wrAiLocalError, wrContinueCandidates, wrContinueChips, wrContinueDirection, wrToneInstr,
} from "./ws-writer-ai.js";
import { modKeyLabel as modKey, modShortcut as modCombo } from "./lib/platform.js";

const apiError = (code, extra = {}) => Object.assign(new Error("raw english message"), { code, details: {}, ...extra });

describe("写作台 AI 失败提示按错误代码分流", () => {
  it("没配模型（*_LLM_NOT_CONFIGURED / author_action / configure_*）→ 去系统设置，而不是让作者白等重试", () => {
    expect(wrAiError(apiError("AUTHOR_PROPOSAL_LLM_NOT_CONFIGURED", { status: 409 })).kind).toBe("config");
    expect(wrAiError(apiError("SOMETHING", { details: { author_action: { view: "settings" } } })).kind).toBe("config");
    expect(wrAiError(apiError("WRITER_DEEP_REVIEW_LLM_FAILED", { details: { next_action: "configure_writer_deep_review_route_and_retry" } })).kind).toBe("config");
  });

  it("断网 / 超时 / 5xx → 重试，而不是叫作者去配置模型", () => {
    expect(wrAiError(apiError("NETWORK_ERROR"))).toMatchObject({ kind: "retry", actionLabel: "重试" });
    expect(wrAiError(apiError("REQUEST_TIMEOUT")).kind).toBe("retry");
    expect(wrAiError(apiError("INTERNAL_ERROR", { status: 500 })).kind).toBe("retry");
    expect(wrAiError(apiError("NETWORK_ERROR")).message).not.toContain("模型");
    expect(wrAiError(apiError("NETWORK_ERROR")).offersSettings).toBeFalsy();
  });

  it("服务器兜底的不可重试内部错误说不清原因 → 重试和去系统设置都给（选区改写没有模型时就是这样失败的）", () => {
    const info = wrAiError(apiError("INTERNAL_ERROR", { status: 500, details: { retryable: false } }));
    expect(info).toMatchObject({ kind: "unclear", actionLabel: "重试", offersSettings: true });
    expect(info.message).toContain("系统设置");
    expect(wrAiError(apiError("DATABASE_BUSY", { status: 503, details: { retryable: true } })).offersSettings).toBeFalsy();
    expect(wrAiError(apiError("AUTHOR_PROPOSAL_LLM_NOT_CONFIGURED", { status: 409 })).offersSettings).toBe(true);
  });

  it("场景没同步好 / 模型没给结果各有自己的说法；从不把服务端的英文原文给作者看", () => {
    expect(wrAiError(wrAiLocalError("no-draft")).kind).toBe("not-ready");
    expect(wrAiError(wrAiLocalError("no-result")).kind).toBe("empty");
    expect(wrAiError(apiError("WHATEVER", { status: 400 })).message).not.toContain("raw english");
    expect(wrAiError(null).kind).toBe("retry");
  });
});

describe("选区改写的字数上限", () => {
  it("选区太长（本地拒绝，没发请求）：说字数、请分段改写；不给「重试」，也不叫作者去配置模型", () => {
    const info = wrAiError(Object.assign(wrAiLocalError("selection-too-long"), { details: { length: 2500, limit: 2000 } }));
    expect(info).toMatchObject({ kind: "too-long", message: "选区太长（2500 字），请分段改写。", actionLabel: "" });
    expect(info.offersSettings).toBeFalsy();
  });

  it("服务端的 PASSAGE_PATCH_TOO_LONG（400）照用它的中文说法；没有中文时按字数说；都不给重试", () => {
    const server = apiError("PASSAGE_PATCH_TOO_LONG", { status: 400, message: "选区太长（2003 字），一次最多改写 2000 字，请分段改写。", details: { length: 2003, limit: 2000 } });
    expect(wrAiError(server)).toMatchObject({ kind: "too-long", message: "选区太长（2003 字），一次最多改写 2000 字，请分段改写。", actionLabel: "" });
    expect(wrAiError(apiError("PASSAGE_PATCH_TOO_LONG", { status: 400, details: { length: 2003 } }))).toMatchObject({ kind: "too-long", message: "选区太长（2003 字），请分段改写。" });
  });
});

describe("局部改写 v4 与深评的拒绝码", () => {
  it("WRITER_PASSAGE_PATCH_EMPTY（502、不带 author_action）→ 模型没给出可用的结果，可以重试；不当成服务器故障，也不叫作者去配置模型", () => {
    const info = wrAiError(apiError("WRITER_PASSAGE_PATCH_EMPTY", { status: 502, details: { reason: "no_options" } }));
    expect(info).toMatchObject({ kind: "empty", actionLabel: "重试" });
    expect(info.message).toContain("模型这次没有给出可用的结果");
    expect(info.offersSettings).toBeFalsy();
    const collapsed = wrAiError(apiError("WRITER_PASSAGE_PATCH_EMPTY", { status: 502, details: { reason: "paragraphs_collapsed" } }));
    expect(collapsed).toMatchObject({ kind: "empty", actionLabel: "重试" });
    expect(collapsed.message).toContain("挤成了一段");
  });

  it("WRITER_DEEP_REVIEW_NO_TEXT / WRITER_PASSAGE_REVIEW_NO_TEXT（409、不带 author_action）→ 说哪里没有字，照用服务端的中文，不给重试", () => {
    const scene = wrAiError(apiError("WRITER_DEEP_REVIEW_NO_TEXT", { status: 409, message: "这一场还没有正文，没有可评的字。先写一段（或起草一稿）再跑 AI 深评。" }));
    expect(scene).toMatchObject({ kind: "no-text", actionLabel: "", message: "这一场还没有正文，没有可评的字。先写一段（或起草一稿）再跑 AI 深评。" });
    expect(scene.offersSettings).toBeFalsy();
    const passage = wrAiError(apiError("WRITER_PASSAGE_REVIEW_NO_TEXT", { status: 409, message: "要看的那一段是空的，没有可看的字。" }));
    expect(passage).toMatchObject({ kind: "no-text", actionLabel: "", message: "要看的那一段是空的，没有可看的字。" });
    expect(wrAiError(apiError("WRITER_PASSAGE_REVIEW_NO_TEXT", { status: 409 })).message).toBe("这一场还没有正文，没有可看的字。");
  });

  it("WRITER_PASSAGE_PATCH_LLM_REQUIRED（409 + author_action）→ 去系统设置；_LLM_FAILED（502）→ 重试", () => {
    expect(wrAiError(apiError("WRITER_PASSAGE_PATCH_LLM_REQUIRED", { status: 409, details: { author_action: { view: "settings" } } })).kind).toBe("config");
    expect(wrAiError(apiError("WRITER_PASSAGE_PATCH_LLM_FAILED", { status: 502 })).kind).toBe("retry");
  });
});

describe("续写候选的方向与快捷词", () => {
  it("按 proposal_source 的方向命名，缺了就按位置", () => {
    expect(wrContinueDirection({ proposal_source: "writer_room_continuation_variants:suspense" }, 0).label).toBe("悬念");
    expect([0, 1, 2].map(i => wrContinueDirection({}, i).label)).toEqual(["动作推进", "关系压力", "悬念"]);
  });

  it("快捷词只放「接着往下写」一类的指令；有设计卡时按本场已填的拍子给方向", () => {
    const labels = wrContinueChips({ beats: [{ label: "目标", text: "拿到钥匙" }, { label: "冲突", text: "" }] }).map(c => c.label);
    expect(labels).toContain("推进到「目标」");
    expect(labels).not.toContain("推进到「冲突」");
    expect(labels.join(" ")).not.toMatch(/删冗余|让节奏更紧/);
    expect(wrContinueChips(null)[0].prompt).toContain("续写下一段");
  });
});

describe("续写候选与调音指令", () => {
  it("generate-set 的响应按方向命名、转义 HTML、按换行分段（空行、行首尾空白不算）；空白的不当成候选", () => {
    const cands = wrContinueCandidates({ proposals: [
      { proposal_id: "p1", proposal_source: "continuation:relationship", content: "她说<好>。\n\n  他没有回答。 \n", rationale: "关系" },
      { proposal_id: "p3", content: "  \n " },
    ] });
    expect(cands).toEqual([{ id: "p1", approach: "关系压力", tone: "info", note: "关系", paras: ["她说&lt;好&gt;。", "他没有回答。"] }]);
  });

  it("一条可用的都没有 → no-result（换个说法重试）", () => {
    expect(() => wrContinueCandidates({ proposals: [{ content: "  " }] })).toThrow(expect.objectContaining({ code: "no-result" }));
    expect(() => wrContinueCandidates({ proposals: [] })).toThrow(expect.objectContaining({ code: "no-result" }));
    expect(() => wrContinueCandidates(null)).toThrow(expect.objectContaining({ code: "no-result" }));
  });

  it("三根滑杆都在中间时是一次自然润色；拉到两端才写进指令", () => {
    expect(wrToneInstr({ warm: 50, expand: 50, direct: 50 })).toContain("自然的文学性润色");
    const instr = wrToneInstr({ warm: 10, expand: 50, direct: 90 });
    expect(instr).toContain("明显更冷峻克制");
    expect(instr).toContain("明显更直白有力");
    expect(instr).not.toContain("凝练");
  });
});

describe("快捷键提示按平台写", () => {
  it("Mac 用 ⌘，其余用 Ctrl", () => {
    expect(modKey({ platform: "MacIntel" })).toBe("⌘");
    expect(modCombo("J", { platform: "MacIntel" })).toBe("⌘J");
    expect(modKey({ platform: "Win32" })).toBe("Ctrl");
    expect(modCombo("J", { platform: "Linux x86_64" })).toBe("Ctrl+J");
  });
});
