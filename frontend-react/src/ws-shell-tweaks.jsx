import React from "react";
import { TweakRadio, TweakSection, TweakSlider, TweakToggle } from "./tweaks-panel.jsx";
import { WS_PREFS, WS_PREF_WRITER_GROUPS, prefDefaults, prefKeys } from "./ws-prefs.js";

/* 「排版与舒适度」面板里的各组控件，全部按 ws-prefs.js 的 schema 生成（范围、选项、默认值只有一份）。
   · GlobalTweaks：全局外观（主题 / 稿纸纹理 / 动效），在哪个页面打开都有；
   · WriterTweaks：写作台自己的布局、专注与稿纸排版，只在写作台打开时出现；
   · SceneTweaks：AI 起草台的显示偏好，只在 AI 起草台打开时出现（前端质检阈值已删除，判定只看后端闸门）。
   WriterTweaks / SceneTweaks / WRITER_TWEAK_DEFAULTS 的导出名保持不变（写作台与起草台从这里引用）。 */

const WRITER_TWEAK_DEFAULTS = Object.freeze(prefDefaults("writer"));

function PrefControl({ prefKey, t, setTweak }) {
  const spec = WS_PREFS[prefKey];
  if (!spec) return null;
  const value = t && t[prefKey] !== undefined ? t[prefKey] : spec.default;
  const onChange = (next) => setTweak(prefKey, next);
  if (spec.type === "bool") return <TweakToggle label={spec.label} hint={spec.hint} value={value !== false} onChange={onChange} />;
  if (spec.type === "enum") return <TweakRadio label={spec.label} hint={spec.hint} value={value} options={spec.options} onChange={onChange} />;
  if (spec.type === "range") {
    return <TweakSlider label={spec.label} hint={spec.hint} value={value} min={spec.min} max={spec.max} step={spec.step} unit={spec.unit || ""} onChange={onChange} />;
  }
  return null;
}

function PrefList({ keys, t, setTweak }) {
  return keys.map((key) => <PrefControl key={key} prefKey={key} t={t} setTweak={setTweak} />);
}

function GlobalTweaks({ t, setTweak }) {
  // 界面模式在侧栏底部有自己的开关，这里不重复
  return (
    <>
      <TweakSection label="外观" />
      <PrefList keys={prefKeys("global").filter((key) => key !== "mode")} t={t} setTweak={setTweak} />
    </>
  );
}

function WriterTweaks({ t, setTweak }) {
  const tw = { ...WRITER_TWEAK_DEFAULTS, ...(t || {}) };
  return (
    <>
      {WS_PREF_WRITER_GROUPS.map((group) => (
        <React.Fragment key={group.id}>
          <TweakSection label={group.label} />
          <PrefList keys={prefKeys("writer", group.id)} t={tw} setTweak={setTweak} />
        </React.Fragment>
      ))}
    </>
  );
}

function SceneTweaks({ t, setTweak }) {
  return (
    <>
      <TweakSection label="AI 起草台" />
      <PrefList keys={prefKeys("scene")} t={t} setTweak={setTweak} />
    </>
  );
}

export { GlobalTweaks, SceneTweaks, WriterTweaks, WRITER_TWEAK_DEFAULTS };
