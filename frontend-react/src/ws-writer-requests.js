import { apiPost } from "./lib/client.js";
import { storeAlert } from "./lib/store-utils.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WrDocs } from "./wr-doc-store.jsx";
import { copyGateAcceptMessage, isCopyGateError } from "./ws-copy-gate.js";
import { wrAiLocalError, wrContinueCandidates, wrIsOfflinePlaceholder } from "./ws-writer-ai.js";

/* ==========================================================
   写作台的 AI 请求（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   · wrContinueMulti(instruction, sceneId)：AI 续写。一次 generate-set 业务意图
     （mode: continuation_variants），服务端按 动作推进 / 关系压力 / 悬念 生成三条
     「只推进下一拍、不改写作者现有正文」的续写（author_proposal_generate 模板）。
   · wrRequestRewrite({ sceneId, text, instruction })：选区改写，接 passages/patch-candidates
     （writer_passage_patch 节点）。返回 { texts, patch }——patch 是这一次候选的裁决把手，
     采纳 / 放弃经 wrDecidePatch 回传（作者偏好画像的学习闭环）。
   · 抄袭门（与绑定的参考书原文连续相同；用了它的专名只提醒、不拦）：生成时每一版都被拦下 → 409，wrAiError 说成 kind copy；
     续写只丢了几版 → 列表上说丢了几版；采纳时被拦下 → 提示第几字要改（ws-copy-gate.js）。
   场景 id 一律由调用方显式传入：过去这里读一个由 WriterRoom 镜像出来的模块级「当前场景」，
   另一个模块级变量在两次渲染之间捎带待裁决的候选。只有单测 / 旧调用不传场景时，
   才回落到目录里「现在在写的那一场」。
   服务端的失败原样抛出（ApiRequestError 带 code / status / details），由 wrAiError 翻译。
   ESM 模块，不写 window。
   ========================================================== */

function fallbackSceneId() {
  try {
    const hit = WsCatalog ? WsCatalog.writingScene() : null;
    return hit && hit.scene ? hit.scene.sid : null;
  } catch (e) {
    return null;
  }
}

export async function wrContinueMulti(instruction, sceneId) {
  const sid = sceneId || fallbackSceneId();
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
   带着它时改写请求按维度走：后端的修补类别、改写策略与偏好画像学的是「对白潜台词」这种维度，
   不再是「润色」两个字；没有发现时维度是 author_instruction，指令本身走 instruction。
   也带上作者稿 id：后端把整场正文当上下文，补丁才接得上前后文。 */
export async function wrRequestRewrite({ sceneId, text, instruction, finding = null }) {
  const sid = sceneId || fallbackSceneId();
  let backendId = null;
  try {
    backendId = sid && WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
  } catch (e) {}
  if (!backendId) throw wrAiLocalError("no-scene");
  let draftId = null;
  try { draftId = await WrDocs.draftId(sid); } catch (e) {}
  const body = {
    object_type: "scene",
    object_id: backendId,
    scene_id: backendId,
    source_excerpt: String(text || "").slice(0, 2000),
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
  if (wrIsOfflinePlaceholder(cand && cand.rationale)) throw wrAiLocalError("no-model");
  if (!options.length) throw wrAiLocalError("no-result");
  return {
    texts: options.slice(0, 3).map((option) => String(option.replacement_text || "").trim()).filter(Boolean),
    patch: { patchId: cand.patch_id, options },
  };
}

/* 候选裁决回传：采纳 = accept 选中的那一版，关掉没采纳 = reject。
   一般的失败只影响偏好学习，不打扰作者（正文已经按作者的选择改好了）。只有一种要说：采纳时被抄袭门拦下——
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
