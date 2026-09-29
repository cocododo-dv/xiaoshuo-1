import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
  getApiBase,
  getClientStorageStatus,
  getOperatorRef,
  getRemoteAccessToken,
  setApiBase,
  setOperatorRef,
  setRemoteAccessToken,
} from "./client.js";


describe("remote access token transport", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: { status: "ok" } }),
    });
  });

  afterEach(() => {
    setRemoteAccessToken("");
    vi.restoreAllMocks();
  });

  it("sends the explicitly configured token without persisting it to localStorage", async () => {
    setRemoteAccessToken("remote-secret");

    await apiGet("/ready");

    const [, options] = global.fetch.mock.calls[0];
    expect(options.headers["X-Novel-Access-Token"]).toBe("remote-secret");
    expect(window.localStorage.getItem("novel-system-remote-access-token")).toBeNull();
  });

  it.each([
    ["POST", () => apiPost("/mutation/post", { value: 1 })],
    ["PATCH", () => apiPatch("/mutation/patch", { value: 1 })],
    ["PUT", () => apiPut("/mutation/put", { value: 1 })],
    ["DELETE", () => apiDelete("/mutation/delete")],
  ])("sends an idempotency key for %s mutations", async (method, invoke) => {
    await invoke();

    const [, options] = global.fetch.mock.calls[0];
    expect(options.method).toBe(method);
    expect(options.headers["X-Idempotency-Key"]).toEqual(expect.any(String));
    expect(options.headers["X-Idempotency-Key"]).not.toBe("");
  });

  it.each([
    "Failed to fetch",
    "NetworkError when attempting to fetch resource.",
    "Load failed",
    "fetch failed",
  ])("reuses the PATCH key after browser transport failure: %s", async (message) => {
    global.fetch
      .mockRejectedValueOnce(new TypeError(message))
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ok: true, data: { status: "ok" } }),
      });

    await expect(apiPatch("/mutation/retry", { value: 1 })).rejects.toMatchObject({
      code: "NETWORK_ERROR",
      retryable: true,
    });
    await apiPatch("/mutation/retry", { value: 1 });

    const firstKey = global.fetch.mock.calls[0][1].headers["X-Idempotency-Key"];
    const secondKey = global.fetch.mock.calls[1][1].headers["X-Idempotency-Key"];
    expect(secondKey).toBe(firstKey);
  });

  it("sends FormData as multipart (no JSON content type) and honours an explicit idempotency key", async () => {
    const form = new FormData();
    form.append("title", "合成参考");
    form.append("file", new Blob(["片段"], { type: "text/plain" }), "a.txt");

    await apiPost("/upload", form, { idempotencyKey: "import-key-1" });
    await apiPost("/upload", form);
    await apiPost("/upload", form);

    const [first, second, third] = global.fetch.mock.calls.map(([, options]) => options);
    expect(first.body).toBe(form);
    expect(first.headers["Content-Type"]).toBeUndefined();
    expect(first.headers["X-Idempotency-Key"]).toBe("import-key-1");
    // 没给键的 multipart：每次新配一个（载荷没法按内容签名，不能拿上一次的键重放）
    expect(second.body).toBe(form);
    expect(second.headers["X-Idempotency-Key"]).toEqual(expect.any(String));
    expect(second.headers["X-Idempotency-Key"]).not.toBe(third.headers["X-Idempotency-Key"]);
  });

  it("two identical mutations in flight share one request and one result (no self-inflicted 409)", async () => {
    const releases = [];
    global.fetch = vi.fn(() => new Promise((resolve) => { releases.push(resolve); }));
    const ok = (data) => ({ ok: true, status: 200, json: async () => ({ ok: true, data }) });

    const first = apiPost("/mutation/double", { value: 1 });
    const second = apiPost("/mutation/double", { value: 1 });
    const other = apiPost("/mutation/double", { value: 2 });
    await vi.waitFor(() => expect(releases).toHaveLength(2)); // 同载荷的两个并成一个；另一个载荷照常另发
    releases[0](ok({ n: 1 }));
    releases[1](ok({ n: 2 }));
    await expect(first).resolves.toEqual({ n: 1 });
    await expect(second).resolves.toEqual({ n: 1 });
    await expect(other).resolves.toEqual({ n: 2 });

    // 前一个结束之后，同载荷再来一次是新的意图：另发一个请求
    global.fetch = vi.fn().mockResolvedValue(ok({ again: true }));
    await expect(apiPost("/mutation/double", { value: 1 })).resolves.toEqual({ again: true });
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("resolves the API base once instead of touching localStorage on every request", async () => {
    await apiGet("/ready");
    const getItem = vi.spyOn(Storage.prototype, "getItem");
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    await apiGet("/ready");
    await apiPost("/mutation/storage", { value: 1 });
    expect(getItem.mock.calls.filter(([key]) => String(key).startsWith("novel-system-api-base"))).toEqual([]);
    expect(setItem).not.toHaveBeenCalled();
    // setApiBase 作废缓存，下一个请求用新地址
    setApiBase("http://127.0.0.1:8123");
    await apiGet("/ready");
    expect(global.fetch.mock.calls.at(-1)[0]).toBe("http://127.0.0.1:8123/ready");
    setApiBase("");
  });

  it("canonicalizes object key order when retaining an uncertain mutation", async () => {
    global.fetch
      .mockRejectedValueOnce(new TypeError("Load failed"))
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ok: true, data: { status: "ok" } }),
      });

    await expect(apiPatch("/mutation/canonical", {
      beta: { second: 2, first: 1 },
      alpha: true,
    })).rejects.toMatchObject({ code: "NETWORK_ERROR", retryable: true });
    await apiPatch("/mutation/canonical", {
      alpha: true,
      beta: { first: 1, second: 2 },
    });

    const firstKey = global.fetch.mock.calls[0][1].headers["X-Idempotency-Key"];
    const secondKey = global.fetch.mock.calls[1][1].headers["X-Idempotency-Key"];
    expect(secondKey).toBe(firstKey);
  });

  it("falls back to in-memory settings when browser storage throws SecurityError", async () => {
    const blocked = () => { throw new DOMException("blocked", "SecurityError"); };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(blocked);

    expect(() => setApiBase("http://127.0.0.1:8123")).not.toThrow();
    expect(() => setOperatorRef("storage-fallback-operator")).not.toThrow();
    expect(() => setRemoteAccessToken("storage-fallback-token")).not.toThrow();
    expect(getApiBase()).toBe("http://127.0.0.1:8123");
    expect(getOperatorRef()).toBe("storage-fallback-operator");
    expect(getRemoteAccessToken()).toBe("storage-fallback-token");

    await apiGet("/ready");
    const [, options] = global.fetch.mock.calls[0];
    expect(options.headers["X-Novel-Access-Token"]).toBe("storage-fallback-token");
    expect(getClientStorageStatus()).toMatchObject({
      local: { available: false, errorName: "SecurityError" },
      session: { available: false, errorName: "SecurityError" },
    });
  });

  it("turns an elapsed request deadline into a typed retryable timeout", async () => {
    vi.useFakeTimers();
    let fetchSignal = null;
    global.fetch = vi.fn((url, options) => new Promise((resolve, reject) => {
      void url;
      void resolve;
      fetchSignal = options.signal;
      options.signal.addEventListener(
        "abort",
        () => reject(new DOMException("aborted", "AbortError")),
        { once: true },
      );
    }));

    const pending = apiGet("/slow", { timeoutMs: 25 });
    const rejected = expect(pending).rejects.toMatchObject({
      code: "REQUEST_TIMEOUT",
      retryable: true,
      details: expect.objectContaining({ timeoutMs: 25 }),
    });
    await vi.advanceTimersByTimeAsync(25);

    await rejected;
    expect(fetchSignal.aborted).toBe(true);
    vi.useRealTimers();
  });

  it("preserves caller cancellation semantics when a deadline is also configured", async () => {
    const controller = new AbortController();
    global.fetch = vi.fn((url, options) => new Promise((resolve, reject) => {
      void url;
      void resolve;
      options.signal.addEventListener(
        "abort",
        () => reject(new DOMException("aborted", "AbortError")),
        { once: true },
      );
    }));

    const pending = apiGet("/cancelled", { signal: controller.signal, timeoutMs: 10_000 });
    controller.abort();

    await expect(pending).rejects.toMatchObject({ code: "REQUEST_ABORTED" });
  });
});
