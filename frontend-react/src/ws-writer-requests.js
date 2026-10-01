import { apiPost } from "./lib/client.js";
import { storeAlert } from "./lib/store-utils.js";
import { sceneApiId } from "./ws-scene-id.js";
import { WrDocs } from "./wr-doc-store.jsx";
import { copyGateAcceptMessage, isCopyGateError } from "./ws-copy-gate.js";
import { wrAiLocalError, wrContinueCandidates } from "./ws-writer-ai.js";

/* ==========================================================
   写作台的 AI 请求（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   · wrContinueMulti(instruction, sceneId)：AI 续写。一次 generate-set 业务意图
     （mode: continuation_variants），服务端按 动作推进 / 关系压力 / 悬念 生成三条
     「只推进下一拍、不改写作者现有正文」的续写（author_proposal_generate 模板）。
   · wrRequestRewrite({ sceneId, text, instruction, finding, sourceParagraphs })：选区改写（超过 WR_REWRITE_MAX_CHARS 就在
     本地拒绝），接 passages/patch-candidates（writer_passage_patch 节点，v4 每一版是几段字 paragraphs[]）。text 是选中的
     那几段、一段一行。返回 { results: [{ paragraphs, text, collapsed }], patch }——patch 是这一次候选的裁决把手，
     采纳 / 放弃经 wrDecidePatch 回传（服务端记下这一次候选的去留；采纳时再过一遍抄袭门）。
   · 抄袭门（与绑定的参考书原文连续相同；用了它的专名只提醒、不拦）：生成时每一版都被拦下 → 409，wrAiError 说成 kind copy；
     续写只丢了几版 → 列表上说丢了几版；采纳时被拦下 → 提示第几字要改（ws-copy-gate.js）。
   场景 id 一律由调用方显式传入：过去这里读一个由 WriterRoom 镜像出来的模块级「当前场景」，
   另一个模块级变量在两次渲染之间捎带待裁决的候选。
   服务端的失败原样抛出（ApiRequestError 带 code / status / details），由 wrAiError 翻译。
   ESM 模块，不写 window。
   ========================================================== */

/* 一次选区改写最多送多少字（按码点数，量的就是要送出去的那段字）。这是改写请求的预算，与批注能圈多少字
   （WR_ANNO_MAX_QUOTE）无关。超了就在本地说「选区太长，请分段改写」：不发请求、不截短——过去只把前 2000 字
   送去改，却把整个选区换掉，后面的字就这么没了。 */
export const WR_REWRITE_MAX_CHARS = 2000;

export async function wrContinueMulti(instruction, sceneId) {
  const sid = sceneId;
  if (!sid) throw wrAiLocalError("no-scene");
  let draftId = null;
  try { draftId = await WrDocs.draftId(sid); } catch (e) {}
  if (!draftId) throw wrAiLocalError("no-draft");
  const generated = await apiPost(`/api/v1/author-drafts/${draftId}/proposals/generate-set`, {
    mode: "continuation_variants",
    instruction: instruction || "续写下一段，自然承接当前正文",
  });
  const cands = wrContinueCandidates(generated);
  // 照抄了参考书的那几版服务端在生成时就丢掉了（一版都不剩才报错）：把丢掉的数目带回去，候选列表上说一句
  const copyBlocked = Number(generated && generated.reference_copy_blocked_count) || 0;
  return Object.defineProperty(cands, "copyBlocked", { value: copyBlocked, enumerable: false });
}

/* finding：深改面板交过来的那条发现（signal_id / dimension / issue / patch.candidate_category）。
   带着它时改写请求按维度走：后端按「对白潜台词」这种维度定修补类别与改写策略，
   不再是「润色」两个字；没有发现时维度是 author_instruction，指令本身走 instruction。
   也带上作者稿 id：后端把整场正文当上下文，补丁才接得上前后文。 */
/* 一版候选 → 段落：v4 的 paragraphs[]（每项再按换行拆一次）；还没同步提示词的安装照旧只给 replacement_text，按换行拆 */
export function wrOptionParagraphs(option) {
  const split = (value) => String(value == null ? "" : value).split(/\n+/).map((part) => part.trim()).filter(Boolean);
  if (option && Array.isArray(option.paragraphs) && option.paragraphs.length) {
    return option.paragraphs.flatMap((item) => (typeof item === "string" ? split(item) : []));
  }
  return split(option && option.replacement_text);
}

export async function wrRequestRewrite({ sceneId, text, instruction, finding = null, sourceParagraphs = null }) {
  const excerpt = String(text || "");
  const length = Array.from(excerpt).length;
  if (length > WR_REWRITE_MAX_CHARS) {
    throw Object.assign(wrAiLocalError("selection-too-long"), { details: { length, limit: WR_REWRITE_MAX_CHARS } });
  }
  const sid = sceneId;
  const backendId = await sceneApiId(sid);
  if (!backendId) throw wrAiLocalError("no-scene");
  let draftId = null;
  try { draftId = await WrDocs.draftId(sid); } catch (e) {}
  const body = {
    object_type: "scene",
    object_id: backendId,
    scene_id: backendId,
    source_excerpt: excerpt,
    issue_dimension: finding && finding.dimension ? String(finding.dimension) : "author_instruction",
    instruction: String(instruction || "").slice(0, 4000),
  };
  if (draftId) {
    body.source_draft_id = draftId;
    body.target_text_ref = `author_draft:${draftId}`;
  }
  if (finding) {
    if (finding.signal_id) body.quality_signal_id = String(finding.signal_id);
    if (finding.issue) body.issue_note = String(finding.issue).slice(0, 2000);
    if (finding.patch && finding.patch.candidate_category) body.candidate_category = finding.patch.candidate_category;
  }
  const data = await apiPost("/api/v1/passages/patch-candidates", body);
  const cand = data && data.candidate;
  const options = (cand && cand.replacement_options) || [];
  /* 选了几段、这一版却只有一段：它把段落挤成了一段——不给作者换进去（后端也不该给，这里再守一道） */
  const sourceCount = Number.isInteger(sourceParagraphs) ? sourceParagraphs : excerpt.split(/\n+/).filter((part) => part.trim()).length;
  const kept = [];
  let collapsedOut = 0;
  options.slice(0, 3).forEach((option) => {
    const paragraphs = wrOptionParagraphs(option);
    if (!paragraphs.length) return;
    if (sourceCount >= 2 && paragraphs.length === 1) { collapsedOut += 1; return; }
    kept.push({ option, result: { paragraphs, text: paragraphs.join("\n"), collapsed: !!option.collapsed } });
  });
  const patch = cand && cand.patch_id ? { patchId: cand.patch_id, options: kept.map((item) => item.option) } : null;
  if (!kept.length) {
    if (cand && cand.patch_id && options.length) wrDecidePatch({ patchId: cand.patch_id, options }, 0, false);
    throw Object.assign(wrAiLocalError("no-result"), { details: collapsedOut ? { reason: "paragraphs_collapsed" } : {} });
  }
  /* 结果与 patch.options 下标一一对应：采纳回传的是作者选的那一版的 option_id */
  return { results: kept.map((item) => item.result), patch };
}

/* 候选裁决回传：采纳 = accept 选中的那一版，关掉没采纳 = reject。
   一般的失败只是这次去留没记上，不打扰作者（正文已经按作者的选择改好了）。只有一种要说：采纳时被抄袭门拦下——
   那一版与参考书原文连续相同（生成之后换了绑定的书时会这样），它已经在正文里了，得告诉作者第几字要改。
   onRefused(message, error) 不给时走应用内提示。返回请求的 promise（测试用；调用方照旧可以不管）。 */
export function wrDecidePatch(patch, pickIndex, accepted, { onRefused = null } = {}) {
  if (!patch || !patch.patchId) return Promise.resolve();
  if (accepted) {
    const option = patch.options[pickIndex] || patch.options[0] || {};
    return apiPost(`/api/v1/passage-patch-candidates/${patch.patchId}/accept`, { selected_option_id: option.option_id || "" }).catch((error) => {
      if (!isCopyGateError(error)) return;
      const message = copyGateAcceptMessage(error);
      if (onRefused) onRefused(message, error);
      else storeAlert(null, message);
    });
  }
  return apiPost(`/api/v1/passage-patch-candidates/${patch.patchId}/reject`, {}).catch(() => {});
}
