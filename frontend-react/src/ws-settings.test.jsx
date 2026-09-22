// 设置页：页签只剩真的有用的四个、ws:settings-tab 视图指令直接落到 AI 模型、
// AI 没就绪时按原因说清楚并一键打开对应服务的编辑表单、外观按 ws-prefs.js 的同一份 schema。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(() => Promise.resolve({})),
  apiPatch: vi.fn(() => Promise.resolve({})),
  apiDelete: vi.fn(() => Promise.resolve({})),
  apiAdminGet: vi.fn(() => Promise.resolve({})),
  apiAdminPost: vi.fn(() => Promise.resolve({})),
  apiAdminDelete: vi.fn(() => Promise.resolve({})),
}));

vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: {
    get: () => [],
    subscribe: () => () => {},
    totals: () => ({ words: 1200, written: 1, planned: 3, today: 0 }),
  },
  useCatalogChapters: () => [],
}));

/* 合成的 LLM 配置：一个服务，密钥存着但解不开（本机配置密钥换过） */
function llmOverview() {
  return {
    runtime: { admin_configured: false, secret_configured: true },
    default_provider_id: "relay_a",
    providers: {
      relay_a: {
        provider_id: "relay_a", provider_type: "openai", base_url: "http://relay.example/v1",
        enabled: true, credential_mode: "api_key", api_mode: "responses",
        models: ["model-x", "model-y"],
        secret: { configured: true, decryptable: false, hint: "sk-...WXYZ" },
      },
    },
    node_routes: {
      neutral_draft: { node_id: "neutral_draft", status: "active", requires_llm: true, group: "scene_generation", label: "Neutral draft", provider_id: "relay_a", model: "model-x", ready: false, readiness_reason: "secret_decrypt_failed", order: 1 },
      hard_qc: { node_id: "hard_qc", status: "active", requires_llm: true, group: "quality", label: "Hard QC", provider_id: "relay_a", model: "model-x", ready: false, readiness_reason: "secret_decrypt_failed", order: 2 },
    },
    missing_active_routes: [],
    readiness: {
      ready: false, provider_count: 1, active_provider_count: 0, active_route_count: 2, ready_route_count: 0, blocked_route_count: 2,
      blocked_routes: [
        { node_id: "neutral_draft", provider_id: "relay_a", model: "model-x", reason: "secret_decrypt_failed" },
        { node_id: "hard_qc", provider_id: "relay_a", model: "model-x", reason: "secret_decrypt_failed" },
      ],
    },
    role_slots: [
      { slot_id: "drafting", label_zh: "写作主力", description_zh: "续写与初稿", node_ids: ["neutral_draft"], current: { provider_id: "relay_a", model: "model-x", mixed: false } },
      { slot_id: "review", label_zh: "审稿质检", description_zh: "质检", node_ids: ["hard_qc"], current: { provider_id: "relay_a", model: "model-x", mixed: false } },
    ],
    provider_catalog: { openai: { label: "OpenAI 兼容", credential_modes: ["api_key"] } },
  };
}

async function mountSettings(props = {}) {
  const client = await import("./lib/client.js");
  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/projects") return Promise.resolve({ items: [{ project_id: "prj-s", title: "试写本" }] });
    if (url === "/api/v1/system-config/llm") return Promise.resolve(llmOverview());
    if (url === "/api/v1/system-config/llm/provider-presets") return Promise.resolve({ presets: [], provider_catalog: {} });
    return Promise.resolve({});
  });
  window.localStorage.setItem("ws_active_work_v1", "prj-s");
  const { WsWorks } = await import("./ws-works.jsx");
  await vi.waitFor(() => expect(WsWorks.activeId()).toBe("prj-s"));
  const { WsSettings } = await import("./ws-settings.jsx");
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const setTweak = vi.fn();
  await act(async () => root.render(<WsSettings go={vi.fn()} t={{ theme: "day", texture: true, motion: "standard", mode: "writer", fontSize: 18, lineHeight: 2.05 }} setTweak={setTweak} {...props} />));
  return {
    client, host, setTweak,
    unmount: async () => { await act(async () => root.unmount()); host.remove(); },
  };
}

const navLabels = (host) => Array.from(host.querySelectorAll(".settings-nav-btn")).map(b => b.textContent.trim());
const btn = (el, text) => Array.from(el.querySelectorAll("button")).find(b => b.textContent.includes(text));
const click = async (el) => { await act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); }); };

describe("设置页", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    window.sessionStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("只剩四个真的有用的页签（写作偏好已删除），当前页签带 aria-current", async () => {
    const view = await mountSettings();
    try {
      expect(navLabels(view.host)).toEqual(["项目", "AI 模型", "外观", "数据与安全"]);
      expect(view.host.querySelector('.settings-nav-btn[aria-current="page"]').textContent).toContain("项目");
      expect(view.host.textContent).not.toContain("写作偏好");
    } finally {
      await view.unmount();
    }
  });

  it("项目字段：Esc 放弃刚敲的内容、恢复原值，不会存上去；输入法组词时的回车不提交", async () => {
    const view = await mountSettings();
    const { WsWorks } = await import("./ws-works.jsx");
    const update = vi.spyOn(WsWorks, "update").mockImplementation(() => {});
    const setValue = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
    try {
      const row = Array.from(view.host.querySelectorAll(".set-row")).find(r => r.textContent.includes("题材"));
      const input = row.querySelector("input");
      const stored = input.value;
      const type = async (text) => {
        await act(async () => {
          setValue.call(input, text);
          input.dispatchEvent(new Event("input", { bubbles: true }));
        });
      };
      const key = async (init) => {
        await act(async () => { input.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init })); });
      };

      await act(async () => { input.focus(); });
      await type("改了但想撤销");
      await key({ key: "Escape" });
      expect(update).not.toHaveBeenCalled();
      expect(input.value).toBe(stored);
      expect(document.activeElement).not.toBe(input);

      // 组词中的回车属于输入法：不失焦、不提交
      await act(async () => { input.focus(); });
      await type("组词中");
      await key({ key: "Enter", isComposing: true, keyCode: 229 });
      expect(document.activeElement).toBe(input);
      expect(update).not.toHaveBeenCalled();

      // 普通回车照常保存（证明上面的「没调用」不是因为保存路径本身坏了）
      await key({ key: "Enter" });
      expect(update).toHaveBeenCalledWith("prj-s", { genre: "组词中" });
    } finally {
      await view.unmount();
    }
  });

  it("外观：行距在快捷面板里细调过时，给一个真能改回整档的按钮", async () => {
    window.sessionStorage.setItem("ws_settings_tab_v1", "appear");
    const view = await mountSettings({ t: { theme: "day", texture: true, motion: "standard", mode: "writer", fontSize: 18, lineHeight: 2.15 } });
    try {
      expect(view.host.textContent).toContain("当前 2.15");
      await click(btn(view.host, "改回标准"));
      expect(view.setTweak).toHaveBeenCalledWith("lineHeight", 2.05);
    } finally {
      await view.unmount();
    }
  });

  it("排队的 ws:settings-tab 指令在挂载时直接落到 AI 模型", async () => {
    const intents = await import("./ws-view-intents.js");
    intents.queueViewIntent("settings", "ws:settings-tab", "ai");
    const view = await mountSettings();
    try {
      expect(view.host.querySelector('.settings-nav-btn[aria-current="page"]').textContent).toContain("AI 模型");
      await vi.waitFor(() => expect(view.host.textContent).toContain("接入状态"));
    } finally {
      await view.unmount();
    }
  });

  it("密钥解不开时按原因说清楚，并一键打开这个服务的编辑表单、光标落在密钥栏", async () => {
    const intents = await import("./ws-view-intents.js");
    intents.queueViewIntent("settings", "ws:settings-tab", "ai");
    const view = await mountSettings();
    try {
      await vi.waitFor(() => expect(view.host.textContent).toContain("解不开"));
      expect(view.host.textContent).toContain("受影响的 AI 功能：2 个");
      // 服务卡片上也标出「密钥不可用」，不再像一切正常
      expect(view.host.querySelector(".set-provider").textContent).toContain("密钥不可用");
      await click(btn(view.host, "重新填写密钥"));
      const key = view.host.querySelector('.set-provider-form input[type="password"]');
      expect(key).toBeTruthy();
      expect(document.activeElement).toBe(key);
      expect(view.host.querySelector(".set-provider-form").textContent).toContain("解不开了");
    } finally {
      await view.unmount();
    }
  });

  it("高级路由用中文功能名，不再印后端的英文 label", async () => {
    window.sessionStorage.setItem("ws_settings_tab_v1", "ai");
    const view = await mountSettings();
    try {
      await vi.waitFor(() => expect(view.host.querySelector(".set-advanced")).toBeTruthy());
      const adv = view.host.querySelector(".set-advanced");
      expect(adv.querySelector("summary").textContent.startsWith("高级路由")).toBe(true);
      expect(adv.textContent).toContain("场景初稿");
      expect(adv.textContent).not.toContain("Neutral draft");
      expect(adv.textContent).toContain("密钥解不开");
    } finally {
      await view.unmount();
    }
  });

  it("外观：开关是 role=switch，字号范围来自偏好 schema，动效与界面模式都在", async () => {
    window.sessionStorage.setItem("ws_settings_tab_v1", "appear");
    const view = await mountSettings();
    try {
      const sw = view.host.querySelector('[role="switch"]');
      expect(sw).toBeTruthy();
      expect(sw.getAttribute("aria-checked")).toBe("true");
      const labelId = sw.getAttribute("aria-labelledby");
      expect(labelId && document.getElementById(labelId).textContent).toBe("稿纸纹理");
      const range = view.host.querySelector('input[type="range"]');
      const { WS_PREFS } = await import("./ws-prefs.js");
      expect(Number(range.min)).toBe(WS_PREFS.fontSize.min);
      expect(Number(range.max)).toBe(WS_PREFS.fontSize.max);
      expect(view.host.textContent).toContain("动效");
      expect(view.host.textContent).toContain("界面模式");
      await click(sw);
      expect(view.setTweak).toHaveBeenCalledWith("texture", false);
      const advanced = Array.from(view.host.querySelectorAll('[role="radio"]')).find(b => b.textContent === "高级");
      await click(advanced);
      expect(view.setTweak).toHaveBeenCalledWith("mode", "advanced");
    } finally {
      await view.unmount();
    }
  });

  it("数据与安全：清除本机缓存不碰批注（它只存在本机），确认框说清楚会保留几条、会删掉什么", async () => {
    window.sessionStorage.setItem("ws_settings_tab_v1", "data");
    const view = await mountSettings();
    const anno = JSON.stringify({ v: 1, items: [
      { id: "a1", quote: "一句", note: "批注一" }, { id: "a2", quote: "另一句", note: "批注二" },
    ] });
    window.localStorage.setItem("wr-anno:sc-1::prj-s", anno);
    window.localStorage.setItem("wr-anno:sc-2::prj-s", JSON.stringify({ v: 1, items: [{ id: "a3", quote: "第三句", note: "批注三" }] }));
    window.localStorage.setItem("wr-doc:sc-1::prj-s", "<p>缓存的正文</p>");
    window.localStorage.setItem("ws_snow_state_v2::prj-s", "{}");
    window.localStorage.setItem("wr-doc:sc-9::other-work", "<p>别的作品</p>");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(console, "error").mockImplementation(() => {}); // jsdom 不实现 location.reload，只打一行 Not implemented
    try {
      await click(btn(view.host, "清除缓存"));
      await vi.waitFor(() => expect(confirm).toHaveBeenCalled());
      const message = confirm.mock.calls[0][0];
      expect(message).toContain("3 条批注不在清除范围内");
      expect(message).toContain("回滚快照");
      await vi.waitFor(() => expect(window.localStorage.getItem("wr-doc:sc-1::prj-s")).toBeNull());
      expect(window.localStorage.getItem("ws_snow_state_v2::prj-s")).toBeNull();
      expect(window.localStorage.getItem("wr-anno:sc-1::prj-s")).toBe(anno);
      expect(window.localStorage.getItem("wr-anno:sc-2::prj-s")).not.toBeNull();
      expect(window.localStorage.getItem("wr-doc:sc-9::other-work")).not.toBeNull();
    } finally {
      await view.unmount();
    }
  });

  it("数据与安全：取消清除时本机什么都不动", async () => {
    window.sessionStorage.setItem("ws_settings_tab_v1", "data");
    const view = await mountSettings();
    window.localStorage.setItem("wr-doc:sc-1::prj-s", "<p>缓存的正文</p>");
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    try {
      await click(btn(view.host, "清除缓存"));
      await vi.waitFor(() => expect(confirm).toHaveBeenCalled());
      expect(confirm.mock.calls[0][0]).toContain("本机批注不在清除范围内");
      expect(window.localStorage.getItem("wr-doc:sc-1::prj-s")).not.toBeNull();
    } finally {
      await view.unmount();
    }
  });

  it("数据与安全：快照一行能直接打开同步与恢复中心", async () => {
    window.sessionStorage.setItem("ws_settings_tab_v1", "data");
    const view = await mountSettings();
    const opened = vi.fn();
    window.addEventListener("ws:recovery-open", opened);
    try {
      await click(btn(view.host, "打开同步与恢复"));
      expect(opened).toHaveBeenCalled();
      expect(view.host.textContent).not.toContain("右下角");
    } finally {
      window.removeEventListener("ws:recovery-open", opened);
      await view.unmount();
    }
  });
});
