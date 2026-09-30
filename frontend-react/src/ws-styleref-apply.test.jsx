// 风格参考页 · 第三步「用于作品」：三种参考方式、样例窗数与实测字数、起草方式；直接用于 / 保存 / 解除、旧版全局应用只读、换回旧书沿用旧设置；本场预览。旧界面的策略、强度、开关都不该再出现。
// （2026-09-30 从 ws-styleref.test.jsx 按步拆出，共用夹具在 ws-styleref.test-helpers.jsx。）
import React, { act } from "react";
import { describe, expect, it, vi } from "vitest";

// 没装路由之前（模块加载时目录 store 就会读一次目录）也回一个空载荷，不让它报「拉取目录失败」
vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(() => Promise.resolve({})),
  apiPost: vi.fn(() => Promise.resolve({})),
  apiPatch: vi.fn(() => Promise.resolve({})),
  apiDelete: vi.fn(() => Promise.resolve({})),
  getOperatorRef: vi.fn(() => "operator"),
}));
const workHolder = vi.hoisted(() => ({ current: { id: "w1", title: "北岸手记" } }));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: {
    active: () => workHolder.current,
    activeId: () => (workHolder.current ? workHolder.current.id : null),
  },
}));

import {
  $, $$, API, bookRow, byTestId, click, client, mountView, openStage, OWN_BINDING, PREVIEW, PROFILE_SUMMARY, settle, setupStyleRefSuite, state,
} from "./ws-styleref.test-helpers.jsx";

setupStyleRefSuite(workHolder);

describe("第三步 · 用于作品", () => {
  async function openApply({ own = false, bookOver = {} } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, ...bookOver })];
    if (own) state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    await mountView();
    await openStage("apply");
    await settle();
  }

  it("三种参考方式、样例窗数（旁边是实测字数）、起草方式；没有旧的策略 / 强度 / 开关", async () => {
    await openApply();
    expect($$('[data-testid="sr-reference-mode"] [role="radio"]').map((b) => b.dataset.value)).toEqual(["full", "samples_only", "card_only"]);
    expect($$('[data-testid="sr-draft-mode"] [role="radio"]').map((b) => b.dataset.value)).toEqual(["style_first", "neutral_first"]);
    const slider = byTestId("sr-sample-windows");
    expect(slider.min).toBe("0");
    expect(slider.max).toBe("16");
    expect(slider.value).toBe("12");
    await settle(400);
    expect(byTestId("sr-sample-readout").textContent).toContain("约带 12 窗、4.8 万字原文");
    const previewCall = client.apiPost.mock.calls.find(([url]) => url.endsWith("/injection-preview"));
    expect(previewCall[1]).toMatchObject({ reference_mode: "full", sample_windows: 12 });
    const text = byTestId("sr-apply-form").textContent;
    for (const gone of ["强度", "策略", "任务类型", "全局"]) expect(text, gone).not.toContain(gone);
  });

  it("只用文风卡：窗数锁住并说明不发原文", async () => {
    await openApply();
    await click($('[data-testid="sr-reference-mode"] [data-value="card_only"]'));
    expect(byTestId("sr-sample-windows").disabled).toBe(true);
    expect(byTestId("sr-sample-readout").textContent).toBe("只用文风卡：起草时不发原文段落。");
  });

  it("「起草不发原文」的书：带原文的两种方式用不了", async () => {
    await openApply({ bookOver: { cloud_policy: "segments_only" } });
    expect(byTestId("sr-apply-segments-only")).toBeTruthy();
    expect($('[data-testid="sr-reference-mode"] [data-value="full"]').disabled).toBe(true);
    expect($('[data-testid="sr-reference-mode"] [data-value="card_only"]').getAttribute("aria-checked")).toBe("true");
  });

  it("直接用于当前作品，结果就地说明", async () => {
    await openApply();
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) return Promise.resolve({ binding: OWN_BINDING, created: true, changed: true, replaced: [] });
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    const submit = byTestId("sr-apply-submit");
    expect(submit.textContent).toContain("用于《北岸手记》");
    await click(submit);
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/apply`, {
      scope: "project", scope_ref_id: "w1", config: { reference_mode: "full", sample_windows: 12, draft_mode: "style_first" },
    });
    expect(byTestId("sr-apply-result").textContent).toContain("已用于《北岸手记》");
  });

  it("已在用：改了窗数才能「保存设置」；解除先确认再删", async () => {
    await openApply({ own: true });
    expect(byTestId("sr-apply-status").textContent).toContain("《北岸手记》正在用这本书的文风：全面模仿 · 12 窗 · 作者手笔直起");
    expect(byTestId("sr-apply-submit").disabled).toBe(true);
    const slider = byTestId("sr-sample-windows");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(slider, "4");
      slider.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(byTestId("sr-apply-submit").textContent).toContain("保存设置");
    client.apiPatch.mockResolvedValueOnce({ binding: { ...OWN_BINDING, config: { ...OWN_BINDING.config, sample_windows: 4 } }, changed: true });
    await click(byTestId("sr-apply-submit"));
    await settle();
    expect(client.apiPatch).toHaveBeenCalledWith(`${API}/bindings/bd-1`, { config: { reference_mode: "full", sample_windows: 4, draft_mode: "style_first" } });
    expect(byTestId("sr-apply-result").textContent).toContain("已保存");

    vi.spyOn(window, "confirm").mockReturnValue(true);
    await click(byTestId("sr-apply-unbind"));
    await settle();
    expect(client.apiDelete).toHaveBeenCalledWith(`${API}/bindings/bd-1`);
  });

  it("用着别的书：说清用这本会替换它", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = {
      project_id: "w1", binding: { ...OWN_BINDING, binding_id: "bd-x", profile_id: "pf-x" }, profile: { profile_id: "pf-x" }, book: { book_id: "bk-x", title: "丁书" },
    };
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-other").textContent).toContain("现在用的是《丁书》的文风；用这本会替换它");
    expect(byTestId("sr-apply-submit").textContent).toContain("改用这本");
  });

  it("作品沿用旧版全局应用：只读，不 PATCH / 不 DELETE 那条全局应用；「用于」给这部作品单独建一条（复核 #3）", async () => {
    const GLOBAL = {
      ...OWN_BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null,
      config: { reference_mode: "samples_only", sample_windows: 6, draft_mode: "style_first", dimension_states: { "scene.dialogue": "emphasize" } },
    };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = { project_id: "w1", binding: GLOBAL, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    state.profileBindings = [GLOBAL];
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-legacy").textContent).toContain("沿用一条旧版的「全部作品」应用，用的就是这本书的文风");
    expect(byTestId("sr-apply-unbind")).toBeNull();
    const submit = byTestId("sr-apply-submit");
    expect(submit.textContent).toContain("用于《北岸手记》");
    expect(submit.disabled).toBe(false);
    // 表单沿用全局应用的设置
    expect(byTestId("sr-sample-windows").value).toBe("6");
    // 「这份文风还用在」里如实列出它（在那里解除才是对全部作品）
    expect(byTestId("sr-apply-others").textContent).toContain("没有自己应用的所有作品");
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) return Promise.resolve({ binding: { ...OWN_BINDING, binding_id: "bd-own" }, created: true, changed: true, replaced: [] });
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    await click(submit);
    await settle();
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(client.apiDelete).not.toHaveBeenCalled();
    const [, body] = client.apiPost.mock.calls.find(([url]) => url.endsWith("/apply"));
    expect(body.scope).toBe("project");
    expect(body.scope_ref_id).toBe("w1");
    expect(body.config).toMatchObject({ reference_mode: "samples_only", sample_windows: 6, draft_mode: "style_first" });
    expect(body.config.dimension_states["scene.dialogue"]).toBe("emphasize");
  });

  it("换回以前用过的这本：表单按它在这部作品上停用的那条应用的设置填，用上就按它来（复核 #4）", async () => {
    const STORED = {
      ...OWN_BINDING, binding_id: "bd-old", status: "disabled",
      config: { reference_mode: "samples_only", sample_windows: 5, draft_mode: "neutral_first", dimension_states: { "theme.values": "exclude" } },
    };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = {
      project_id: "w1", binding: { ...OWN_BINDING, binding_id: "bd-x", profile_id: "pf-x" }, profile: { profile_id: "pf-x" }, book: { book_id: "bk-x", title: "丁书" },
    };
    state.profileBindings = [STORED];
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-other").textContent).toContain("用这本会替换它（《丁书》在《北岸手记》上的设置保留，以后换回来还在）");
    expect(byTestId("sr-apply-stored").textContent).toContain("这本书上次用于《北岸手记》时的设置已经填在下面");
    expect($('[data-testid="sr-reference-mode"] [data-value="samples_only"]').getAttribute("aria-checked")).toBe("true");
    expect(byTestId("sr-sample-windows").value).toBe("5");
    expect($('[data-testid="sr-draft-mode"] [data-value="neutral_first"]').getAttribute("aria-checked")).toBe("true");
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) return Promise.resolve({ binding: { ...STORED, status: "active" }, created: false, changed: true, replaced: [{ binding_id: "bd-x", profile_id: "pf-x", book_title: "丁书" }] });
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    await click(byTestId("sr-apply-submit"));
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/apply`, {
      scope: "project", scope_ref_id: "w1", config: { reference_mode: "samples_only", sample_windows: 5, draft_mode: "neutral_first" },
    });
    expect(byTestId("sr-apply-result").textContent).toContain("按这本书上次用于它时的设置");
  });

  it("用于之后「这份文风还用在」还在（清单重读，不是删掉了事）（复核 #9）", async () => {
    const SCENE_BINDING = { ...OWN_BINDING, binding_id: "bd-s", scope: "scene", scope_ref_id: "sc-9" };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.profileBindings = [SCENE_BINDING];
    await mountView();
    await openStage("apply");
    await settle();
    expect(byTestId("sr-apply-others").textContent).toContain("某一场");
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) {
        state.profileBindings = [SCENE_BINDING, OWN_BINDING];
        state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
        return Promise.resolve({ binding: OWN_BINDING, created: true, changed: true, replaced: [] });
      }
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    await click(byTestId("sr-apply-submit"));
    await settle();
    await settle();
    expect(byTestId("sr-apply-result")).toBeTruthy();
    expect(byTestId("sr-apply-others")).toBeTruthy();
    expect(byTestId("sr-apply-others").textContent).toContain("某一场");
  });

  it("「起草不发原文」的书：状态行按真正生效的参考方式说（只用文风卡），不说「全面模仿 · 12 窗」（复核 #11）", async () => {
    await openApply({ own: true, bookOver: { cloud_policy: "segments_only" } });
    // 后端给的 effective_reference_mode 在这里缺席也照样按书的原文范围推
    expect(byTestId("sr-apply-status").textContent).toContain("正在用这本书的文风：只用文风卡（这本书起草不发原文） · 作者手笔直起");
    expect(byTestId("sr-apply-status").textContent).not.toContain("12 窗");
    expect(byTestId("sr-apply-segments-only").textContent).toContain("这本书导入时选了「起草不发原文」");
  });

  it("本场预览：挑一场，看样例窗（第几章 · 章首 / 章末、梗概、标签）和文风卡", async () => {
    await openApply();
    await settle();
    const select = byTestId("sr-scene-select");
    expect($$("option", select).map((o) => o.textContent)).toContain("第 1 章 · 第 2 场「夜渡」");
    await act(async () => {
      select.value = "sc-1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    client.apiPost.mockClear();
    await click(byTestId("sr-scene-preview-run"));
    await settle();
    const [url, body] = client.apiPost.mock.calls.find(([u]) => u.endsWith("/injection-preview"));
    expect(url).toBe(`${API}/profiles/pf-a/injection-preview`);
    expect(body).toMatchObject({ scene_id: "sc-1", project_id: "w1", sample_windows: 12 });
    const result = byTestId("sr-scene-preview-result");
    expect(result.textContent).toContain("开章的一场 · 开章引入");
    const win = $(".sr-window", result);
    expect(win.textContent).toContain("第 3 章 · 章首");
    expect(win.textContent).toContain("某人在渡口等船");
    expect(win.textContent).toContain("按章内位置挑");
    expect(win.textContent).toContain("叙述为主");
    expect(byTestId("sr-block-card").textContent).toContain("对白常常只有半句");
    await click($(".sr-window-head", result));
    await settle();
    expect($(".sr-window-text", result).textContent).toContain("渡口的灯一盏一盏灭了。");
  });

  it("没学过：先去学习文风", async () => {
    await mountView();
    await openStage("apply");
    expect(byTestId("sr-apply-empty")).toBeTruthy();
  });

  it("旧版全局应用的状态卡：对所有没有自己应用的作品生效、要解除去下面「这份文风还用在」里解除；下面照旧有「解除」（清理 C4）", async () => {
    const GLOBAL = { ...OWN_BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = { project_id: "w1", binding: GLOBAL, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    state.profileBindings = [GLOBAL];
    await mountView();
    await openStage("apply");
    await settle();
    const text = byTestId("sr-apply-legacy").textContent;
    expect(text).toContain("这条旧版应用对所有没有自己应用的作品生效，要解除请到下面「这份文风还用在」里解除（那里解除才是对全部作品）");
    expect(text).toContain("点「用于《北岸手记》」给这部作品单独建一条应用");
    expect(text).not.toContain("只读、不改也不解除");
    expect(byTestId("sr-apply-unbind")).toBeNull();
    const others = byTestId("sr-apply-others");
    expect(others.textContent).toContain("没有自己应用的所有作品");
    expect([...others.querySelectorAll("button")].some((b) => b.textContent === "解除")).toBe(true);
  });

  it("用于作品被拒（画像依据变过，author_action 指向学习）：出错行说中文并给「去学习文风」，点了落到「学习文风」（sr-apply-error）", async () => {
    await openApply();
    client.apiPost.mockImplementation((url) => {
      if (url.endsWith("/apply")) {
        return Promise.reject(Object.assign(new Error("profile stale"), { code: "STYLE_REFERENCE_PROFILE_STALE", status: 409, details: { author_action: { action: "learn_style", book_id: "bk-a" } } }));
      }
      if (url.endsWith("/injection-preview")) return Promise.resolve(PREVIEW);
      return Promise.resolve({});
    });
    await click(byTestId("sr-apply-submit"));
    await settle();
    expect(byTestId("sr-apply-error").textContent).toContain("这份画像的依据变过了");
    expect(byTestId("sr-apply-error").textContent).not.toContain("profile stale");
    expect(byTestId("sr-apply-error-action").textContent).toBe("去学习文风");
    await click(byTestId("sr-apply-error-action"));
    await settle();
    expect($(".sr-step.is-active").dataset.stage).toBe("learn");
  });

  it("没有打开作品：用于作品只说明先打开一部（sr-apply-no-work）", async () => {
    workHolder.current = null;
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    await mountView();
    await openStage("apply");
    expect(byTestId("sr-apply-no-work").textContent).toContain("还没有打开作品");
    expect(byTestId("sr-apply-form")).toBeNull();
  });

  it("本场预览出错：说中文原因（sr-scene-preview-error）", async () => {
    await openApply();
    await settle();
    const select = byTestId("sr-scene-select");
    await act(async () => { select.value = "sc-1"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    client.apiPost.mockImplementation((url) => (url.endsWith("/injection-preview")
      ? Promise.reject(Object.assign(new Error("profile not found"), { code: "STYLE_REFERENCE_PROFILE_NOT_FOUND", status: 404 }))
      : Promise.resolve({})));
    await click(byTestId("sr-scene-preview-run"));
    await settle();
    expect(byTestId("sr-scene-preview-error").textContent).toContain("这份文风画像已经不在了");
    expect(byTestId("sr-scene-preview-error").textContent).not.toContain("profile not found");
    expect(byTestId("sr-scene-preview-result")).toBeNull();
  });

  it("本场预览的窗口标签：示范的维度念成中文名，配额叫「维度示范」，不再有「手法」（O1）", async () => {
    await openApply();
    await settle();
    const select = byTestId("sr-scene-select");
    await act(async () => { select.value = "sc-1"; select.dispatchEvent(new Event("change", { bubbles: true })); });
    client.apiPost.mockImplementation((url) => (url.endsWith("/injection-preview")
      ? Promise.resolve({ ...PREVIEW, windows: [{ ...PREVIEW.windows[0], slot: "dimension", dimensions: ["language.rhetoric", "scene.dialogue"], devices: ["留白"] }] })
      : Promise.resolve({})));
    await click(byTestId("sr-scene-preview-run"));
    await settle();
    const win = $(".sr-window", byTestId("sr-scene-preview-result"));
    expect(win.textContent).toContain("维度示范");
    expect(win.textContent).toContain("修辞手法");
    expect(win.textContent).toContain("对话写法");
    expect(win.textContent).not.toContain("留白");
    expect(win.textContent).not.toContain("language.rhetoric");
  });
});
