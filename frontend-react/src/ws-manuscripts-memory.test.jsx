// 成稿中心 · 记忆页签：这一章的章摘要（故事记忆）、作者修改、让 AI 重写、全书的线索台账与编辑通读，与事实抽取的欠账提示。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock("./ws-notify.jsx", () => ({ wsConfirm: vi.fn() }));

import { apiGet, apiPost } from "./lib/client.js";
import { wsConfirm } from "./ws-notify.jsx";
import { ManuMemory, digestToDraft, draftToDigest, threadAlertText, threadWhere } from "./ws-manuscripts-memory.jsx";

const DIGEST = {
  summary: "林昭在档案馆找到旧信，决定去钟楼。",
  ending_state: "林昭揣着旧信出门。",
  character_changes: [{ character: "林昭", change: "拿到了旧信" }],
  threads: [{ id: "c1-T1", thread: "旧信是谁寄出的", status: "opened", note: "信上没有署名", payoff_chapter_id: null }],
};

const THREAD = {
  thread_id: "c1-T1", thread: "旧信是谁寄出的", status: "open", open: true, opened_in: 1, last_touched_in: 1,
  resolved_in: null, planned_payoff_in: null, alert: "cold", dormant_chapters: 4, mark: null, mark_note: "", history: [],
};

const LEDGER = {
  current_number: 5,
  open: [THREAD, { ...THREAD, thread_id: "c2-T1", thread: "钟楼的钥匙", opened_in: 2, last_touched_in: 3, alert: null, planned_payoff_in: 6 }],
  closed: [{ ...THREAD, thread_id: "c1-T2", thread: "守夜人为何隐瞒", open: false, alert: null, mark: "dropped", mark_note: "" }],
  alert_count: 1,
};

const EDITORIAL = {
  interval_chapters: 3,
  latest: {
    review_id: "r1", status: "active", origin: "auto", created_at: "2026-10-12T00:00:00Z", through_chapter_id: "c3", through_chapter_number: 3,
    window: [{ chapter_id: "c4", number: 4, title: "渡口" }],
    assessment: "前三章林昭一直独来独往，人物弧没动。",
    findings: [{ kind: "arc_stall", severity: "high", chapters: [1, 2], finding: "林昭三章都没求过人", suggestion: "下一章让他不得不开口" }],
    guidance: [
      { guidance_id: "g1", review_id: "r1", chapter_id: "c4", chapter_number: 4, text: "让林昭在渡口第一次开口求人", author_text: null, reason: "人物弧停滞", status: "proposed", decided_at: null },
      { guidance_id: "g2", review_id: "r1", chapter_id: null, chapter_number: null, text: "换一种收尾方式", author_text: null, reason: "收尾重复", status: "accepted", decided_at: "x" },
    ],
  },
  history: [],
  accepted: [
    { guidance_id: "g2", review_id: "r1", chapter_id: null, chapter_number: null, text: "换一种收尾方式", author_text: null, reason: "", status: "accepted", window: [4] },
    { guidance_id: "g0", review_id: "r0", chapter_id: "c4", chapter_number: 4, text: "让守夜人回来", author_text: "让守夜人在渡口回来", reason: "", status: "accepted", window: null },
  ],
  can_run: true,
  readable_through: 3,
  next_auto_after_chapter: 6,
};

function overview(entry = {}, extra = {}) {
  return {
    project_id: "prj-main",
    chapters: [
      { chapter_id: "c1", number: 1, title: "旧信", status: "fresh", eligible: true, scene_count: 2, written_scene_count: 2, digest_row_id: "cd1", origin: "llm", digest: DIGEST, ...entry },
      { chapter_id: "c2", number: 2, title: "钟楼", status: "not_ready", eligible: false, scene_count: 2, written_scene_count: 1, digest_row_id: null, origin: null, digest: null },
    ],
    threads: LEDGER,
    editorial: EDITORIAL,
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
  it("shows the chapter digest with its status and its thread updates", async () => {
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    expect(apiGet).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory");
    expect(host.querySelector('[data-testid="manuscript-memory-status"]').textContent).toBe("已是最新");
    expect(host.textContent).toContain("林昭在档案馆找到旧信，决定去钟楼。");
    expect(host.textContent).toContain("埋下 旧信是谁寄出的：信上没有署名");
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
    const textareas = host.querySelectorAll('[data-testid="manuscript-memory"] .ms-canon-manual-grid textarea');
    expect(textareas).toHaveLength(3); // 发生了什么、章末、人物变化——线索不在这里改
    const [summary, , changes] = textareas;
    await typeInto(summary, "林昭找到旧信（作者改过）。");
    await typeInto(changes, "林昭：拿到了旧信\n守夜人：认出了信");
    apiGet.mockResolvedValue(overview({ origin: "author", digest: { ...DIGEST, summary: "林昭找到旧信（作者改过）。" } }));
    await click(host.querySelector('[data-testid="manuscript-memory-save"]'));

    expect(apiPost).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory/chapters/c1/digest", {
      base_row_id: "cd1",
      digest: {
        summary: "林昭找到旧信（作者改过）。",
        ending_state: "林昭揣着旧信出门。",
        character_changes: [{ character: "林昭", change: "拿到了旧信" }, { character: "守夜人", change: "认出了信" }],
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
  it("edits only the text and never sends thread updates", () => {
    const draft = digestToDraft(DIGEST);
    expect(Object.keys(draft)).toEqual(["summary", "ending_state", "character_changes"]);
    const back = draftToDigest({ ...draft, character_changes: "林昭：拿到了旧信\n没有冒号的一行" });
    expect(back).toEqual({ summary: DIGEST.summary, ending_state: DIGEST.ending_state, character_changes: [{ character: "林昭", change: "拿到了旧信" }] });
  });

  it("describes where a thread stands and why it needs attention", () => {
    expect(threadWhere({ opened_in: 1, last_touched_in: 3, planned_payoff_in: 6 })).toBe("第 1 章埋下 · 第 3 章最近推进 · 计划第 6 章兑现");
    expect(threadWhere({ opened_in: 2, last_touched_in: 2, resolved_in: 4, planned_payoff_in: 4 })).toBe("第 2 章埋下 · 第 4 章兑现");
    expect(threadAlertText(THREAD)).toBe("已经 4 章没碰");
    expect(threadAlertText({ alert: "payoff_missed", planned_payoff_in: 3 })).toBe("计划第 3 章兑现，还没兑现");
    expect(threadAlertText({ alert: null })).toBe("");
  });
});

describe("thread ledger and editorial read-through", () => {
  it("lists open threads with their alerts and marks one without losing a digest edit", async () => {
    apiPost.mockResolvedValue({});
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    await click(host.querySelector('[data-testid="manuscript-memory-edit"]'));
    await typeInto(host.querySelector("textarea"), "正在改的一句。");
    const ledger = host.querySelector('[data-testid="manuscript-memory-ledger"]');
    expect(ledger.textContent).toContain("1 条要留意");
    const rows = ledger.querySelectorAll('[data-testid="manuscript-memory-thread"]');
    expect(rows[0].textContent).toContain("旧信是谁寄出的已经 4 章没碰");
    expect(rows[1].textContent).toContain("第 2 章埋下 · 第 3 章最近推进 · 计划第 6 章兑现");
    expect(ledger.textContent).toContain("已经收掉或放下的 1 条");

    await click([...rows[0].querySelectorAll("button")].find((b) => b.textContent === "放下"));
    expect(apiPost).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory/threads/c1-T1/mark", { status: "dropped" });
    expect(apiGet).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("已放下这条线");
    expect(host.querySelector("textarea").value).toBe("正在改的一句。"); // 摘要编辑还在

    const closedRow = host.querySelector(".ms-memory-closed [data-testid='manuscript-memory-thread']");
    expect(closedRow.textContent).toContain("你放下了");
    await click([...closedRow.querySelectorAll("button")].find((b) => b.textContent === "取消标记"));
    expect(apiPost).toHaveBeenLastCalledWith("/api/v1/projects/prj-main/story-memory/threads/c1-T2/mark", { status: "open" });
  });

  it("shows the latest read-through and decides its guidance", async () => {
    apiPost.mockResolvedValue({});
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    const panel = host.querySelector('[data-testid="manuscript-memory-editorial"]');
    expect(panel.textContent).toContain("读到第 3 章 · 自动");
    expect(panel.textContent).toContain("前三章林昭一直独来独往，人物弧没动。");
    expect(panel.textContent).toContain("严重人物弧停滞第 1、2 章");
    expect(panel.textContent).toContain("建议：下一章让他不得不开口");
    const [proposed, accepted] = panel.querySelectorAll('[data-testid="manuscript-memory-guidance"]');
    expect(proposed.textContent).toContain("第 4 章待你决定");
    expect(accepted.textContent).toContain("第 4 章已采纳"); // 针对整段：显示通读的后面几章
    // 以前采纳、仍在生效的（不在最新一份里的）另列
    expect(panel.textContent).toContain("以前采纳、仍在生效的意见");
    expect(panel.textContent).toContain("让守夜人在渡口回来");
    expect(panel.textContent).toContain("下一次自动通读：写到第 6 章");

    await click(proposed.querySelector('[data-testid="manuscript-memory-guidance-accept"]'));
    expect(apiPost).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory/editorial-guidance/g1/decision", { decision: "accept" });

    const fresh = host.querySelector('[data-testid="manuscript-memory-editorial"] [data-testid="manuscript-memory-guidance"]');
    await click([...fresh.querySelectorAll("button")].find((b) => b.textContent.includes("改后采纳")));
    await typeInto(fresh.querySelector("textarea"), "让林昭在渡口求他最不信的人");
    await click([...fresh.querySelectorAll("button")].find((b) => b.textContent.includes("采纳这一版")));
    expect(apiPost).toHaveBeenLastCalledWith("/api/v1/projects/prj-main/story-memory/editorial-guidance/g1/decision", {
      decision: "accept",
      text: "让林昭在渡口求他最不信的人",
    });

    const second = host.querySelectorAll('[data-testid="manuscript-memory-editorial"] [data-testid="manuscript-memory-guidance"]')[1];
    await click(second.querySelector('[data-testid="manuscript-memory-guidance-dismiss"]'));
    expect(apiPost).toHaveBeenLastCalledWith("/api/v1/projects/prj-main/story-memory/editorial-guidance/g2/decision", { decision: "dismiss" });
  });

  it("runs a read-through on request and explains when there is none yet", async () => {
    apiGet.mockResolvedValue(overview({}, { editorial: { ...EDITORIAL, latest: null, accepted: [] } }));
    apiPost.mockResolvedValue({});
    const host = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    expect(host.textContent).toContain("还没有通读过。每写完 3 章");
    await click(host.querySelector('[data-testid="manuscript-memory-review-run"]'));
    expect(apiPost).toHaveBeenCalledWith("/api/v1/projects/prj-main/story-memory/editorial-review", {});
    expect(host.textContent).toContain("编辑通读写好了");

    apiGet.mockResolvedValue(overview({}, { llm_enabled: false }));
    const paused = await render(<ManuMemory projectId="prj-main" chapterId="c1" />);
    expect(paused.querySelector('[data-testid="manuscript-memory-review-run"]')).toBeNull();
  });
});
