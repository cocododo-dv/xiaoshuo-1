// lib/format.js：时间与数字文案。钉住各口径的边界（同一家族不同口径不能被合并成一个）。
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  agoLabel, dayTimeLabel, recentOrDayTimeLabel, formatMonthDayTime, isoMonthDay, formatLocalMonthDayTime, localDayKey,
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

  it("日期串直接截取（日桶照原样标）", () => {
    expect(isoMonthDay("2026-09-29")).toBe("09-29");
    expect(isoMonthDay(null)).toBe("");
  });

  /* 按浏览器所在的时区换算：同一个 UTC 时间戳，东八区与 UTC 看到的钟点不同（审计 F05-04 / F05-05） */
  describe("本地时区", () => {
    const tz = process.env.TZ;
    afterEach(() => { if (tz === undefined) delete process.env.TZ; else process.env.TZ = tz; });

    it("formatLocalMonthDayTime：UTC 时间戳按本地钟点显示，不截取 UTC 串", () => {
      process.env.TZ = "Asia/Shanghai";
      expect(formatLocalMonthDayTime("2026-09-29T07:00:00.123456+00:00")).toBe("09-29 15:00");
      expect(formatLocalMonthDayTime("2026-09-29T20:30:00Z")).toBe("09-30 04:30");
      process.env.TZ = "UTC";
      expect(formatLocalMonthDayTime("2026-09-29T07:00:00+00:00")).toBe("09-29 07:00");
      expect(formatLocalMonthDayTime(null)).toBe("");
      expect(formatLocalMonthDayTime("不是时间")).toBe("");
    });

    it("localDayKey：本地日历日，东八区凌晨不算前一天", () => {
      process.env.TZ = "Asia/Shanghai";
      // 北京时间 9 月 30 日 06:00 = UTC 9 月 29 日 22:00
      expect(localDayKey(Date.parse("2026-09-29T22:00:00Z"))).toBe("2026-09-30");
      expect(new Date(Date.parse("2026-09-29T22:00:00Z")).toISOString().slice(0, 10)).toBe("2026-09-29");
      process.env.TZ = "UTC";
      expect(localDayKey(Date.parse("2026-09-29T22:00:00Z"))).toBe("2026-09-29");
      expect(localDayKey("不是时间")).toBe("");
      expect(localDayKey()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    });
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
