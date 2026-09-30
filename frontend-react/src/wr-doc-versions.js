import { apiGet } from "./lib/client.js";
import { htmlToParagraphs } from "./manuscript-html.js";
import { WsCatalog } from "./ws-catalog.jsx";

/* ==========================================================
   WrDocVersions — 正文修订历史（FE-ALIGN F2）
   ----------------------------------------------------------
   成稿中心「对比」的数据源：后端每次保存都会落一行修订快照（author_draft_revisions，自动保存 5 分钟至多一份 +
   每次采纳 / 晋升一份，批准 #8），这里按 sid 分页取版本列表与任一版正文，并提供句级 diff（LCS）。
   只读：作者稿用 GET …/author-drafts/scene/{scene_id}/current 找（重评 R15a）——没有作者稿就是没有版本，
   不再像以前那样走 WrDocs 的 POST ensure、只是看一眼就替这一场建一份空作者稿。
   ========================================================== */

/* 版本列表一页的条数（后端上限 100） */
export const VERSIONS_PAGE_SIZE = 50;

/* 后端 scene_id → 它的作者稿 id。作者稿一经建立 id 不变：列表读到过就记下，逐版取正文时不再多读一次 */
const draftIdByScene = new Map();

/* 这一场眼下的作者稿 id（没有作者稿 / 这一场还没同步到后端 → null）。只读，不建稿。 */
async function currentDraftId(sid, { reuse = false } = {}) {
  let sceneId = null;
  try { sceneId = await WsCatalog.backendSceneId(sid); } catch (e) { sceneId = null; }
  if (!sceneId) return null;
  if (reuse && draftIdByScene.has(sceneId)) return draftIdByScene.get(sceneId);
  const data = await apiGet(`/api/v1/author-drafts/scene/${encodeURIComponent(sceneId)}/current`);
  const draftId = (data && data.draft && data.draft.draft_id) || null;
  if (draftId) draftIdByScene.set(sceneId, draftId);
  else draftIdByScene.delete(sceneId);
  return draftId;
}

/* 句级 diff：A=旧版段落、B=新版段落 → 按 B 版式分段的 same/del/add 片段 */
export function diffSentences(aParas, bParas) {
  const split = (paras) => {
    const out = [];
    (paras || []).forEach((p, pi) => {
      const parts = String(p).split(/(?<=[。！？!?；;…])/).map(s => s.trim()).filter(Boolean);
      (parts.length ? parts : [String(p)]).forEach(s => out.push({ p: pi, s }));
    });
    return out;
  };
  const A = split(aParas), B = split(bParas);
  const n = A.length, m = B.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i].s === B[j].s ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const segs = [];
  let i = 0, j = 0, adds = 0, dels = 0;
  const delPara = () => (j < m ? B[j].p : (m ? B[m - 1].p : 0));
  while (i < n && j < m) {
    if (A[i].s === B[j].s) { segs.push({ t: "same", text: B[j].s, p: B[j].p }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { segs.push({ t: "del", text: A[i].s, p: delPara() }); dels++; i++; }
    else { segs.push({ t: "add", text: B[j].s, p: B[j].p }); adds++; j++; }
  }
  while (i < n) { segs.push({ t: "del", text: A[i].s, p: delPara() }); dels++; i++; }
  while (j < m) { segs.push({ t: "add", text: B[j].s, p: B[j].p }); adds++; j++; }
  const paras = [];
  segs.forEach(seg => {
    if (!paras.length || paras[paras.length - 1].p !== seg.p) paras.push({ p: seg.p, segs: [] });
    paras[paras.length - 1].segs.push(seg);
  });
  return { paras, adds, dels };
}


export const WrDocVersions = {
  /* 版本列表的一页（新 → 旧）：{ items: [{revisionNo, words, origin, at}], nextCursor }。
     nextCursor 不为空就还有更早的版本：再用 { cursor: nextCursor } 取下一页。没有作者稿 / 还没同步到后端 → 空页。 */
  async list(sid, { cursor = null, limit = VERSIONS_PAGE_SIZE } = {}) {
    const draftId = await currentDraftId(sid, { reuse: !!cursor });
    if (!draftId) return { items: [], nextCursor: null };
    const query = `limit=${encodeURIComponent(limit)}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
    const data = await apiGet(`/api/v1/author-drafts/${draftId}/revisions?${query}`);
    return {
      items: ((data && data.items) || []).map(r => ({
        revisionNo: r.revision_no,
        words: r.words || 0,
        origin: r.origin || "edited",
        at: r.created_at || "",
      })),
      nextCursor: (data && data.pagination && data.pagination.has_next && data.pagination.next_cursor) || null,
    };
  },
  /* 某一版正文 → 段落数组（剥 HTML）；没有作者稿 → [] */
  async paras(sid, revisionNo) {
    const draftId = await currentDraftId(sid, { reuse: true });
    if (!draftId) return [];
    const data = await apiGet(`/api/v1/author-drafts/${draftId}/revisions/${revisionNo}`);
    return htmlToParagraphs((data && data.revision && data.revision.content) || "");
  },
  diff: diffSentences,
};
