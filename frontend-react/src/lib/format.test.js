// lib/format.js：时间与数字文案。钉住各口径的边界（同一家族不同口径不能被合并成一个）。
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  agoLabel, dayTimeLabel, recentOrDayTimeLabel, formatMonthDayTime, isoMonthDay, isoMonthDayTime,
  formatIntOrDash, formatPercentRounded, formatPercent, formatDurationClock, formatMinutesApprox,
  wanFixed, formatCountWan, formatCharsWan,
} from "./format.js";

describe("时间文案", () => {
  afterEach(() => { vi.useRealTimers(); });

  it("agoLabel：刚刚 / 分钟 / 小时 / 天", () => {
    vi.useFakeTimers({ now: new Date(2026, 8, 29, 12, 0, 0) });
    const now = Date.now();
    expect(agoLabel(now - 30_000)).toBe("刚刚");
    expect(agoLabel(now - 5 * 60_000)).toBe("5 分钟前");
    expect(agoLabel(now - 3 * 3600_000)).toBe("3 小时前");
    expect(agoLabel(now - 50 * 3600_000)).toBe("2 天前");
  });

  it("dayTimeLabel 今天 / 跨天；recentOrDayTimeLabel 一小时内说分钟、更早不说「N 小时前」", () => {
    vi.useFakeTimers({ now: new Date(2026, 8, 29, 12, 0, 0) });
    expect(dayTimeLabel(new Date(2026, 8, 29, 8, 5).getTime())).toBe("今天 08:05");
    expect(dayTimeLabel(new Date(2026, 8, 27, 21, 30).getTime())).toBe("9 月 27 日 21:30");
    expect(dayTimeLabel("不是时间")).toBe("");
    expect(recentOrDayTimeLabel(Date.now() - 20 * 60_000)).toBe("20 分钟前");
    expect(recentOrDayTimeLabel(Date.now() - 3 * 3600_000)).toBe("今天 09:00");
    expect(formatMonthDayTime(new Date(2026, 8, 29, 7, 3))).toBe("9 月 29 日 07:03");
    expect(formatMonthDayTime(null)).toBe("");
  });

  it("ISO 串直接截取，不换时区", () => {
    expect(isoMonthDay("2026-09-29")).toBe("09-29");
    expect(isoMonthDayTime("2026-09-29T08:30:00Z")).toBe("09-29 08:30");
    expect(isoMonthDay(null)).toBe("");
  });
});

describe("数字文案", () => {
  it("百分比两种口径、千分位、时长", () => {
    expect(formatIntOrDash(null)).toBe("—");
    expect(formatPercentRounded(0.456)).toBe("46%");
    expect(formatPercentRounded(undefined)).toBe("—");
    expect(formatPercent(0.034)).toBe("3.4%");
    expect(formatPercent(0.03)).toBe("3%");
    expect(formatPercent(0.456)).toBe("46%");
    expect(formatPercent("x")).toBe("—");
    expect(formatDurationClock(125)).toBe("2:05");
    expect(formatMinutesApprox(0.5)).toBe("不到 1 分钟");
    expect(formatMinutesApprox(42)).toBe("约 42 分钟");
    expect(formatMinutesApprox(120)).toBe("约 2 小时");
    expect(formatMinutesApprox(135)).toBe("约 2 小时 15 分钟");
  });

  it("万：定点、token / 字数", () => {
    expect(wanFixed(43800, 1)).toBe("4.4");
    expect(formatCountWan(9800)).toBe((9800).toLocaleString("zh-CN"));
    expect(formatCountWan(43800)).toBe("4.4 万");
    expect(formatCountWan(40000)).toBe("4 万");
    expect(formatCountWan(250000)).toBe("25 万");
    expect(formatCountWan(-1)).toBe("—");
    expect(formatCharsWan(980)).toBe("980 字");
    expect(formatCharsWan(43800)).toBe("4.4 万字");
  });
});
