import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./ws-catalog.jsx", () => ({ WsDemoTag: () => null }));
vi.mock("./ws-works.jsx", () => ({ WsWorks: { activeId: () => "new-book" } }));
vi.mock("./ws-review.jsx", () => ({ rvPush: vi.fn() }));

import {
  SrImportDialog, SR_CLOUD_POLICIES, SR_RIGHTS_TERMS, srImportBook, srRightsReady,
  srRunImport, srImportProgressView, srImportEntries, srImportDismiss, SrImportProgressPanel,
  srActivityApply, srActivityStop,
} from "./ws-styleref.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const mounted = [];

async function renderDialog(props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<SrImportDialog {...props} />));
  await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  return host;
}

const chooseBtn = (host) => host.querySelector('[data-testid="sr-import-choose-file"]');
/* 导入成功后 srSyncBooks 会经真实 client 再打 fetch 拉书库，只挑 import-upload 那一次。 */
const uploadCalls = (fetchMock) => fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/books/import-upload"));
const tick = (host, testId) => act(async () => host.querySelector(`[data-testid="${testId}"]`).click());

/* 桩掉文件选择器 + 书名 prompt + alert + fetch，返回 fetch mock；
   createElement 只劫持 "input"（srImportBook 用它造隐藏 file input），其余照常。 */
function stubImportPipeline({ response } = {}) {
  const originalCreate = document.createElement.bind(document);
  const fileInput = {
    type: "", accept: "", files: [new File(["片段"], "参考.md", { type: "text/markdown" })],
    onchange: null,
    click() { return this.onchange(); },
  };
  const createSpy = vi.spyOn(document, "createElement").mockImplementation((tag, options) => (
    tag === "input" ? fileInput : originalCreate(tag, options)
  ));
  vi.spyOn(window, "prompt").mockReturnValue("参考书");
  vi.spyOn(window, "alert").mockImplementation(() => {});
  const fetchMock = vi.fn().mockResolvedValue(response || {
    ok: true,
    status: 200,
    headers: new Headers(),
    json: async () => ({ ok: true, data: { book: { total_chars: 2 } }, books: [] }),
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, createSpy };
}

/* 导入成功不再弹 alert：进度面板的条目到达 succeeded 即为完成信号。 */
const importSucceeded = () => srImportEntries().filter((e) => e.status === "succeeded");

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  srImportEntries().forEach((e) => srImportDismiss(e.key));
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("参考书导入的数据出域选择", () => {
  it("展示三档策略并默认仅本机，作者可显式选择按段落送云", async () => {
    const onChoose = vi.fn();
    const host = await renderDialog({ open: true, onClose: vi.fn(), onChoose });

    expect(SR_CLOUD_POLICIES.map((item) => item.id)).toEqual([
      "local_only", "segments_only", "allow_full_cloud",
    ]);
    expect(host.textContent).toContain("仅保存在本机");
    expect(host.textContent).toContain("只发送所需段落");
    expect(host.textContent).toContain("允许全文上云");
    expect(host.querySelector('input[value="local_only"]').checked).toBe(true);

    await act(async () => host.querySelector('input[value="segments_only"]').click());
    await tick(host, "sr-rights-analysis");
    await tick(host, "sr-rights-send");
    await act(async () => chooseBtn(host).click());

    expect(onChoose).toHaveBeenCalledWith("segments_only", {
      declared: true, analysis_rights: true, send_rights: true,
    });
  });

  it("云端策略未勾发送权时「选择文件」保持禁用，勾满才放行", async () => {
    const onChoose = vi.fn();
    const host = await renderDialog({ open: true, onClose: vi.fn(), onChoose });

    // 默认 local_only：只要求分析权，不出现发送权勾选框
    expect(host.querySelector('[data-testid="sr-rights-send"]')).toBeNull();
    expect(host.textContent).toContain(SR_RIGHTS_TERMS.analysis);
    expect(chooseBtn(host).disabled).toBe(true);

    await act(async () => host.querySelector('input[value="allow_full_cloud"]').click());
    expect(host.querySelector('[data-testid="sr-rights-send"]')).toBeTruthy();
    expect(host.textContent).toContain(SR_RIGHTS_TERMS.send);
    expect(chooseBtn(host).disabled).toBe(true);

    // 只勾分析权：云端策略仍不放行，并给出说明
    await tick(host, "sr-rights-analysis");
    expect(chooseBtn(host).disabled).toBe(true);
    expect(host.querySelector('[data-testid="sr-rights-hint"]').textContent).toContain("发送权");
    await act(async () => chooseBtn(host).click());
    expect(onChoose).not.toHaveBeenCalled();

    await tick(host, "sr-rights-send");
    expect(chooseBtn(host).disabled).toBe(false);
    expect(host.querySelector('[data-testid="sr-rights-hint"]')).toBeNull();
    await act(async () => chooseBtn(host).click());
    expect(onChoose).toHaveBeenCalledWith("allow_full_cloud", {
      declared: true, analysis_rights: true, send_rights: true,
    });
  });

  it("切回仅本机后声明里的发送权恒为 false，不带走多余授权", async () => {
    const onChoose = vi.fn();
    const host = await renderDialog({ open: true, onClose: vi.fn(), onChoose });

    await act(async () => host.querySelector('input[value="segments_only"]').click());
    await tick(host, "sr-rights-analysis");
    await tick(host, "sr-rights-send");
    await act(async () => host.querySelector('input[value="local_only"]').click());

    expect(host.querySelector('[data-testid="sr-rights-send"]')).toBeNull();
    expect(chooseBtn(host).disabled).toBe(false);
    await act(async () => chooseBtn(host).click());
    expect(onChoose).toHaveBeenCalledWith("local_only", {
      declared: true, analysis_rights: true, send_rights: false,
    });
  });

  it("srRightsReady 与后端 _normalize_rights_declaration 的红线一致", () => {
    expect(srRightsReady("local_only", null)).toBe(false);
    expect(srRightsReady("local_only", { analysis_rights: true, send_rights: false })).toBe(true);
    expect(srRightsReady("segments_only", { analysis_rights: true, send_rights: false })).toBe(false);
    expect(srRightsReady("segments_only", { analysis_rights: true, send_rights: true })).toBe(true);
    expect(srRightsReady("allow_full_cloud", { analysis_rights: false, send_rights: true })).toBe(false);
  });

  it("Escape 关闭并把焦点还给打开它的按钮", async () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    const onClose = vi.fn();
    const host = await renderDialog({ open: true, onClose, onChoose: vi.fn() });
    expect(host.querySelector('[role="dialog"]')).toBeTruthy();

    await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(onClose).toHaveBeenCalledTimes(1);

    const { root } = mounted[mounted.length - 1];
    await act(async () => root.render(<SrImportDialog open={false} onClose={onClose} onChoose={vi.fn()} />));
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});

describe("srImportBook 上传表单的权属声明", () => {
  it("把作者所选策略原样写入上传表单，不再硬编码 segments_only", async () => {
    const { fetchMock } = stubImportPipeline();

    srImportBook("allow_full_cloud", { declared: true, analysis_rights: true, send_rights: true });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    await vi.waitFor(() => expect(importSucceeded()).toHaveLength(1));
    expect(window.alert).not.toHaveBeenCalled();

    const request = uploadCalls(fetchMock)[0][1];
    expect(request.body).toBeInstanceOf(FormData);
    expect(request.body.get("cloud_policy")).toBe("allow_full_cloud");
  });

  it("segments_only 的表单带 rights_declaration JSON，send_rights=true 且署名 operator", async () => {
    const { fetchMock } = stubImportPipeline();

    srImportBook("segments_only", { declared: true, analysis_rights: true, send_rights: true });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    await vi.waitFor(() => expect(importSucceeded()).toHaveLength(1));

    const request = uploadCalls(fetchMock)[0][1];
    expect(request.method).toBe("POST");
    expect(request.headers["X-Idempotency-Key"]).toMatch(/^sr-import-/);
    expect(request.headers["X-Operator-Ref"]).toBe("operator");
    const raw = request.body.get("rights_declaration");
    expect(typeof raw).toBe("string");
    expect(JSON.parse(raw)).toEqual({
      declared: true,
      analysis_rights: true,
      send_rights: true,
      declared_by: "operator",
    });
  });

  it("仅本机且已确认分析权：声明写入 send_rights=false；未声明则不附字段", async () => {
    const { fetchMock } = stubImportPipeline();

    srImportBook("local_only", { declared: true, analysis_rights: true, send_rights: false });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    await vi.waitFor(() => expect(importSucceeded()).toHaveLength(1));
    expect(JSON.parse(uploadCalls(fetchMock)[0][1].body.get("rights_declaration"))).toEqual({
      declared: true, analysis_rights: true, send_rights: false, declared_by: "operator",
    });

    srImportBook("local_only");
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(2));
    await vi.waitFor(() => expect(importSucceeded()).toHaveLength(2));
    const undeclared = uploadCalls(fetchMock)[1][1].body;
    expect(undeclared.get("cloud_policy")).toBe("local_only");
    expect(undeclared.has("rights_declaration")).toBe(false);
  });

  it("云端策略没有发送权声明：同步抛错，不开文件选择器、不发请求", () => {
    const { fetchMock, createSpy } = stubImportPipeline();

    expect(() => srImportBook("segments_only")).toThrow(/发送权/);
    expect(() => srImportBook("allow_full_cloud", { declared: true, analysis_rights: true, send_rights: false }))
      .toThrow(/发送权/);
    expect(createSpy).not.toHaveBeenCalledWith("input");
    expect(window.prompt).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("后端仍拒绝时原样透出信封里的 message 与 code", async () => {
    const { fetchMock } = stubImportPipeline({
      response: {
        ok: false,
        status: 400,
        headers: new Headers(),
        json: async () => ({
          ok: false,
          data: null,
          error: {
            code: "STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED",
            message: "云端策略需要用户显式声明发送权；请确认声明或改用 local_only。",
            details: {},
          },
          request_id: "req_test",
        }),
      },
    });

    srImportBook("segments_only", { declared: true, analysis_rights: true, send_rights: true });
    await vi.waitFor(() => expect(uploadCalls(fetchMock)).toHaveLength(1));
    // 风格参考页没挂着时弹窗兜底；面板条目同样带原因。
    await vi.waitFor(() => expect(window.alert).toHaveBeenCalledTimes(1));
    const shown = window.alert.mock.calls[0][0];
    expect(shown).toContain("导入失败");
    expect(shown).toContain("云端策略需要用户显式声明发送权；请确认声明或改用 local_only。");
    expect(shown).toContain("STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED");
    const failed = srImportEntries().find((e) => e.status === "failed");
    expect(failed.error).toContain("STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED");
  });
});

/* ---- 2026-09-15 导入进度：POST 挂起期间轮询 /imports/{key}/progress，面板画进度条 ---- */

function jsonResponse(payload, status = 200) {
  return { ok: status < 400, status, headers: new Headers(), json: async () => payload };
}

function deferred() {
  let resolve; let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/* fetch 按 URL 分流：上传 POST 由测试手动放行；进度轮询按脚本依次返回；书库列表返回新书。 */
function stubProgressPipeline({ progressScript = [] } = {}) {
  const upload = deferred();
  const progressCalls = [];
  const fetchMock = vi.fn(async (url, init) => {
    const target = String(url);
    if (target.endsWith("/books/import-upload")) return upload.promise;
    if (target.includes("/imports/")) {
      progressCalls.push(target);
      const step = progressScript[Math.min(progressCalls.length - 1, progressScript.length - 1)];
      if (!step) return jsonResponse({ ok: false, data: null, error: { code: "STYLE_REFERENCE_IMPORT_PROGRESS_UNKNOWN", message: "unknown" } }, 404);
      return jsonResponse({ ok: true, data: { progress: step } });
    }
    if (target.endsWith("/style-reference/books")) {
      return jsonResponse({ ok: true, data: { books: [{ book_id: "sr_book_new", title: "参考书", author_label: null, total_chars: 12345, status: "ready" }] } });
    }
    return jsonResponse({ ok: true, data: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, upload, progressCalls };
}

const RUNNING_SNAPSHOT = {
  import_key: "sr-import-test", title: "参考书", source: "upload", status: "running", phase: "classify",
  phase_label: "段落分类", percent: 40, elapsed_seconds: 12.5, eta_seconds: 30,
  chars_total: 12345, paragraphs_total: 244,
  classify: { mode: "llm", batches_done: 2, batches_total: 5, llm_calls: 2, node_id: "style_ref_paragraph_classify_bulk" },
  book_id: null, paragraphs_count: null, error: null,
};

describe("导入进度：轮询 + 面板", () => {
  it("srImportProgressView 把服务端快照与终态翻成百分比和文案", () => {
    const now = 100_000;
    expect(srImportProgressView({ status: "running", startedAt: now - 3_000, server: null }, now))
      .toEqual({ percent: 0, percentText: "0%", detail: "上传中 · 已用 0:03" });
    const running = srImportProgressView({ status: "running", startedAt: now - 65_000, server: RUNNING_SNAPSHOT }, now);
    expect(running.percent).toBe(40);
    expect(running.detail).toBe("段落分类 2/5 批 · 已用 1:05 · 预计还需 0:30");
    // 2026-09-15 严格 LLM：没有「锚定集之外走启发式」这种模式了，批次文案只报进度
    const capped = srImportProgressView({
      status: "running", startedAt: now, server: { ...RUNNING_SNAPSHOT, percent: 30, eta_seconds: null, classify: { mode: "llm", batches_done: 3, batches_total: 8 } },
    }, now);
    expect(capped.detail).toBe("段落分类 3/8 批 · 已用 0:00");
    const heuristic = srImportProgressView({
      status: "running", startedAt: now, server: { ...RUNNING_SNAPSHOT, percent: 75, eta_seconds: null, classify: { mode: "heuristic", batches_done: 0, batches_total: 0 } },
    }, now);
    expect(heuristic.detail).toBe("段落分类（启发式） · 已用 0:00");
    const persisting = srImportProgressView({ status: "running", startedAt: now, server: { ...RUNNING_SNAPSHOT, phase: "persist", phase_label: "写入书库", percent: 90 } }, now);
    expect(persisting).toEqual({ percent: 90, percentText: "90%", detail: "写入书库 · 已用 0:00" });
    // 服务端快照的 percent 在完成前封顶 99，完成态恒 100。
    expect(srImportProgressView({ status: "running", startedAt: now, server: { ...RUNNING_SNAPSHOT, percent: 100 } }, now).percent).toBe(99);
    expect(srImportProgressView({ status: "succeeded", startedAt: now - 192_000, chars: 1_900_000, paragraphs: 26677 }, now))
      .toEqual({ percent: 100, percentText: "100%", detail: "已导入 · 1,900,000 字 · 26,677 段 · 用时 3:12" });
    expect(srImportProgressView({ status: "failed", startedAt: now, server: RUNNING_SNAPSHOT, error: "网络断了" }, now))
      .toEqual({ percent: 40, percentText: "40%", detail: "导入失败：网络断了" });
  });

  it("srRunImport 在 POST 挂起期间轮询进度，成功后停止轮询、刷新书库并广播新书", async () => {
    const { upload, progressCalls } = stubProgressPipeline({ progressScript: [null, RUNNING_SNAPSHOT] });
    const imported = [];
    const onImported = (event) => imported.push(event.detail);
    window.addEventListener("sr:book-imported", onImported);
    try {
      const file = new File(["片段"], "参考.txt", { type: "text/plain" });
      const run = srRunImport({ file, title: "参考书", cloudPolicy: "local_only", importKey: "sr-import-test", pollMs: 5 });

      // 发请求前就有本地条目；第一次轮询 404（尚未登记）不算失败，第二次拿到 40%。
      expect(srImportEntries()[0]).toMatchObject({ key: "sr-import-test", status: "running", phase: "upload" });
      await vi.waitFor(() => expect(progressCalls.length).toBeGreaterThanOrEqual(2));
      await vi.waitFor(() => expect(srImportEntries()[0].server?.percent).toBe(40));
      expect(srImportEntries()[0].status).toBe("running");
      expect(progressCalls[0]).toContain("/api/v2/style-reference/imports/sr-import-test/progress");

      upload.resolve(jsonResponse({ ok: true, data: { book: { book_id: "sr_book_new", total_chars: 12345 }, paragraphs_count: 244 } }));
      const book = await run;
      expect(book.book_id).toBe("sr_book_new");
      const entry = srImportEntries()[0];
      expect(entry).toMatchObject({ status: "succeeded", phase: "done", percent: 100, bookId: "sr_book_new", chars: 12345, paragraphs: 244 });
      expect(imported).toEqual([{ bookId: "sr_book_new", importKey: "sr-import-test" }]);
      // 轮询已停：再等几个周期，进度请求数不再增长。
      const settled = progressCalls.length;
      await new Promise((resolve) => setTimeout(resolve, 40));
      expect(progressCalls.length).toBe(settled);
    } finally {
      window.removeEventListener("sr:book-imported", onImported);
    }
  });

  it("服务端回 ingesting（严格 LLM 的后台分类）：条目留在运行态交给活动清单，新书立即选中，任务完成再算成功", async () => {
    const { upload, progressCalls } = stubProgressPipeline({ progressScript: [RUNNING_SNAPSHOT] });
    const imported = [];
    const onImported = (event) => imported.push(event.detail);
    window.addEventListener("sr:book-imported", onImported);
    try {
      const file = new File(["片段"], "参考.txt", { type: "text/plain" });
      const run = srRunImport({ file, title: "参考书", cloudPolicy: "local_only", importKey: "sr-import-job", pollMs: 5 });
      await vi.waitFor(() => expect(progressCalls.length).toBeGreaterThanOrEqual(1));
      upload.resolve(jsonResponse({ ok: true, data: { book: { book_id: "sr_book_new", total_chars: 12345, status: "ingesting" }, paragraphs_count: 244, classification: { state: "queued", batches_total: 10 } } }));
      const book = await run;
      expect(book.status).toBe("ingesting");
      const entry = srImportEntries()[0];
      expect(entry).toMatchObject({ key: "sr-import-job", status: "running", phase: "classify", owned: false, bookId: "sr_book_new", chars: 12345, paragraphs: 244 });
      expect(imported).toEqual([{ bookId: "sr_book_new", importKey: "sr-import-job" }]);
      // 按键轮询已停（POST 已返回），后续由活动清单喂
      const settled = progressCalls.length;
      await new Promise((resolve) => setTimeout(resolve, 40));
      expect(progressCalls.length).toBe(settled);
      srActivityApply([{ key: "sr-import-job", kind: "import", status: "succeeded", book_id: "sr_book_new", title: "参考书", percent: 100, phase: "done", phase_label: "完成", paragraphs_count: 244, chars_total: 12345 }]);
      expect(srImportEntries()[0].status).toBe("succeeded");
      expect(srImportProgressView(srImportEntries()[0]).detail).toContain("已导入 · 12,345 字 · 244 段");
    } finally {
      window.removeEventListener("sr:book-imported", onImported);
      srActivityStop();
    }
  });

  it("POST 网络失败：条目标失败并带原因，轮询停止，调用方拿到异常", async () => {
    const { upload, progressCalls } = stubProgressPipeline({ progressScript: [RUNNING_SNAPSHOT] });
    const file = new File(["片段"], "参考.txt", { type: "text/plain" });
    const run = srRunImport({ file, title: "参考书", cloudPolicy: "local_only", importKey: "sr-import-fail", pollMs: 5 });
    await vi.waitFor(() => expect(progressCalls.length).toBeGreaterThanOrEqual(1));
    upload.reject(new TypeError("Failed to fetch"));
    await expect(run).rejects.toThrow("Failed to fetch");
    expect(srImportEntries()[0]).toMatchObject({ key: "sr-import-fail", status: "failed", error: "Failed to fetch" });
    const settled = progressCalls.length;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(progressCalls.length).toBe(settled);
  });

  it("面板：在跑时画进度条，完成后给「打开」并切到新书", async () => {
    const { upload } = stubProgressPipeline({ progressScript: [RUNNING_SNAPSHOT] });
    const onOpenBook = vi.fn();
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    mounted.push({ root, host });
    await act(async () => root.render(<SrImportProgressPanel onOpenBook={onOpenBook} />));
    expect(host.querySelector('[data-testid="sr-import-progress"]')).toBeNull();

    const file = new File(["片段"], "参考.txt", { type: "text/plain" });
    let run;
    await act(async () => { run = srRunImport({ file, title: "参考书", cloudPolicy: "local_only", importKey: "sr-import-panel", pollMs: 5 }); });
    await vi.waitFor(() => expect(host.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("40"));
    expect(host.textContent).toContain("段落分类 2/5 批");
    expect(host.querySelector('[data-import-status="running"]')).toBeTruthy();
    expect(host.textContent).not.toContain("打开");

    await act(async () => { upload.resolve(jsonResponse({ ok: true, data: { book: { book_id: "sr_book_new", total_chars: 12345 }, paragraphs_count: 244 } })); await run; });
    await vi.waitFor(() => expect(host.querySelector('[data-import-status="succeeded"]')).toBeTruthy());
    expect(host.querySelector('[role="progressbar"]').getAttribute("aria-valuenow")).toBe("100");
    expect(host.textContent).toContain("已导入 · 12,345 字 · 244 段");
    const open = Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "打开");
    await act(async () => open.click());
    expect(onOpenBook).toHaveBeenCalledWith("sr_book_new");
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-import-progress"]')).toBeNull());
  });
});
