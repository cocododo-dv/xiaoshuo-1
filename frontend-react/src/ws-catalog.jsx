import { WsWorks } from "./ws-works.jsx";
import { apiGet } from "./lib/client.js";
import { createSubscribers, storeAlert, useStoreTick } from "./lib/store-utils.js";
import { createKeyedLoader } from "./lib/store-kit.js";
import { catFromApiChapter, catNormalizeAct } from "./ws-catalog-adapt.js";
import { createCatalogWriter } from "./ws-catalog-diff.js";
import { catalogCurrentChapter, catalogFocusScene } from "./ws-catalog-focus.js";
import { WsTrashStore, onTrashRestored } from "./ws-trash-store.js";
import { adoptModuleListeners, emit, retireModuleListeners } from "./lib/events.js";
import { isRealWorkId } from "./lib/work-id.js";
import { readyWorkId } from "./lib/ready-work.js";

/* ==========================================================
   WsCatalog — 章节 / 场景单一真相源（per-work）
   ----------------------------------------------------------
   目录真相 = GET /api/v2/projects/{id}/catalog；这里是它的同步内存缓存：
     · 启动 / 换作品时装载，写入走乐观缓存 + set() 的差异派发（ws-catalog-diff.js），失败以服务端为准重拉；
     · 场景 sid = 后端 slug（阶段 X 起就是稳定的 scene_id）；乐观新建的场先用临时 sid，重拉后经别名找回；
     · 字数回写：正文保存响应的 rollup → 场景 / 章字数与书架统计（WsWorks）。
   形状映射在 ws-catalog-adapt.js，「该写哪一场」的规则在 ws-catalog-focus.js，回收站在 ws-trash-store.js
   （这里照旧转出 WsTrashStore）。
   ========================================================== */

/* 能发请求的当前作品（加载占位 / 空书架 / 新建作品还没拿到正式 id 时是 null） */
const catActiveId = () => readyWorkId(WsWorks);


/* ---- 给乐观创建、还没有后端 id 的场补一个**临时** sid ----
   后端建好之后目录重拉，这一场拿到稳定的 scene_id；临时 sid 经 catTrackAliases 仍解析到同一场。
   临时 sid 刻意不长成位置式旧 slug（ch02s1）的样子：那个形状留给旧深链的兜底解析，两者不能撞名。 */
let catTempSeq = 0;
function catStamp(list) {
  return (list || []).map((c) => ({
    ...c,
    scenes: (c.scenes || []).map((s) => {
      if (s.sid) return s;
      catTempSeq += 1;
      return { ...s, sid: `tmp_${c.id}_${Date.now().toString(36)}_${catTempSeq}` };
    }),
  }));
}

/* 汇总同步进 WsWorks（切换器 / 主页进度同源）。
   FE-ALIGN P2（D2）：字数/今日/streak 改读后端 writing-stats（只读派生，
   经 WsWorks.__applyDerived 注入，WsWorks.update 的回写路径已删除）；
   chaptersWritten 在目录统一（P3）前仍取本地目录 rollup。 */
function catPushTotals() {
  if (!WsWorks) return;
  const id = catActiveId();
  if (!isRealWorkId(id)) return; // 启动占位作品（列表尚未从后端返回）
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
    /* 仅在值变化时注入 —— __applyDerived 会通知 WsWorks 的订阅者，
       值没变也注入只会让主页、切换器白白重渲一轮 */
    if (w && (w.wordsTotal !== next.wordsTotal || w.wordsToday !== next.wordsToday
      || w.streak !== next.streak || w.chaptersWritten !== next.chaptersWritten)) {
      WsWorks.__applyDerived(id, next);
    }
  }).catch(() => {
    /* 后端不可达：保持现值（缓存影子），不再做本地回写 */
  });
}

/* 正文每次自动保存都会回写 rollup：它已经带着 words_total（直接用），但今日字数 / 连续天数还得
   GET writing-stats。过去每存一次就发一次（审计 F01-07）；现在同一段写作里合并成停笔后的一次。
   rollup 带上 words_today / streak_days 时（后端补上这两个字段之后）连这一次也省掉。 */
const CAT_TOTALS_SETTLE_MS = 15_000;
let catTotalsTimer = null;
let catTotalsLastAt = 0;
/* 节流（首尾都发）：连续写作时至多每 15 秒问一次，停笔后再补最后一次——纯尾部防抖会让今日字数在
   不停笔时一直不动（复核 Q1-R2） */
function catPushTotalsSoon() {
  const now = Date.now();
  if (now - catTotalsLastAt >= CAT_TOTALS_SETTLE_MS && !catTotalsTimer) {
    catTotalsLastAt = now;
    catPushTotals();
    return;
  }
  if (catTotalsTimer) return;
  const wait = Math.max(0, CAT_TOTALS_SETTLE_MS - (now - catTotalsLastAt));
  catTotalsTimer = setTimeout(() => { catTotalsTimer = null; catTotalsLastAt = Date.now(); catPushTotals(); }, wait);
}

/* rollup 里现成的统计直接注入书架（值没变就不注入，免得主页、切换器白白重渲）；
   返回 true = 今日 / 连续天数也齐了，不必再问 writing-stats */
function catApplyRollupStats(rollup) {
  const id = catActiveId();
  if (!isRealWorkId(id) || !rollup) return false;
  const next = {};
  if (typeof rollup.words_total === "number") next.wordsTotal = rollup.words_total;
  if (typeof rollup.words_today === "number") next.wordsToday = rollup.words_today;
  if (typeof rollup.streak_days === "number") next.streak = rollup.streak_days;
  next.chaptersWritten = catLoad(id).filter(c => ((c.words && c.words.cur) || 0) > 0).length;
  try {
    const w = WsWorks.list().find(x => x.id === id);
    if (w && Object.keys(next).some(key => w[key] !== next[key])) WsWorks.__applyDerived(id, next);
  } catch (e) { return false; }
  return "wordsToday" in next && "streak" in next;
}

/* ---- store（FE-ALIGN Phase 3：目录真相源 = /api/v2/projects/{id}/catalog）----
   · get() 保持同步：每作品一份内存缓存，启动/切换作品时从 API 填充，
     写后乐观更新 + 端点调用，失败整体重拉（服务端为准）+ 提示。
   · 视图章节/场景形状与原型一致；C4 裁决：反应场景的 RDD 映射进
     goal/obstacle/turn 槽位，附 kindFields 标签元数据。
   · set() 写穿点改为 diff 拆解：字段变化→PATCH，新增→POST，删除→v1 trash，
     排序→v1 scene-order（复用既有逻辑，不另起排序端点）。
   · 6 月原型时期的旧本机目录（arr.chapters.v2::<id>）不再上行（批准 #25，重评 R16）：那一次性迁移和它
     调用的 POST catalog/import 都已删除，浏览器里的旧键原样留着、不再读。 */

/* ---- 缓存与装载 ---- */
const CAT_EMPTY = Object.freeze([]);
const catCache = {};       // workId → view chapters
const catReadyMap = {};    // workId → API 已返回
const catErrorMap = {};    // workId → 最近一次装载错误；区分“真空目录”和“请求失败”
const catSubs = createSubscribers();

const catAliasMap = {};       // workId → { 旧 sid（乐观创建时的临时 sid）: 现在的 sid }
const CAT_SID_MIGRATED_LS = "ws_sid_migrated_v1";
/* 按场景落地的本机键前缀（写作台读缓存 / 未同步标记 / 场景笔记，AI 起草台运行记录）与两份 sid 名单 */
const CAT_SID_KEY_PREFIXES = ["wr-doc:", "wr-doc-pending:", "wr-notes:", "wr-notes-pending:", "scn-run:"];
const CAT_SID_LIST_KEYS = ["scn-queue:v1", "scn-queue-dismissed:v1"];

/* 反向依赖的登记口（审计 F01-02）：写作台正文 store 与雪花同步层都 import 目录，目录不能反过来 import 它们
   （会闭成环），过去就去读 window.WrDocs / window.SnowSync。现在由它们在加载时来这里登记：
   · loaded：每次目录装载成功后调用（fn(workId)）——正文 store 据此预热当前在写那一场；
   · planTitlesSynced：台子上改的章名被后端写穿到章计划后、目录重拉之前 await（fn(workId)）——雪花缓存接章表。 */
const catLoadedHooks = new Set();
const catPlanTitleHooks = new Set();
function catRegister(set, fn) {
  if (typeof fn !== "function") return () => {};
  set.add(fn);
  return () => { set.delete(fn); };
}

function catNotify() {
  catSubs.notify();
  emit("ws:catalog-changed");
}

/* sid 解析：直接命中 → 会话内别名（乐观创建的临时 sid，建好之后后端给的是稳定 id）→ 位置式旧 slug
   （待办卡 / 旧深链里存下来的 ch08s3；语义与从前一样——「现在排在那个位置上的场」）。 */
function catResolveScene(workId, sid) {
  if (!sid) return null;
  const chapters = catLoad(workId);
  const find = (pred) => {
    for (const c of chapters) {
      const s = (c.scenes || []).find(pred);
      if (s) return { chapter: c, scene: s, index: c.scenes.indexOf(s) };
    }
    return null;
  };
  const direct = find(x => x.sid === sid);
  if (direct) return direct;
  const alias = (catAliasMap[workId] || {})[sid];
  if (alias) { const hit = find(x => x.sid === alias); if (hit) return hit; }
  return find(x => x.legacySid && x.legacySid === sid);
}

/* 目录重拉后：上一份缓存里同一个后端场景换了 sid（只会是乐观创建的临时 sid）→ 记别名，
   手里还攥着临时 sid 的视图（刚建完章就开始写的写作台）继续找得到这一场。 */
function catTrackAliases(workId, prev, next) {
  const bySceneId = {};
  next.forEach(c => (c.scenes || []).forEach(s => { if (s.backendId) bySceneId[s.backendId] = s.sid; }));
  const map = catAliasMap[workId] || (catAliasMap[workId] = {});
  (prev || []).forEach(c => (c.scenes || []).forEach(s => {
    const now = s.backendId && bySceneId[s.backendId];
    if (now && now !== s.sid) map[s.sid] = now;
  }));
}

/* 一次性迁移（每部作品一次）：场景 sid 从位置式（ch08s3）换成稳定的 scene_id 之后，把按旧 sid 落地的
   本机键挪到新 sid 上——映射取此刻的目录位置，与升级前「下次打开时会读到的那一场」完全一致，不引入新错位。 */
function catMigrateSidKeys(workId, chapters) {
  try {
    const marker = CAT_SID_MIGRATED_LS + "::" + workId;
    if (localStorage.getItem(marker)) return;
    const map = {};
    chapters.forEach(c => (c.scenes || []).forEach(s => { if (s.legacySid && s.legacySid !== s.sid) map[s.legacySid] = s.sid; }));
    if (!Object.keys(map).length) return; // 旧后端（slug 仍是位置式）：没有可迁的，也不打标记
    Object.keys(map).forEach((legacy) => {
      CAT_SID_KEY_PREFIXES.forEach((prefix) => {
        const from = `${prefix}${legacy}::${workId}`;
        const to = `${prefix}${map[legacy]}::${workId}`;
        const value = localStorage.getItem(from);
        if (value == null) return;
        if (localStorage.getItem(to) == null) localStorage.setItem(to, value);
        localStorage.removeItem(from);
      });
    });
    CAT_SID_LIST_KEYS.forEach((base) => {
      const key = `${base}::${workId}`;
      const raw = localStorage.getItem(key);
      if (!raw) return;
      try {
        const list = JSON.parse(raw);
        if (Array.isArray(list)) localStorage.setItem(key, JSON.stringify([...new Set(list.map(x => map[x] || x))]));
      } catch (e) {}
    });
    localStorage.setItem(marker, new Date().toISOString());
  } catch (e) {
    console.warn("[WsCatalog] 本机场景键迁移失败（下次再试）:", e);
  }
}
const catApiBase = (id) => `/api/v2/projects/${id}/catalog`;

/* React 订阅者会把返回值放进 effect 依赖。未装载时若每次都创建新 []，
   任何“收到目录后同步本地视图”的 effect 都会 setState → render → 新 [] →
   再 setState，最终触发 Maximum update depth。空快照必须保持引用稳定。 */
function catLoad(workId) { return catCache[workId] || CAT_EMPTY; }

/* 读取器（lib/store-kit）：按作品合并在飞请求；本机写入进行中或写入之前发出的读取回来时不写缓存——
   否则写后补读会并进写入之前那一次，回来的是写入前的服务端状态，刚改的标题在屏上退回去（审计 F01-05）。 */
const catLoader = createKeyedLoader({
  async fetch(workId) {
    const data = await apiGet(catApiBase(workId));
    return ((data && data.chapters) || []).map(catFromApiChapter);
  },
  apply(workId, mapped) {
    // 2026-09-19 的场景编号迁移（位置式 sid → 稳定的 scene_id）照计划再留一轮（重评 R16）
    catMigrateSidKeys(workId, mapped);
    catTrackAliases(workId, catCache[workId], mapped);
    // 新建一场的回包丢了（建好了、回包没回来）：这一次重读里认出它，临时 sid 记成它的别名（复核 W1-R7B-1）
    catWriter.reconcileCreates(workId, mapped).forEach(([from, to]) => {
      (catAliasMap[workId] || (catAliasMap[workId] = {}))[from] = to;
    });
    catCache[workId] = mapped;
    catReadyMap[workId] = true;
    delete catErrorMap[workId];
    catNotify();
    catPushTotals();
    catLoadedHooks.forEach((fn) => { try { fn(workId); } catch (e) {} });
  },
  onError(workId, e) {
    catErrorMap[workId] = e instanceof Error ? e : new Error("章节目录加载失败");
    console.warn("[WsCatalog] 拉取目录失败:", e);
    catNotify();
  },
});

/* 装载（在飞就复用）。新作品开始装载时立即通知订阅者清掉上一部作品的场景选择，避免跨作品串稿。 */
function catFetch(workId) {
  if (!isRealWorkId(workId)) return Promise.resolve(false);
  const fresh = !catLoader.inflight(workId);
  if (fresh) delete catErrorMap[workId];
  const run = catLoader.load(workId);
  if (fresh) catNotify();
  return run;
}

/* 以服务端为准重读：在飞的那一次（可能早于服务端刚发生的变化）作废，结束后恰好再读一次 */
function catRefetch(workId) {
  if (!isRealWorkId(workId)) return Promise.resolve(false);
  return catLoader.invalidate(workId);
}

/* 写失败统一提示；以服务端为准的重拉由 catLoader.write 收尾时统一做 */
function catRecover(error) {
  storeAlert(error, "目录保存失败，已恢复为服务端版本。");
}

/* 写入引擎（ws-catalog-diff.js）：set() 的差异 → 端点调用；缓存与读取器从这里注入 */
const catWriter = createCatalogWriter({
  catLoad, catActiveId, catResolveScene, catApiBase, catRecover, catPlanTitleHooks,
  catWrite: (workId, run) => catLoader.write(workId, run),
});
const catBackendSceneId = catWriter.backendSceneId;
const catDispatchDiff = catWriter.dispatchDiff;
let catNewChapterSeq = 0;   // 同一毫秒里连建两章（双击「新建章节」）时临时 id 也不重名

const WsCatalog = {
  get() { return catLoad(catActiveId()); },
  /* 目录是否已从后端装载（短暂为空数组时视图可区分 loading / 真空目录） */
  ready() { return !!catReadyMap[catActiveId()]; },
  loadError() { return catErrorMap[catActiveId()] || null; },
  set(next) {
    const id = catActiveId();
    if (!catReadyMap[id]) {
      storeAlert(null, "章节目录尚未从服务端加载完成，本次修改未提交。请先重试加载目录。");
      catFetch(id);
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
    catRefetch(id);
    catNotify();
    return this.get();
  },
  /* —— 查找 —— */
  sceneById(sid) { return catResolveScene(catActiveId(), sid); },
  /* 后端场景 id → 现在的 sid（待办卡 / 同步状态按后端 id 说话） */
  sidForBackendId(sceneId) {
    if (!sceneId) return "";
    for (const c of this.get()) { const s = (c.scenes || []).find(x => x.backendId === sceneId); if (s) return s.sid; }
    return "";
  },
  currentChapter() { return catalogCurrentChapter(this.get()); },
  /* 「现在该写哪一场」——规则见 ws-catalog-focus.js（主页、写作台、AI 起草台共用；后端 focus_scene_payload 是它的镜像） */
  focusScene() { return catalogFocusScene(this.get()); },
  writingScene() { return this.focusScene(); },
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
  /* 新场景的三拍是空的，和 addChapter 那一场空白场同一份配方：没人写过的目标不替作者编。
     旧版在这里写进「（本场目标待规划）」这句占位，落库后后端和各处视图都得再把它认作「没填」；
     场景设计卡本来就把空拍显示成「（待规划）」。 */
  addScene(chId, title) {
    this.set(this.get().map(c => c.id !== chId ? c : {
      ...c, scenes: [...c.scenes, { title: title || "新场景", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" }],
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
  /* 新建一章——全应用只有这一份配方（章节编排的页头 / 卷尾 / 序列栏 / 空目录、写作台的「创建第一章」都走这里）。
     addChapter({ title, act, afterId })：
       · afterId：接在这一章后面，默认沿用它的卷；
       · 只给 act：接在这一卷的最后一章后面（这一卷还空着就按卷序插在前一卷之后）；
       · 都不给（写作台的 addChapter()）：接在全书最后，沿用最后一章的卷；
       · 旧调用 addChapter("章名") 仍然有效。
     不带张力 / 线索 / 时间 / 地点 / 入口出口 / 章承诺，字数目标为空，戏剧卡是空的，只有一场空白的场——
     没人填过的东西不替作者编：默认张力会让「故事弧线」和体检的张力项死灰复燃，4000 字的目标会让进度一下变成 100%。
     返回新章（乐观缓存里的那一份）。 */
  addChapter(opts) {
    const o = typeof opts === "string" ? { title: opts } : (opts || {});
    const chs = this.get();
    const after = o.afterId ? chs.find(c => c.id === o.afterId) : null;
    const actOrder = ["act1", "act2", "act3"];
    const act = catNormalizeAct(o.act || (after && after.act) || (chs.length ? chs[chs.length - 1].act : "act1"));
    let insertAt = chs.length;
    if (after) insertAt = chs.indexOf(after) + 1;
    else if (o.act) {
      const rank = actOrder.indexOf(act);
      const lastSame = chs.map(c => c.act).lastIndexOf(act);
      if (lastSame >= 0) insertAt = lastSame + 1;
      else {
        const lastEarlier = chs.reduce((at, c, i) => (actOrder.indexOf(c.act) < rank ? i : at), -1);
        insertAt = lastEarlier + 1;
      }
    }
    /* 已批准终稿的章在目录里的位置是锁死的（后端 chapter-order 会 409）：新章插在它前面会把它往后挤一格，
       所以最早只能插在最后一章已批准终稿之后 */
    const lastApproved = chs.map(c => c.state).lastIndexOf("approved");
    insertAt = Math.max(insertAt, lastApproved + 1);
    const first = chs.length === 0;
    const n = String(insertAt + 1).padStart(2, "0");
    /* 接在书尾时 id 就是后端会给的位置式 slug（chNN），写作台刚建完就在写也对得上；插在中间时不能占用
       后面那一章现在的 slug，先给一个临时 id，目录重拉之后章节编排按位置找回它 */
    const id = insertAt === chs.length && !chs.some(c => c.id === "ch" + n) ? "ch" + n : `ch-new-${Date.now().toString(36)}-${++catNewChapterSeq}`;
    const ch = {
      id, act, n, title: String(o.title || "").trim() || `第 ${insertAt + 1} 章`,
      state: first ? "writing" : "planned", current: first,
      words: { cur: 0, target: 0 },
      drama: { promise: "", spine: "", arc: "", problem: "", aftertaste: "", ending: "", forbidden: "", notes: "" },
      scenes: [{ title: "新场景", kind: "主动", state: first ? "writing" : "todo", goal: "", obstacle: "", turn: "" }],
    };
    const next = chs.slice();
    next.splice(insertAt, 0, ch);
    this.set(next);
    return this.get().find(c => c.id === id) || null;
  },
  /* 雪花构思 → 目录只有一条路径：分章面板确认 → SnowSync.materialize（后端物化 +
     批准大纲）。这里曾有个 adoptOutline 包装和一个 __adoptByDiff 降级实现 —— P2 之前
     它们按闸门状态在三条产出完全不同的落库路径之间分叉（后端物化全书落一章 / 前端脊柱
     锚点 / 只建空壳章），雪花做得越完整反而掉进越差的那条。分章算法搬到后端、作者在预览
     面板里确认之后，两者都没有了调用方，留着只会让人以为还有第二条路。 */
  /* —— 字数（写作器自动保存时调用）：本地即时更新；
     权威 rollup 由正文保存响应经 applyWordsRollup 注入，统计走服务端 —— */
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
  /* 反向依赖的登记口（见 catLoadedHooks）：返回注销函数 */
  onLoaded(fn) { return catRegister(catLoadedHooks, fn); },
  onPlanTitlesSynced(fn) { return catRegister(catPlanTitleHooks, fn); },
  /* 以服务端为准重读一部作品的目录（省略 = 当前作品）：在飞的那一次作废，结束后恰好再读一次。
     服务端在别处改了目录（送审 / 批准、回流、章任务跑完、雪花物化）之后调它。 */
  refresh(workId) { return catRefetch(workId || catActiveId()); },
  /* 旧名：还有写作台 / AI 起草台 / 构思视图的几处在用（ws-writer-room、ws-chapter-run-state、ws-scene-api、
     ws-snow-editors-story），它们的包换成 refresh 之后删掉 */
  __refresh(workId) { return catRefetch(workId || catActiveId()); },
  /* 场景 sid → 后端 scene_id（async：乐观新建的场等它建好）；没有后端 id（还没同步到后端、不在目录里）是 undefined。
     写作台、AI 起草台、正文 store 按后端 id 发请求都经它（视图一侧的唯一入口是 ws-scene-id.js 的 sceneApiId） */
  backendSceneId: catBackendSceneId,
  /* 正文保存回包的 words_rollup → 这一场 / 这一章的字数与书架统计（服务端算的数为准） */
  applyWordsRollup(sid, rollup) {
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
    if (!catApplyRollupStats(rollup)) catPushTotalsSoon();
  },
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

/* 启动 & 切换作品：装载目录 + 同步统计（进度同源）。
   模块在 HMR / 测试 resetModules 后可能重新执行：先撤掉旧实例挂在 window 上的监听器（在文件末尾登记）。 */
retireModuleListeners("ws-catalog");
/* 统计由目录装载成功时推一次（catLoader.apply）；启动 / 换作品时不再在目录还空着的时候先推一次 */
try { catFetch(catActiveId()); } catch (e) {}
const catOnWorkChanged = () => {
  clearTimeout(catTotalsTimer); catTotalsTimer = null;
  try { catFetch(catActiveId()); } catch (e) {}
};
window.addEventListener("ws:work-changed", catOnWorkChanged);


adoptModuleListeners("ws-catalog", () => {
  window.removeEventListener("ws:work-changed", catOnWorkChanged);
  clearTimeout(catTotalsTimer);
});

/* 回收站恢复了章 / 场：目录以服务端为准重读（回收站模块不 import 目录，见 ws-trash-store.js） */
onTrashRestored(() => { catRefetch(catActiveId()); });

Object.assign(window, { WsCatalog, useCatalogChapters, WsTrashStore });

/* ESM 导出（Phase 1 机械追加；window.* 赋值过渡期保留） */
export { WsCatalog, useCatalogChapters, WsTrashStore };
