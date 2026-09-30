// W1 复核二 · W1-R2A-1：用真的 lib/client.js（可重试的失败留着幂等键、同样的在飞写入并成一个）对着一台按后端契约办事的
// 假服务端（fetch 替身）：X-Idempotency-Key 存过的回包原样重放；PATCH 按修订号比对，409 带 details.current_revision_no；
// ensure 回服务端眼下的草稿。网络故障：lost = 服务端办完了、回包没回来（fetch 抛 TypeError）；down = 根本没到服务端。
// 每一条断言的都是安全的结果：回包丢了的那一次 ensure 不会被服务端按同一个键重放给之后的读取（核对、冲突之后读服务端版本）。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_CHAP, DEFAULT_PROJECT } from "./test-helpers.js";

const T = { timeout: 5000, interval: 20 };

function fakeServer() {
  const draft = { id: "d1", revision: 1, content: "<p>起点</p>" };
  const stored = new Map(); // 幂等键 → 当时的回包
  const faults = [];
  const log = [];
  const envelope = (status, body) => ({ ok: status < 400, status, json: async () => body });
  const ok = (data) => envelope(200, { ok: true, data, request_id: "r" });
  const fail = (status, code, message, details = {}) => envelope(status, { ok: false, error: { code, message, details }, request_id: "r" });
  const snapshot = () => ({ draft: { draft_id: draft.id, revision_no: draft.revision, content: draft.content } });
  async function handle(method, path, headers, body) {
    if (method === "GET") {
      if (path === "/api/v2/projects") return ok({ items: [DEFAULT_PROJECT] });
      if (/^\/api\/v2\/projects\/[^/]+\/catalog/.test(path)) return ok({ chapters: [DEFAULT_CHAP] });
      return ok({});
    }
    const key = headers["X-Idempotency-Key"];
    if (key && stored.has(key)) {
      log.push(`${method} replayed`);
      return ok(stored.get(key));
    }
    if (method === "POST" && /\/author-drafts\/scene\/s1\/ensure$/.test(path)) {
      const data = snapshot();
      stored.set(key, data);
      log.push(`ensure -> rev${draft.revision}`);
      return ok(data);
    }
    if (method === "PATCH" && path === "/api/v1/author-drafts/d1") {
      if (Number(body.base_revision_no) !== draft.revision) {
        log.push(`patch base${body.base_revision_no} -> 409`);
        return fail(409, "AUTHOR_DRAFT_CONFLICT", "author draft has changed; refresh before saving", { current_revision_no: draft.revision });
      }
      if (body.content !== draft.content) {
        draft.revision += 1;
        draft.content = body.content;
      }
      const data = snapshot();
      stored.set(key, data);
      log.push(`patch base${body.base_revision_no} -> rev${draft.revision}`);
      return ok(data);
    }
    return ok({});
  }
  const fetchImpl = vi.fn(async (url, init = {}) => {
    const path = String(url).replace(/^https?:\/\/[^/]+/, "");
    const method = (init.method || "GET").toUpperCase();
    const headers = init.headers || {};
    const body = init.body ? JSON.parse(init.body) : undefined;
    const index = faults.findIndex((fault) => fault.method === method && fault.match.test(path));
    const fault = index >= 0 ? faults.splice(index, 1)[0] : null;
    if (fault && fault.kind === "down") {
      log.push(`${method} down`);
      throw new TypeError("Failed to fetch");
    }
    const response = await handle(method, path, headers, body);
    if (fault && fault.kind === "lost") {
      log.push(`${method} response lost`);
      throw new TypeError("Failed to fetch");
    }
    return response;
  });
  return { draft, faults, log, fetchImpl };
}

let srv;
beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  srv = fakeServer();
  vi.stubGlobal("fetch", srv.fetchImpl);
  vi.spyOn(window, "alert").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

async function loadDocs() {
  await import("./ws-catalog.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
  await vi.waitFor(() => expect(window.WsCatalog.get().length).toBeGreaterThan(0), T);
  const mod = await import("./wr-doc-store.jsx");
  const events = [];
  mod.WrDocs.subscribe((kind, detail) => events.push({ kind, html: detail && detail.html }));
  mod.WrDocs.load("ch01s1");
  await vi.waitFor(() => expect(mod.WrDocs.state("ch01s1").revision).toBe(1), T);
  return { mod, events };
}

const settle = async () => {
  for (let i = 0; i < 12; i += 1) await new Promise((resolve) => setTimeout(resolve, 0));
  await new Promise((resolve) => setTimeout(resolve, 150));
};
const alerts = () => window.alert.mock.calls.map(([message]) => String(message));
const lostOnce = (method, match) => srv.faults.push({ method, match, kind: "lost" });
const downOnce = (method, match) => srv.faults.push({ method, match, kind: "down" });

describe("复核二 · 真客户端的幂等键重放不会骗过核对与冲突之后的读取（W1-R2A-1）", () => {
  it("R2-D 自己那一次回包丢了，下一次 409，核对的 ensure 断网一次：不开冲突、不提示；联网后最新的一稿落到服务端", async () => {
    const { mod, events } = await loadDocs();
    lostOnce("PATCH", /author-drafts\/d1$/);
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(srv.draft).toMatchObject({ revision: 2, content: "<p>起点，一</p>" });
    downOnce("POST", /ensure$/);
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一，二</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    await settle();
    expect(alerts()).toEqual([]);
    expect(events.filter((event) => event.kind === "conflict-resolved")).toEqual([]);
    expect(mod.WrDocs.cachedHTML("ch01s1")).toBe("<p>起点，一，二</p>");
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() => expect(srv.draft).toMatchObject({ revision: 3, content: "<p>起点，一，二</p>" }), T);
    await vi.waitFor(() => expect(mod.WrDocs.state("ch01s1")).toMatchObject({ dirty: false, revision: 3, lastSaveError: null }), T);
    expect(alerts()).toEqual([]);
    expect(mod.WrRecovery.list()).toEqual([]);
  });

  it("R2-E 重新打开这一场时后台复核的 ensure 回包丢了；之后自己的保存回包丢了、下一次 409：核对读到的是服务端眼下的版本（不是那次 ensure 的重放），认出自己那一稿", async () => {
    const { mod, events } = await loadDocs();
    lostOnce("POST", /ensure$/);
    mod.WrDocs.load("ch01s1");                                   // 回到这一场：后台复核，回包丢了（服务端按它的键存着 rev 1）
    await settle();
    expect(srv.log).toContain("POST response lost");
    lostOnce("PATCH", /author-drafts\/d1$/);
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，一</p>")).rejects.toMatchObject({ code: "NETWORK_ERROR" });
    expect(srv.draft.revision).toBe(2);
    await mod.WrDocs.save("ch01s1", "<p>起点，一，二</p>");
    expect(srv.draft).toMatchObject({ revision: 3, content: "<p>起点，一，二</p>" });
    expect(srv.log.filter((line) => line === "POST replayed")).toEqual([]);
    expect(alerts()).toEqual([]);
    expect(events.filter((event) => event.kind === "conflict-resolved")).toEqual([]);
    expect(mod.WrRecovery.list()).toEqual([]);
  });

  it("R2-G 后台复核的 ensure 回包丢了，另一台设备随后存了 rev 2，自己的保存 409：冲突之后读到的是 rev 2，不提示「暂时读不到服务端」", async () => {
    const { mod, events } = await loadDocs();
    lostOnce("POST", /ensure$/);
    mod.WrDocs.load("ch01s1");
    await settle();
    srv.draft.revision = 2;                                      // 另一台设备
    srv.draft.content = "<p>另一台设备的正文</p>";
    await expect(mod.WrDocs.save("ch01s1", "<p>起点，本机又写</p>")).rejects.toMatchObject({ code: "AUTHOR_DRAFT_CONFLICT" });
    await vi.waitFor(() => expect(events.some((event) => event.kind === "conflict-resolved")).toBe(true), T);
    expect(events.filter((event) => event.kind === "conflict-resolved").map((event) => event.html)).toEqual(["<p>另一台设备的正文</p>"]);
    expect(alerts().filter((message) => message.includes("读不到服务端"))).toEqual([]);
    expect(alerts().some((message) => message.includes("别处被修改"))).toBe(true);   // 这一次是真的
    expect(mod.WrRecovery.list().map((entry) => entry.html)).toContain("<p>起点，本机又写</p>");
    expect(srv.draft.content).toBe("<p>另一台设备的正文</p>");
  });

  it("R2-H（对照）自己那一次回包丢了，离场冲刷补发同一稿：客户端带着同一个键补发、服务端重放成功回包，不开冲突、不提示", async () => {
    const { mod, events } = await loadDocs();
    lostOnce("PATCH", /author-drafts\/d1$/);
    void mod.WrDocs.save("ch01s1", "<p>起点，离场前写的</p>").catch(() => {});
    await expect(mod.WrDocs.flush("ch01s1", { retry: true })).resolves.toBe("saved");
    expect(srv.draft).toMatchObject({ revision: 2, content: "<p>起点，离场前写的</p>" });
    expect(srv.log).toContain("PATCH replayed");
    expect(alerts()).toEqual([]);
    expect(events.filter((event) => event.kind === "conflict-resolved")).toEqual([]);
    expect(mod.WrDocs.state("ch01s1")).toMatchObject({ revision: 2, dirty: false, lastSaveError: null });
  });
});
