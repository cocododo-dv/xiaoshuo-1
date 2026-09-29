import { apiGet, apiPost } from "./lib/client.js";
import { emit } from "./lib/events.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { activeWork, captureDirectionBriefs, captureResync, captureTriage, snowReadyFlags } from "./ws-snow-sync-state.js";
import { adoptServerChapters } from "./ws-snow-hydrate.js";
import { flushSnowPush } from "./ws-snow-push.js";

/* ==========================================================
   雪花同步 · 分章与物化的接口（2026-09-29 从 ws-snow-sync.jsx 原样搬出）
   预览 / AI 建议 / AI 起章名 / 处置孤儿场 / 物化（materialize + outline/approve）。
   每一步之前先排空本机还没上行的编辑（flushSnowPush）；物化后本机 07 章表接过服务端的章表。
   ========================================================== */

async function attachMaterializationGate(result, workId) {
  try {
    const workspace = await apiGet(`/api/v2/projects/${workId}/snowflake-workspace`);
    snowReadyFlags[workId] = !!(workspace && workspace.ready_to_materialize);
    captureResync(workId, workspace);
    captureTriage(workId, workspace);
    captureDirectionBriefs(workId, workspace);
    return { ...(result || {}), materialization_gate: (workspace && workspace.materialization_gate) || null };
  } catch (error) {
    // 预览本身已经成功时，不因第二次只读检查失败而抹掉方案；最终 materialize 仍会
    // 由后端权威闸门把关，并把最新 details 回给面板。
    return { ...(result || {}), materialization_gate: null };
  }
}


/* 分章预览（只读）。strategy：auto（服务端按现状挑）/ from_scenes（按场景分章）/ spine_anchor / even /
   keep_current。options.scenesPerChapter / options.targetChapterCount 只对 from_scenes 有意义——
   作者在面板里填的「每章约 N 场」。 */
async function chapterPreview(strategy, options, workId) {
  const opts = options && typeof options === "object" ? options : {};
  const id = (typeof options === "string" ? options : workId) || activeWork();
  await flushSnowPush(id);
  const body = strategy ? { strategy } : {};
  if (Number(opts.scenesPerChapter) > 0) body.scenes_per_chapter = Math.round(Number(opts.scenesPerChapter));
  if (Number(opts.targetChapterCount) > 0) body.target_chapter_count = Math.round(Number(opts.targetChapterCount));
  const preview = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/chapter-plan/preview`, body);
  return attachMaterializationGate(preview, id);
}

/* 让 AI 给一份分章建议（只读，不落库）。fail-closed：LLM 没配好会 409 上抛，
   调用方如实提示去配置 —— 绝不拿规则算出来的东西冒充 AI 建议。 */
async function chapterSuggest(baseStrategy, workId) {
  const id = workId || activeWork();
  await flushSnowPush(id);
  const suggestion = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/chapter-plan/suggest`,
    baseStrategy ? { base_strategy: baseStrategy } : {});
  return attachMaterializationGate(suggestion, id);
}

/* AI 起章名（阶段 W，只读，不落库）：chapters = 面板此刻的章表 [{row_uid, title, act, spine, scene_plan_ids}]
   （含还没确认的 new:* 章）。只给系统起的占位名起名；options.renameAll 连作者起过的也重起。
   fail-closed：LLM 没配好 409 上抛，模型没给出可用章名 502 上抛——调用方如实提示。 */
async function chapterTitles(chapters, options, workId) {
  const opts = options && typeof options === "object" ? options : {};
  const id = workId || activeWork();
  if (!id) throw new Error("作品尚未就绪");
  const body = { chapters: Array.isArray(chapters) ? chapters : [] };
  if (opts.renameAll) body.rename_all = true;
  return apiPost(`/api/v2/projects/${id}/snowflake-workspace/chapter-plan/titles`, body);
}

/* 处置孤儿场：action = "discard"（正文一并进回收站）/ "keep"（正文留在目录里）。
   孤儿场 = 作者从 09 删掉、但目录里已经有场景卡（可能已写正文）的那些场。它们是
   分章面板上的 blocker，没有这个动作就永远清不掉，「确认分章」按钮从此点不动。 */
async function resolveOrphanedScene(scenePlanId, action, workId) {
  const id = workId || activeWork();
  const data = await apiPost(
    `/api/v2/projects/${id}/snowflake-workspace/orphaned-scenes/${scenePlanId}/resolve`,
    { action });
  return data;
}

/* 物化主路径：approved scene plans → ChapterGoal/SceneCard（成功后目录重拉）。
   plan = 分章面板确认时的 {chapters, assignments}，与物化同一事务落库，
   不留「分了章但没物化」的中间态。
   注意：materialize 端点只建 pending OutlinePlan，章节要 outline/approve 才落库——
   必须两步都走，否则目录为空却谎称「已并入 N 章」。返回真实 created_chapter_count。 */
async function materialize(workId, plan) {
  const id = workId || activeWork();
  await flushSnowPush(id);
  const data = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/materialize`, plan || {});
  // 分章此刻已经落库（与 materialize 同一事务）：不管下面的 outline/approve 成不成，本机的 07 章节表
  // 都要先接过服务端的章表——否则一次失败的批准之后，本机旧章表会在下一次 07 上行时把它冲掉。
  try { await adoptServerChapters(id); } catch (e) {}
  const approved = await apiPost(`/api/v2/projects/${id}/snowflake-workspace/outline/approve`, {});
  try { WsCatalog.reset(); } catch (e) {}
  const createdChapters = (approved && approved.created_chapter_count) || 0;
  // 服务端这一步可能自动把空章 / 占位章移入回收站、或把场景卡取回：让开着的回收站也跟上
  const trashMoved = ["trashed_empty_chapters", "trashed_placeholder_chapters", "restored_chapter_ids", "restored_scene_ids"]
    .some((key) => Array.isArray(approved && approved[key]) && approved[key].length > 0);
  if (trashMoved) { emit("ws:trash-changed"); }
  return {
    ...(data || {}),
    created_chapter_count: createdChapters,
    // 阶段 W：重新分章后变空的旧章已移入回收站 / 这一版又用到的章已从回收站取回
    trashed_empty_chapters: (approved && approved.trashed_empty_chapters) || [],
    restored_chapter_ids: (approved && approved.restored_chapter_ids) || [],
    restored_scene_ids: (approved && approved.restored_scene_ids) || [],
    // 阶段 X：手建的空白占位章（「第 1 章 / 开场」，一个字没写）已移入回收站，这一版的章从第 1 章排起
    trashed_placeholder_chapters: (approved && approved.trashed_placeholder_chapters) || [],
    // 阶段 Y：目录里有已终审的章，按章表排会挪动它 → 这次没排，新章接在最后
    chapter_order_held: !!(approved && approved.chapter_order_held),
  };
}

export { attachMaterializationGate, chapterPreview, chapterSuggest, chapterTitles, resolveOrphanedScene, materialize };
