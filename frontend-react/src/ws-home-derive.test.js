// 主页派生纯函数单测（2026-09-16「流程」并入主页）：
// 进度脊必须只从目录真相派生——每章一段、状态计数、前线唯一且与焦点卡同源、
// 场景计数不再按旧占位符过滤、深链 sid 指向在写的场景。
import { describe, it, expect } from "vitest";
import { HM_CHAPTER_STATES, HM_CHAPTER_STATE_LABELS, hmCurrentChapter, hmDeriveSpine } from "./ws-home-derive.js";

const CH = (n, state, extra = {}) => ({ id: `ch${n}`, n: String(n).padStart(2, "0"), title: `第${n}章`, state, scenes: [], ...extra });

describe("hmCurrentChapter", () => {
  it("空目录返回 null", () => {
    expect(hmCurrentChapter([])).toBeNull();
    expect(hmCurrentChapter(null)).toBeNull();
  });

  it("current 标记优先于「在写」，其次「在写」，最后回落到末章", () => {
    const writing = CH(2, "writing");
    const current = CH(3, "draft", { current: true });
    expect(hmCurrentChapter([CH(1, "approved"), writing, current])).toBe(current);
    expect(hmCurrentChapter([CH(1, "approved"), writing, CH(3, "planned")])).toBe(writing);
    const last = CH(3, "planned");
    expect(hmCurrentChapter([CH(1, "approved"), CH(2, "review"), last])).toBe(last);
  });
});

describe("hmDeriveSpine", () => {
  it("图例词表与顺序覆盖后端全部章节状态", () => {
    expect(HM_CHAPTER_STATES).toEqual(["approved", "review", "draft", "writing", "planned", "todo"]);
    HM_CHAPTER_STATES.forEach(k => expect(HM_CHAPTER_STATE_LABELS[k]).toBeTruthy());
  });

  it("空目录：0 章、无分段、计数全零——绝不用目标章数或虚构章填充", () => {
    const spine = hmDeriveSpine([]);
    expect(spine.total).toBe(0);
    expect(spine.segments).toEqual([]);
    expect(Object.values(spine.counts).every(v => v === 0)).toBe(true);
    expect(spine.scenes).toEqual({ total: 0, done: 0, writing: 0, todo: 0, chapters: 0 });
    expect(spine.front).toBeNull();
  });

  it("每章一段：状态计数、未知状态归入 planned、前线只标当前章", () => {
    const chapters = [
      CH(1, "approved"),
      CH(2, "review"),
      CH(3, "writing", { current: true }),
      CH(4, "writing"),
      CH(5, "mystery"),
      CH(6, "todo"),
    ];
    const spine = hmDeriveSpine(chapters);
    expect(spine.total).toBe(6);
    expect(spine.segments.map(s => s.state)).toEqual(["approved", "review", "writing", "writing", "planned", "todo"]);
    expect(spine.counts).toEqual({ approved: 1, review: 1, draft: 0, writing: 2, planned: 1, todo: 1 });
    expect(spine.segments.filter(s => s.front).map(s => s.n)).toEqual(["03"]);
    expect(spine.front).toBe("03");
  });

  it("场景计数不按旧占位符过滤，未知场景状态归入待写；sid 指向在写的场景，否则第一场，没有场景则为空", () => {
    const chapters = [
      CH(1, "approved", { scenes: [{ sid: "ch01s1", state: "done" }, { sid: "ch01s2", state: "done" }] }),
      CH(2, "writing", { current: true, scenes: [{ sid: "ch02s1", state: "done" }, { sid: "ch02s2", state: "writing" }, { sid: "ch02s3", state: "todo" }] }),
      CH(3, "planned", { scenes: [{ sid: "ch03s1", state: "todo", title: "待规划场景", goal: "—" }, { sid: "ch03s2", state: "weird" }] }),
      CH(4, "planned"),
    ];
    const spine = hmDeriveSpine(chapters);
    expect(spine.scenes).toEqual({ total: 7, done: 3, writing: 1, todo: 3, chapters: 3 });
    expect(spine.segments.map(s => s.sid)).toEqual(["ch01s1", "ch02s2", "ch03s1", ""]);
  });
});
