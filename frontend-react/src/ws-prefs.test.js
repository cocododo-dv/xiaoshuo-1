// 界面偏好 schema（ws-prefs.js）单测：一份默认值、一份范围；旧的 ws_tweaks_v1 原样读得回来。
import { describe, expect, it } from "vitest";
import {
  WS_LINE_HEIGHT_PRESETS, WS_PREFS, WS_PREF_DEFAULTS, clampPref, lineHeightPreset, normalizePrefs, prefDefaults, prefKeys,
} from "./ws-prefs.js";

describe("ws-prefs schema", () => {
  it("默认值只有一份，覆盖 ws_tweaks_v1 的全部键", () => {
    expect(Object.keys(WS_PREF_DEFAULTS).sort()).toEqual([
      "ambient", "aiPlace", "focus", "fontSize", "lineHeight", "measure", "mode", "motion",
      "scnDensity", "scnFont", "scnLog",
      "texture", "theme", "typewriter", "wrLayout",
    ].sort());
    expect(WS_PREF_DEFAULTS).toMatchObject({ theme: "day", mode: "writer", fontSize: 18, lineHeight: 2.05, scnFont: 16 });
    expect(prefDefaults("writer")).toEqual({
      wrLayout: "desk", focus: "light", ambient: true, typewriter: false, aiPlace: "tray", measure: 680, fontSize: 18, lineHeight: 2.05,
    });
  });

  it("按 scope / group 取键，顺序即面板里的顺序", () => {
    expect(prefKeys("writer", "paper")).toEqual(["measure", "fontSize", "lineHeight"]);
    expect(prefKeys("scene")).toEqual(["scnFont", "scnDensity", "scnLog"]);
    // 已删除的前端质检阈值不再有控件
    expect(prefKeys("sceneQc")).toEqual([]);
  });

  it("clampPref 把值收进范围并按步长取整；非法枚举 / 类型回到默认", () => {
    expect(clampPref("fontSize", 40)).toBe(WS_PREFS.fontSize.max);
    expect(clampPref("fontSize", 3)).toBe(WS_PREFS.fontSize.min);
    expect(clampPref("lineHeight", 2.0500000001)).toBe(2.05);
    expect(clampPref("theme", "neon")).toBe("day");
    expect(clampPref("texture", "yes")).toBe(true);
    expect(clampPref("unknownKey", "原样")).toBe("原样");
  });

  it("normalizePrefs：旧数据原样读回、缺的键补默认、未知键保留", () => {
    const saved = { theme: "night", fontSize: 21, scnShort: 70, legacyFlag: 1 };
    const out = normalizePrefs(saved);
    expect(out).toMatchObject({ theme: "night", fontSize: 21, scnShort: 70, legacyFlag: 1, mode: "writer", measure: 680 });
    expect(normalizePrefs(null)).toEqual({ ...WS_PREF_DEFAULTS });
  });

  it("设置·外观的三档行距与面板的连续行距读写同一个值", () => {
    expect(WS_LINE_HEIGHT_PRESETS.map(p => p.value)).toEqual(["snug", "normal", "airy"]);
    WS_LINE_HEIGHT_PRESETS.forEach(p => expect(lineHeightPreset(p.lineHeight)).toBe(p.value));
    expect(lineHeightPreset(undefined)).toBe("normal");
  });
});
