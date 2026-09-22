import { describe, expect, it } from "vitest";
import {
  wrAiError, wrAiLocalError, wrContinueCandidates, wrContinueChips, wrContinueDirection, wrToneInstr,
} from "./ws-writer-ai.js";
import { modCombo, modKey } from "./ws-writer-keys.js";

const apiError = (code, extra = {}) => Object.assign(new Error("raw english message"), { code, details: {}, ...extra });

describe("写作台 AI 失败提示按错误代码分流", () => {
  it("没配模型（*_LLM_NOT_CONFIGURED / author_action / configure_*）→ 去系统设置，而不是让作者白等重试", () => {
    expect(wrAiError(apiError("AUTHOR_PROPOSAL_LLM_NOT_CONFIGURED", { status: 409 })).kind).toBe("config");
    expect(wrAiError(apiError("SOMETHING", { details: { author_action: { view: "settings" } } })).kind).toBe("config");
    expect(wrAiError(apiError("WRITER_DEEP_REVIEW_LLM_FAILED", { details: { next_action: "configure_writer_deep_review_route_and_retry" } })).kind).toBe("config");
    expect(wrAiError(wrAiLocalError("no-model"))).toMatchObject({ kind: "config", actionLabel: "去系统设置" });
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
  it("generate-set 的响应按方向命名、转义 HTML、去掉换行；离线占位不当成候选", () => {
    const cands = wrContinueCandidates({ proposals: [
      { proposal_id: "p1", proposal_source: "continuation:relationship", content: "她说<好>\n。", rationale: "关系" },
      { proposal_id: "p2", content: "占位", rationale: "offline deterministic stub" },
      { proposal_id: "p3", content: "  " },
    ] });
    expect(cands).toEqual([{ id: "p1", approach: "关系压力", tone: "slate", note: "关系", html: "她说&lt;好&gt;。" }]);
  });

  it("一条可用的都没有：全是离线占位 → no-model（去系统设置），否则 → no-result（换个说法重试）", () => {
    expect(() => wrContinueCandidates({ proposals: [{ content: "占位", rationale: "Offline Deterministic" }] }))
      .toThrow(expect.objectContaining({ code: "no-model" }));
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
