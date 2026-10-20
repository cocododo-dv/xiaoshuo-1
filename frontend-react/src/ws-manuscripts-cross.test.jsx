// 成稿中心 · 跨章页签：全书反复出现的写法（每一处进写作台那一场的那一条）、章与章的形状、线索提醒。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn() }));

import { apiGet } from "./lib/client.js";
import { ManuCrossCheck } from "./ws-manuscripts-cross.jsx";

const T = { timeout: 4000, interval: 20 };

const CHAPTERS = [
  { id: "ch01", backendId: "c1", n: "01", title: "雨城", scenes: [{ sid: "ch01s1", backendId: "s1", title: "旧信" }, { sid: "ch01s2", backendId: "s2", title: "案卷" }] },
  { id: "ch02", backendId: "c2", n: "02", title: "档案室", scenes: [{ sid: "ch02s1", backendId: "s3", title: "灯" }] },
];

function report(overrides = {}) {
  return {
    project_id: "prj-main", scenes_with_text: 3, chapters_with_text: 2, chapters_complete: 2, chars: 24721, reference_bound: true,
    repeats: [
      {
        phrase: "手指慢慢攥得发白", signal_id: "repeat:abc", count: 2, scene_count: 2, chapter_count: 2, reference_count: 0, habitual: false, ignored: false,
        occurrences: [
          { chapter_id: "c1", chapter_number: 1, scene_id: "s1", scene_number: 1, paragraph_index: 0, excerpt: "林昭手指慢慢攥得发白，把旧信塞回案卷", ignored: false },
          { chapter_id: "c2", chapter_number: 2, scene_id: "s3", scene_number: 1, paragraph_index: 3, excerpt: "许望手指慢慢攥得发白", ignored: true },
        ],
      },
      {
        phrase: "从口袋里摸出", signal_id: "repeat:def", count: 3, scene_count: 3, chapter_count: 2, reference_count: 40, habitual: true, ignored: false,
        occurrences: [{ chapter_id: "c1", chapter_number: 1, scene_id: "s2", scene_number: 2, paragraph_index: 1, excerpt: "从口袋里摸出一封旧信", ignored: false }],
      },
    ],
    shapes: {
      chapters: [{ chapter_id: "c1", number: 1, complete: true }, { chapter_id: "c2", number: 2, complete: true }],
      streaks: [], lengths: { median_chars: null, items: [], enough: false }, streak_min_chapters: 3, length_min_chapters: 3,
    },
    threads: { current_number: 2, open_count: 2, payoff_missed: 1, cold: 0, items: [{ thread_id: "t1", thread: "旧信是谁寄的", alert: "payoff_missed", opened_in: 1, last_touched_in: 1, planned_payoff_in: 2 }] },
    edge_kind_labels: { question: "问句" },
    ...overrides,
  };
}

const mounted = [];
async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}
const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const button = (host, text) => [...host.querySelectorAll("button")].find((node) => node.textContent.includes(text));

beforeEach(() => { vi.clearAllMocks(); apiGet.mockResolvedValue(report()); });
afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
});

describe("成稿中心 · 跨章", () => {
  it("列出全书反复出现的写法；点一处进写作台那一场、停在同一条（signal_id）；忽略过的那一处标出来", async () => {
    const go = vi.fn();
    const host = await render(<ManuCrossCheck projectId="prj-main" chapters={CHAPTERS} go={go} />);
    await vi.waitFor(() => expect(host.textContent).toContain("「手指慢慢攥得发白」"), T);
    expect(apiGet).toHaveBeenCalledWith("/api/v1/projects/prj-main/cross-chapter-check");
    expect(host.textContent).toContain("全书已写 2 章 3 场、24,721 字");
    const items = host.querySelectorAll('[data-testid="cross-repeat"]');
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("2 次 · 2 场");
    expect(items[0].textContent).toContain("第 1 章第 1 场");
    expect(items[0].textContent).toContain("这一处已忽略");
    expect(items[1].textContent).toContain("参考作者也常这么写");
    await click(items[0].querySelectorAll('[data-testid="cross-repeat-go"]')[1]);
    expect(go).toHaveBeenCalledWith("writer", [
      { type: "ws:writer-scene", detail: "ch02s1" },
      { type: "ws:writer-posture", detail: { posture: "deep", signal_id: "repeat:abc" } },
    ]);
  });

  it("章与章：不到三章写完时说清楚；够了就列连着几章同样的收尾与篇幅异常的章", async () => {
    const host = await render(<ManuCrossCheck projectId="prj-main" chapters={CHAPTERS} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="cross-shapes"]')).not.toBeNull(), T);
    expect(host.querySelector('[data-testid="cross-shapes"]').textContent).toContain("要至少 3 章写完");

    apiGet.mockResolvedValue(report({
      shapes: {
        chapters: [1, 2, 3].map((n) => ({ chapter_id: `c${n}`, number: n, complete: true })),
        streaks: [{ edge: "ending", kind: "question", label: "问句", chapter_ids: ["c1", "c2", "c3"], chapter_numbers: [1, 2, 3] }],
        lengths: { median_chars: 9000, enough: true, items: [{ chapter_id: "c3", number: 3, chars: 21000, ratio: 2.33, kind: "long" }] },
        streak_min_chapters: 3, length_min_chapters: 3,
      },
    }));
    await click(host.querySelector('[data-testid="cross-reload"]'));
    await vi.waitFor(() => expect(host.querySelector('[data-testid="cross-streak"]')).not.toBeNull(), T);
    expect(host.querySelector('[data-testid="cross-streak"]').textContent).toContain("第 1 章、第 2 章、第 3 章连着 3 章都停在问句上");
    expect(host.querySelector('[data-testid="cross-length"]').textContent).toContain("2.33 倍，偏长");
  });

  it("线索：有提醒的线列出来，「去「记忆」页签」交给成稿中心切页签；读不到时给重试", async () => {
    const onOpenMemory = vi.fn();
    const host = await render(<ManuCrossCheck projectId="prj-main" chapters={CHAPTERS} go={vi.fn()} onOpenMemory={onOpenMemory} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="cross-threads"]')).not.toBeNull(), T);
    const threads = host.querySelector('[data-testid="cross-threads"]');
    expect(threads.textContent).toContain("该兑现了");
    expect(threads.textContent).toContain("计划在第 2 章兑现");
    await click(button(threads, "去「记忆」页签"));
    expect(onOpenMemory).toHaveBeenCalledTimes(1);

    apiGet.mockRejectedValueOnce(Object.assign(new Error("服务器没有回答"), { code: "NETWORK" }));
    const failing = await render(<ManuCrossCheck projectId="prj-other" chapters={CHAPTERS} go={vi.fn()} />);
    await vi.waitFor(() => expect(failing.textContent).toContain("服务器没有回答"), T);
    apiGet.mockResolvedValue(report({ repeats: [] }));
    await click(button(failing, "重试"));
    await vi.waitFor(() => expect(failing.textContent).toContain("没有在不同场里反复出现的写法"), T);
  });
});
