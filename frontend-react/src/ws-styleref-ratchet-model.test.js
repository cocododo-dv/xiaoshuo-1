// 风格参考 ·「配置对比」的说法与判断（纯函数）：表单 → 请求体、两边一样、预估一句话、历史徽标、揭晓后的结论、读的与数字一致不一致、盲读选择的切换。
import { describe, expect, it } from "vitest";
import {
  srRatchetAgreement, srRatchetArmDiff, srRatchetBody, srRatchetDefaultForm, srRatchetPreviewStats, srRatchetReadingView,
  srRatchetRunTag, srRatchetRunTitle, srRatchetSameArms, srRatchetToggleVote, srRatchetVerdictStats, srRatchetVerdictView,
  srRatchetVotesDone,
} from "./ws-styleref-ratchet-model.js";

const CURRENT = { model: "flash-x", windows: 6, placement: "user_tail" };

describe("配置对比 · 表单", () => {
  it("A、B 都从现在的配置起步；一样就比不了，改一项或选 A/A 就行；B 栏标出和 A 不一样的项", () => {
    const form = srRatchetDefaultForm(CURRENT);
    expect(srRatchetSameArms(form)).toBe(true);
    expect(srRatchetSameArms({ ...form, aa: true })).toBe(false);
    const changed = { ...form, b: { ...form.b, windows: 10, model: "opus-y" } };
    expect(srRatchetSameArms(changed)).toBe(false);
    expect(srRatchetArmDiff(changed)).toEqual(["model", "windows"]);
    expect(srRatchetArmDiff({ ...changed, aa: true })).toEqual([]);
  });

  it("请求体：A/A 的 B = A 再写一份（rep 2）；空的窗数不送", () => {
    const form = srRatchetDefaultForm(CURRENT, { passages: 3 });
    expect(srRatchetBody({ ...form, aa: true })).toEqual({
      a: { model: "flash-x", windows: 6, placement: "user_tail" },
      b: { model: "flash-x", windows: 6, placement: "user_tail", rep: 2 },
      passages: 3,
    });
    expect(srRatchetBody({ ...form, b: { ...form.b, windows: "" } }).b).toEqual({ model: "flash-x", placement: "user_tail" });
  });

  it("预估：调几次、多少字、几份稿子上次写过", () => {
    const passages = [{}, {}, {}];
    const fresh = srRatchetPreviewStats({ calls: 9, plot_calls: 3, draft_calls: { A: 3, B: 3 }, passages, input_chars_estimate: 61000 });
    expect(fresh.map((s) => [s.label, s.value, s.unit])).toEqual([["调用模型", 9, "次"], ["输入约", "6.1", "万字"], ["上次写过", 0, "份"]]);
    expect(fresh[0].hint).toBe("要点 3 · A 3 · B 3");
    const reuse = srRatchetPreviewStats({ calls: 3, plot_calls: 0, draft_calls: { A: 0, B: 3 }, passages, input_chars_estimate: 9000 });
    expect(reuse.map((s) => s.value)).toEqual([3, "9,000", 3]);
    expect(reuse[2].hint).toBe("不再写");
  });

  it("一次比较的名字只说 B 改了什么；只差「第几份」= 量噪声", () => {
    const a = { model: "flash-x", windows: 12, placement: "user_tail" };
    expect(srRatchetRunTitle({ A: a, B: { ...a, windows: 8 } })).toBe("样例 12 → 8 窗");
    expect(srRatchetRunTitle({ A: a, B: { ...a, model: "opus-y", placement: "system" } }))
      .toBe("模型 flash-x → opus-y；样例放在要求后面 → 放在系统提示里");
    expect(srRatchetRunTitle({ A: a, B: { ...a, rep: 2 } })).toBe("量噪声 · flash-x · 样例 12 窗");
  });
});

describe("配置对比 · 结果", () => {
  it("历史徽标：揭晓前只说盲读进度，揭晓后才说谁更像", () => {
    const base = { status: "succeeded", reading: { done: 1, total: 4 } };
    expect(srRatchetRunTag({ ...base, revealed: false, winner: "B" })).toEqual({ tone: "info", label: "待盲读 1/4" });
    expect(srRatchetRunTag({ ...base, revealed: true, winner: "B" }).label).toBe("B 更像");
    expect(srRatchetRunTag({ ...base, revealed: true, winner: null }).label).toBe("分不出");
    expect(srRatchetRunTag({ ...base, revealed: true, same_config: true, noise: 0.2134 }).label).toBe("噪声 0.213");
    expect(srRatchetRunTag({ status: "failed" }).tone).toBe("danger");
  });

  it("结论：B 每段都更近才算赢；A/A 说的是噪声", () => {
    const verdict = { winner: "B", wins_a: 0, wins_b: 3, mean_gain: 0.41, epsilon: 0.2, passages: 3 };
    expect(srRatchetVerdictView({ verdict, epsilon_source: "缺省值" })).toMatchObject({ tone: "ok", title: "B 更像" });
    expect(srRatchetVerdictStats({ verdict, epsilon_source: "缺省值" }).map((s) => [s.label, s.value]))
      .toEqual([["结论", "B 更像"], ["B 更近", "3/3"], ["B 平均近", "0.410"], ["门槛", "0.200"]]);
    expect(srRatchetVerdictStats({ verdict: { ...verdict, winner: "A", mean_gain: -0.3 } })[2]).toMatchObject({ label: "A 平均近", value: "0.300" });
    expect(srRatchetVerdictStats({ verdict, same_config: true, noise: 0.19 })[0]).toMatchObject({ label: "噪声（平均差）", value: "0.190" });
    const none = srRatchetVerdictView({ verdict: { ...verdict, winner: null, wins_a: 1, wins_b: 2, mean_gain: 0.05 } });
    expect(none.title).toBe("分不出");
    expect(none.text).toContain("B 更近 2 段、A 更近 1 段");
    expect(srRatchetVerdictView({ verdict, same_config: true, noise: 0.19 }).title).toContain("0.190");
  });

  it("读出来的与数字：一致 / 相反（以读的为准）；读成平手不算矛盾", () => {
    const summary = { verdict: { winner: "B" } };
    const readB = { done: 3, prefer: { A: 0, B: 3, tie: 0 }, original_best: 2 };
    expect(srRatchetReadingView(readB).text).toBe("你读的：B 排在 A 前面 3 段、A 在前 0 段；原文被你认作最像 2/3 段。");
    expect(srRatchetReadingView(readB, { sameConfig: true }).text).toBe("你读的：原文被你认作最像 2/3 段。");
    expect(srRatchetAgreement(summary, readB).tone).toBe("ok");
    expect(srRatchetAgreement(summary, { ...readB, prefer: { A: 2, B: 1, tie: 0 } }).tone).toBe("warn");
    expect(srRatchetAgreement(summary, { ...readB, prefer: { A: 1, B: 1, tie: 1 } })).toBeNull();
    expect(srRatchetAgreement({ verdict: { winner: null } }, readB)).toBeNull();
  });
});

describe("配置对比 · 盲读选择", () => {
  it("同一份不能既最像又最不像；再点一次取消；两项都选了才算读完", () => {
    let vote = srRatchetToggleVote(null, "best", "甲");
    expect(vote).toEqual({ best: "甲", worst: null });
    vote = srRatchetToggleVote(vote, "worst", "甲");
    expect(vote).toEqual({ best: null, worst: "甲" });
    vote = srRatchetToggleVote(vote, "best", "乙");
    expect(vote).toEqual({ best: "乙", worst: "甲" });
    expect(srRatchetToggleVote(vote, "best", "乙")).toEqual({ best: null, worst: "甲" });
    const reading = [{ index: 1 }, { index: 2 }];
    expect(srRatchetVotesDone(reading, { 1: vote, 2: { best: "丙" } })).toBe(1);
  });
});
