import { apiPatch, apiPost } from "./lib/client.js";
import { hasAuthorText, sanitizeManuscriptHTML } from "./manuscript-html.js";
import { WsDiagnosis } from "./ws-diagnosis-summary.jsx";
import { wsNotify } from "./ws-notify.jsx";
import { adoptModuleListeners, emit, retireModuleListeners } from "./lib/events.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { activeWorkId, recoveryCreate, recoveryList } from "./wr-recovery-store.js";
import {
  cacheWrite, docText, pendingClear, pendingRead, pendingWrite, readCache, sameManuscriptText, toDocHTML,
} from "./wr-doc-cache.js";

/* ==========================================================
   WrDocs — 写作器正文文档 store（FE-ALIGN Phase 3；2026-09-30 W1 起是一台保存状态机）
   ----------------------------------------------------------
   正文真相源 = author-drafts 主路径（POST ensure + PATCH /{draft_id}），字数统计与目录 rollup 由保存响应回流
   （words_rollup）。localStorage 的 wr-doc:<sid> 键是「同步读缓存」（本机这一层在 wr-doc-cache.js）：写作器在
   render / effect 里同步取文档，API 负责水合与持久化。sid = 目录 slug（阶段 X 起就是稳定的 scene_id）；后端 scene_id 经 WsCatalog 映射。

   保存按场（作品 id::sid）各一台状态机，住在模块里——换视图、写作台重新挂载都还在：
   · save(sid, html)：调用之内就把正文写进本机缓存 + 未同步标记（网络怎样都先在本机落地，刷新也丢不了），
     然后要么发出去，要么替换排队的那一稿：排队只留最新的一份，更早排队的被它取代。
   · 同一场同一时刻只有一次 PATCH 在路上。它回来——
       存上了：有排队的，就带着新的修订号接着发；
       失败（断网 / 5xx）：正文留在本机、仍是未同步，等下一次保存或显式 flush 再发；再发的永远是最新的一稿，
         绝不把较旧的一稿补发到较新的后面。这一稿也许其实存上了（回包丢了）：记下它（unsure）；
       409（服务端在别处改过）：进入冲突。撞上 409 的这一次和上一次没等到回包的用的是同一个修订号时，先核对
         （checking）：服务端眼下正好是那一稿、修订号只往前走了一步，那就是自己存上的——接上那个修订号，把最新的一稿
         接着发，不开冲突、不提示；不是就进入冲突。核对期间不发任何请求。
   · 冲突：先把本机所有没上服务端的正文（撞上 409 的那一稿、排队的那一稿，去重；同步与恢复里已有一模一样的也不再放）
     放进「同步与恢复」（放不进本机存储时只留在本次会话里，并给出醒目的提示和入口），扔掉排队的，标上 conflictPending，
     再读服务端版本。读到之前，任何保存都拒绝（AUTHOR_DRAFT_CONFLICT）——绝不在作者没见过的修订号上保存；
     这期间存进来的字只进本机缓存，换掉读缓存之前一并放进同步与恢复。读服务端版本用的是冲突之后新发的 ensure
     （冲突之前发出、还没回来的那一次先等它落地，不拿它的回包），回包的修订号比撞上 409 的那一次还旧就当没读到。
     读到了：读缓存换成服务端版本，清掉 conflictPending 和 lastSaveError，订阅者收到 conflict-resolved
     （带服务端正文，写作台据此换掉编辑器里的字）；读不到：保持冲突，状态是保存失败，下一次保存 / 窗口重新
     聚焦或联网 / 退避计时到了再读。
   · 规则：冲突时编辑器里一律是服务端版本，本机的字都在「同步与恢复」（作者拍板 #20c 的更严格做法：
     另一台设备的版本从不被覆盖，本机的版本也都留着）。
   · 水合之前就有保存排着（作者在旧的读缓存上写了字）：服务端版本和作者写时编辑器里那份的文字（连同格式）不同，
     同样按冲突处理。服务端是新建的空稿（修订号 1）时本机那份就是工作稿；更高修订号上的空稿是在别处清空的，照常算服务端版本。
   · 已水合、没有本机改动的一场，每次 load 都在后台再问一次服务端（POST ensure 幂等）：修订号往前走了就换读缓存、通知 loaded。
   · 采纳归档（acceptCanonical）：服务端在一个事务里存下并提升了采纳的那一稿。还在路上的那一次保存就此作废
     （superseded）：它回来时——不管失败、409 还是别的——不补发、不停着、不开冲突、不提示；排队 / 停着 / 冲突中的
     本机稿不再发，和采纳的不一样又不在同步与恢复里的先留一份。
   · replace(sid, html)（同步与恢复的「恢复」「重试同步」）：和 save 一样在调用之内落本机缓存并排进保存，同一个调用里
     就通知写作台换稿——编辑器、本机缓存和之后要同步的始终是同一稿，PATCH 失败时也是。
   · 同一浏览器开两个标签页：两份 store 共用 localStorage（读缓存、未同步标记、恢复记录），各有各的内存状态。
     每个标签页记着自己编辑器眼下在哪一份正文上（shown：load 交出去的、通知过的、编辑器交回来的），水合 / 复核拿服务端
     版本和它比，不和共用的读缓存比——另一个标签页先存了，这一页照样收到 loaded（没写字时换稿）或走冲突（写了字时）。
     保证的是——服务端按修订号拒绝后到的那一次（409），那个标签页走冲突副本、换成服务端版本，谁也盖不掉谁；
     冲突副本和恢复记录各有自己的键，两个标签页都看得到。不保证的是——同一场的读缓存键只有一个，后写的标签页
     会覆盖先写的；一个标签页断网时没同步上的字，若另一个标签页随后在同一场存过，就只剩在那个标签页的编辑器
     和内存里，关掉它之前要让它同步（或进同步与恢复）。
   订阅（WrDocs.subscribe，fn(kind, detail)）：
     "state" { sid, workId, …state() }；"loaded" { sid, workId, html, reason, …state() }（读缓存换成了服务端 / 恢复 / 采纳的正文）；
     "conflict-resolved" { sid, workId, html }（冲突之后读到了服务端版本）。
   ========================================================== */

/* 作品id::sid → 一场的状态。必须带作品前缀：同名 slug（ch01s1）在每部作品都存在，裸 sid 会把
   PATCH 打到上一部作品的 draft（跨作品数据污染）。 */
const docMeta = {};

function metaKeyOf(workId, sid) {
  return `${workId}::${sid}`;
}

function metaFor(workId, sid) {
  const key = metaKeyOf(workId, sid);
  return docMeta[key] || (docMeta[key] = {
    workId,
    sid,
    sceneId: null,            // 后端 scene_id（解析过一次就记下：作品换了以后目录里查不到这一场）
    draftId: null,
    revision: 0,
    serverContent: "",
    currentFinalSceneRowId: null,
    lastPromotedRevisionNo: null,
    lastPromotedFinalSceneRowId: null,
    canonicalDirty: true,
    hydrated: false,
    hydrating: null,          // 进行中的水合（后来的调用方等同一次）
    revalidating: null,
    preHydrateBase: undefined, // 水合之前的第一稿是在哪份正文上写的
    pendingAtLoad: false,
    shown: undefined,         // 这个标签页的编辑器眼下在哪一份正文上（见文件头「两个标签页」）
    dirty: false,             // 本机有服务端还没确认的正文（路上 / 排队 / 失败待重发 / 核对中 / 冲突中）
    saveVersion: 0,
    savedVersion: 0,
    savedAt: null,
    lastSaveData: null,
    inFlight: null,           // 路上的那一次 { html, version, base, superseded }；html 为 null 时还在准备（水合）
    queued: null,             // 下一次要发的那一稿 { html, version }，只留最新的
    stalled: false,           // 上一次失败后 queued 停着，等下一次保存或显式 flush
    unsure: null,             // 发出去却没等到回包的几稿 { base, htmls }：它们也许已经存上了
    checking: null,           // 409 之后核对撞上的是不是自己那一稿（见 startOwnCheck）
    conflict: null,           // 409 之后、服务端版本读到之前（见 openConflict）
    lastConflict: null,
    waiters: [],
    lastSaveError: null,
    cacheError: null,
    localDurable: true,
  });
}

function meta(sid) {
  return metaFor(activeWorkId(), sid);
}

function isActiveWork(m) {
  return m.workId === activeWorkId();
}

function finalIdFromRef(ref) {
  if (typeof ref !== "string" || !ref.startsWith("final_scene:")) return null;
  return ref.slice("final_scene:".length) || null;
}

function absorbServerState(m, data) {
  const draft = data && data.draft;
  if (draft) {
    if (draft.draft_id) m.draftId = draft.draft_id;
    if (Number.isInteger(draft.revision_no)) m.revision = draft.revision_no;
    if (Object.prototype.hasOwnProperty.call(draft, "content")) m.serverContent = sanitizeManuscriptHTML(draft.content || "");
    if (Object.prototype.hasOwnProperty.call(draft, "last_promoted_revision_no")) {
      m.lastPromotedRevisionNo = draft.last_promoted_revision_no;
    }
    if (Object.prototype.hasOwnProperty.call(draft, "last_promoted_final_scene_row_id")) {
      m.lastPromotedFinalSceneRowId = draft.last_promoted_final_scene_row_id;
    }
    if (typeof draft.canonical_dirty === "boolean") m.canonicalDirty = draft.canonical_dirty;
    else if (Number.isInteger(draft.revision_no)) m.canonicalDirty = draft.revision_no !== m.lastPromotedRevisionNo;
  }
  if (data && Object.prototype.hasOwnProperty.call(data, "runtime_final_ref")) {
    m.currentFinalSceneRowId = finalIdFromRef(data.runtime_final_ref);
  }
}

function snapshotOf(m) {
  return {
    draftId: m.draftId,
    revision: m.revision,
    dirty: m.dirty,
    canonicalDirty: m.canonicalDirty,
    currentFinalSceneRowId: m.currentFinalSceneRowId,
    lastPromotedRevisionNo: m.lastPromotedRevisionNo,
    lastPromotedFinalSceneRowId: m.lastPromotedFinalSceneRowId,
    lastSaveError: m.lastSaveError,
    cacheError: m.cacheError,
    localDurable: m.localDurable,
    conflictPending: !!m.conflict,
    saving: (!!m.inFlight && !m.inFlight.superseded) || (!!m.queued && !m.stalled) || !!m.checking,
    savedAt: m.savedAt,
  };
}

/* ---- 通知 ---- */

const docListeners = new Set();
function notifyDoc(kind, detail) {
  docListeners.forEach((fn) => { try { fn(kind, detail); } catch (e) { /* 订阅者出错不打断保存 */ } });
}

function notifyState(m) {
  notifyDoc("state", { sid: m.sid, workId: m.workId, ...snapshotOf(m) });
}

/* 读缓存换了一份正文：写作台随之换稿（或读到的就是作者正在写的底稿、接着写），这个标签页的编辑器从此在它上面 */
function notifyLoadedMeta(m, reason) {
  const html = readCache(m.workId, m.sid);
  m.shown = html;
  notifyDoc("loaded", { sid: m.sid, workId: m.workId, ...snapshotOf(m), html, reason });
}

/* 本地稿被放进「同步与恢复」时告诉作者一声，并给一个直接打开它的按钮（入口在左侧导航栏底部）。
   外壳的提示层没挂上时（单测里单独加载 store）退回浏览器提示框。 */
function recoveryNotice(m, message, tone = "warn") {
  wsNotify({
    message,
    tone,
    timeout: tone === "danger" ? 20000 : 12000,
    action: {
      label: "打开同步与恢复",
      onClick: () => { emit("ws:recovery-open", { sid: m.sid }); },
    },
  });
}

const NOTICE = {
  conflict: "这份正文在别处被修改过，已加载服务端的最新版本。你本地没保存上的内容放进了「同步与恢复」，可以比较差异、恢复或导出。",
  conflictNothingKept: "这份正文在别处被修改过，已加载服务端的最新版本。",
  conflictVolatile: "这份正文在别处被修改过，编辑器已换成服务端的最新版本。你本机没保存上的正文因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。",
  conflictLoadFailed: "这份正文在别处被修改过，但暂时读不到服务端的最新版本。你本机的正文已放进「同步与恢复」，编辑器里的字也还在；连上服务器后会自动加载服务端版本，在那之前这一场不再保存。",
  conflictLoadFailedVolatile: "这份正文在别处被修改过，但暂时读不到服务端的最新版本；你本机的正文因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里。连上服务器后会自动加载服务端版本，在那之前这一场不再保存——刷新或关掉页面前请打开「同步与恢复」导出。",
  pendingAtLoad: "上次会话有没保存到服务端的本地正文，已加载服务端版本。你的本地稿放进了「同步与恢复」，可以比较差异、恢复或导出。",
  pendingAtLoadVolatile: "上次会话有没保存到服务端的本地正文。浏览器存储空间不足，它只放进了本次会话的「同步与恢复」（本机缓存里也还留着一份，直到这一场再保存）；编辑器显示的是服务端版本。请打开「同步与恢复」导出，或清理旧记录。",
  server: "这一场在别处有更新，已加载服务端的最新版本。你刚才在旧版本上写的几句放进了「同步与恢复」，可以比较差异、恢复或导出。",
  replaced: "编辑器换成了新的正文，你刚才没保存的几句放进了「同步与恢复」，可以比较差异、恢复或导出。",
  volatile: "编辑器换成了新的正文。你刚才没保存的几句因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。",
};

/* 同步与恢复里这一场已经持久地留着的、与 html 一模一样（字、分段、格式）的那一份（采纳前的作者稿备份、上一次冲突副本……） */
function keptEntryOf(m, html) {
  const text = docText(html);
  return recoveryList().find((entry) => (
    entry.durable !== false && entry.sid === m.sid && entry.workId === m.workId && docText(entry.html) === text
  )) || null;
}

/* 把一段本机正文放进「同步与恢复」。没有字的不放，已经一模一样留着的不再放一份；
   返回记录（durable=false 表示只在本次会话里）或 null */
function keepCopy(m, html, reason, label = `场景 ${m.sid} · 冲突本地稿`) {
  if (!hasAuthorText(html)) return null;
  return keptEntryOf(m, html) || recoveryCreate({ sid: m.sid, workId: m.workId, html, type: "conflict", reason, label });
}

/* ---- 错误 ---- */

function unavailableError() {
  return Object.assign(new Error("场景尚未就绪，草稿未保存到服务端"), { code: "AUTHOR_DRAFT_UNAVAILABLE" });
}
function holdError() {
  return Object.assign(new Error("这份正文在别处被修改过，服务端的最新版本还没读下来；这一稿先留在本机，暂不保存"), { code: "AUTHOR_DRAFT_CONFLICT" });
}
function staleBaseError() {
  return Object.assign(new Error("这一场在别处有更新：刚才是在这台电脑较旧的缓存上写的"), { code: "AUTHOR_DRAFT_CONFLICT" });
}
function replacedError() {
  return Object.assign(new Error("这一场的正文刚被采纳归档替换；没存上的本机正文已放进「同步与恢复」"), { code: "AUTHOR_DRAFT_CONFLICT" });
}
function staleReadError() {
  return Object.assign(new Error("读到的服务端版本比撞上冲突的那一次还旧"), { code: "AUTHOR_DRAFT_STALE_READ" });
}

/* ---- 等待者：save() / flush() 等「这一稿（或更新的一稿）有了结果」 ----
   detailed 的等待者（replace 用）收到 { data, version, html }：带走它的那一次保存发的是哪一稿 */

function waitFor(m, version, detailed = false) {
  if (version <= m.savedVersion) {
    return Promise.resolve(detailed ? { data: m.lastSaveData, version: m.savedVersion, html: null } : m.lastSaveData);
  }
  const promise = new Promise((resolve, reject) => { m.waiters.push({ version, resolve, reject, detailed }); });
  promise.catch(() => {}); // 调用方不接失败时（离场冲刷、冒烟脚本）不算未处理的拒绝；接的照样收到
  return promise;
}

function resolveWaiters(m, version, data, html) {
  const rest = [];
  m.waiters.forEach((w) => {
    if (w.version <= version) w.resolve(w.detailed ? { data, version, html } : data);
    else rest.push(w);
  });
  m.waiters = rest;
}

function rejectWaiters(m, error, fromVersion = 0) {
  const rest = [];
  m.waiters.forEach((w) => { if (w.version > fromVersion) w.reject(error); else rest.push(w); });
  m.waiters = rest;
}

function outcomeOf(m, version) {
  return waitFor(m, version).then(
    () => "saved",
    (e) => (e && e.code === "AUTHOR_DRAFT_CONFLICT" ? "conflict" : "failed"),
  );
}

/* ---- 服务端草稿 ---- */

async function sceneIdOf(m) {
  if (m.sceneId) return m.sceneId;
  if (!isActiveWork(m)) return null; // 目录只认当前作品
  let sceneId = null;
  try { sceneId = await WsCatalog.__backendSceneId(m.sid); } catch (e) { sceneId = null; }
  if (sceneId) m.sceneId = sceneId;
  return sceneId || null;
}

// 作品id::sid → 正在进行的 ensure { seq, promise }。同一场同一时刻只发一次 POST ensure：版本列表、
// 某一版正文、draftId 常被同时调用（开发模式下 React 还会把挂载 effect 连跑两遍），
// 两个 ensure 带着同一个幂等键撞在一起，后一个会拿到 409 IDEMPOTENCY_REQUEST_IN_PROGRESS。
// 请求结束（成功或失败）即移除，之后的调用重新请求。seq 按发出的先后递增：冲突之后要的是「冲突之后发出的」那一次。
const ensureInflight = new Map();
let ensureSeq = 0;

/* POST ensure（幂等，返回服务端当前的草稿）。只取回包，吸收与否由调用方决定。目录里还没有这一场时返回 null。
   minSeq：只接受第 minSeq 次（含）之后发出的 ensure——更早发出、还在路上的那一次先等它落地（客户端会把同样的
   在飞请求并成一个，不等就还是拿到它的回包），再发一次新的。 */
function requestDraft(m, minSeq = 0) {
  const key = metaKeyOf(m.workId, m.sid);
  const pending = ensureInflight.get(key);
  if (pending && pending.seq >= minSeq) return pending.promise;
  if (pending) return pending.promise.then(() => {}, () => {}).then(() => requestDraft(m, minSeq));
  const seq = ++ensureSeq;
  const request = (async () => {
    const sceneId = await sceneIdOf(m);
    if (!sceneId) return null;
    return apiPost(`/api/v1/author-drafts/scene/${sceneId}/ensure`, {});
  })();
  const entry = { seq, promise: null };
  entry.promise = request.finally(() => {
    if (ensureInflight.get(key) === entry) ensureInflight.delete(key);
  });
  ensureInflight.set(key, entry);
  return entry.promise;
}

async function ensureDraftMeta(m) {
  if (m.draftId) return m;
  const data = await requestDraft(m);
  if (data && !m.draftId) {
    absorbServerState(m, data);
    notifyState(m);
  }
  return m;
}

/* ---- 水合 ---- */

/* 水合：把读缓存和服务端草稿对齐（一场一次；冲突中 = 再读一次服务端版本）。进行中的水合，后来的调用方等同一次；出错抛给调用方。 */
function hydrateMeta(m) {
  if (m.conflict) return conflictLoad(m).then(() => { if (m.conflict) throw m.lastSaveError; return m; });
  if (m.hydrated) return Promise.resolve(m);
  if (m.hydrating) return m.hydrating;
  const run = (async () => {
    await ensureDraftMeta(m);
    if (m.draftId == null || m.hydrated || m.conflict) return m; // 目录里还没有这一场的后端 id：下次再水合
    settleHydrate(m);
    return m;
  })();
  const shared = run.finally(() => { if (m.hydrating === shared) m.hydrating = null; });
  m.hydrating = shared;
  return shared;
}

/* 服务端草稿到手之后决定读缓存怎么办 */
function settleHydrate(m) {
  const serverHTML = toDocHTML(m.serverContent || "");
  // 修订号 1 的空稿是 ensure 刚建的：本机缓存里的字就是工作稿。更高修订号上的空稿是在别处清空的，照常算服务端版本
  const freshBlank = !serverHTML && !(m.revision > 1);
  const cached = readCache(m.workId, m.sid);
  const shown = m.shown === undefined ? cached : m.shown;
  const base = m.preHydrateBase === undefined ? shown : m.preHydrateBase;
  const pendingAtLoad = m.pendingAtLoad;
  m.preHydrateBase = undefined;
  m.pendingAtLoad = false;

  if (m.dirty) {
    // 水合之前就有保存排着：作者是在当时编辑器里那一份（base）上写的。服务端就是那一份（或是新建的空稿）——照常保存；
    // 服务端在别处改过——本机写的字按冲突处理，服务端版本上屏，绝不带着服务端的修订号把它盖掉。
    if (freshBlank || sameManuscriptText(base, serverHTML)) {
      m.hydrated = true;
      return;
    }
    const texts = [m.inFlight && m.inFlight.html, m.queued && m.queued.html];
    if (pendingAtLoad) texts.push(base);
    openConflict(m, staleBaseError(), texts, { serverKnown: true });
    return;
  }

  if (pendingRead(m) != null) {
    // 上个会话保存失败 / 没等到回包就关了页面：本机较新的稿不得被静默覆盖
    const localText = cached != null && hasAuthorText(cached);
    if (localText && freshBlank) {
      m.hydrated = true; // 服务端是新建的空稿：本机这份就是工作稿，未同步标记留着，下一次保存传上去
      return;
    }
    if (localText && !sameManuscriptText(cached, serverHTML)) {
      const entry = keepCopy(m, cached, "上次会话未同步，服务端已有不同版本", `场景 ${m.sid} · 未同步本地稿`);
      const durable = !entry || entry.durable !== false;
      showServerVersion(m, serverHTML, durable);
      m.hydrated = true;
      if (durable) pendingClear(m);
      notifyState(m);
      notifyLoadedMeta(m, "server");
      recoveryNotice(m, durable ? NOTICE.pendingAtLoad : NOTICE.pendingAtLoadVolatile, durable ? "warn" : "danger");
      return;
    }
    pendingClear(m); // 内容一致（上次实际保上了）/ 本机没有稿：静默消费标记
  }

  // 服务端版本和读缓存不同，或者和这个标签页编辑器里那一份不同（另一个标签页已经把读缓存换成了它）：换读缓存、通知 loaded
  if (!freshBlank && (serverHTML !== (cached || "") || serverHTML !== (shown || ""))) {
    if (serverHTML !== (cached || "")) {
      const written = cacheWrite(m, serverHTML);
      m.localDurable = written.ok;
      m.cacheError = written.error;
    }
    m.hydrated = true;
    notifyLoadedMeta(m, "server");
    return;
  }
  m.hydrated = true; // 与读缓存一致；或服务端新建的空稿（读缓存为空时视图显示开场占位，有本机稿时本机稿就是工作稿）
}

/* 服务端版本进读缓存。durable=false（本机稿没能持久留进同步与恢复）时只进会话内存：本机存储里那份本机稿和未同步标记留着。 */
function showServerVersion(m, serverHTML, durable) {
  const written = cacheWrite(m, serverHTML, { durable });
  m.localDurable = durable && written.ok;
  m.cacheError = durable
    ? written.error
    : Object.assign(new Error("本地恢复空间不足：服务端版本只在本次会话里，本机稿还留在本机缓存"), { code: "LOCAL_STORAGE_QUOTA" });
}

function isClean(m) {
  return m.hydrated && !m.dirty && !m.inFlight && !m.queued && !m.conflict && !m.checking;
}

/* 已水合、没有本机改动的一场：后台再问一次服务端（F03-24：在别的设备上改过的场，重新打开时不再一直显示旧缓存）。
   这期间作者开始写了就整个不吸收——保存带的还是旧修订号，服务端动过就 409，走冲突。 */
function revalidate(m) {
  if (!isClean(m) || m.revalidating || m.hydrating) return;
  const run = requestDraft(m).then((data) => {
    const draft = data && data.draft;
    if (!draft || !isClean(m)) return;
    const replaced = !!(draft.draft_id && m.draftId && draft.draft_id !== m.draftId);
    if (!replaced && (!Number.isInteger(draft.revision_no) || draft.revision_no < m.revision)) return;
    const moved = replaced || draft.revision_no > m.revision;
    absorbServerState(m, data);
    if (moved) {
      const serverHTML = toDocHTML(m.serverContent || "");
      const cached = readCache(m.workId, m.sid);
      const shown = m.shown === undefined ? cached : m.shown;
      if (serverHTML !== (cached || "") || serverHTML !== (shown || "")) {
        if (serverHTML !== (cached || "")) {
          const written = cacheWrite(m, serverHTML);
          m.localDurable = written.ok;
          m.cacheError = written.error;
        }
        notifyState(m);
        notifyLoadedMeta(m, "server");
        return;
      }
    }
    notifyState(m);
  }).catch(() => { /* 后台复核失败无妨：下次打开再问 */ }).finally(() => {
    if (m.revalidating === run) m.revalidating = null;
  });
  m.revalidating = run;
}

/* ---- 保存 ---- */

/* 本机缓存 + 未同步标记落地，然后发出去或替换排队的那一稿。返回这一稿的结果（detailed 见 waitFor） */
function saveMeta(m, html, detailed = false) {
  if (!m.hydrated && !m.dirty && m.preHydrateBase === undefined) {
    // 水合之前的第一稿：记下作者是在编辑器里哪一份正文上写的（水合时与服务端版本比，文字不同就按冲突处理）
    m.preHydrateBase = m.shown === undefined ? readCache(m.workId, m.sid) : m.shown;
    m.pendingAtLoad = pendingRead(m) != null;
  }
  const written = cacheWrite(m, html);
  m.shown = written.html;
  m.localDurable = written.ok;
  m.cacheError = written.error;
  // 从本地写入开始就标记未同步：浏览器若在请求完成前退出，下次水合会先留恢复副本，不会把服务端旧稿静默盖回本地
  pendingWrite(m);
  m.dirty = true;
  m.canonicalDirty = true;
  const version = ++m.saveVersion;
  const outcome = waitFor(m, version, detailed);
  if (m.conflict) {
    // 服务端版本还没读到：这一稿只留在本机（读缓存 + 未同步标记；换掉读缓存之前放进同步与恢复），不发
    rejectWaiters(m, m.conflict.hold, version - 1);
    notifyState(m);
    void conflictLoad(m);
    return outcome;
  }
  m.lastSaveError = null;
  m.queued = { html: written.html, version };
  m.stalled = false;
  notifyState(m);
  pump(m);
  return outcome;
}

function pump(m) {
  if (m.inFlight || m.conflict || m.checking || m.stalled || !m.queued) return;
  const flight = { html: null, version: 0, base: null, superseded: false };
  m.inFlight = flight;
  void runFlight(m, flight);
}

async function runFlight(m, flight) {
  try {
    // 这一场第一次保存：先水合（可能发现作者是在旧缓存上写的 → 冲突，排队的已经进了同步与恢复）
    if (!m.hydrated) await hydrateMeta(m);
    if (m.conflict) {
      if (m.inFlight === flight) m.inFlight = null;
      return;
    }
    if (!m.draftId) throw unavailableError();
    const next = m.queued;
    if (!next) { // 排队的被收走了（采纳归档 / 水合时发现是在旧缓存上写的）：没有要发的
      if (m.inFlight === flight) m.inFlight = null;
      return;
    }
    m.queued = null;
    flight.html = next.html;
    flight.version = next.version;
    flight.base = m.revision;
    const data = await apiPatch(`/api/v1/author-drafts/${m.draftId}`, {
      content: flight.html,
      base_revision_no: flight.base,
    });
    onSaved(m, flight, data);
  } catch (e) {
    onFailed(m, flight, e);
  }
}

function onSaved(m, flight, data) {
  if (m.inFlight === flight) m.inFlight = null;
  if (flight.superseded) {
    settleSuperseded(m, data);
    return;
  }
  absorbServerState(m, data);
  const draft = data && data.draft;
  if (!draft || !Object.prototype.hasOwnProperty.call(draft, "content")) m.serverContent = flight.html;
  m.unsure = null; // 这个修订号上存上了：之前没等到回包的那几稿都没存上
  m.savedVersion = Math.max(m.savedVersion, flight.version);
  m.lastSaveData = data;
  m.savedAt = Date.now();
  if (!m.queued) {
    // 最新的一稿存上了：才能消费跨会话的未同步标记
    m.dirty = false;
    m.lastSaveError = null;
    pendingClear(m);
  }
  if (data && data.words_rollup && isActiveWork(m)) WsCatalog.__applyWordsRollup(m.sid, data.words_rollup);
  /* 2026-09-22 场景诊断第三轮：正文一存，服务端把这一场 / 这一章开着的发现数带回来，角标随之更新 */
  if (data && data.diagnosis_rollup) {
    try { WsDiagnosis.applyRollup(data.diagnosis_rollup); } catch (e) {}
  }
  resolveWaiters(m, flight.version, data, flight.html);
  notifyState(m);
  pump(m); // 排队的那一稿带着新的修订号接着发
}

function onFailed(m, flight, e) {
  if (m.inFlight === flight) m.inFlight = null;
  if (flight.superseded) {
    settleSuperseded(m, null);
    return;
  }
  if (e && e.code === "AUTHOR_DRAFT_CONFLICT") {
    if (flight.html != null && mayBeOwnSave(m, flight, e)) {
      startOwnCheck(m, flight, e);
      return;
    }
    openConflict(m, e, [flight.html, m.queued && m.queued.html], { minRevision: conflictFloor(flight, e) });
    return;
  }
  if (flight.html == null && !m.queued) {
    // 还在准备（水合）时就失败了、又没有要发的（排队的被采纳收走了）：没有字要重发
    notifyState(m);
    return;
  }
  // 断网 / 服务端出错：正文留在本机（读缓存 + 未同步标记），停着等下一次保存或显式 flush——再发的一定是最新的一稿
  if (flight.html != null) {
    rememberUnsure(m, flight);
    if (!m.queued) m.queued = { html: flight.html, version: flight.version };
  }
  m.stalled = true;
  m.lastSaveError = e;
  pendingWrite(m);
  if (!m.localDurable && m.queued) {
    recoveryCreate({
      sid: m.sid,
      workId: m.workId,
      html: m.queued.html,
      type: "unsynced",
      reason: "断网或服务端保存失败；浏览器缓存也不可用",
      label: `场景 ${m.sid} · 会话内未同步稿`,
    });
  }
  console.warn("[WrDocs] 正文保存失败（本机已留底，下次保存再发）:", e);
  rejectWaiters(m, e);
  notifyState(m);
}

/* 采纳归档时还在路上的那一次回来了（acceptCanonical 把它标成 superseded）。它的字已经在采纳前的备份 / 同步与恢复里，
   等它的调用方在采纳时就收到了结果：失败、409 都不补发、不停着、不开冲突、不提示，只让采纳之后排队的那一稿接着发。 */
function settleSuperseded(m, data) {
  const draft = data && data.draft;
  if (draft && Number.isInteger(draft.revision_no) && draft.revision_no > m.revision && !m.dirty) {
    // 按说到不了这里（采纳用的就是这一次的修订号，服务端按修订号拒绝它）；真存上了，就当服务端有了新版本：编辑器跟着换
    absorbServerState(m, data);
    const serverHTML = toDocHTML(m.serverContent || "");
    const written = cacheWrite(m, serverHTML);
    m.localDurable = written.ok;
    m.cacheError = written.error;
    notifyState(m);
    notifyLoadedMeta(m, "server");
    return;
  }
  notifyState(m);
  pump(m);
}

/* ---- 回包丢了的那一稿 ---- */

/* 发出去却没等到回包（断网 / 5xx）：它也许已经存上了。同一个修订号上记几稿，修订号换了就重记 */
function rememberUnsure(m, flight) {
  if (!m.unsure || m.unsure.base !== flight.base) m.unsure = { base: flight.base, htmls: [] };
  if (!m.unsure.htmls.includes(flight.html)) m.unsure.htmls = [...m.unsure.htmls, flight.html].slice(-4);
}

/* 撞上 409 的这一次和没等到回包的那几稿用的是同一个修订号：可能撞上的是自己（服务端回的当前修订号若在，得正好往前一步） */
function mayBeOwnSave(m, flight, e) {
  if (!m.unsure || m.unsure.base !== flight.base) return false;
  const current = e && e.details && e.details.current_revision_no;
  return !Number.isInteger(current) || current === flight.base + 1;
}

/* 409 之后服务端的修订号至少是多少：撞上的那一次的修订号 + 1（服务端回了当前修订号时取两者大的） */
function conflictFloor(flight, e) {
  const floor = Number.isInteger(flight.base) ? flight.base + 1 : 0;
  const current = e && e.details && e.details.current_revision_no;
  return Number.isInteger(current) ? Math.max(current, floor) : floor;
}

/* 核对（不发任何保存）：服务端眼下若正好是没等到回包的那几稿之一、修订号只往前走了一步，就是自己存上的——
   接上那个修订号，把最新的一稿（排队的，或撞上 409 的这一稿）接着发，不开冲突、不提示。
   否则进入冲突（服务端版本已经读到，就地换上）；读不到时按冲突处理，本机稿先进同步与恢复，服务端版本稍后再读。
   等这几稿结果的调用方一直等着，核对完随最后的结果兑现。 */
function startOwnCheck(m, flight, error) {
  const check = {
    error,
    html: flight.html,
    base: m.unsure.base,
    candidates: m.unsure.htmls.slice(),
    floor: conflictFloor(flight, error),
    minSeq: ensureSeq + 1,
  };
  m.checking = check;
  if (!m.queued) m.queued = { html: flight.html, version: flight.version };
  m.stalled = false;
  notifyState(m);
  requestDraft(m, check.minSeq).then(
    (data) => { if (m.checking === check) finishOwnCheck(m, check, data); },
    () => { if (m.checking === check) finishOwnCheck(m, check, null); },
  );
}

function finishOwnCheck(m, check, data) {
  m.checking = null;
  const draft = data && data.draft;
  const sameDraft = !!(draft && draft.draft_id && (!m.draftId || draft.draft_id === m.draftId));
  if (sameDraft && draft.revision_no === check.base + 1
      && check.candidates.some((html) => sameManuscriptText(html, toDocHTML(draft.content || "")))) {
    m.unsure = null;
    absorbServerState(m, data);
    notifyState(m);
    pump(m);
    return;
  }
  const texts = [check.html, m.queued && m.queued.html];
  const fresh = draft && draft.draft_id
    && (!sameDraft || !Number.isInteger(draft.revision_no) || draft.revision_no >= check.floor);
  if (fresh) {
    absorbServerState(m, data);
    openConflict(m, check.error, texts, { serverKnown: true });
    return;
  }
  openConflict(m, check.error, texts, { minRevision: check.floor });
}

/* ---- 冲突 ---- */

/* 409（或水合发现作者在旧缓存上写了字）：本机没上服务端的正文先进同步与恢复，扔掉排队的，标上冲突，再读服务端版本。
   minRevision：读回来的服务端版本至少得是这个修订号（更旧的是冲突之前的快照）。 */
function openConflict(m, error, texts, { serverKnown = false, minRevision = 0 } = {}) {
  const kept = new Set();
  let durable = true;
  let keptAny = false;
  texts.forEach((html) => {
    if (html == null) return;
    const text = docText(html);
    if (!text || kept.has(text)) return;
    const entry = keepCopy(m, html, "服务端在别处更新（409 冲突）");
    if (!entry) return;
    kept.add(text);
    keptAny = true;
    if (entry.durable === false) durable = false;
  });
  m.queued = null;
  m.stalled = false;
  m.checking = null;
  const episode = {
    error, hold: holdError(), kept, keptAny, durable, attempts: 0, timer: null, loading: null, failNotified: false, resolving: false,
    minRevision, minSeq: ensureSeq + 1,
  };
  m.conflict = episode;
  m.lastSaveError = episode.hold;
  m.dirty = true;
  // 本机稿都持久地进了同步与恢复：刷新后不必再备份一次；只进了会话内存：标记留着，刷新后按跨会话的路径再留一次
  if (durable) pendingClear(m); else pendingWrite(m);
  rejectWaiters(m, error);
  notifyState(m);
  if (serverKnown) resolveConflict(m, episode);
  else void conflictLoad(m);
}

const CONFLICT_RETRY_BASE_MS = 2000;
const CONFLICT_RETRY_MAX_MS = 60000;
let retired = false; // 模块被新实例取代（开发时热更新 / 单测 resetModules）后，旧实例的计时器不再动作

/* 读服务端版本（同一次冲突只有一次在路上）：冲突之后新发的 ensure，回包比撞上的那一次还旧就当没读到 */
function conflictLoad(m) {
  const episode = m.conflict;
  if (!episode) return Promise.resolve();
  if (episode.loading) return episode.loading;
  clearTimeout(episode.timer);
  episode.timer = null;
  const run = requestDraft(m, episode.minSeq).then((data) => {
    if (m.conflict !== episode) return;
    const draft = data && data.draft;
    if (!draft || !draft.draft_id) throw unavailableError();
    if (draft.draft_id === m.draftId && Number.isInteger(draft.revision_no) && draft.revision_no < episode.minRevision) {
      throw staleReadError();
    }
    absorbServerState(m, data);
    resolveConflict(m, episode);
  }).catch((e) => {
    if (m.conflict === episode) conflictLoadFailed(m, episode, e);
  }).finally(() => {
    if (episode.loading === run) episode.loading = null;
  });
  episode.loading = run;
  return run;
}

function resolveConflict(m, episode) {
  const serverHTML = toDocHTML(m.serverContent || "");
  // 冲突之后本机又存过（读到服务端版本之前作者接着写、离场冲刷）：读缓存里那一份换掉之前先留进同步与恢复
  const cached = readCache(m.workId, m.sid);
  const cachedText = docText(cached);
  if (cachedText && !episode.kept.has(cachedText) && !sameManuscriptText(cached, serverHTML)) {
    const entry = keepCopy(m, cached, "服务端在别处更新（409 冲突）之后本机又写的正文");
    if (entry) {
      episode.kept.add(cachedText);
      episode.keptAny = true;
      if (entry.durable === false) episode.durable = false;
    }
  }
  showServerVersion(m, serverHTML, episode.durable);
  clearTimeout(episode.timer);
  m.conflict = null;
  m.lastConflict = episode;
  m.dirty = false;
  m.lastSaveError = null;
  m.unsure = null;
  m.hydrated = true;
  m.preHydrateBase = undefined;
  m.pendingAtLoad = false;
  m.shown = serverHTML;
  if (episode.durable) pendingClear(m);
  notifyState(m);
  // 写作台在这里把编辑器换成服务端版本；编辑器里还没交出来的字经 keepLocalCopy 并进这一次（同一条提示）
  episode.resolving = true;
  notifyDoc("conflict-resolved", { sid: m.sid, workId: m.workId, html: serverHTML });
  episode.resolving = false;
  if (!episode.durable) recoveryNotice(m, NOTICE.conflictVolatile, "danger");
  else recoveryNotice(m, episode.keptAny ? NOTICE.conflict : NOTICE.conflictNothingKept);
}

function conflictLoadFailed(m, episode, e) {
  episode.attempts += 1;
  m.lastSaveError = episode.hold;
  notifyState(m);
  if (!episode.failNotified) {
    episode.failNotified = true;
    recoveryNotice(m, episode.durable ? NOTICE.conflictLoadFailed : NOTICE.conflictLoadFailedVolatile, episode.durable ? "warn" : "danger");
  }
  console.warn("[WrDocs] 冲突之后读取服务端版本失败，稍后重试:", e);
  const delay = Math.min(CONFLICT_RETRY_MAX_MS, CONFLICT_RETRY_BASE_MS * 2 ** Math.max(0, episode.attempts - 1));
  clearTimeout(episode.timer);
  episode.timer = setTimeout(() => {
    episode.timer = null;
    if (!retired && m.conflict === episode) void conflictLoad(m);
  }, delay);
}

/* 窗口重新聚焦 / 重新联网：冲突中还没读到服务端版本的，马上再读 */
function onWake() {
  if (retired) return;
  Object.values(docMeta).forEach((m) => { if (m.conflict && !m.conflict.loading) void conflictLoad(m); });
}
retireModuleListeners("wr-doc-sync");
try {
  window.addEventListener("focus", onWake);
  window.addEventListener("online", onWake);
} catch (e) { /* 没有 window 的环境 */ }
adoptModuleListeners("wr-doc-sync", () => {
  retired = true;
  try {
    window.removeEventListener("focus", onWake);
    window.removeEventListener("online", onWake);
  } catch (e) {}
  Object.values(docMeta).forEach((m) => { if (m.conflict) clearTimeout(m.conflict.timer); });
});

/* ---- flush：等这一场本机的字有结果 ---- */

/* → "saved" | "conflict" | "failed"（从不抛）。失败后停着的最新一稿，显式 flush 再发一次；
   retry：路上那一次在等的时候失败了，再发一次最新的一稿（离场冲刷用：只一次）。冲突中顺手再读服务端版本。 */
async function flushMeta(m, { retry = false } = {}) {
  if (m.conflict) {
    void conflictLoad(m);
    return "conflict";
  }
  if (!m.dirty) return "saved";
  if (!m.inFlight && !m.queued) return m.lastSaveError ? "failed" : "saved";
  if (m.stalled && !m.inFlight) { m.stalled = false; pump(m); }
  let outcome = await outcomeOf(m, m.saveVersion);
  if (outcome === "failed" && retry && !m.conflict && m.dirty) {
    if (m.stalled && !m.inFlight) { m.stalled = false; pump(m); }
    if (m.inFlight || (m.queued && !m.stalled)) outcome = await outcomeOf(m, m.saveVersion);
  }
  return outcome;
}

/* 提升前等路上 / 排队 / 核对中的那一稿有结果；失败后停着的不再发（提升只提升已经存上的） */
async function settledMeta(m) {
  if (m.conflict || !m.dirty || m.stalled || (!m.inFlight && !m.queued)) return;
  await outcomeOf(m, m.saveVersion);
}

const KEEP_REASONS = {
  conflict: "服务端在别处更新（409 冲突）时编辑器里还有没保存的改动",
  server: "这一场在别处有更新，编辑器换成服务端版本时还有没保存的改动",
  restore: "编辑器换成恢复的正文时还有没保存的改动",
  adopt: "编辑器换成采纳归档的正文时还有没保存的改动",
};

function scopeMeta(sid, options) {
  return options && options.workId ? metaFor(options.workId, sid) : meta(sid);
}

const WrDocs = {
  /* 解析 sid → 后端 author-draft draft_id（不存在则 ensure 建一份空稿）；
     供"AI 续写"等需要真实 draft_id 发起 LLM 调用的功能复用同一份映射缓存。 */
  async draftId(sid) {
    if (!sid) return null;
    const m = await ensureDraftMeta(meta(sid));
    return m.draftId || null;
  },
  /* 同步读：返回缓存（可能为 null = 从未写过），调用方（写作台）把它放进编辑器；后台水合（已水合且没有本机改动时
     后台复核），出错吞掉 */
  load(sid) {
    if (!sid) return null;
    const m = meta(sid);
    const html = readCache(m.workId, m.sid);
    m.shown = html;
    if (m.hydrated && !m.conflict) revalidate(m);
    else hydrateMeta(m).catch((e) => { console.warn("[WrDocs] 文档水合失败:", sid, e); });
    return html;
  },
  /* 显式等待服务端草稿水合（与进行中的水合共用一次；读不到服务器时抛错）；跨页面采用 AI 稿前用它确认作者正文是否已存在。 */
  async hydrate(sid) {
    if (!sid) return null;
    const m = meta(sid);
    await hydrateMeta(m);
    return readCache(m.workId, m.sid);
  },
  /* 写：调用之内本机缓存 + 未同步标记落地；PATCH 按场一次一个，排队的只留最新一稿。
     返回这一稿的结果（被更新的一稿取代时随它一起有结果）。options.workId：这一场所属的作品（离场冲刷时作品可能已换）。 */
  save(sid, html, options = {}) {
    if (!sid) return Promise.reject(Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" }));
    return saveMeta(scopeMeta(sid, options), html);
  },
  /* 把这一场整篇换成 html（同步与恢复的「恢复」「重试同步」）：和 save 一样在调用之内落本机缓存并排进保存，
     同一个调用里就通知写作台换稿（loaded，reason 默认 restore；编辑器里还没交出来的字写作台先留进同步与恢复）——
     编辑器、本机缓存和之后要同步的始终是同一稿，PATCH 失败时也是（停在本机，下一次保存 / 离场时再发）。
     → Promise<{ data, carried }>：carried = 服务端存下的就是这一稿（没被作者随后在它上面接着写的更新一稿取代）。 */
  replace(sid, html, options = {}) {
    if (!sid) return Promise.reject(Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" }));
    const m = scopeMeta(sid, options);
    const settled = saveMeta(m, html, true);
    const version = m.saveVersion;
    const wanted = readCache(m.workId, m.sid);
    notifyLoadedMeta(m, options.reason || "restore");
    return settled.then((result) => ({
      data: result.data,
      carried: result.version === version || (result.html != null && sameManuscriptText(result.html, wanted)),
    }));
  },
  /* 等这一场本机的字有结果 → "saved" | "conflict" | "failed"（从不抛）。options.retry：离场冲刷，失败时再发一次最新的一稿 */
  flush(sid, options = {}) {
    if (!sid) return Promise.resolve("saved");
    return flushMeta(scopeMeta(sid, options), options);
  },
  /* 当前草稿、保存与权威正文同步状态的只读快照。 */
  state(sid) {
    if (!sid) return null;
    return snapshotOf(meta(sid));
  },
  /* 写作台换稿（冲突、别处的新版本、恢复、采纳）时，编辑器里还没交给 WrDocs 的那几句经这里留进「同步与恢复」。
     与这次冲突已经留过的、与读缓存（新版本）一样的都不再留。冲突里的并进冲突那一条提示，其余的自己提示一句。 */
  keepLocalCopy(sid, html, options = {}) {
    if (!sid) return null;
    const m = scopeMeta(sid, options);
    const reason = options.reason || "conflict";
    const text = docText(html);
    if (!text) return null;
    const episode = m.conflict || (m.lastConflict && m.lastConflict.resolving ? m.lastConflict : null);
    if (episode && episode.kept.has(text)) return null;
    if (sameManuscriptText(html, readCache(m.workId, m.sid))) return null;
    const entry = keepCopy(m, html, KEEP_REASONS[reason] || KEEP_REASONS.conflict);
    if (!entry) return null;
    if (episode) {
      episode.kept.add(text);
      episode.keptAny = true;
      if (entry.durable === false) episode.durable = false;
      return entry;
    }
    if (entry.durable === false) recoveryNotice(m, NOTICE.volatile, "danger");
    else recoveryNotice(m, reason === "server" ? NOTICE.server : NOTICE.replaced);
    return entry;
  },
  /* 把已成功保存的场景草稿显式提升为权威正文。v1 仅支持“事实未变”。 */
  async promote(sid, options = {}) {
    if (!sid) throw Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" });
    const m = meta(sid);
    await settledMeta(m);
    if (m.conflict) throw m.conflict.hold;
    if (m.lastSaveError) throw m.lastSaveError;
    await ensureDraftMeta(m);
    if (!m.draftId) {
      throw Object.assign(new Error("场景尚未就绪，无法提升权威正文"), { code: "AUTHOR_DRAFT_UNAVAILABLE" });
    }
    if (m.dirty) {
      throw Object.assign(new Error("草稿仍有未保存改动"), { code: "AUTHOR_DRAFT_UNSAVED" });
    }
    const expectedFinal = Object.prototype.hasOwnProperty.call(options, "expectedCurrentFinalSceneRowId")
      ? options.expectedCurrentFinalSceneRowId
      : m.currentFinalSceneRowId;
    const data = await apiPost(`/api/v1/author-drafts/${m.draftId}/promote-canonical`, {
      base_revision_no: m.revision,
      expected_current_final_scene_row_id: expectedFinal == null ? null : expectedFinal,
      narrative_effect: options.narrativeEffect || "requires_reconcile",
      accepted_warning_codes: options.acceptedWarningCodes || [],
    });
    m.currentFinalSceneRowId = data.final_scene_row_id;
    m.lastPromotedRevisionNo = data.draft_revision_no;
    m.lastPromotedFinalSceneRowId = data.final_scene_row_id;
    // 提升在路上时作者又存了一稿：提升的是较早的那个修订号，眼下的草稿仍待提升
    m.canonicalDirty = Boolean(data.canonical_dirty)
      || (Number.isInteger(data.draft_revision_no) && data.draft_revision_no !== m.revision)
      || m.dirty;
    notifyState(m);
    return data;
  },
  /* adopt-current 的 exact_author_draft 已在一个服务端事务内完成保存与提升。
     这里只吸收权威回包和刷新读缓存，绝不能再 PATCH 一次制造新修订。服务端现在就是这一稿：
     路上那一次作废（它回来时不补发、不开冲突），排队 / 失败后停着 / 核对中 / 冲突中的本机稿都不再发——
     本机最新的那一稿和采纳的不一样、又不在同步与恢复里（采纳前的作者稿备份通常就是它）时先留一份。 */
  acceptCanonical(sid, html, data) {
    if (!sid) throw Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" });
    const m = meta(sid);
    const normalized = sanitizeManuscriptHTML(html || "");
    const serverDraft = data && data.author_draft;
    if (!serverDraft || !serverDraft.draft_id || !Number.isInteger(serverDraft.revision_no)) {
      throw Object.assign(new Error("归档响应缺少作者稿修订信息"), { code: "AUTHOR_DRAFT_ADOPTION_RESPONSE_INVALID" });
    }
    if (m.draftId && m.draftId !== serverDraft.draft_id) {
      throw Object.assign(new Error("归档响应属于另一份作者稿"), { code: "AUTHOR_DRAFT_ADOPTION_MISMATCH" });
    }
    if (m.dirty) {
      // 本机缓存里的就是交给 WrDocs 的最新一稿（路上 / 排队 / 停着 / 冲突中写的）
      const local = readCache(m.workId, m.sid);
      if (local != null && !sameManuscriptText(local, normalized)) {
        keepCopy(m, local, "采纳 AI 稿归档时本机还有没存上的正文");
      }
    }
    if (m.inFlight && m.inFlight.html != null) m.inFlight.superseded = true;
    m.queued = null;
    m.stalled = false;
    if (m.conflict) clearTimeout(m.conflict.timer);
    m.conflict = null;
    m.checking = null;
    m.unsure = null;
    rejectWaiters(m, replacedError());
    const cached = cacheWrite(m, normalized);
    m.localDurable = cached.ok;
    m.cacheError = cached.error;
    m.serverContent = normalized;
    m.dirty = false;
    m.lastSaveError = null;
    m.hydrated = true;
    m.preHydrateBase = undefined;
    m.pendingAtLoad = false;
    absorbServerState(m, {
      draft: { ...serverDraft, content: normalized },
      runtime_final_ref: data.final_scene_row_id ? `final_scene:${data.final_scene_row_id}` : null,
    });
    pendingClear(m);
    notifyState(m);
    notifyLoadedMeta(m, "adopt");
    return snapshotOf(m);
  },
  /* 正文状态 / 读缓存变化的订阅：fn(kind, detail)，返回退订函数（见文件头） */
  subscribe(fn) {
    docListeners.add(fn);
    return () => { docListeners.delete(fn); };
  },
  /* 本机读缓存里这一场的正文（不触发水合；可能是 null = 从未写过）。别的台子要读缓存时用它，
     不必自己拼 wr-doc: 键去读 localStorage（会绕过会话内存里配额不足时的那一份）。 */
  cachedHTML(sid) {
    if (!sid) return null;
    return readCache(activeWorkId(), sid);
  },
  /* 当前在写场景预热（目录装载后调用）。已被新实例取代的旧实例（热更新 / 单测 resetModules）不再动作 */
  hydrateActive() {
    if (retired) return;
    try {
      const w = WsCatalog.writingScene();
      if (w && w.scene && w.scene.sid) hydrateMeta(meta(w.scene.sid)).catch(() => {});
    } catch (e) {}
  },
};

/* 版本列表 / 某一版正文要 draftId 时复用同一条 ensure（wr-doc-versions.js） */
function ensureDraft(sid) {
  return ensureDraftMeta(meta(sid));
}

export { WrDocs, ensureDraft };
