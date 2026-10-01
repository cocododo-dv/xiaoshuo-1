import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  clearViewIntents,
  flushViewIntents,
  navigateWithViewIntent,
  queueViewIntent,
  queueViewIntents,
  setViewIntentTargetReady,
  writerIntents,
} from "./ws-view-intents.js";

describe("跨页面意图队列", () => {
  beforeEach(() => {
    clearViewIntents();
    history.replaceState(null, "", "#home");
    vi.useRealTimers();
  });

  it("等目标页面完成挂载后再按顺序投递事件", () => {
    const seen = [];
    const onScene = (event) => seen.push([event.type, event.detail]);
    const onPosture = (event) => seen.push([event.type, event.detail]);
    window.addEventListener("ws:writer-scene", onScene);
    window.addEventListener("ws:writer-posture", onPosture);

    queueViewIntents("writer", [
      { type: "ws:writer-scene", detail: "SC-9" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);
    expect(seen).toEqual([]);
    expect(flushViewIntents("writer")).toBe(2);
    expect(seen).toEqual([
      ["ws:writer-scene", "SC-9"],
      ["ws:writer-posture", "deep"],
    ]);
    expect(flushViewIntents("writer")).toBe(0);

    window.removeEventListener("ws:writer-scene", onScene);
    window.removeEventListener("ws:writer-posture", onPosture);
  });

  it("同一视图的高频指令有上限，避免未挂载页面无限占用内存", () => {
    const received = [];
    const handler = (event) => received.push(event.detail);
    window.addEventListener("ws:test-intent", handler);
    for (let index = 0; index < 40; index += 1) {
      queueViewIntent("writer", "ws:test-intent", index);
    }
    expect(flushViewIntents("writer")).toBe(24);
    expect(received).toEqual([...Array(24)].map((_, index) => index + 16));
    window.removeEventListener("ws:test-intent", handler);
  });

  it("直接跨模块导航时先保存意图，再触发 hash 路由", () => {
    expect(navigateWithViewIntent("scene", "ws:scene-enqueue", { sid: "SC-2" })).toBe(true);
    expect(location.hash).toBe("#scene");
    const received = [];
    const handler = (event) => received.push(event.detail);
    window.addEventListener("ws:scene-enqueue", handler);
    flushViewIntents("scene");
    expect(received).toEqual([{ sid: "SC-2" }]);
    window.removeEventListener("ws:scene-enqueue", handler);
  });

  it("要求就绪时不会提前消费，目标注册监听器后自动领取", () => {
    const received = [];
    queueViewIntent("snowflake", "ws:snow-step", "planning");
    expect(flushViewIntents("snowflake", window, { onlyWhenReady: true })).toBe(0);
    const handler = (event) => received.push(event.detail);
    window.addEventListener("ws:snow-step", handler);
    expect(setViewIntentTargetReady("snowflake")).toBe(1);
    expect(received).toEqual(["planning"]);
    setViewIntentTargetReady("snowflake", false);
    window.removeEventListener("ws:snow-step", handler);
  });
});

describe("去写作台的一组意图（writerIntents）", () => {
  it("有场：先定位这一场，再带深改姿态；带发现的 signal_id 时深改面板选中它", () => {
    expect(writerIntents("SC-1")).toEqual([{ type: "ws:writer-scene", detail: "SC-1" }]);
    expect(writerIntents("SC-1", { deep: true })).toEqual([
      { type: "ws:writer-scene", detail: "SC-1" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);
    expect(writerIntents("SC-1", { signalId: "sig-9" })).toEqual([
      { type: "ws:writer-scene", detail: "SC-1" },
      { type: "ws:writer-posture", detail: { posture: "deep", signal_id: "sig-9" } },
    ]);
  });

  it("没有场：深改 / 发现不带姿态（章级结果只回写作台）；点名的姿态照样带上", () => {
    expect(writerIntents("", { deep: true })).toEqual([]);
    expect(writerIntents(null, { signalId: "sig-9" })).toEqual([]);
    expect(writerIntents(undefined)).toEqual([]);
    expect(writerIntents("", { posture: "deep" })).toEqual([{ type: "ws:writer-posture", detail: "deep" }]);
    expect(writerIntents("SC-2", { posture: "deep" })).toEqual([
      { type: "ws:writer-scene", detail: "SC-2" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);
  });
});
