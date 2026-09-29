import { apiGet } from "./lib/client.js";
import { htmlToParagraphs } from "./manuscript-html.js";
import { ensureDraft } from "./wr-doc-sync.js";

/* ==========================================================
   WrDocVersions — 正文修订历史（FE-ALIGN F2）
   ----------------------------------------------------------
   成稿中心「对比」的数据源：后端每次保存都会落一行修订快照
   （author_draft_revisions），这里按 sid 取版本列表与任一版正文，
   并提供句级 diff（LCS）。draftId 复用 WrDocs 的 ensure 链路。
   ========================================================== */

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
  /* 版本列表（新→旧）：[{revisionNo, words, origin, at}]；无后端目录映射时返回 [] */
  async list(sid) {
    const m = await ensureDraft(sid);
    if (!m.draftId) return [];
    const data = await apiGet(`/api/v1/author-drafts/${m.draftId}/revisions`);
    return ((data && data.items) || []).map(r => ({
      revisionNo: r.revision_no,
      words: r.words || 0,
      origin: r.origin || "edited",
      at: r.created_at || "",
    }));
  },
  /* 某一版正文 → 段落数组（剥 HTML） */
  async paras(sid, revisionNo) {
    const m = await ensureDraft(sid);
    if (!m.draftId) return [];
    const data = await apiGet(`/api/v1/author-drafts/${m.draftId}/revisions/${revisionNo}`);
    return htmlToParagraphs((data && data.revision && data.revision.content) || "");
  },
  diff: diffSentences,
};
