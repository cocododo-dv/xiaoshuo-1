import { apiDelete, apiGet, apiPatch, apiPost } from "./lib/client.js";
import { srNormalizeConfig } from "./ws-styleref-model.js";
import { randomSuffix } from "./lib/ids.js";
import {
  SR_API, srActiveWorkId, srBookTitle, srBooks, srCacheDelete, srCacheEntries, srCacheGet, srCacheKeys, srCachePut,
  srCacheSet, srEmit, srLoadLearn, srLoadProfileBindings, srLoadProjectBinding, srMarkBookDeleted, srResetCoreForTests,
  srSetBooks, srSyncBooks,
} from "./ws-styleref-store-core.js";
import { srActivityForgetBook, srActivityPoke, srActivityTrack, srResetActivityForTests } from "./ws-styleref-store-activity.js";

/* ==========================================================
   风格参考 · store（2026-09-23 v3 重建；2026-09-30 拆成三块，审计 F05-15）
   ----------------------------------------------------------
   · 底座 ws-styleref-store-core.js：当前作品、订阅频道、书库摘要、GET /runtime、按需读的详情缓存
     （一本书 / 学习信息 / 文风画像 / 一部作品的生效绑定 / 一份画像的全部绑定）；
   · 参考书活动 ws-styleref-store-activity.js：作业表条目与 /activity 轮询；
   · 这个文件：写操作——文风卡 ✓ / ✗、用于作品 / 改配置 / 解除、删书、导入、重新分类、学习、本场预览、原文段落、
     禁用词——并照旧转出前两块里视图与单测 import 的名字（门面）。
   写操作都是「先改界面、再等服务端；失败回滚并把错误抛给调用方」（✓ / ✗、改绑定、解除、批量删除、用于作品），
   说法由界面按 ws-styleref-model 的 srErrorInfo 给。所有请求都经 lib/client.js（上传也是：FormData）。
   依赖只朝一个方向（facade → core / activity，activity → core），不写 window。
   ========================================================== */

export {
  srConfigureHost, srSubscribe, srSetViewMounted, srSessionUi, srRememberSession,
  srBooks, srBooksState, srBookById, srSyncBooks, srRuntime, srLoadRuntime,
  srBookDetail, srLoadBookDetail, srLearnInfo, srLoadLearn, srProfileDetail, srLoadProfile,
  srProjectBinding, srLoadProjectBinding, srProfileBindings, srLoadProfileBindings,
} from "./ws-styleref-store-core.js";
export {
  SR_ACTIVITY_VANISH_GRACE_MS, srActivityEntries, srActivityFor, srActivityTrack, srActivityApply, srActivityDismiss,
  srActivityClearFinished, srActivityStart, srActivityPoke,
} from "./ws-styleref-store-activity.js";

const API = SR_API;

/* 绑定变了（解除、删书）：读过的作品的生效绑定全部重读（旧版的全局绑定挂在每一部没有自己应用的作品上，
   解除它影响的不只一部），再加上 extra 与当前作品 */
function srReloadProjectBindings(...extra) {
  const ids = new Set(extra.filter(Boolean));
  const active = srActiveWorkId();
  if (active) ids.add(active);
  for (const key of srCacheKeys()) if (key.startsWith("project:")) ids.add(key.slice("project:".length));
  ids.forEach((id) => { srLoadProjectBinding(id, { force: true }); });
}

/* 读过的「一份画像的全部绑定」重读（界面上「这份文风还用在」读它；删掉缓存不重读，那一块就消失了） */
function srReloadBindingLists(profileIds = null) {
  const only = profileIds ? new Set(profileIds.filter(Boolean)) : null;
  for (const key of srCacheKeys()) {
    if (!key.startsWith("bindings:")) continue;
    const profileId = key.slice("bindings:".length);
    if (!only || only.has(profileId)) srLoadProfileBindings(profileId, { force: true });
  }
}

/* ==========================================================
   文风卡一句的 ✓ / ✗（先改界面，失败回滚）
   ========================================================== */
function srWithLineState(profile, lineId, state) {
  const states = { ...(profile.card_line_states || {}) };
  if (state) states[lineId] = state; else delete states[lineId];
  return {
    ...profile,
    card_line_states: states,
    dimensions: (profile.dimensions || []).map((dim) => (
      (dim.lines || []).some((line) => line.line_id === lineId)
        ? { ...dim, lines: dim.lines.map((line) => (line.line_id === lineId ? { ...line, state: state || null } : line)) }
        : dim
    )),
  };
}

export async function srSetCardLineState(profileId, lineId, state) {
  const key = `profile:${profileId}`;
  const entry = srCacheGet(key);
  const before = entry && entry.data ? entry.data : null;
  if (before) srCacheSet(key, { ...entry, data: srWithLineState(before, lineId, state) });
  try {
    const result = await apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/card-lines/${encodeURIComponent(lineId)}`, { state: state || null });
    const now = srCacheGet(key);
    if (now && now.data && result && result.card_line_states) {
      const reconciled = { ...now.data, card_line_states: result.card_line_states };
      reconciled.dimensions = (reconciled.dimensions || []).map((dim) => ({
        ...dim,
        lines: (dim.lines || []).map((line) => ({ ...line, state: result.card_line_states[line.line_id] || null })),
      }));
      srCacheSet(key, { ...now, data: reconciled });
    }
    return result;
  } catch (e) {
    if (before) srCacheSet(key, { ...(srCacheGet(key) || entry), data: before });
    throw e;
  }
}

/* ==========================================================
   用于作品：直接绑定 / 改配置 / 解除（先改界面，失败回滚）
   ========================================================== */

/* 与后端 merge_config 同一口径：顶层三键覆盖，维度状态按维合并 */
export function srMergeConfig(base, patch) {
  const current = srNormalizeConfig(base);
  const p = patch || {};
  const next = { ...current };
  ["reference_mode", "sample_windows", "draft_mode"].forEach((k) => { if (p[k] != null) next[k] = p[k]; });
  if (p.dimension_states) next.dimension_states = { ...current.dimension_states, ...p.dimension_states };
  return srNormalizeConfig(next);
}

function srPatchBooksApplied(mutator) {
  let changed = false;
  const next = srBooks().map((book) => {
    const patched = mutator(book);
    if (patched !== book) changed = true;
    return patched;
  });
  if (!changed) return;
  srSetBooks(next);
  srEmit("books");
}

function srBookOfProfile(profileId) {
  return srBooks().find((b) => b.profile && b.profile.profile_id === profileId) || null;
}

/* 把画像用于作品。先把这部作品的生效绑定改成「在用这份」（pending），成功后换成服务端的结果并刷新书库；
   失败回滚。返回 { binding, created, changed, replaced }。
   baseConfig：这份画像在这部作品上已有的（停用的）绑定的配置——换回这本书时后端在它上面合并，乐观值也照它算。 */
export async function srApplyProfile(profileId, { projectId, config = {}, baseConfig = null } = {}) {
  if (!profileId || !projectId) throw Object.assign(new Error("还没有打开作品"), { code: "SR_NO_WORK" });
  const key = `project:${projectId}`;
  const entry = srCacheGet(key);
  const before = entry ? entry.data : null;
  const booksBefore = srBooks();
  const book = srBookOfProfile(profileId);
  const sameOwn = !!(before && before.binding && before.binding.profile_id === profileId
    && before.binding.scope === "project" && before.binding.scope_ref_id === projectId);
  const optimistic = {
    ...(before || { project_id: projectId }),
    binding: {
      binding_id: sameOwn ? before.binding.binding_id : null,
      profile_id: profileId,
      scope: "project",
      scope_ref_id: projectId,
      status: "active",
      config: srMergeConfig(sameOwn ? before.binding.config : baseConfig, config),
      pending: true,
    },
    profile: book ? book.profile : (before ? before.profile : null),
    book: book ? { book_id: book.id, title: book.title, cloud_policy: book.cloudPolicy } : (before ? before.book : null),
  };
  srCacheSet(key, { phase: "ready", data: optimistic, error: null });
  srPatchBooksApplied((b) => {
    const others = (b.appliedProjects || []).filter((item) => item.project_id !== projectId);
    if (b.profile && b.profile.profile_id === profileId) {
      return { ...b, appliedProjects: [...others, { project_id: projectId, profile_id: profileId, binding_id: null, config: optimistic.binding.config }] };
    }
    return others.length !== (b.appliedProjects || []).length ? { ...b, appliedProjects: others } : b;
  });
  let result;
  try {
    result = await apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/apply`, { scope: "project", scope_ref_id: projectId, config });
  } catch (e) {
    srCacheSet(key, entry || { phase: "idle", data: null, error: null });
    srSetBooks(booksBefore);
    srEmit("books");
    throw e;
  }
  const current = srCacheGet(key);
  srCacheSet(key, { phase: "ready", data: { ...(current ? current.data : optimistic), binding: result.binding }, error: null });
  /* 「这份文风还用在」读这份画像的绑定清单：重读，不是删掉（删掉没人重读，那一块就消失了）；被这次换下来的
     别的画像的绑定停用了，读过它们清单的也重读 */
  srLoadProfileBindings(profileId, { force: true });
  srReloadBindingLists(((result && result.replaced) || []).map((r) => r && r.profile_id).filter((pid) => pid && pid !== profileId));
  srLoadProjectBinding(projectId, { force: true });
  srSyncBooks();
  return result;
}

/* 改一条绑定的配置（维度状态按维合并）。projectId 给了就先改那部作品缓存里的配置，失败回滚。 */
export async function srUpdateBinding(bindingId, patch, { projectId = null } = {}) {
  const key = projectId ? `project:${projectId}` : null;
  const entry = key ? srCacheGet(key) : null;
  const before = entry ? entry.data : null;
  const booksBefore = srBooks();
  if (before && before.binding && before.binding.binding_id === bindingId) {
    const nextConfig = srMergeConfig(before.binding.config, patch);
    srCacheSet(key, { ...entry, data: { ...before, binding: { ...before.binding, config: nextConfig } } });
    srPatchBooksApplied((b) => (
      (b.appliedProjects || []).some((item) => item.binding_id === bindingId)
        ? { ...b, appliedProjects: b.appliedProjects.map((item) => (item.binding_id === bindingId ? { ...item, config: nextConfig } : item)) }
        : b
    ));
  }
  try {
    const result = await apiPatch(`${API}/bindings/${encodeURIComponent(bindingId)}`, { config: patch });
    const now = key ? srCacheGet(key) : null;
    if (now && now.data && now.data.binding && now.data.binding.binding_id === bindingId && result && result.binding) {
      srCacheSet(key, { ...now, data: { ...now.data, binding: result.binding } });
    }
    return result;
  } catch (e) {
    if (key && entry) srCacheSet(key, entry);
    srSetBooks(booksBefore);
    srEmit("books");
    throw e;
  }
}

/* 文风画像页的「重点 / 正常 / 不学」：写在当前作品的绑定上 */
export function srSetDimensionState(projectId, bindingId, dimension, state) {
  return srUpdateBinding(bindingId, { dimension_states: { [dimension]: state } }, { projectId });
}

/* 解除一条绑定。先把作品缓存与书库里的「在用」去掉，失败回滚。
   缓存里凡是生效绑定就是这一条的作品都先改成「没有在用」（旧版的全局绑定挂在每一部没有自己应用的作品上）；
   成功后这些作品、当前作品与 projectId 的生效绑定一律重读——解除的不是作品自己那条时，作品页也不会还说「在用」。 */
export async function srUnbind(bindingId, { projectId = null, profileId = null } = {}) {
  const booksBefore = srBooks();
  const touched = [];
  for (const [key, entry] of srCacheEntries()) {
    if (!key.startsWith("project:")) continue;
    if (entry && entry.data && entry.data.binding && entry.data.binding.binding_id === bindingId) {
      touched.push([key, entry]);
      srCachePut(key, { ...entry, data: { ...entry.data, binding: null } });
    }
  }
  if (touched.length) srEmit("detail");
  srPatchBooksApplied((b) => (
    (b.appliedProjects || []).some((item) => item.binding_id === bindingId)
      ? { ...b, appliedProjects: b.appliedProjects.filter((item) => item.binding_id !== bindingId) }
      : b
  ));
  try {
    const result = await apiDelete(`${API}/bindings/${encodeURIComponent(bindingId)}`);
    if (profileId) srLoadProfileBindings(profileId, { force: true });
    srReloadProjectBindings(projectId);
    srSyncBooks();
    return result;
  } catch (e) {
    touched.forEach(([key, entry]) => srCachePut(key, entry));
    if (touched.length) srEmit("detail");
    srSetBooks(booksBefore);
    srEmit("books");
    throw e;
  }
}

/* ==========================================================
   删书（单本 / 多本都走批量删除）：先从书库里拿掉，服务端没删成的放回来
   返回服务端的结果外加 deletedIds（真删掉的书，含早已不在的）与 failedItems（没删成的那几条 results）——
   视图只按真删掉的书挪开当前页、报数（审计 F05-06：以前把选中的全部当成删掉了，没删成的那本也会被切走）。
   ========================================================== */
const srDeleteFailed = (item) => !item.deleted && !(item.error && item.error.code === "STYLE_REFERENCE_BOOK_NOT_FOUND");

export async function srDeleteBooks(bookIds) {
  const ids = Array.from(new Set((bookIds || []).filter(Boolean)));
  if (!ids.length) return { results: [], deleted_count: 0, failed_count: 0, deletedIds: [], failedItems: [] };
  const booksBefore = srBooks();
  srSetBooks(booksBefore.filter((b) => !ids.includes(b.id)));
  srEmit("books");
  let result;
  try {
    result = await apiPost(`${API}/books/bulk-delete`, { book_ids: ids });
  } catch (e) {
    srSetBooks(booksBefore);
    srEmit("books");
    throw e;
  }
  const failedItems = ((result && result.results) || []).filter(srDeleteFailed);
  const failed = new Set(failedItems.map((item) => item.book_id));
  if (failed.size) {
    srSetBooks([...srBooks(), ...booksBefore.filter((b) => failed.has(b.id))]);
    srEmit("books");
  }
  const deleted = ids.filter((id) => !failed.has(id));
  const deletedProfiles = new Set();
  deleted.forEach((id) => {
    srMarkBookDeleted(id);
    ["book:", "learn:"].forEach((prefix) => srCacheDelete(`${prefix}${id}`));
    const was = booksBefore.find((b) => b.id === id);
    if (was && was.profile && was.profile.profile_id) deletedProfiles.add(was.profile.profile_id);
    srActivityForgetBook(id);
  });
  if (deleted.length) {
    for (const [key, entry] of srCacheEntries()) {
      if (key.startsWith("profile:") && entry && entry.data && deleted.includes(entry.data.book_id)) deletedProfiles.add(key.slice("profile:".length));
    }
    deletedProfiles.forEach((profileId) => {
      srCacheDelete(`profile:${profileId}`);
      srCacheDelete(`bindings:${profileId}`);
    });
    /* 删书连同它的绑定一起删了：用着它的作品先改成「没有在用」，再把读过的作品生效绑定与各画像的绑定清单
       都重读，哪一页都不再说它「在用」 */
    for (const [key, entry] of srCacheEntries()) {
      if (!key.startsWith("project:") || !entry || !entry.data) continue;
      const bound = entry.data.binding;
      const bookOf = entry.data.book && entry.data.book.book_id;
      if ((bound && deletedProfiles.has(bound.profile_id)) || (bookOf && deleted.includes(bookOf))) {
        srCachePut(key, { ...entry, data: { ...entry.data, binding: null, profile: null, book: null } });
      }
    }
    srReloadProjectBindings();
    srReloadBindingLists();
  }
  srEmit("detail");
  srEmit("activity");
  srSyncBooks();
  return { ...result, deletedIds: deleted, failedItems };
}

/* ==========================================================
   导入：multipart 经 lib/client 的 apiPost（FormData），同一个幂等键重试时后端重放
   成功：把分类作业登记进活动表、刷新书库、广播 sr:book-imported；失败原样抛给导入对话框
   ========================================================== */
export async function srRunImport({ file, title, authorLabel = null, cloudPolicy, rightsDeclaration = null, importKey = null }) {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("title", title);
  if (authorLabel && String(authorLabel).trim()) form.append("author_label", String(authorLabel).trim());
  form.append("cloud_policy", cloudPolicy);
  if (rightsDeclaration) form.append("rights_declaration", JSON.stringify(rightsDeclaration));
  const key = importKey || `sr-import-${Date.now().toString(36)}${randomSuffix(4)}`;
  const data = (await apiPost(`${API}/books/import-upload`, form, { idempotencyKey: key })) || {};
  const book = data.book || {};
  if (data.job_id) srActivityTrack(data.job_id, { kind: "classify", mode: "import", book_id: book.book_id, title: book.title || title });
  await srSyncBooks();
  srEmit("imported", { bookId: book.book_id || null, jobId: data.job_id || null });
  return data;
}

/* ==========================================================
   段落分类：用模型重新分类（就地重标类型，保留画像与绑定）/ 继续 / 取消
   ========================================================== */
export async function srRetype(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/reclassify`, { mode: "retype" });
  if (data && data.job_id) srActivityTrack(data.job_id, { kind: "classify", mode: "retype", book_id: bookId, title: srBookTitle(bookId) });
  srSyncBooks();
  return data;
}

export async function srResumeClassification(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/reclassify`, { resume: true });
  if (data && data.job_id) srActivityTrack(data.job_id, { kind: "classify", mode: data.mode, book_id: bookId, title: srBookTitle(bookId) });
  srSyncBooks();
  return data;
}

export async function srCancelClassification(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/classification/cancel`, {});
  srActivityPoke();
  return data;
}

/* ==========================================================
   学习文风：开始 / 继续 / 取消
   ========================================================== */
/* resume：从断点续跑；force：正文很短也学（作业因 input_too_small 失败、或建作业时 409 STYLE_REFERENCE_INPUT_TOO_SMALL 之后）；
   retag：全书窗口的标签都重打（缺省只补标签版本旧了的窗） */
export async function srStartLearn(bookId, { resume = false, force = false, retag = false } = {}) {
  const body = {};
  if (resume) body.resume = true;
  if (force) body.force = true;
  if (retag) body.retag = true;
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/learn`, body);
  if (data && data.job_id) srActivityTrack(data.job_id, { kind: "learn", book_id: bookId, title: srBookTitle(bookId) });
  srLoadLearn(bookId, { force: true });
  srSyncBooks();
  return data;
}

export async function srCancelLearn(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/learn/cancel`, {});
  srActivityPoke();
  srLoadLearn(bookId, { force: true });
  return data;
}

/* ==========================================================
   本场预览（只读，不缓存：设置一变就重算）；当前作品的场从目录 store 读（ws-styleref-ui 的 useSrWorkScenes）
   ========================================================== */
export async function srScenePreview(profileId, { sceneId = null, projectId = null, config = {} } = {}) {
  const c = srNormalizeConfig(config);
  const body = {
    reference_mode: c.reference_mode,
    sample_windows: c.sample_windows,
    dimension_states: c.dimension_states,
    draft_mode: c.draft_mode,
  };
  if (sceneId) body.scene_id = sceneId;
  if (projectId) body.project_id = projectId;
  return apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/injection-preview`, body);
}

/* 参考书原文的一段范围（本场预览里展开一个样例窗；只在本机给作者看）：闭区间，一次至多 80 段 */
export async function srLoadParagraphs(bookId, start, end) {
  const qs = new URLSearchParams({ start: String(start), end: String(end) });
  const data = await apiGet(`${API}/books/${encodeURIComponent(bookId)}/paragraphs?${qs.toString()}`);
  return { paragraphs: (data && data.paragraphs) || [], capped: !!(data && data.capped), end: data ? data.end : end };
}

/* ==========================================================
   禁用词（本书专名由学习作业识别；作者自己加的「起草时不许出现」的词）
   ========================================================== */
export async function srLoadBannedTerms(profileId) {
  return ((await apiGet(`${API}/profiles/${encodeURIComponent(profileId)}/banned-terms`)) || {}).terms || [];
}

export async function srAddBannedTerm(profileId, term) {
  return apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/banned-terms`, { term, scope: "generation" });
}

export async function srRemoveBannedTerm(termId) {
  return apiDelete(`${API}/banned-terms/${encodeURIComponent(termId)}`);
}

/* 用模型重新分类（就地）的费用估算：每次点「用模型重新分类」都现问（审计 F05-23：以前经详情缓存读、却总是强制重读，
   缓存从来没命中过） */
export async function srFetchClassifyEstimate(bookId) {
  return ((await apiGet(`${API}/books/${encodeURIComponent(bookId)}/classification/estimate`)) || {}).estimate || null;
}

/* 单测用：清空模块级状态（底座、活动表，并停掉轮询） */
export function srResetForTests() {
  srResetCoreForTests();
  srResetActivityForTests();
}
