import { apiPost } from "./lib/client.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { WrDocs } from "./wr-doc-store.jsx";
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
  return wrContinueCandidates(generated);
}

export async function wrRequestRewrite({ sceneId, text, instruction }) {
  const sid = sceneId || fallbackSceneId();
  let backendId = null;
  try {
    backendId = sid && WsCatalog && WsCatalog.__backendSceneId ? await WsCatalog.__backendSceneId(sid) : null;
  } catch (e) {}
  if (!backendId) throw wrAiLocalError("no-scene");
  const data = await apiPost("/api/v1/passages/patch-candidates", {
    object_type: "scene",
    object_id: backendId,
    scene_id: backendId,
    source_excerpt: String(text || "").slice(0, 2000),
    issue_dimension: instruction,
  });
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
   失败只影响偏好学习，不打扰作者（正文已经按作者的选择改好了）。 */
export function wrDecidePatch(patch, pickIndex, accepted) {
  if (!patch || !patch.patchId) return;
  if (accepted) {
    const option = patch.options[pickIndex] || patch.options[0] || {};
    apiPost(`/api/v1/passage-patch-candidates/${patch.patchId}/accept`, { selected_option_id: option.option_id || "" }).catch(() => {});
  } else {
    apiPost(`/api/v1/passage-patch-candidates/${patch.patchId}/reject`, {}).catch(() => {});
  }
}
