// lib/events.js：广播、模块级监听的跨重载去重。
import { describe, expect, it, vi } from "vitest";
import { adoptModuleListeners, emit, retireModuleListeners } from "./events.js";

describe("emit", () => {
  it("带 detail / 不带 detail 的广播；不带时 detail 是 null", () => {
    const seen = [];
    const on = (event) => seen.push([event.type, event.detail]);
    window.addEventListener("ws:test-event", on);
    emit("ws:test-event", { a: 1 });
    emit("ws:test-event");
    window.removeEventListener("ws:test-event", on);
    expect(seen).toEqual([["ws:test-event", { a: 1 }], ["ws:test-event", null]]);
  });

  it("没有 CustomEvent 的环境里静默（不抛）", () => {
    const original = globalThis.CustomEvent;
    globalThis.CustomEvent = undefined;
    try { expect(() => emit("ws:test-event", 1)).not.toThrow(); } finally { globalThis.CustomEvent = original; }
  });
});

describe("retire / adoptModuleListeners", () => {
  it("模块重新执行时先撤掉上一个实例登记的清理函数，只撤一次", () => {
    const first = vi.fn();
    retireModuleListeners("test-owner");
    adoptModuleListeners("test-owner", first);
    retireModuleListeners("test-owner");
    expect(first).toHaveBeenCalledTimes(1);
    retireModuleListeners("test-owner");
    expect(first).toHaveBeenCalledTimes(1);
    const second = vi.fn();
    adoptModuleListeners("test-owner", second);
    const other = vi.fn();
    adoptModuleListeners("other-owner", other);
    retireModuleListeners("test-owner");
    expect(second).toHaveBeenCalledTimes(1);
    expect(other).not.toHaveBeenCalled();
    retireModuleListeners("other-owner");
  });
});
