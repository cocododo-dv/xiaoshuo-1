// lib/poll.js：看页面可见性的轮询——隐藏时放慢（或暂停），回到前台立刻问一次，run 返回 false 就停。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPoller } from "./poll.js";

function setHidden(hidden) {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (hidden ? "hidden" : "visible") });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("createPoller", () => {
  beforeEach(() => { vi.useFakeTimers(); setHidden(false); });
  afterEach(() => { vi.useRealTimers(); setHidden(false); });

  it("按间隔问，run 返回 false 就停", async () => {
    let n = 0;
    const run = vi.fn(async () => { n += 1; return n < 3; });
    const poller = createPoller({ run, interval: 1000 });
    poller.start();
    await vi.advanceTimersByTimeAsync(999);
    expect(run).toHaveBeenCalledTimes(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(run).toHaveBeenCalledTimes(3);
    expect(poller.active()).toBe(false);
  });

  it("页面隐藏时放慢到 hiddenInterval；回到前台立刻问一次，再按原节奏", async () => {
    const run = vi.fn(async () => true);
    const poller = createPoller({ run, interval: 1000, hiddenInterval: 30_000 });
    poller.start(0);
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(1);
    setHidden(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(run).toHaveBeenCalledTimes(2); // 隐藏前排好的那一次照常问；之后按 30 秒
    await vi.advanceTimersByTimeAsync(10_000);
    expect(run).toHaveBeenCalledTimes(2);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1000);
    expect(run).toHaveBeenCalledTimes(4);
    poller.stop();
  });

  it("hiddenInterval 为 null：隐藏时整个暂停；stop 之后回到前台也不再问", async () => {
    const run = vi.fn(async () => true);
    const poller = createPoller({ run, interval: 1000, hiddenInterval: null });
    setHidden(true);
    poller.start();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(run).toHaveBeenCalledTimes(0);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(run).toHaveBeenCalledTimes(1);
    poller.stop();
    setHidden(true);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("间隔可以是函数（每轮重算），run 抛错照常继续", async () => {
    let calls = 0;
    const run = vi.fn(async () => { calls += 1; if (calls === 1) throw new Error("网络抖动"); return calls < 3; });
    const poller = createPoller({ run, interval: () => (calls < 2 ? 100 : 500) });
    poller.start();
    await vi.advanceTimersByTimeAsync(100);
    expect(run).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(100);
    expect(run).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(499);
    expect(run).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(run).toHaveBeenCalledTimes(3);
    expect(poller.active()).toBe(false);
  });
});
