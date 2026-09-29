// lib/store-kit.js：store 的共用骨架——按键合并的读取器（带写入序号与失效重读）、小 store、统一的错误形状。
// 读取器的用例先按审计 F01-05 的三个真实现象写成（目录连改两次名、回收站 / 待办拉取途中换作品），
// 照旧 store 的写法（在飞就复用同一个请求、回来就写缓存）会在这些用例上转红。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createKeyedLoader, createStore, toStoreError } from "./store-kit.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/* 假后端：每个 GET 挂起，由用例决定什么时候、按什么状态回来 */
function fakeServer() {
  const requests = [];
  const fetch = vi.fn((key, ...args) => {
    const d = deferred();
    requests.push({ key, args, ...d });
    return d.promise;
  });
  return { requests, fetch };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("createKeyedLoader · 合并与按键隔离", () => {
  it("同一个键在飞时再读，复用同一个请求，结果只写一次", async () => {
    const server = fakeServer();
    const apply = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply });
    const first = loader.load("work-a");
    const second = loader.load("work-a");
    expect(server.fetch).toHaveBeenCalledTimes(1);
    expect(loader.inflight("work-a")).toBe(true);
    server.requests[0].resolve({ chapters: 3 });
    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(true);
    expect(apply).toHaveBeenCalledTimes(1);
    expect(apply).toHaveBeenCalledWith("work-a", { chapters: 3 });
    expect(loader.inflight("work-a")).toBe(false);
  });

  it("只按键合并：另一部作品的读取不会并进在飞的请求（换作品后不会停在上一部的结果上）", async () => {
    const server = fakeServer();
    const applied = [];
    const loader = createKeyedLoader({ fetch: server.fetch, apply: (key, data) => applied.push([key, data]) });
    const a = loader.load("work-a");
    const b = loader.load("work-b");
    expect(server.fetch.mock.calls.map(([key]) => key)).toEqual(["work-a", "work-b"]);
    server.requests[1].resolve("B");
    server.requests[0].resolve("A");
    await expect(a).resolves.toBe(true);
    await expect(b).resolves.toBe(true);
    expect(applied).toEqual([["work-b", "B"], ["work-a", "A"]]);
  });

  it("isCurrent 为假时结果与失败都不上报：回收站 / 待办这类只存「当前作品」一份的 store，拉取途中换了作品", async () => {
    const server = fakeServer();
    let active = "work-a";
    const apply = vi.fn();
    const onError = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply, onError, isCurrent: (key) => key === active });
    const a = loader.load("work-a");
    active = "work-b";
    const b = loader.load("work-b");
    expect(server.fetch).toHaveBeenCalledTimes(2);
    server.requests[0].resolve({ items: ["上一部的条目"] });
    await expect(a).resolves.toBe(false);
    expect(apply).not.toHaveBeenCalled();
    server.requests[1].resolve({ items: ["这一部的条目"] });
    await expect(b).resolves.toBe(true);
    expect(apply).toHaveBeenCalledWith("work-b", { items: ["这一部的条目"] });
    // 上一部的请求失败也不该把错误记到这一部头上
    const a2 = loader.load("work-a");
    server.requests[2].reject(new Error("上一部读失败"));
    await expect(a2).resolves.toBe(false);
    expect(onError).not.toHaveBeenCalled();
  });

  it("isCurrent 自己抛错按「不是当前」处理，load 仍不 reject", async () => {
    const server = fakeServer();
    const apply = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply, isCurrent: () => { throw new Error("书架还没好"); } });
    const p = loader.load("work-a");
    server.requests[0].resolve(1);
    await expect(p).resolves.toBe(false);
    expect(apply).not.toHaveBeenCalled();
  });
});

describe("createKeyedLoader · 写入序号", () => {
  it("写入之前发出的读取回来时不覆盖本机刚写的状态", async () => {
    const server = fakeServer();
    const apply = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply });
    const p = loader.load("work-a");
    loader.noteWrite("work-a");
    server.requests[0].resolve({ title: "写入前的服务端状态" });
    await expect(p).resolves.toBe(false);
    expect(apply).not.toHaveBeenCalled();
    // 写入之后发出的读取照常生效
    const q = loader.load("work-a");
    server.requests[1].resolve({ title: "写入后" });
    await expect(q).resolves.toBe(true);
    expect(apply).toHaveBeenCalledWith("work-a", { title: "写入后" });
  });

  it("写入序号按键分开：另一部作品的写入不影响这一部的读取", async () => {
    const server = fakeServer();
    const apply = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply });
    const p = loader.load("work-a");
    loader.noteWrite("work-b");
    server.requests[0].resolve("A");
    await expect(p).resolves.toBe(true);
  });

  it("write(key, run)：写入进行中结束的读取不写缓存；写完（成功或失败）恰好补读一次；重叠的写入只在最后一个结束后补读", async () => {
    const server = fakeServer();
    const applied = [];
    const loader = createKeyedLoader({ fetch: server.fetch, apply: (key, data) => applied.push(data) });
    const patch1 = deferred();
    const patch2 = deferred();
    const w1 = loader.write("work-a", () => patch1.promise);
    const w2 = loader.write("work-a", () => patch2.promise);
    expect(loader.writing("work-a")).toBe(true);
    // 别处在写入进行中触发了一次读取：服务端可能还没处理写入，回来的结果不可信
    const during = loader.load("work-a");
    server.requests[0].resolve("写入进行中读到的");
    await expect(during).resolves.toBe(false);
    patch1.resolve("ok-1");
    await expect(w1).resolves.toBe("ok-1");
    expect(server.fetch).toHaveBeenCalledTimes(1); // 还有一个写入没结束：不补读
    patch2.reject(new Error("第二次保存失败"));
    await expect(w2).rejects.toThrow("第二次保存失败");
    expect(loader.writing("work-a")).toBe(false);
    expect(server.fetch).toHaveBeenCalledTimes(2); // 失败也以服务端为准补读
    server.requests[1].resolve("写完之后的服务端状态");
    await flush();
    expect(applied).toEqual(["写完之后的服务端状态"]);
  });

  it("write 的 reload: false 不补读，但写入进行中发出的读取仍作废", async () => {
    const server = fakeServer();
    const apply = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply });
    const patch = deferred();
    const w = loader.write("work-a", () => patch.promise, { reload: false });
    const during = loader.load("work-a");
    patch.resolve();
    await w;
    server.requests[0].resolve("写入进行中发出的");
    await expect(during).resolves.toBe(false);
    expect(server.fetch).toHaveBeenCalledTimes(1);
    expect(apply).not.toHaveBeenCalled();
  });
});

describe("createKeyedLoader · 失效重读", () => {
  it("在飞时 invalidate：在飞的结果作废，结束后恰好再读一次（连续失效三次也只多读一次），所有等待者拿到重读的结果", async () => {
    const server = fakeServer();
    const applied = [];
    const loader = createKeyedLoader({ fetch: server.fetch, apply: (key, data) => applied.push(data) });
    const first = loader.load("work-a");
    const i1 = loader.invalidate("work-a");
    const i2 = loader.invalidate("work-a");
    const i3 = loader.invalidate("work-a");
    const coalesced = loader.load("work-a");
    expect(server.fetch).toHaveBeenCalledTimes(1);
    server.requests[0].resolve("失效之前的");
    await flush();
    expect(server.fetch).toHaveBeenCalledTimes(2);
    expect(applied).toEqual([]);
    server.requests[1].resolve("重读的");
    await expect(Promise.all([first, i1, i2, i3, coalesced])).resolves.toEqual([true, true, true, true, true]);
    expect(applied).toEqual(["重读的"]);
    expect(server.fetch).toHaveBeenCalledTimes(2);
  });

  it("空闲时 invalidate 立刻读一次", async () => {
    const server = fakeServer();
    const apply = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply });
    const p = loader.invalidate("work-a", { migrate: false });
    expect(server.fetch).toHaveBeenCalledWith("work-a", { migrate: false });
    server.requests[0].resolve(1);
    await expect(p).resolves.toBe(true);
  });

  it("重读用最近一次 invalidate 给的参数；在飞的那次请求失败时也照样重读、不上报它的错误", async () => {
    const server = fakeServer();
    const onError = vi.fn();
    const apply = vi.fn();
    const loader = createKeyedLoader({ fetch: server.fetch, apply, onError });
    const p = loader.load("work-a", { migrate: true });
    loader.invalidate("work-a", { migrate: false });
    server.requests[0].reject(new Error("作废的那次失败了"));
    await flush();
    expect(onError).not.toHaveBeenCalled();
    expect(server.fetch).toHaveBeenLastCalledWith("work-a", { migrate: false });
    server.requests[1].resolve("ok");
    await expect(p).resolves.toBe(true);
  });

  it("失效后键已经不是当前的：不再重读", async () => {
    const server = fakeServer();
    let active = "work-a";
    const loader = createKeyedLoader({ fetch: server.fetch, apply: vi.fn(), isCurrent: (key) => key === active });
    const p = loader.load("work-a");
    loader.invalidate("work-a");
    active = "work-b";
    server.requests[0].resolve("A");
    await expect(p).resolves.toBe(false);
    expect(server.fetch).toHaveBeenCalledTimes(1);
  });
});

describe("createKeyedLoader · 失败", () => {
  it("当前键读失败：onError 收到错误，load 解析为 false、从不 reject；apply 抛错同样走 onError", async () => {
    const server = fakeServer();
    const onError = vi.fn();
    const loader = createKeyedLoader({
      fetch: server.fetch,
      apply: (key, data) => { if (data === "坏数据") throw new Error("映射失败"); },
      onError,
    });
    const p = loader.load("work-a");
    const boom = new Error("读不到目录");
    server.requests[0].reject(boom);
    await expect(p).resolves.toBe(false);
    expect(onError).toHaveBeenCalledWith("work-a", boom);
    const q = loader.load("work-a");
    server.requests[1].resolve("坏数据");
    await expect(q).resolves.toBe(false);
    expect(onError).toHaveBeenLastCalledWith("work-a", expect.objectContaining({ message: "映射失败" }));
  });

  it("fetch 同步抛错也按失败处理", async () => {
    const onError = vi.fn();
    const loader = createKeyedLoader({ fetch: () => { throw new Error("同步炸了"); }, apply: vi.fn(), onError });
    await expect(loader.load("work-a")).resolves.toBe(false);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(loader.inflight("work-a")).toBe(false);
  });
});

/* 审计 F01-05 的原样场景：写作台大纲里连着改两次章名。
   改名 #1 写完后补读 A；A 还在飞（或已被服务端处理）时改名 #2 开始；A 带着 #2 之前的章名回来——
   旧写法（在飞就复用、回来就写缓存）让第二次改名在屏上退回去，补读又被并进 A，最后停在旧名字上。 */
describe("F01-05 · 目录连改两次名", () => {
  function catalogHarness() {
    const server = fakeServer();
    const cache = { title: "旧信" };
    const shown = [];
    const loader = createKeyedLoader({
      fetch: server.fetch,
      apply: (key, data) => { cache.title = data.title; shown.push(data.title); },
    });
    const rename = (title, patch) => {
      cache.title = title; // 乐观写
      shown.push(title);
      return loader.write("work-a", () => patch.promise);
    };
    return { server, cache, shown, loader, rename };
  }

  it("A 在改名 #2 的 PATCH 落地之前回来：屏上不退回旧名，#2 落地后补读收敛到服务端的新名", async () => {
    const { server, cache, shown, rename } = catalogHarness();
    const patch1 = deferred();
    const w1 = rename("案卷", patch1);
    patch1.resolve();
    await w1; // #1 写完 → 补读 A 发出
    expect(server.fetch).toHaveBeenCalledTimes(1);
    const patch2 = deferred();
    const w2 = rename("雨城", patch2);
    server.requests[0].resolve({ title: "案卷" }); // A：服务端还没处理 #2
    await flush();
    expect(cache.title).toBe("雨城");
    patch2.resolve();
    await w2;
    expect(server.fetch).toHaveBeenCalledTimes(2);
    server.requests[1].resolve({ title: "雨城" });
    await flush();
    expect(cache.title).toBe("雨城");
    expect(shown).toEqual(["案卷", "雨城", "雨城"]);
  });

  it("A 一直在飞到改名 #2 写完：#2 的补读不并进 A，A 作废，再读一次拿到新名", async () => {
    const { server, cache, shown, rename } = catalogHarness();
    const patch1 = deferred();
    const w1 = rename("案卷", patch1);
    patch1.resolve();
    await w1;
    const patch2 = deferred();
    const w2 = rename("雨城", patch2);
    patch2.resolve();
    await w2; // #2 的补读请求到来时 A 还在飞
    expect(server.fetch).toHaveBeenCalledTimes(1);
    server.requests[0].resolve({ title: "案卷" }); // A 回来：它早于 #2 落地
    await flush();
    expect(cache.title).toBe("雨城");
    expect(server.fetch).toHaveBeenCalledTimes(2);
    server.requests[1].resolve({ title: "雨城" });
    await flush();
    expect(cache.title).toBe("雨城");
    expect(shown).toEqual(["案卷", "雨城", "雨城"]);
  });
});

describe("createStore", () => {
  it("set 浅合并对象、传函数整体替换；值没变不通知；订阅者抛错不影响别人；退订后不再收到", () => {
    const store = createStore({ status: "idle", items: [] });
    const seen = [];
    const off = store.subscribe(() => seen.push(store.get().status));
    store.subscribe(() => { throw new Error("坏订阅者"); });
    const before = store.get();
    store.set({ status: "idle" });
    expect(store.get()).toBe(before);
    expect(seen).toEqual([]);
    store.set({ status: "loading" });
    expect(store.get()).toEqual({ status: "loading", items: before.items });
    store.set((prev) => ({ ...prev, status: "ready" }));
    expect(seen).toEqual(["loading", "ready"]);
    off();
    store.set({ status: "error" });
    expect(seen).toEqual(["loading", "ready"]);
  });

  it("非对象状态直接替换", () => {
    const store = createStore(0);
    const fn = vi.fn();
    store.subscribe(fn);
    store.set(3);
    store.set(3);
    store.set((n) => n + 1);
    expect(store.get()).toBe(4);
    expect(fn).toHaveBeenCalledTimes(2);
  });

  describe("useStore", () => {
    let host;
    let root;
    afterEach(async () => {
      if (root) await act(async () => root.unmount());
      if (host) host.remove();
      root = null;
      host = null;
    });

    it("按选择器订阅：选中的值变了才重渲，内联选择器返回的新对象不会死循环", async () => {
      const store = createStore({ status: "idle", count: 0 });
      const renders = { status: 0, pair: 0 };
      function Status() {
        renders.status += 1;
        return <span data-testid="status">{store.useStore((s) => s.status)}</span>;
      }
      function Pair() {
        renders.pair += 1;
        const pair = store.useStore((s) => ({ status: s.status, count: s.count }));
        return <span data-testid="pair">{`${pair.status}:${pair.count}`}</span>;
      }
      host = document.createElement("div");
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => root.render(<><Status /><Pair /></>));
      expect(host.querySelector("[data-testid=status]").textContent).toBe("idle");
      const statusRenders = renders.status;
      await act(async () => { store.set({ count: 1 }); });
      expect(host.querySelector("[data-testid=pair]").textContent).toBe("idle:1");
      expect(renders.status).toBe(statusRenders);
      await act(async () => { store.set({ status: "ready" }); });
      expect(host.querySelector("[data-testid=status]").textContent).toBe("ready");
      expect(host.querySelector("[data-testid=pair]").textContent).toBe("ready:1");
    });
  });
});

describe("toStoreError", () => {
  afterEach(() => vi.restoreAllMocks());

  it("code 取错误码、没有就取 HTTP 状态、再没有用兜底码；message 同理；带上是否离线", () => {
    expect(toStoreError({ code: "CATALOG_CONFLICT", status: 409, message: "章序冲突" }, "兜底")).toEqual({
      code: "CATALOG_CONFLICT", message: "章序冲突", offline: false,
    });
    expect(toStoreError({ status: 502 }, "服务端没回应")).toEqual({ code: 502, message: "服务端没回应", offline: false });
    expect(toStoreError(null, "读不到书架")).toEqual({ code: "NETWORK_ERROR", message: "读不到书架", offline: false });
  });

  it("extra 给兜底码与附加字段（雪花同步的 scope）", () => {
    expect(toStoreError(new Error(""), "同步失败，请稍后重试", { code: "SYNC_FAILED", scope: "hydrate" })).toEqual({
      code: "SYNC_FAILED", message: "同步失败，请稍后重试", offline: false, scope: "hydrate",
    });
  });

  it("浏览器报告离线时 offline 为 true", () => {
    vi.spyOn(window.navigator, "onLine", "get").mockReturnValue(false);
    expect(toStoreError(new Error("Failed to fetch"), "兜底").offline).toBe(true);
  });
});
