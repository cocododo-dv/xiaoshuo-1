// 风格参考页 · 页面外壳（三步 + 对照检查、空书库）、参考书库的多选删除与页头单本删除、导入对话框（默认范围来自 /runtime、三档说法、权属声明、重复导入 →「打开这本」、没有模型 / 仅本机被拒时锁住）。
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
  $, $$, API, bookRow, byTestId, click, client, mountView, openStage, PROFILE_SUMMARY, settle, setupStyleRefSuite, state,
} from "./ws-styleref.test-helpers.jsx";

setupStyleRefSuite(workHolder);

describe("页面外壳", () => {
  it("三步：参考书 → 学习文风 → 用于作品，外加随时可用的「对照检查」；没学过的书落在「学习文风」；旧界面的叫法都不在", async () => {
    await mountView();
    expect($$(".sr-step").map((b) => b.dataset.stage)).toEqual(["book", "learn", "apply", "check"]);
    expect($('.sr-step[data-stage="check"]').getAttribute("aria-label")).toBe("对照检查（等前一步）");
    expect($(".sr-step.is-active").dataset.stage).toBe("learn");
    const text = document.body.textContent;
    for (const gone of ["维度矩阵", "注入", "回测", "强度", "MIXED", "策略 A", "任务类型", "👍", "👎", "前 200 段", "锚定集"]) {
      expect(text, gone).not.toContain(gone);
    }
  });

  it("空书库：给「导入第一本参考书」", async () => {
    state.books = [];
    await mountView();
    expect(byTestId("sr-import-first")).toBeTruthy();
  });
});

describe("参考书库：多选删除", () => {
  it("选两本 → 「删除所选」→ 确认框列出书名 → 批量删除", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书", author_label: "" })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => {
      if (url === `${API}/books/bulk-delete`) {
        return Promise.resolve({ results: [{ book_id: "bk-a", deleted: true }, { book_id: "bk-b", deleted: true }], deleted_count: 2, failed_count: 0 });
      }
      return Promise.resolve({});
    });
    await mountView();
    await click(byTestId("sr-books-select"));
    const boxes = $$("[data-sr-select]");
    expect(boxes.map((b) => b.dataset.srSelect)).toEqual(["bk-a", "bk-b"]);
    expect(byTestId("sr-books-delete-selected").disabled).toBe(true);
    for (const box of boxes) await click(box);
    expect($(".sr-select-all b").textContent).toBe("2");
    state.books = [];
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(confirm).toHaveBeenCalledTimes(1);
    const message = confirm.mock.calls[0][0];
    expect(message).toContain("删除 2 本参考书？");
    expect(message).toContain("《甲书》、《乙书》");
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bulk-delete`, { book_ids: ["bk-a", "bk-b"] });
    expect(byTestId("sr-select-bar")).toBeNull();
  });

  it("多选删除有一本没删成、而它正是眼下打开的那本：页面留在它上面，不跳到别的书（F05-06）", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书" }), bookRow({ book_id: "bk-c", title: "丙书" })];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => (url === `${API}/books/bulk-delete`
      ? Promise.resolve({
        results: [{ book_id: "bk-a", deleted: false, error: { code: "STYLE_REFERENCE_BOOK_LEARNING" } }, { book_id: "bk-b", deleted: true }],
        deleted_count: 1, failed_count: 1,
      })
      : Promise.resolve({})));
    await mountView();
    expect($(".sr-stage-title").textContent).toBe("甲书");
    await openStage("book");
    await click(byTestId("sr-books-select"));
    await click($('[data-sr-select="bk-a"]'));
    await click($('[data-sr-select="bk-b"]'));
    state.books = [bookRow(), bookRow({ book_id: "bk-c", title: "丙书" })];
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect($(".sr-stage-title").textContent).toBe("甲书");
    expect($(".sr-step.is-active").dataset.stage).toBe("book");
    const said = window.alert.mock.calls.map(([m]) => m).join("\n");
    expect(said).toContain("删除了 1 本，另有 1 本没删成。《甲书》：这本书正在学习文风");
  });

  it("删掉的是眼下打开的那本：切到它原来位置上的邻居（F05-06）", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书" }), bookRow({ book_id: "bk-c", title: "丙书" })];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => (url === `${API}/books/bulk-delete`
      ? Promise.resolve({ results: [{ book_id: "bk-b", deleted: true }], deleted_count: 1, failed_count: 0 })
      : Promise.resolve({})));
    await mountView();
    await click($('[data-sr-book="bk-b"]'));
    await settle();
    expect($(".sr-stage-title").textContent).toBe("乙书");
    state.books = [bookRow(), bookRow({ book_id: "bk-c", title: "丙书" })];
    await click(byTestId("sr-header-more"));
    await click(byTestId("sr-header-delete"));
    await settle();
    expect($(".sr-stage-title").textContent).toBe("丙书");
    expect(window.alert.mock.calls.map(([m]) => m).join("\n")).toContain("已删除参考书《乙书》");
  });

  it("确认框点取消：不发删除请求", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($("[data-sr-select]"));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/bulk-delete"))).toBe(false);
    expect($$("[data-sr-select]")).toHaveLength(1);
  });

  it("正在用于当前作品的书，确认框里点明", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: [{ project_id: "w1", project_title: "北岸手记", binding_id: "bd-1", config: {} }] })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($("[data-sr-select]"));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("《北岸手记》正在用《甲书》的文风");
  });

  it("用着这本书的每一部作品都点出来，不只当前作品；页头单本删除也一样（复核 #5）", async () => {
    state.books = [bookRow({
      profile: PROFILE_SUMMARY,
      applied_projects: [
        { project_id: "w1", project_title: "北岸手记", binding_id: "bd-1", config: {} },
        { project_id: "w2", project_title: "南山", binding_id: "bd-2", config: {} },
      ],
    })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($("[data-sr-select]"));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("《北岸手记》、《南山》正在用《甲书》的文风");
    expect(confirm.mock.calls[0][0]).toContain("这些作品起草新场景时不再带它的文风");
    await click(byTestId("sr-header-more"));
    await click(byTestId("sr-header-delete"));
    await settle();
    expect(confirm.mock.calls[1][0]).toContain("《北岸手记》、《南山》正在用《甲书》的文风");
  });

  it("删掉的书没删成：说中文原因，不把后端的英文原话给作者看（复核 #14）", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书" })];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => (url === `${API}/books/bulk-delete`
      ? Promise.resolve({ results: [{ book_id: "bk-a", deleted: false, error: { code: "INTERNAL_ERROR", message: "IntegrityError: FOREIGN KEY constraint failed" } }], deleted_count: 0, failed_count: 1 })
      : Promise.resolve({})));
    await mountView();
    await click(byTestId("sr-books-select"));
    await click($('[data-sr-select="bk-a"]'));
    await click(byTestId("sr-books-delete-selected"));
    await settle();
    const said = window.alert.mock.calls.map(([m]) => m).join("\n");
    expect(said).toContain("删除了 0 本，另有 1 本没删成。《甲书》：请稍后重试。");
    expect(said).not.toContain("IntegrityError");
  });
});

describe("导入对话框", () => {
  async function openImport() {
    await click(byTestId("sr-books-import"));
    await settle();
  }

  it("默认范围跟着 /runtime；三档说法如实；非本机要两项权属声明", async () => {
    state.runtime = { llm_enabled: true, llm_is_local: false, default_cloud_policy: "allow_full_cloud" };
    await mountView();
    await openImport();
    const radios = $$('input[name="sr-cloud-policy"]');
    expect(radios.map((r) => r.value)).toEqual(["local_only", "segments_only", "allow_full_cloud"]);
    expect(radios.find((r) => r.checked).value).toBe("allow_full_cloud");
    const policyText = byTestId("sr-import-policy").textContent;
    expect(policyText).toContain("仅本机模型");
    // 中间一档的名字说它真做的事：分类、学习照样发整段，只有起草不发原文（复核 #6）
    expect(policyText).toContain("起草不发原文分类和学习时正文照样发给云端");
    expect(policyText).not.toContain("只发短句");
    expect(policyText).toContain("可发送全文");
    expect(policyText).toContain("按当前模型推荐");
    expect(byTestId("sr-rights-send")).toBeTruthy();
    expect(byTestId("sr-import-submit").disabled).toBe(true);
    expect(byTestId("sr-import-why").textContent).toBe("先确认权属声明");
  });

  it("没有模型：说明并锁住「导入」", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView();
    await openImport();
    expect(byTestId("sr-import-no-llm")).toBeTruthy();
    expect($('input[name="sr-cloud-policy"]:checked').value).toBe("local_only");
    expect(byTestId("sr-import-why").textContent).toContain("先接入模型");
    expect(byTestId("sr-rights-send")).toBeNull();
    // 没有模型就谈不上「按当前模型推荐」
    expect(byTestId("sr-import-policy").textContent).not.toContain("按当前模型推荐");
  });

  it("选文件自动填书名，导入发 FormData；同一份文本 → 说出书名并给「打开这本」", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书" })];
    await mountView();
    await openImport();
    await click(byTestId("sr-rights-analysis"));
    await click(byTestId("sr-rights-send"));
    const input = byTestId("sr-import-file");
    const file = new File(["第一章\n一段正文。"], "丙书.txt", { type: "text/plain" });
    Object.defineProperty(input, "files", { value: [file], configurable: true });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(byTestId("sr-import-title").value).toBe("丙书");
    client.apiPost.mockImplementation((url) => {
      if (url === `${API}/books/import-upload`) {
        return Promise.reject(Object.assign(new Error("duplicate"), {
          code: "STYLE_REFERENCE_BOOK_DUPLICATE", details: { book_id: "bk-b", title: "乙书" },
        }));
      }
      return Promise.resolve({});
    });
    await click(byTestId("sr-import-submit"));
    await settle();
    const [url, body] = client.apiPost.mock.calls.find(([u]) => u.endsWith("/import-upload"));
    expect(url).toBe(`${API}/books/import-upload`);
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("cloud_policy")).toBe("allow_full_cloud");
    expect(JSON.parse(body.get("rights_declaration"))).toMatchObject({ analysis_rights: true, send_rights: true });
    expect(byTestId("sr-import-error").textContent).toContain("书库里已经有同一份文本：《乙书》。");
    await click(byTestId("sr-import-error-action"));
    await settle();
    expect($(".sr-stage-title").textContent).toBe("乙书");
    expect(byTestId("sr-import-submit")).toBeNull();
  });

  it("请求还在路上时关了对话框：失败走提示层说出来，不因再打开时清空而丢掉（复核 #16）", async () => {
    await mountView();
    await openImport();
    await click(byTestId("sr-rights-analysis"));
    await click(byTestId("sr-rights-send"));
    const input = byTestId("sr-import-file");
    Object.defineProperty(input, "files", { value: [new File(["一段正文。"], "丙书.txt", { type: "text/plain" })], configurable: true });
    await act(async () => { input.dispatchEvent(new Event("change", { bubbles: true })); });
    let reject;
    client.apiPost.mockImplementation((url) => (url === `${API}/books/import-upload`
      ? new Promise((_resolve, rej) => { reject = rej; })
      : Promise.resolve({})));
    await click(byTestId("sr-import-submit"));
    await click($('button[aria-label="关闭导入"]'));
    await settle();
    expect(byTestId("sr-import-submit")).toBeNull();
    await act(async () => { reject(Object.assign(new Error("empty"), { code: "STYLE_REFERENCE_BOOK_EMPTY" })); });
    await settle();
    expect(window.alert).toHaveBeenCalledWith("《丙书》没有导入：这个文件里没有可以当参考的正文。");
    // 再打开：是一次新的导入，不带上一次的错误
    await openImport();
    expect(byTestId("sr-import-error")).toBeNull();
  });

  it("配的是云端模型、选「仅本机模型」：提示分类会被拒并锁住「导入」（sr-import-local-blocked）", async () => {
    state.runtime = { llm_enabled: true, llm_is_local: false, default_cloud_policy: "allow_full_cloud" };
    await mountView();
    await openImport();
    expect(byTestId("sr-import-local-blocked")).toBeNull();
    await click($('input[name="sr-cloud-policy"][value="local_only"]'));
    await settle();
    expect(byTestId("sr-import-local-blocked").textContent).toContain("「仅本机模型」的书会被拒绝");
    expect(byTestId("sr-import-submit").disabled).toBe(true);
    expect(byTestId("sr-import-why").textContent).toContain("导入不了");
  });
});
