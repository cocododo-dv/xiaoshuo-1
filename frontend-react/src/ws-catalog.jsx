import React from "react";
import { WsWorks, wsKey } from "./ws-works.jsx";
import { apiDelete, apiGet, apiPatch, apiPost } from "./lib/client.js";
import { createSubscribers, storeAlert, useStoreTick } from "./lib/store-utils.js";

/* global window, React */
/* ==========================================================
   WsCatalog — 章节 / 场景单一真相源（per-work）
   ----------------------------------------------------------
   过去章节表在 home / writer / author / manuscripts / palette
   各存一份且互相矛盾。这一层把「全书结构」收敛成一个 store：
     · 数据形状：GMC 场景、戏剧卡、字数预算、张力值
     · 持久化键沿用作者台已有的 wsKey("arr.chapters")，
       老用户在编排台做过的编辑直接成为全局真相
     · 新建作品 → 空（由写作器引导建第一章），一切章节真相来自后端
     · 字数回写：写作器保存正文时按增量更新 场景字数 →
       章节字数 → 作品总字数 / 今日字数（WsWorks）
   场景 id（sid）在首次载入时按 chId + "s" + 序号 确定性补齐，
   与写作器历史上使用的 ch08s3 等 id 兼容。
   ========================================================== */

const CAT_LS = "arr.chapters.v2";            // v2：目录收敛后的新键；旧键由旧版编排台自动写入的陈旧种子，不再读取
const CAT_DAY_LS = "ws_words_today_v1";     // 每日写作字数 { d, n }
const CAT_STREAK_LS = "ws_streak_v1";       // 连续写作天数 { last, streak }，按作品

const catKey = (base) => (wsKey ? wsKey(base) : base);
const catActiveId = () => { try { return WsWorks ? WsWorks.activeId() : null; } catch (e) { return null; } };


/* ---- 确定性补齐场景 sid：chId + "s" + 序号（兼容写作器历史 id）---- */
function catStamp(list) {
  return (list || []).map((c) => {
    const used = new Set((c.scenes || []).map(s => s.sid).filter(Boolean));
    let i = 0;
    return {
      ...c,
      scenes: (c.scenes || []).map((s) => {
        if (s.sid) return s;
        i += 1; let sid = c.id + "s" + i;
        while (used.has(sid)) { i += 1; sid = c.id + "s" + i; }
        used.add(sid);
        return { ...s, sid };
      }),
    };
  });
}

function catSeedFor(workId) {
  return []; // 目录真相来自后端；本地不再内置任何种子章节
}

/* 汇总同步进 WsWorks（切换器 / 主页进度同源）。
   FE-ALIGN P2（D2）：字数/今日/streak 改读后端 writing-stats（只读派生，
   经 WsWorks.__applyDerived 注入，WsWorks.update 的回写路径已删除）；
   chaptersWritten 在目录统一（P3）前仍取本地目录 rollup。 */
function catPushTotals() {
  if (!WsWorks) return;
  const id = catActiveId();
  if (!id || id === "__loading__") return; // 启动占位作品（列表尚未从后端返回）
  const chs = catLoad(id);
  const written = chs.filter(c => ((c.words && c.words.cur) || 0) > 0).length;
  apiGet(`/api/v2/projects/${id}/writing-stats`).then((stats) => {
    if (!stats || !WsWorks.__applyDerived) return;
    const w = WsWorks.list().find(x => x.id === id);
    const next = {
      wordsTotal: stats.words_total || 0,
      wordsToday: stats.words_today || 0,
      streak: stats.streak_days || 0,
      chaptersWritten: written,
    };
    /* 仅在值变化时注入 —— __applyDerived 会广播 ws:work-changed，
       而本函数又监听该事件，无守卫会形成异步自激循环 */
    if (w && (w.wordsTotal !== next.wordsTotal || w.wordsToday !== next.wordsToday
      || w.streak !== next.streak || w.chaptersWritten !== next.chaptersWritten)) {
      WsWorks.__applyDerived(id, next);
    }
  }).catch(() => {
    /* 后端不可达：保持现值（缓存影子），不再做本地回写 */
  });
}

/* ---- store（FE-ALIGN Phase 3：目录真相源 = /api/v2/projects/{id}/catalog）----
   · get() 保持同步：每作品一份内存缓存，启动/切换作品时从 API 填充，
     写后乐观更新 + 端点调用，失败整体重拉（服务端为准）+ 提示。
   · 视图章节/场景形状与原型一致；C4 裁决：反应场景的 RDD 映射进
     goal/obstacle/turn 槽位，附 kindFields 标签元数据。
   · set() 写穿点改为 diff 拆解：字段变化→PATCH，新增→POST，删除→v1 trash，
     排序→v1 scene-order（复用既有逻辑，不另起排序端点）。
   · 一次性迁移：旧 localStorage 目录编辑（arr.chapters.v2::<id>）在后端目录
     为空时经 POST catalog/import 上行，打 ws_catalog_migrated_v1::<id> 标记。 */

const KIND_FIELDS_GCS = ["目标", "阻碍", "挫折"];
const KIND_FIELDS_RDD = ["反应", "两难", "决定"];
const CAT_MIGRATED_LS = "ws_catalog_migrated_v1";

function catFromApiScene(s) {
  const reactive = s.kind === "reactive";
  const b = s.brief || {};
  return {
    sid: s.slug,
    backendId: s.scene_id,
    title: s.title,
    kind: reactive ? "反应" : "主动",
    state: s.state,
    words: s.words || 0,
    goal: reactive ? (b.reaction || "") : (b.goal || ""),
    obstacle: reactive ? (b.dilemma || "") : (b.conflict || ""),
    turn: reactive ? (b.decision || "") : (b.setback || ""),
    povName: s.pov_character_name || "",
    povId: s.pov_character_id || "",
    kindFields: reactive ? KIND_FIELDS_RDD : KIND_FIELDS_GCS,
    // 阶段 D：最近一次准定稿评审的场景三问（坩埚可辨 / 三拍落地 / Yes-No-Maybe），无评审则 null
    storyCheck: s.story_check || null,
  };
}

function catFromApiChapter(c) {
  return {
    id: c.slug,
    backendId: c.chapter_id,
    act: c.act || "act1",
    n: c.no,
    title: c.title,
    state: c.state,
    tension: typeof c.tension === "number" ? c.tension : 0.3,
    pov: c.pov || "",
    time: c.time_label || "",
    place: c.place || "",
    current: !!c.current,
    words: { cur: (c.words && c.words.cur) || 0, target: (c.words && c.words.target) || 0 },
    entry: c.entry || "",
    exit: c.exit || "",
    align: c.align !== false,
    promise: c.promise || "",
    drama: { promise: "", spine: "", arc: "", problem: "", aftertaste: "", ending: "", forbidden: "", notes: "", ...(c.drama || {}) },
    threads: c.threads || [],
    scenes: (c.scenes || []).map(catFromApiScene),
  };
}

/* 章对象 diff → PATCH 载荷（只含变化字段） */
function catChapterPatch(prev, next) {
  const patch = {};
  if (next.title !== prev.title) patch.title = next.title;
  if (next.state !== prev.state) patch.state = next.state;
  const prevTarget = (prev.words && prev.words.target) || 0;
  const nextTarget = (next.words && next.words.target) || 0;
  if (nextTarget !== prevTarget) patch.words_target = nextTarget || null;
  if (next.act !== prev.act) patch.act = next.act;
  if (next.tension !== prev.tension) patch.tension = next.tension;
  if (next.pov !== prev.pov) patch.pov = next.pov;
  if (next.time !== prev.time) patch.time_label = next.time;
  if (next.place !== prev.place) patch.place = next.place;
  if (next.entry !== prev.entry) patch.entry = next.entry;
  if (next.exit !== prev.exit) patch.exit = next.exit;
  if (next.align !== prev.align) patch.align = next.align;
  if (next.promise !== prev.promise) patch.promise = next.promise;
  if (JSON.stringify(next.drama || {}) !== JSON.stringify(prev.drama || {})) patch.drama = next.drama || {};
  if (JSON.stringify(next.threads || []) !== JSON.stringify(prev.threads || [])) patch.threads = next.threads || [];
  if (next.current && !prev.current) patch.current = true;
  return patch;
}

function catScenePatch(prev, next) {
  const patch = {};
  if (next.title !== prev.title) patch.title = next.title;
  if (next.state !== prev.state) patch.state = next.state;
  // POV：FE 只跟「名字」打交道，后端按名 find-or-create 角色并回填 id（放在换型早退之前，避免同时改型丢 pov）
  if ((next.povName || "") !== (prev.povName || "")) patch.pov_character_name = next.povName || "";
  const reactive = next.kind === "反应";
  if (next.kind !== prev.kind) {
    patch.kind = reactive ? "reactive" : "proactive";
    // 换型时把三个槽位整体写到新键
    patch.brief = reactive
      ? { reaction: next.goal || "", dilemma: next.obstacle || "", decision: next.turn || "" }
      : { goal: next.goal || "", conflict: next.obstacle || "", setback: next.turn || "" };
    return patch;
  }
  const brief = {};
  if (next.goal !== prev.goal) brief[reactive ? "reaction" : "goal"] = next.goal || "";
  if (next.obstacle !== prev.obstacle) brief[reactive ? "dilemma" : "conflict"] = next.obstacle || "";
  if (next.turn !== prev.turn) brief[reactive ? "decision" : "setback"] = next.turn || "";
  if (Object.keys(brief).length) patch.brief = brief;
  return patch;
}

function catSceneCreateBody(s, at) {
  const reactive = s.kind === "反应";
  return {
    title: s.title,
    kind: reactive ? "reactive" : "proactive",
    at,
    state: s.state === "active" ? "writing" : (s.state || "todo"),
    brief: reactive
      ? { reaction: s.goal || "", dilemma: s.obstacle || "", decision: s.turn || "" }
      : { goal: s.goal || "", conflict: s.obstacle || "", setback: s.turn || "" },
  };
}

/* ---- 缓存与装载 ---- */
const CAT_EMPTY = Object.freeze([]);
const catCache = {};       // workId → view chapters
const catReadyMap = {};    // workId → API 已返回
const catErrorMap = {};    // workId → 最近一次装载错误；区分“真空目录”和“请求失败”
const catSubs = createSubscribers();
const catPendingCreates = {}; // slug/sid → 创建中的 Promise（后端 id 待回填）

function catNotify() {
  catSubs.notify();
  try { window.dispatchEvent(new CustomEvent("ws:catalog-changed")); } catch (e) {}
}
const catApiBase = (id) => `/api/v2/projects/${id}/catalog`;

/* React 订阅者会把返回值放进 effect 依赖。未装载时若每次都创建新 []，
   任何“收到目录后同步本地视图”的 effect 都会 setState → render → 新 [] →
   再 setState，最终触发 Maximum update depth。空快照必须保持引用稳定。 */
function catLoad(workId) { return catCache[workId] || CAT_EMPTY; }

const catFetching = {};
function catFetch(workId, options) {
  const migrate = !options || options.migrate !== false;
  if (!workId || workId === "__loading__") return Promise.resolve();
  if (catFetching[workId]) return catFetching[workId];
  delete catErrorMap[workId];
  catFetching[workId] = (async () => {
    try {
      let data = await apiGet(catApiBase(workId));
      let chapters = (data && data.chapters) || [];
      if (migrate && !chapters.length) {
        const imported = await catMigrateLegacy(workId);
        if (imported) {
          data = await apiGet(catApiBase(workId));
          chapters = (data && data.chapters) || [];
        }
      }
      catCache[workId] = chapters.map(catFromApiChapter);
      catReadyMap[workId] = true;
      delete catErrorMap[workId];
      catNotify();
      catPushTotals();
      try { window.WrDocs && window.WrDocs.hydrateActive && window.WrDocs.hydrateActive(); } catch (e) {}
    } catch (e) {
      catErrorMap[workId] = e instanceof Error ? e : new Error("章节目录加载失败");
      console.warn("[WsCatalog] 拉取目录失败:", e);
      catNotify();
    } finally {
      delete catFetching[workId];
    }
  })();
  // 新作品开始装载时立即通知订阅者清掉上一部作品的场景选择，避免跨作品串稿。
  catNotify();
  return catFetching[workId];
}

/* 旧 localStorage 目录编辑 → 一次性上行（仅后端目录为空时；import 端点 loopback 免 token） */
async function catMigrateLegacy(workId) {
  try {
    if (localStorage.getItem(CAT_MIGRATED_LS + "::" + workId)) return false;
    const raw = localStorage.getItem(CAT_LS + "::" + workId);
    const parsed = raw ? JSON.parse(raw) : null;
    if (!Array.isArray(parsed) || !parsed.length) {
      localStorage.setItem(CAT_MIGRATED_LS + "::" + workId, new Date().toISOString());
      return false;
    }
    await apiPost(catApiBase(workId) + "/import", { chapters: parsed });
    localStorage.setItem(CAT_MIGRATED_LS + "::" + workId, new Date().toISOString());
    return true;
  } catch (e) {
    console.warn("[WsCatalog] 旧目录迁移失败（保留旧键，下次再试）:", e);
    return false;
  }
}

/* 写失败统一恢复：以服务端为准整体重拉 + 提示 */
function catRecover(error) {
  storeAlert(error, "目录保存失败，已恢复为服务端版本。");
  catFetch(catActiveId(), { migrate: false });
}

/* 后端 id 解析（含等待乐观创建完成） */
async function catBackendChapterId(chId) {
  const find = () => { const c = catLoad(catActiveId()).find(x => x.id === chId); return c && c.backendId; };
  let id = find();
  if (!id && catPendingCreates[chId]) { await catPendingCreates[chId]; id = find(); }
  return id;
}
async function catBackendSceneId(sid) {
  const lookup = () => {
    for (const c of catLoad(catActiveId())) {
      const s = (c.scenes || []).find(x => x.sid === sid);
      if (s) return { scene: s, chapter: c };
    }
    return null;
  };
  let hit = lookup();
  if (hit && !hit.scene.backendId && catPendingCreates[hit.chapter.id]) {
    await catPendingCreates[hit.chapter.id];
    hit = lookup();
  }
  if (hit && !hit.scene.backendId && catPendingCreates[sid]) {
    await catPendingCreates[sid];
    hit = lookup();
  }
  return hit && hit.scene.backendId;
}

function catCreateChapterViaApi(workId, nc) {
  const p = (async () => {
    const result = await apiPost(`${catApiBase(workId)}/chapters`, {
      title: nc.title,
      state: nc.state === "active" ? "writing" : nc.state,
      current: !!nc.current,
      words_target: (nc.words && nc.words.target) || null,
      act: nc.act,
      tension: nc.tension,
      pov: nc.pov,
      time_label: nc.time,
      place: nc.place,
      entry: nc.entry,
      exit: nc.exit,
      align: nc.align,
      promise: nc.promise,
      drama: nc.drama || {},
      threads: nc.threads || [],
      with_scene: false,
    });
    const created = result && result.chapter;
    if (!created) return;
    const mine = catLoad(workId).find(c => c.id === nc.id);
    if (mine) mine.backendId = created.chapter_id;
    for (let i = 0; i < (nc.scenes || []).length; i++) {
      const s = nc.scenes[i];
      const sres = await apiPost(
        `${catApiBase(workId)}/chapters/${created.chapter_id}/scenes`,
        catSceneCreateBody(s, i)
      );
      const mineScene = mine && (mine.scenes || []).find(x => x.sid === s.sid);
      if (mineScene && sres && sres.scene) mineScene.backendId = sres.scene.scene_id;
    }
  })();
  catPendingCreates[nc.id] = p;
  p.finally(() => { delete catPendingCreates[nc.id]; });
  return p;
}

function catCreateSceneViaApi(workId, chId, s, at) {
  const p = (async () => {
    const chapterId = await catBackendChapterId(chId);
    if (!chapterId) return;
    const res = await apiPost(`${catApiBase(workId)}/chapters/${chapterId}/scenes`, catSceneCreateBody(s, at));
    const chapter = catLoad(workId).find(x => x.id === chId);
    const mine = chapter && (chapter.scenes || []).find(x => x.sid === s.sid);
    if (mine && res && res.scene) mine.backendId = res.scene.scene_id;
  })();
  catPendingCreates[s.sid] = p;
  p.finally(() => { delete catPendingCreates[s.sid]; });
  return p;
}

/* 批量软删：后端逐条判定，把过不去的项放进 blocked 而不是抛错
   （已批准终稿、章下已有单删场景…）。静默丢弃会让作者以为删掉了、刷新后又冒出来，
   所以这里把 blocked 翻成异常，交由 catRecover 提示 + 以服务端为准重拉。 */
async function catTrash(path, body) {
  const res = await apiPost(path, body);
  const blocked = (res && res.blocked) || [];
  if (blocked.length) {
    const seen = [];
    blocked.forEach((b) => {
      const msg = (b && (b.message || b.code)) || "未说明原因";
      if (!seen.includes(msg)) seen.push(msg);
    });
    throw new Error(`有 ${blocked.length} 项未能删除：${seen.slice(0, 3).join("；")}${seen.length > 3 ? "…" : ""}`);
  }
  return res;
}

/* set() 写穿点的 diff 拆解（视图层零修改的关键）：乐观缓存已先行，
   这里按差异派发端点调用；任何一步失败 → 整体重拉恢复。
   删除先于其它操作、且章/场各合并成一次批量调用：批量删 20 章不再打 20 个请求，
   场景删除也必须早于 scene-order（后端要求顺序集合覆盖章内全部在册场景）。 */
async function catDispatchDiff(workId, prev, next) {
  const ops = [];
  const prevById = Object.fromEntries(prev.map(c => [c.id, c]));
  const nextIds = new Set(next.map(c => c.id));
  const trashChapterIds = [];
  const trashSceneIds = [];
  for (const c of prev) {
    if (!nextIds.has(c.id) && c.backendId) trashChapterIds.push(c.backendId);
  }
  for (const nc of next) {
    const pc = prevById[nc.id];
    if (!pc) continue;
    const nextSids = new Set((nc.scenes || []).map(s => s.sid));
    for (const s of pc.scenes || []) {
      if (!nextSids.has(s.sid) && s.backendId) trashSceneIds.push(s.backendId);
    }
  }
  if (trashChapterIds.length) ops.push(() => catTrash("/api/v1/chapters/trash", { chapter_ids: trashChapterIds }));
  if (trashSceneIds.length) ops.push(() => catTrash("/api/v1/scenes/trash", { scene_ids: trashSceneIds }));
  for (const nc of next) {
    const pc = prevById[nc.id];
    if (!pc) {
      ops.push(() => catCreateChapterViaApi(workId, nc));
      continue;
    }
    const patch = catChapterPatch(pc, nc);
    if (Object.keys(patch).length) {
      ops.push(async () => {
        const chapterId = await catBackendChapterId(nc.id);
        if (chapterId) await apiPatch(`${catApiBase(workId)}/chapters/${chapterId}`, patch);
      });
    }
    const prevScenes = pc.scenes || [];
    const nextScenes = nc.scenes || [];
    const prevBySid = Object.fromEntries(prevScenes.map(s => [s.sid, s]));
    const nextSids = new Set(nextScenes.map(s => s.sid));
    nextScenes.forEach((s, index) => {
      const ps = prevBySid[s.sid];
      if (!ps) {
        ops.push(() => catCreateSceneViaApi(workId, nc.id, s, index));
        return;
      }
      const scenePatch = catScenePatch(ps, s);
      if (Object.keys(scenePatch).length) {
        ops.push(async () => {
          const sceneId = await catBackendSceneId(s.sid);
          if (sceneId) await apiPatch(`${catApiBase(workId)}/scenes/${sceneId}`, scenePatch);
        });
      }
    });
    const prevOrder = prevScenes.map(s => s.sid).filter(sid => nextSids.has(sid));
    const nextOrder = nextScenes.map(s => s.sid).filter(sid => prevBySid[sid]);
    if (prevOrder.join("|") !== nextOrder.join("|")) {
      ops.push(async () => {
        const chapterId = await catBackendChapterId(nc.id);
        if (!chapterId) return;
        const sceneIds = [];
        for (const s of nextScenes) {
          const sceneId = await catBackendSceneId(s.sid);
          if (sceneId) sceneIds.push(sceneId);
        }
        if (sceneIds.length) {
          await apiPost(`/api/v1/chapters/${chapterId}/scene-order`, {
            scene_ids: sceneIds,
            last_scene_id: sceneIds[sceneIds.length - 1],
          });
        }
      });
    }
  }
  /* 章节拖拽过去只改了内存顺序，刷新即还原。顺序也是目录真相的一部分：
     所有增删/重排操作完成后，再用完整的服务端 chapter_id 集合一次性落 display_order。
     任何 id 无法解析都 fail-closed，避免把半份顺序写进后端。
     纯删除不改变存活章的相对次序，这里按「存活章」比对，免得每次删除都追发一次
     等价于现状的 chapter-order（后端要求提交全量在册集合）。 */
  const prevChapterOrder = prev.map((c) => c.id).filter((id) => nextIds.has(id));
  const nextChapterOrder = next.map((c) => c.id);
  if (next.length && prevChapterOrder.join("|") !== nextChapterOrder.join("|")) {
    ops.push(async () => {
      const chapterIds = [];
      for (const chapter of next) {
        const chapterId = await catBackendChapterId(chapter.id);
        if (!chapterId) throw new Error(`章节「${chapter.title || chapter.id}」尚未完成创建，顺序未保存。`);
        chapterIds.push(chapterId);
      }
      await apiPost(`${catApiBase(workId)}/chapter-order`, { chapter_ids: chapterIds });
    });
  }
  if (!ops.length) return;
  try {
    for (const op of ops) await op();
    catFetch(workId, { migrate: false }); // 以服务端编号/rollup 收敛
    try { window.dispatchEvent(new CustomEvent("ws:trash-changed")); } catch (e) {}
  } catch (e) {
    catRecover(e);
  }
}

const WsCatalog = {
  get() { return catLoad(catActiveId()); },
  /* 目录是否已从后端装载（短暂为空数组时视图可区分 loading / 真空目录） */
  ready() { return !!catReadyMap[catActiveId()]; },
  loadError() { return catErrorMap[catActiveId()] || null; },
  set(next) {
    const id = catActiveId();
    if (!catReadyMap[id]) {
      storeAlert(null, "章节目录尚未从服务端加载完成，本次修改未提交。请先重试加载目录。");
      if (!catFetching[id]) catFetch(id, { migrate: false });
      return false;
    }
    const prev = catLoad(id);
    catCache[id] = catStamp(Array.isArray(next) ? next : []);
    catNotify();
    catDispatchDiff(id, prev, catCache[id]);
    return true;
  },
  /* 重置 = 丢弃本地缓存、以服务端为准重拉 */
  reset() {
    const id = catActiveId();
    delete catCache[id];
    catReadyMap[id] = false;
    delete catErrorMap[id];
    catFetch(id, { migrate: false });
    catNotify();
    return this.get();
  },
  /* —— 查找 —— */
  sceneById(sid) {
    for (const c of this.get()) { const s = (c.scenes || []).find(x => x.sid === sid); if (s) return { chapter: c, scene: s, index: c.scenes.indexOf(s) }; }
    return null;
  },
  currentChapter() {
    const chs = this.get();
    return chs.find(c => c.current) || chs.find(c => c.state === "writing") || chs[chs.length - 1] || null;
  },
  writingScene() {
    const chs = this.get();
    const cur = this.currentChapter();
    if (cur) {
      const s = (cur.scenes || []).find(x => x.state === "writing");
      if (s) return { chapter: cur, scene: s, index: cur.scenes.indexOf(s) };
    }
    for (const c of chs) { const s = (c.scenes || []).find(x => x.state === "writing"); if (s) return { chapter: c, scene: s, index: c.scenes.indexOf(s) }; }
    if (cur && cur.scenes && cur.scenes.length) return { chapter: cur, scene: cur.scenes[0], index: 0 };
    return null;
  },
  /* —— 写作器结构操作（经 set() 的 diff 引擎落端点）—— */
  renameScene(chId, sid, title) {
    this.set(this.get().map(c => c.id !== chId ? c : { ...c, scenes: c.scenes.map(s => s.sid === sid ? { ...s, title } : s) }));
  },
  moveScene(chId, from, to) {
    this.set(this.get().map(c => {
      if (c.id !== chId) return c;
      const scenes = c.scenes.slice(); const [m] = scenes.splice(from, 1); scenes.splice(to, 0, m);
      return { ...c, scenes };
    }));
  },
  addScene(chId, title) {
    this.set(this.get().map(c => c.id !== chId ? c : {
      ...c, scenes: [...c.scenes, { title: title || "新场景", kind: "主动", state: "todo", goal: "（本场目标待规划）", obstacle: "", turn: "" }],
    }));
  },
  removeScene(chId, sid) {
    this.set(this.get().map(c => c.id !== chId ? c : { ...c, scenes: c.scenes.filter(s => s.sid !== sid) }));
  },
  /* —— 批量删除（编排台 / 写作台大纲的多选）——
     一次 set() 出一份新目录，diff 引擎把它压成单次批量 trash 调用；
     章与场同批时，被删章下的场不再单独进场景桶（后端会随章一起软删，
     而「章下已有单独回收的场景」恰好是章删除的阻断条件）。 */
  removeChapters(ids) {
    const drop = new Set(ids || []);
    if (!drop.size) return false;
    return this.set(this.get().filter(c => !drop.has(c.id)));
  },
  removeScenes(sids) {
    const drop = new Set(sids || []);
    if (!drop.size) return false;
    return this.set(this.get().map(c => ({ ...c, scenes: (c.scenes || []).filter(s => !drop.has(s.sid)) })));
  },
  /* 章 + 场混选：先摘掉整章，再从存活章里摘场 */
  removeMixed(chapterIds, sids) {
    const dropCh = new Set(chapterIds || []);
    const dropSc = new Set(sids || []);
    if (!dropCh.size && !dropSc.size) return false;
    return this.set(this.get()
      .filter(c => !dropCh.has(c.id))
      .map(c => (dropSc.size ? { ...c, scenes: (c.scenes || []).filter(s => !dropSc.has(s.sid)) } : c)));
  },
  addChapter(title) {
    const chs = this.get();
    const n = String(chs.length + 1).padStart(2, "0");
    const id = "ch" + n + (chs.some(c => c.id === "ch" + n) ? "-" + Date.now().toString(36) : "");
    const ch = {
      id, act: "act1", n, title: (title || "").trim() || `第 ${chs.length + 1} 章`, state: "writing",
      tension: 0.3, pov: "", time: "", place: "", current: true,
      words: { cur: 0, target: 4000 },
      entry: "", exit: "", align: true, promise: "",
      drama: { promise: "", spine: "", arc: "", problem: "", aftertaste: "", ending: "", forbidden: "", notes: "" },
      threads: [],
      scenes: [{ title: "开场", kind: "主动", state: "writing", goal: "（本场目标待规划）", obstacle: "", turn: "" }],
    };
    this.set([...chs.map(c => ({ ...c, current: false })), ch]);
    return this.get().find(c => c.id === id);
  },
  /* 雪花构思 → 目录只有一条路径：分章面板确认 → SnowSync.materialize（后端物化 +
     批准大纲）。这里曾有个 adoptOutline 包装和一个 __adoptByDiff 降级实现 —— P2 之前
     它们按闸门状态在三条产出完全不同的落库路径之间分叉（后端物化全书落一章 / 前端脊柱
     锚点 / 只建空壳章），雪花做得越完整反而掉进越差的那条。分章算法搬到后端、作者在预览
     面板里确认之后，两者都没有了调用方，留着只会让人以为还有第二条路。 */
  /* —— 字数（写作器自动保存时调用）：本地即时更新；
     权威 rollup 由正文保存响应经 __applyWordsRollup 注入，统计走服务端 —— */
  recordSceneWords(sid, count) {
    const hit = this.sceneById(sid);
    if (!hit) return;
    const delta = count - (hit.scene.words || 0);
    if (!delta) return;
    const id = catActiveId();
    catCache[id] = this.get().map(c => c.id !== hit.chapter.id ? c : {
      ...c,
      words: { ...c.words, cur: Math.max(0, ((c.words && c.words.cur) || 0) + delta) },
      scenes: c.scenes.map(s => s.sid === sid ? { ...s, words: count } : s),
    });
    catNotify();
  },
  totals() {
    const chs = this.get();
    const words = chs.reduce((s, c) => s + ((c.words && c.words.cur) || 0), 0);
    const active = WsWorks ? WsWorks.active() : null;
    return {
      words,
      written: chs.filter(c => ((c.words && c.words.cur) || 0) > 0).length,
      planned: chs.length,
      approved: chs.filter(c => c.state === "approved").length,
      today: (active && active.wordsToday) || 0,
    };
  },
  subscribe(fn) { return catSubs.subscribe(fn); },
  /* —— FE-ALIGN 内部接缝（非契约面）—— */
  __backendSceneId: catBackendSceneId,
  __applyWordsRollup(sid, rollup) {
    if (!rollup) return;
    const hit = this.sceneById(sid);
    const id = catActiveId();
    if (hit) {
      catCache[id] = this.get().map(c => c.id !== hit.chapter.id ? c : {
        ...c,
        words: { ...c.words, cur: rollup.chapter_words },
        scenes: c.scenes.map(s => s.sid === sid ? { ...s, words: rollup.scene_words } : s),
      });
      catNotify();
    }
    catPushTotals();
  },
  __refresh(workId) { return catFetch(workId || catActiveId(), { migrate: false }); },
};

/* hook：订阅目录 + 作品切换 */
function useCatalogChapters() {
  useStoreTick((bump) => {
    const un = WsCatalog.subscribe(bump);
    window.addEventListener("ws:work-changed", bump);
    return () => { un(); window.removeEventListener("ws:work-changed", bump); };
  });
  return WsCatalog.get();
}

/* 启动 & 切换作品：装载目录 + 同步统计（进度同源） */
if (window.__wsCatalogGlobalHandlers) {
  const old = window.__wsCatalogGlobalHandlers;
  window.removeEventListener("ws:work-changed", old.catalogWorkChanged);
  window.removeEventListener("ws:work-changed", old.trashWorkChanged);
  window.removeEventListener("ws:trash-changed", old.trashChanged);
}
try { catFetch(catActiveId()); } catch (e) {}
try { catPushTotals(); } catch (e) {}
const catOnWorkChanged = () => {
  try { catFetch(catActiveId()); } catch (e) {}
  try { catPushTotals(); } catch (e) {}
};
window.addEventListener("ws:work-changed", catOnWorkChanged);


/* ==========================================================
   WsTrashStore — 回收站（FE-ALIGN Phase 4：接真后端统一三级列表）
   GET /api/v2/trash?project_id=… = 全局作品桶 + 当前作品的章/场景桶；
   软删端点自动产生条目，push() 退化为「触发刷新」的兼容壳。
   restore/purge 走对应端点；失败由 store 自行提示并以服务端为准刷新。
   ========================================================== */
const trashSubs = createSubscribers();
let trashCache = [];
let trashFetching = null;
const TRASH_KIND_LABEL = { work: "作品", chapter: "章节", scene: "场景" };

function trashNotify() { trashSubs.notify(); }

function trashAdapt(item) {
  return {
    id: item.id,
    kind: TRASH_KIND_LABEL[item.kind] || item.kind || "内容",
    title: item.kind === "work" ? `《${item.title}》· 整部` : item.title,
    removedAt: item.removed_at ? (Date.parse(item.removed_at) || Date.now()) : Date.now(),
    restorable: item.restorable !== false,
    payload: { type: item.kind },
  };
}

function trashFetch() {
  if (trashFetching) return trashFetching;
  const id = catActiveId();
  const qs = id && id !== "__loading__" ? `?project_id=${encodeURIComponent(id)}` : "";
  trashFetching = apiGet(`/api/v2/trash${qs}`).then((data) => {
    trashCache = ((data && data.items) || []).map(trashAdapt);
    trashNotify();
  }).catch((e) => {
    console.warn("[WsTrashStore] 拉取回收站失败:", e);
  }).finally(() => { trashFetching = null; });
  return trashFetching;
}

const WsTrashStore = {
  list() { return trashCache; },
  /* 兼容壳：各软删端点已自动产生后端条目，这里只触发刷新（旧调用点无害化） */
  push(item) {
    trashFetch();
    return { id: "pending", removedAt: Date.now(), ...(item || {}) };
  },
  restore(id) {
    apiPost(`/api/v2/trash/${encodeURIComponent(id)}/restore`, {}).then(() => {
      trashFetch();
      if (String(id).startsWith("work:")) {
        if (WsWorks && WsWorks.__refresh) WsWorks.__refresh();
      } else {
        catFetch(catActiveId(), { migrate: false });
      }
    }).catch((e) => {
      storeAlert(e, "恢复失败。");
      trashFetch();
    });
    return true; // 乐观返回；失败走上面的独立提示
  },
  purge(id) {
    apiDelete(`/api/v2/trash/${encodeURIComponent(id)}`).then(() => {
      trashFetch();
    }).catch((e) => {
      storeAlert(e, "永久删除失败。");
      trashFetch();
    });
  },
  async clear() {
    const failures = [];
    // 子项先删、作品最后删，避免清除作品时级联删除子项后，后续请求误报 404。
    const rank = { scene: 0, chapter: 1, work: 2 };
    const items = trashCache.slice().sort((a, b) => (
      (rank[a.payload && a.payload.type] ?? 3) - (rank[b.payload && b.payload.type] ?? 3)
    ));
    for (const it of items) {
      try {
        await apiDelete(`/api/v2/trash/${encodeURIComponent(it.id)}`);
      } catch (e) {
        failures.push({ item: it, error: e });
      }
    }
    await trashFetch();
    if (failures.length) {
      const first = failures[0].error;
      const reason = first && first.message ? `：${first.message}` : "";
      try { window.alert(`回收站未能完全清空，${failures.length} 条仍需重试${reason}`); } catch (e) {}
      return false;
    }
    return true;
  },
  subscribe(fn) { return trashSubs.subscribe(fn); },
};

try { trashFetch(); } catch (e) {}
const trashOnWorkChanged = () => { try { trashFetch(); } catch (e) {} };
window.addEventListener("ws:work-changed", trashOnWorkChanged);
/* 软删端点完成后的精确刷新信号（WsWorks.remove / 目录删除成功时 dispatch） */
const trashOnChanged = () => { try { trashFetch(); } catch (e) {} };
window.addEventListener("ws:trash-changed", trashOnChanged);
window.__wsCatalogGlobalHandlers = {
  catalogWorkChanged: catOnWorkChanged,
  trashWorkChanged: trashOnWorkChanged,
  trashChanged: trashOnChanged,
};

Object.assign(window, { WsCatalog, useCatalogChapters, WsTrashStore });

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { WsCatalog, useCatalogChapters, WsTrashStore };
