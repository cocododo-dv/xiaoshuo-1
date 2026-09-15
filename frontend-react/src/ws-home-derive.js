/* ==========================================================
   主页派生（纯函数，不依赖 store，可单测）
   2026-09-16：「流程」视图并入主页。整本书逐章所在阶段（进度脊）与
   场景计数从目录真相派生；旧流程页的「流程体检」（永远只有固定一条）、
   按「略过」触发的回流条目、阶段加权百分比都不再保留——进度只留字数口径。
   ========================================================== */

/* 章节状态词表与后端 CatalogService.CHAPTER_STATES 同源；数组顺序即图例顺序 */
const HM_CHAPTER_STATES = ["approved", "review", "draft", "writing", "planned", "todo"];
const HM_CHAPTER_STATE_LABELS = {
  approved: "定稿", review: "送审", draft: "草稿", writing: "在写", planned: "规划", todo: "待写",
};

/* 当前章：作者标记的 current 优先，其次第一章「在写」，最后回落到末章。
   主页焦点卡与进度脊的「前线」共用这一条规则，两处永远指向同一章。 */
function hmCurrentChapter(chapters) {
  const list = Array.isArray(chapters) ? chapters.filter(Boolean) : [];
  if (!list.length) return null;
  return list.find(c => c.current) || list.find(c => c.state === "writing") || list[list.length - 1];
}

/* 目录 → 进度脊模型：
   segments  每章一段（n / title / state / front / sid），sid 指向该章在写的场景（否则第一场），供深链进写作房间
   counts    各章节状态的章数（图例）
   scenes    全书场景计数（已规划 / 完成 / 在写 / 待写，以及铺了场的章数）
   未知的章节状态归入 planned；未知的场景状态归入 todo。 */
function hmDeriveSpine(chapters) {
  const list = Array.isArray(chapters) ? chapters.filter(Boolean) : [];
  const cur = hmCurrentChapter(list);
  const counts = {};
  HM_CHAPTER_STATES.forEach(k => { counts[k] = 0; });
  const scenes = { total: 0, done: 0, writing: 0, todo: 0, chapters: 0 };
  const segments = list.map(c => {
    const state = HM_CHAPTER_STATE_LABELS[c.state] ? c.state : "planned";
    counts[state] += 1;
    const rows = Array.isArray(c.scenes) ? c.scenes.filter(Boolean) : [];
    if (rows.length) scenes.chapters += 1;
    rows.forEach(s => {
      scenes.total += 1;
      if (s.state === "done") scenes.done += 1;
      else if (s.state === "writing") scenes.writing += 1;
      else scenes.todo += 1;
    });
    const target = rows.find(s => s.state === "writing") || rows[0] || null;
    return {
      n: c.n,
      title: c.title || "",
      state,
      front: c === cur,
      sid: target && target.sid ? target.sid : "",
    };
  });
  return { total: list.length, segments, counts, scenes, front: cur ? cur.n : null };
}

export { HM_CHAPTER_STATES, HM_CHAPTER_STATE_LABELS, hmCurrentChapter, hmDeriveSpine };
