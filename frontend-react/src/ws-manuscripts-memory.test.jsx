// 成稿中心 · 记忆页签：这一章的章摘要（故事记忆）、作者修改、让 AI 重写，与事实抽取的欠账提示。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock("./ws-notify.jsx", () => ({ wsConfirm: vi.fn() }));

import { apiGet, apiPost } from "./lib/client.js";
import { wsConfirm } from "./ws-notify.jsx";
import { ManuMemory, digestToDraft, draftToDigest } from "./ws-manuscripts-memory.jsx";

const DIGEST = {
  summary: "林昭在档案馆找到旧信，决定去钟楼。",
  ending_state: "林昭揣着旧信出门。",
  character_changes: [{ character: "林昭", change: "拿到了旧信" }],
  open_threads: [{ thread: "旧信是谁寄出的", opened_in: 1 }],
  resolved_threads: [],
};

function overview(entry = {}, extra = {}) {
  return {
    project_id: "prj-main",
    chapters: [
      { chapter_id: "c1", number: 1, title: "旧信", status: "fresh", eligible: true, scene_count: 2, written_scene_count: 2, digest_row_id: "cd1", origin: "llm", digest: DIGEST, ...entry },
      { chapter_id: "c2", number: 2, title: "钟楼", status: "not_ready", eligible: false, scene_count: 2, written_scene_count: 1, digest_row_id: null, origin: null, digest: null },
    ],
    extraction: { pending_scene_count: 0, degraded_scene_count: 0 },
    llm_enabled: true,
    ...extra,
  };
}

const mounted = [];
async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  await act(async () => {});
  return host;
}
const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const typeInto = (node, value) => act(async () => {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
  setter.call(node, value);
  node.dispatchEvent(new Event("input", { bubbles: true }));
});

beforeEach(() => { vi.clearAllMocks(); apiGet.mockResolvedValue(overview()); });
afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
});

describe("ManuMemory", () => {
  it("shows the chapter digest with its status and open threads", async () => {
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    expect(apiGet).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory");
    expect(host.querySelector('[data-testid="manuscript-memory-status"]').textContent).toBe("已是最新");
    expect(host.textContent).toContain("林昭在档案馆找到旧信，决定去钟楼。");
    expect(host.textContent).toContain("旧信是谁寄出的第 1 章起");
    expect(host.textContent).toContain("林昭：拿到了旧信");
  });

  it("explains an unfinished chapter and offers no AI rewrite", async () => {
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c2" />);
    expect(host.querySelector('[data-testid="manuscript-memory-status"]').textContent).toBe("本章还没写完");
    expect(host.textContent).toContain("1/2 场有终稿");
    expect(host.querySelector('[data-testid="manuscript-memory-regenerate"]')).toBeNull();
    expect(host.querySelector('[data-testid="manuscript-memory-edit"]')).toBeNull();
  });

  it("reports the extraction backlog and a paused worker", async () => {
    apiGet.mockResolvedValue(overview({}, { extraction: { pending_scene_count: 2, degraded_scene_count: 1 }, llm_enabled: false }));
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    expect(host.textContent).toContain("还有 3 场的事实没抽完（1 场上次失败）");
    expect(host.textContent).toContain("后台暂停");
  });

  it("saves the author's edit against the digest it was opened on and refetches", async () => {
    apiPost.mockResolvedValue({});
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    await click(host.querySelector('[data-testid="manuscript-memory-edit"]'));
    const [summary, , , threads] = host.querySelectorAll("textarea");
    await typeInto(summary, "林昭找到旧信（作者改过）。");
    await typeInto(threads, "旧信是谁寄出的\n守夜人为何隐瞒");
    apiGet.mockResolvedValue(overview({ origin: "author", digest: { ...DIGEST, summary: "林昭找到旧信（作者改过）。" } }));
    await click(host.querySelector('[data-testid="manuscript-memory-save"]'));

    expect(apiPost).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory/chapters/c1/digest", {
      base_row_id: "cd1",
      digest: {
        summary: "林昭找到旧信（作者改过）。",
        ending_state: "林昭揣着旧信出门。",
        character_changes: [{ character: "林昭", change: "拿到了旧信" }],
        open_threads: [{ thread: "旧信是谁寄出的", opened_in: 1 }, { thread: "守夜人为何隐瞒", opened_in: 1 }],
        resolved_threads: [],
      },
    });
    expect(apiGet).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("林昭找到旧信（作者改过）。");
    expect(host.textContent).toContain("你改过");
  });

  it("refuses an empty summary without a request and shows a save conflict", async () => {
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    await click(host.querySelector('[data-testid="manuscript-memory-edit"]'));
    await typeInto(host.querySelector("textarea"), "   ");
    await click(host.querySelector('[data-testid="manuscript-memory-save"]'));
    expect(apiPost).not.toHaveBeenCalled();
    expect(host.textContent).toContain("「发生了什么」不能为空。");

    await typeInto(host.querySelector("textarea"), "改了一句。");
    apiPost.mockRejectedValue(Object.assign(new Error("这一章的摘要在你编辑期间换了一份：刷新后再改"), { code: "CHAPTER_DIGEST_CHANGED" }));
    await click(host.querySelector('[data-testid="manuscript-memory-save"]'));
    expect(host.textContent).toContain("这一章的摘要在你编辑期间换了一份");
  });

  it("asks before letting AI overwrite an author-edited digest", async () => {
    apiGet.mockResolvedValue(overview({ origin: "author", status: "stale" }));
    wsConfirm.mockResolvedValue(false);
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    expect(host.querySelector('[data-testid="manuscript-memory-status"]').textContent).toBe("正文改过，待更新");
    await click(host.querySelector('[data-testid="manuscript-memory-regenerate"]'));
    expect(wsConfirm).toHaveBeenCalledTimes(1);
    expect(apiPost).not.toHaveBeenCalled();

    wsConfirm.mockResolvedValue(true);
    apiPost.mockResolvedValue({});
    await click(host.querySelector('[data-testid="manuscript-memory-regenerate"]'));
    expect(apiPost).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory/chapters/c1/digest/regenerate", {});
  });
});

describe("digest draft round trip", () => {
  it("keeps carried threads' chapter of origin and dates new ones to this chapter", () => {
    const draft = digestToDraft({ ...DIGEST, resolved_threads: [{ thread: "钟声为何停了", resolution: "守夜人拔了钟锤" }] });
    expect(draft.resolved_threads).toBe("钟声为何停了：守夜人拔了钟锤");
    const back = draftToDigest({ ...draft, open_threads: "旧信是谁寄出的\n北境来的马车" }, DIGEST, 4);
    expect(back.open_threads).toEqual([{ thread: "旧信是谁寄出的", opened_in: 1 }, { thread: "北境来的马车", opened_in: 4 }]);
    expect(back.resolved_threads).toEqual([{ thread: "钟声为何停了", resolution: "守夜人拔了钟锤" }]);
  });
});
