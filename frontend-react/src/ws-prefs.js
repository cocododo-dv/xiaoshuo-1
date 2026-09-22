import React from "react";

/* ==========================================================
   界面偏好：一份 schema（键、取值范围、选项、默认值）
   「排版与舒适度」面板现在按它生成控件；设置·外观之后也改读它——
   过去同一个「正文字号」在面板里是 15–24、在设置里是 14–22，行距一处是连续滑杆、一处是三档，
   默认值在 ws-app.jsx 和 ws-shell-tweaks.jsx 各抄一份。
   存储仍是 localStorage 的 ws_tweaks_v1（键名与形状不变，旧数据原样读得回来）。
   纯 ESM，不写 window。
   ========================================================== */

const WS_PREFS_LS = "ws_tweaks_v1";

/* scope：global（全局外观）/ writer（写作台）/ scene（AI 起草台显示）。
   起草台的前端质检阈值（scnShort / scnRepeat / scnLong）与戏剧卡边条（scnBeats）已随本地质检启发式一起删除：
   后端闸门是唯一的判定。旧数据里的这些键由 normalizePrefs 原样保留，不再有控件。 */
const WS_PREFS = {
  theme: {
    scope: "global", label: "主题", type: "enum", default: "day",
    options: [{ value: "day", label: "白昼" }, { value: "dusk", label: "暮色" }, { value: "night", label: "夜灯" }],
  },
  texture: { scope: "global", label: "稿纸纹理", type: "bool", default: true },
  motion: {
    scope: "global", label: "动效", type: "enum", default: "standard",
    options: [{ value: "off", label: "关" }, { value: "subtle", label: "轻" }, { value: "standard", label: "标准" }],
  },
  mode: {
    scope: "global", label: "界面模式", type: "enum", default: "writer",
    options: [{ value: "writer", label: "作家" }, { value: "advanced", label: "高级" }],
  },

  wrLayout: {
    scope: "writer", group: "layout", label: "布局", type: "enum", default: "desk",
    options: [{ value: "desk", label: "书桌三栏" }, { value: "immersive", label: "沉浸稿纸" }],
  },
  focus: {
    scope: "writer", group: "focus", label: "柔和专注", hint: "暗化当前段以外的段落", type: "enum", default: "light",
    options: [{ value: "off", label: "关" }, { value: "light", label: "轻" }, { value: "medium", label: "中" }, { value: "deep", label: "深" }],
  },
  ambient: { scope: "writer", group: "focus", label: "当前行氛围光", type: "bool", default: true },
  typewriter: { scope: "writer", group: "focus", label: "打字机滚动", hint: "当前行保持在屏幕中间", type: "bool", default: false },
  aiPlace: {
    scope: "writer", group: "focus", label: "AI 候选位置", type: "enum", default: "tray",
    options: [{ value: "tray", label: "底部托盘" }, { value: "drawer", label: "右侧抽屉" }],
  },
  measure: { scope: "writer", group: "paper", label: "稿纸宽度", type: "range", default: 680, min: 560, max: 860, step: 20, unit: "px" },
  fontSize: { scope: "writer", group: "paper", label: "正文字号", type: "range", default: 18, min: 14, max: 24, step: 1, unit: "px" },
  lineHeight: { scope: "writer", group: "paper", label: "行距", type: "range", default: 2.05, min: 1.5, max: 2.6, step: 0.05 },

  scnFont: { scope: "scene", label: "正文字号", type: "range", default: 16, min: 15, max: 20, step: 1, unit: "px" },
  scnDensity: {
    scope: "scene", label: "证据栏密度", type: "enum", default: "cozy",
    options: [{ value: "cozy", label: "疏朗" }, { value: "compact", label: "紧凑" }],
  },
  scnLog: { scope: "scene", label: "运行记录默认展开", type: "bool", default: true },

};

/* 写作台面板里的分组次序与标题 */
const WS_PREF_WRITER_GROUPS = [
  { id: "layout", label: "工作台布局" },
  { id: "focus", label: "专注与协作" },
  { id: "paper", label: "稿纸排版" },
];

/* 设置·外观的行距是三档；和面板的连续滑杆读写同一个 lineHeight */
const WS_LINE_HEIGHT_PRESETS = [
  { value: "snug", label: "紧凑", lineHeight: 1.8 },
  { value: "normal", label: "标准", lineHeight: 2.05 },
  { value: "airy", label: "宽松", lineHeight: 2.3 },
];

const WS_PREF_DEFAULTS = Object.freeze(Object.fromEntries(Object.entries(WS_PREFS).map(([key, spec]) => [key, spec.default])));

function prefKeys(scope, group) {
  return Object.keys(WS_PREFS).filter((key) => WS_PREFS[key].scope === scope && (!group || WS_PREFS[key].group === group));
}

function prefDefaults(scope) {
  return Object.fromEntries(prefKeys(scope).map((key) => [key, WS_PREFS[key].default]));
}

/* 把一个值收进 schema 允许的范围；schema 之外的键原样返回 */
function clampPref(key, value) {
  const spec = WS_PREFS[key];
  if (!spec) return value;
  if (spec.type === "bool") return typeof value === "boolean" ? value : spec.default;
  if (spec.type === "enum") return spec.options.some((o) => o.value === value) ? value : spec.default;
  if (spec.type === "range") {
    const n = Number(value);
    if (!Number.isFinite(n)) return spec.default;
    const clamped = Math.min(spec.max, Math.max(spec.min, n));
    const decimals = (String(spec.step).split(".")[1] || "").length;
    return Number(clamped.toFixed(decimals));
  }
  return value;
}

/* 读回的偏好：默认值打底，已知键收进范围，未知键原样保留（向后兼容） */
function normalizePrefs(saved) {
  const out = { ...WS_PREF_DEFAULTS };
  if (saved && typeof saved === "object") {
    Object.keys(saved).forEach((key) => { out[key] = WS_PREFS[key] ? clampPref(key, saved[key]) : saved[key]; });
  }
  return out;
}

function lineHeightPreset(lineHeight) {
  const lh = Number(lineHeight) || WS_PREFS.lineHeight.default;
  if (lh <= 1.9) return "snug";
  if (lh >= 2.25) return "airy";
  return "normal";
}

function readStoredPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(WS_PREFS_LS));
    return normalizePrefs(saved && typeof saved === "object" ? saved : null);
  } catch (e) {
    return normalizePrefs(null);
  }
}

/* 偏好的单一状态源（App 顶层用一次，往下传 t / setTweak）。setTweak(key, value) 或 setTweak({ ...edits })。 */
function usePrefs() {
  const [values, setValues] = React.useState(readStoredPrefs);
  const setPref = React.useCallback((keyOrEdits, value) => {
    const edits = typeof keyOrEdits === "object" && keyOrEdits !== null ? keyOrEdits : { [keyOrEdits]: value };
    setValues((prev) => {
      const next = { ...prev };
      Object.keys(edits).forEach((key) => { next[key] = clampPref(key, edits[key]); });
      try { localStorage.setItem(WS_PREFS_LS, JSON.stringify(next)); } catch (e) {}
      return next;
    });
  }, []);
  return [values, setPref];
}

export {
  WS_PREFS, WS_PREFS_LS, WS_PREF_DEFAULTS, WS_PREF_WRITER_GROUPS, WS_LINE_HEIGHT_PRESETS,
  prefKeys, prefDefaults, clampPref, normalizePrefs, lineHeightPreset, usePrefs,
};
