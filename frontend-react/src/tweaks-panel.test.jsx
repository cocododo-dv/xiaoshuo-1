import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TweakRadio, TweaksPanel } from "./tweaks-panel.jsx";
import { SceneTweaks, WriterTweaks } from "./ws-shell-tweaks.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root;
let host;

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
});

describe("Tweaks 分段单选", () => {
  it("按钮指针事件与 click 不会重复提交同一次选择", async () => {
    const onChange = vi.fn();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(
      <TweakRadio
        label="稿纸宽度"
        value="narrow"
        options={[{ label: "窄", value: "narrow" }, { label: "宽", value: "wide" }]}
        onChange={onChange}
      />,
    ));

    const wide = host.querySelector('button[aria-label="宽"]');
    await act(async () => {
      wide.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, clientX: 20 }));
      wide.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("wide");
  });
});

async function render(node) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(node));
}

describe("Tweaks 分段单选 · 键盘", () => {
  it("四个一字选项排成分段（不再退成下拉框），方向键在组内移动并选中", async () => {
    const onChange = vi.fn();
    await render(
      <TweakRadio label="柔和专注" value="light"
        options={[{ value: "off", label: "关" }, { value: "light", label: "轻" }, { value: "medium", label: "中" }, { value: "deep", label: "深" }]}
        onChange={onChange} />,
    );
    expect(host.querySelector("select")).toBeNull();
    const radios = [...host.querySelectorAll('[role="radio"]')];
    expect(radios.map(r => r.getAttribute("tabindex"))).toEqual(["-1", "0", "-1", "-1"]);
    await act(async () => { radios[1].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(onChange).toHaveBeenLastCalledWith("medium");
    await act(async () => { radios[1].dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true })); });
    expect(onChange).toHaveBeenLastCalledWith("deep");
  });

  it("方向键从有焦点的那段数起：焦点在没选中的段上时，→ 走到它的下一段，而不是从选中段反着走", async () => {
    const onChange = vi.fn();
    await render(
      <TweakRadio label="主题" value="night"
        options={[{ value: "day", label: "白昼" }, { value: "dusk", label: "暮色" }, { value: "night", label: "夜灯" }]}
        onChange={onChange} />,
    );
    const [day, dusk] = [...host.querySelectorAll('[role="radio"]')];
    day.focus();
    await act(async () => { day.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(onChange).toHaveBeenLastCalledWith("dusk");
    expect(document.activeElement).toBe(dusk);
  });
});

describe("排版与舒适度面板", () => {
  it("ws:tweaks-open 打开一个带中文标题的对话框，焦点进面板；Esc 关闭并把焦点还给打开它的按钮", async () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    await render(
      <TweaksPanel title="排版与舒适度">
        <TweakRadio label="主题" value="day" options={[{ value: "day", label: "白昼" }, { value: "night", label: "夜灯" }]} onChange={() => {}} />
      </TweaksPanel>,
    );
    expect(document.querySelector(".twk-panel")).toBeNull();
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:tweaks-open")); });
    const panel = document.querySelector(".twk-panel");
    expect(panel.getAttribute("role")).toBe("dialog");
    expect(panel.textContent).toContain("排版与舒适度");
    expect(panel.querySelector(".twk-x").getAttribute("aria-label")).toBe("关闭排版与舒适度");
    expect(panel.contains(document.activeElement)).toBe(true);

    await act(async () => { document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(document.querySelector(".twk-panel")).toBeNull();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("打开时焦点落在选中的那段上（夜灯主题下是「夜灯」，不是排在前面、tabindex=-1 的「白昼」）", async () => {
    await render(
      <TweaksPanel title="排版与舒适度">
        <TweakRadio label="主题" value="night" options={[{ value: "day", label: "白昼" }, { value: "night", label: "夜灯" }]} onChange={() => {}} />
      </TweaksPanel>,
    );
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:tweaks-open")); });
    expect(document.activeElement.textContent).toBe("夜灯");
    expect(document.activeElement.getAttribute("tabindex")).toBe("0");
  });

  it("写作台 / 起草台的控件按 schema 生成：范围与默认值只有一份", async () => {
    const setTweak = vi.fn();
    await render(<div><WriterTweaks t={{}} setTweak={setTweak} /><SceneTweaks t={{}} setTweak={setTweak} /></div>);
    const slider = (label) => host.querySelector(`input[type="range"][aria-label="${label}"]`);
    const font = [...host.querySelectorAll('input[type="range"][aria-label="正文字号"]')];
    expect(font.map(n => [n.min, n.max, n.value])).toEqual([["14", "24", "18"], ["15", "20", "16"]]);
    // 前端质检阈值已删除（起草台的判定只看后端闸门），面板里不再有这组滑杆
    expect(slider("短句率目标")).toBeNull();
    expect([...host.querySelectorAll(".twk-sect")].map(n => n.textContent)).toEqual(["工作台布局", "专注与协作", "稿纸排版", "AI 起草台"]);
    await act(async () => { host.querySelector('[role="switch"][aria-label="打字机滚动"]').click(); });
    expect(setTweak).toHaveBeenCalledWith("typewriter", true);
  });
});
