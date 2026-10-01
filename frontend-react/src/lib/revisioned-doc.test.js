// lib/revisioned-doc.js：带修订号的写穿文档（本场笔记用它）。组件层的换场隔离在 ws-writer-notes.test.jsx。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRevisionedDocs } from "./revisioned-doc.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

const conflict = () => Object.assign(new Error("conflict"), { code: "NOTE_CONFLICT" });

function makeDocs(server) {
  const calls = { save: [], load: 0 };
  const docs = createRevisionedDocs({
    cacheKey: (id) => `note:${id}`,
    pendingKey: (id) => `note-pending:${id}`,
    load: vi.fn(async (id) => { calls.load += 1; return server.load ? server.load(id) : { value: server.value, revision: server.revision }; }),
    save: vi.fn((id, value, baseRevision) => {
      calls.save.push({ id, value, baseRevision });
      return server.save(id, value, baseRevision);
    }),
    reloadRevision: vi.fn(async () => server.revision),
    isConflict: (error) => !!(error && error.code === "NOTE_CONFLICT"),
    debounceMs: 500,
  });
  return { docs, calls };
}

const settle = async () => { for (let i = 0; i < 6; i += 1) await Promise.resolve(); };

beforeEach(() => {
  vi.useFakeTimers();
  window.localStorage.clear();
});
afterEach(() => { vi.useRealTimers(); });

describe("createRevisionedDocs", () => {
  it("打开时先给本机那一份，读到服务器之后换成服务器的并清掉未同步标记", async () => {
    window.localStorage.setItem("note:a", "本机旧稿");
    const server = { value: "服务器的笔记", revision: 3, save: async () => ({ revision: 4 }) };
    const { docs } = makeDocs(server);
    const onChange = vi.fn();
    const doc = docs.open("a", onChange);
    expect(doc.value()).toBe("本机旧稿");
    expect(doc.status()).toBe("loading");
    await settle();
    expect(doc.value()).toBe("服务器的笔记");
    expect(doc.status()).toBe("saved");
    expect(window.localStorage.getItem("note:a")).toBe("服务器的笔记");
    expect(onChange).toHaveBeenCalled();
  });

  it("防抖到点才存；前一次还在路上时后一次排在它后面、带着它拿回的修订号", async () => {
    const first = deferred();
    const server = { value: "", revision: 1, save: vi.fn((id, value) => (value === "一" ? first.promise : Promise.resolve({ revision: 3 }))) };
    const { docs, calls } = makeDocs(server);
    const doc = docs.open("a");
    await settle();
    doc.edit("一");
    expect(calls.save).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(500);
    expect(calls.save).toEqual([{ id: "a", value: "一", baseRevision: 1 }]);
    doc.edit("一二");
    await vi.advanceTimersByTimeAsync(500);
    expect(calls.save).toHaveLength(1);
    first.resolve({ revision: 2 });
    await settle();
    expect(calls.save[1]).toEqual({ id: "a", value: "一二", baseRevision: 2 });
    await settle();
    expect(doc.status()).toBe("saved");
    expect(window.localStorage.getItem("note-pending:a")).toBeNull();
  });

  it("服务器回冲突：重读修订号、状态报冲突；重试带着新的修订号", async () => {
    const server = { value: "", revision: 1, save: vi.fn().mockRejectedValueOnce(conflict()).mockResolvedValue({ revision: 6 }) };
    const { docs, calls } = makeDocs(server);
    const doc = docs.open("a");
    await settle();
    server.revision = 5; // 另一台设备存过
    doc.edit("本机写的");
    await vi.advanceTimersByTimeAsync(500);
    await settle();
    expect(doc.status()).toBe("conflict");
    expect(window.localStorage.getItem("note-pending:a")).not.toBeNull();
    doc.retry();
    await settle();
    expect(calls.save[1]).toEqual({ id: "a", value: "本机写的", baseRevision: 5 });
    expect(doc.status()).toBe("saved");
  });

  it("上个会话没存上的一稿（有标记、与服务器不同）留着给重试，不冒充冲突", async () => {
    window.localStorage.setItem("note:a", "上次没存上的");
    window.localStorage.setItem("note-pending:a", "1");
    const server = { value: "服务器的", revision: 2, save: async () => ({ revision: 3 }) };
    const { docs, calls } = makeDocs(server);
    const doc = docs.open("a");
    expect(doc.status()).toBe("local");
    await settle();
    expect(doc.value()).toBe("上次没存上的");
    expect(doc.status()).toBe("local");
    doc.retry();
    await settle();
    expect(calls.save).toEqual([{ id: "a", value: "上次没存上的", baseRevision: 2 }]);
    expect(doc.status()).toBe("saved");
  });

  it("关掉时没到点的改动立刻存回这一份；再打开同一份先等它存完再读服务器", async () => {
    const saving = deferred();
    const server = { value: "", revision: 1, save: vi.fn(() => saving.promise) };
    const { docs, calls } = makeDocs(server);
    const doc = docs.open("a");
    await settle();
    expect(calls.load).toBe(1);
    doc.edit("离开前写的");
    doc.close();
    await settle();
    expect(calls.save).toEqual([{ id: "a", value: "离开前写的", baseRevision: 1 }]);
    doc.edit("关掉之后不再收");
    expect(window.localStorage.getItem("note:a")).toBe("离开前写的");

    server.value = "离开前写的";
    server.revision = 2;
    const reopened = docs.open("a");
    await settle();
    expect(calls.load).toBe(1);              // 离开时那一次还没回来：先不读服务器
    expect(reopened.status()).toBe("local"); // 未同步标记还在
    saving.resolve({ revision: 2 });
    await settle();
    await settle();
    expect(calls.load).toBe(2);
    expect(reopened.status()).toBe("saved");
    expect(reopened.value()).toBe("离开前写的");
  });

  it("读不了（load 给 null，例如场景还没同步到服务器）：状态不动", async () => {
    const server = { load: () => null, save: async () => ({ revision: 1 }) };
    const { docs } = makeDocs(server);
    const doc = docs.open("a");
    await settle();
    expect(doc.status()).toBe("loading");
  });
});

/* 换了名字（乐观新建的场：临时 sid → 稳定的 scene_id）：本机这一层跟到新名字下，不丢没同步上的字，也不盖新名字下自己的那一份 */
describe("createRevisionedDocs · 换了名字", () => {
  it("open(新名字, { renamedFrom })：先给旧名字那一份；旧那一份离开时的保存没成（还是未同步）就搬过去，显示它、状态是未同步", async () => {
    const saving = deferred();
    const server = { value: "", revision: 1, save: vi.fn(() => saving.promise) };
    const { docs, calls } = makeDocs(server);
    const old = docs.open("tmp_1");
    await settle();
    old.edit("新建时写的笔记");
    old.close();                                   // 换名：没到点的那一稿立刻存（在路上）
    const renamed = docs.open("s9", null, { renamedFrom: "tmp_1" });
    expect(renamed.value()).toBe("新建时写的笔记");
    expect(renamed.status()).toBe("local");
    await settle();
    expect(calls.load).toBe(1);                    // 旧名字那一次保存没回来：新名字先不读服务器
    saving.reject(Object.assign(new Error("offline"), { code: "NETWORK_ERROR" }));
    await settle();
    await settle();
    expect(window.localStorage.getItem("note:s9")).toBe("新建时写的笔记");
    expect(window.localStorage.getItem("note-pending:s9")).not.toBeNull();
    expect(window.localStorage.getItem("note:tmp_1")).toBeNull();
    expect(window.localStorage.getItem("note-pending:tmp_1")).toBeNull();
    expect(renamed.value()).toBe("新建时写的笔记");
    expect(renamed.status()).toBe("local");
  });

  it("旧那一份离开时的保存成了：只搬读缓存，新名字读到服务器的、状态是已存", async () => {
    const saving = deferred();
    const server = { value: "", revision: 1, save: vi.fn(() => saving.promise) };
    const { docs } = makeDocs(server);
    const old = docs.open("tmp_1");
    await settle();
    old.edit("新建时写的笔记");
    old.close();
    const renamed = docs.open("s9", null, { renamedFrom: "tmp_1" });
    server.value = "新建时写的笔记";
    server.revision = 2;
    saving.resolve({ revision: 2 });
    await settle();
    await settle();
    expect(renamed.status()).toBe("saved");
    expect(renamed.value()).toBe("新建时写的笔记");
    expect(window.localStorage.getItem("note-pending:s9")).toBeNull();
    expect(window.localStorage.getItem("note:tmp_1")).toBeNull();
  });

  it("rename：旧名字那一份正开着时不动（它换名时自己搬）；没开着就搬；新名字下已有自己的一份时不盖它", async () => {
    const server = { value: "", revision: 1, save: async () => ({ revision: 2 }) };
    const { docs } = makeDocs(server);
    window.localStorage.setItem("note:tmp_1", "没同步上的");
    window.localStorage.setItem("note-pending:tmp_1", "1");
    const open = docs.open("tmp_1");
    expect(docs.rename("tmp_1", "s9")).toBe(false);
    expect(window.localStorage.getItem("note:s9")).toBeNull();
    open.close();
    await settle();
    expect(docs.rename("tmp_1", "s9")).toBe(true);
    await settle();
    expect(window.localStorage.getItem("note:s9")).toBe("没同步上的");
    expect(window.localStorage.getItem("note-pending:s9")).toBe("1");
    expect(window.localStorage.getItem("note:tmp_1")).toBeNull();

    // 新名字下已有自己的一份：标着未同步的旧稿原样留着；只是读缓存的旧稿扔掉
    window.localStorage.setItem("note:tmp_2", "旧名字下没同步上的");
    window.localStorage.setItem("note-pending:tmp_2", "1");
    window.localStorage.setItem("note:tmp_3", "旧名字下的读缓存");
    expect(docs.rename("tmp_2", "s9")).toBe(true);
    expect(docs.rename("tmp_3", "s9")).toBe(true);
    expect(window.localStorage.getItem("note:s9")).toBe("没同步上的");
    expect(window.localStorage.getItem("note:tmp_2")).toBe("旧名字下没同步上的");
    expect(window.localStorage.getItem("note:tmp_3")).toBeNull();
  });
});

