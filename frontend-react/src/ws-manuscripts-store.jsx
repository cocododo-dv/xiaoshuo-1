/* ==========================================================
   WsManuStore — 成稿中心正文 store（Wave 1 · 结果闭环治理 §5.2）
   ----------------------------------------------------------
   唯一正文来源 = 后端章节聚合（GET /api/v1/chapter-manuscripts/{chapter_id}，
   服务端以 FinalScene 归档行为源）。localStorage 的 wr-doc:* 是写作器的
   编辑缓存，不再作为「成稿」来源——清缓存不丢稿的前提是稿在后端。
   形态同其余 store：同步内存缓存 + 异步 refresh，视图零等待读 body()。
   键 = 后端 chapter_id（目录卡的 backendId），不是 FE slug。
   ========================================================== */

import { apiGet, apiPatch, apiPost } from "./lib/client.js";

// chapterBackendId → { status: idle|loading|ready|error, detail, error }
const manuCache = {};
const manuInflight = {};

function dispatchManuscriptState(chapterId, status) {
  try {
    window.dispatchEvent(new CustomEvent("ws:manuscripts-loaded", {
      detail: { chapterId, status },
    }));
  } catch (e) {}
}

function normalizedLoadError(error) {
  return {
    code: (error && error.code) || "MANUSCRIPT_LOAD_FAILED",
    message: (error && error.message) || "服务端正文加载失败。",
  };
}

function toParas(content) {
  return String(content || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function manuscriptChapterEligible(chapter) {
  if (!chapter) return false;
  if (chapter.state !== "planned") return true;
  if (Number(chapter.words && chapter.words.cur) > 0) return true;
  return (chapter.scenes || []).some((scene) => ["done", "archived"].includes(scene.state));
}

function manuscriptDisplayState(state) {
  return state === "planned" ? "plan" : state;
}

const WsManuStore = {
  /**
   * 拉取权威章节聚合。状态是显式的，不再把“未加载”和“服务端失败”折成同一个 null。
   * 并发请求仍幂等去重；重试会从 error 重新进入 loading。
   */
  async refresh(chapterId) {
    if (!chapterId) return null;
    if (manuInflight[chapterId]) return manuInflight[chapterId];
    manuCache[chapterId] = { status: "loading", detail: null, error: null };
    dispatchManuscriptState(chapterId, "loading");
    manuInflight[chapterId] = (async () => {
      try {
        const detail = await apiGet(`/api/v1/chapter-manuscripts/${chapterId}`);
        manuCache[chapterId] = { status: "ready", detail, error: null };
      } catch (e) {
        manuCache[chapterId] = { status: "error", detail: null, error: normalizedLoadError(e) };
      } finally {
        delete manuInflight[chapterId];
        dispatchManuscriptState(chapterId, manuCache[chapterId].status);
      }
      return manuCache[chapterId];
    })();
    return manuInflight[chapterId];
  },

  /** 作者显式生成/刷新章节汇总；写成功后立即重拉成稿详情，视图不拿旧缓存冒充结果。 */
  async aggregate(chapterId) {
    if (!chapterId) throw new Error("缺少章节标识，无法生成章节汇总。");
    const result = await apiPost(`/api/v1/chapters/${chapterId}/runtime/aggregate/final`, {});
    delete manuCache[chapterId];
    const loaded = await this.refresh(chapterId);
    if (!loaded || loaded.status !== "ready") {
      throw new Error((loaded && loaded.error && loaded.error.message) || "章节汇总已生成，但重新加载服务端正文失败。");
    }
    return result;
  },

  /**
   * 章节流转必须等待服务端确认，不能只改本地目录状态。
   * review/draft 仍由目录端点维护；approved 只能走项目终稿闸门。
   */
  async setReviewState(projectId, chapterId, state) {
    if (!projectId || !chapterId) throw new Error("缺少作品或章节标识，无法更新审阅状态。");
    if (!['review', 'draft'].includes(state)) throw new Error("审阅状态无效。");
    return apiPatch(`/api/v2/projects/${projectId}/catalog/chapters/${chapterId}`, { state });
  },

  /** 明确记录作者已经通读当前服务端正文；批准接口会复核同一正文哈希。 */
  async confirmRead(projectId, chapterId, note = "") {
    if (!projectId || !chapterId) throw new Error("缺少作品或章节标识，无法确认通读。");
    return apiPost(`/api/v1/projects/${projectId}/chapters/${chapterId}/read-confirm`, { note });
  },

  /** 终稿批准是项目级权威操作，不允许由目录 PATCH 冒充。 */
  async approveFinal(projectId, chapterId, revisionNotes = "") {
    if (!projectId || !chapterId) throw new Error("缺少作品或章节标识，无法批准终稿。");
    const result = await apiPost(
      `/api/v1/projects/${projectId}/chapters/${chapterId}/approve-final`,
      { revision_notes: revisionNotes },
    );
    this.invalidate(chapterId);
    return result;
  },

  /** 重开终稿会由服务端级联撤销该章及其后的批准链，并留下审计记录。 */
  async reopenFinal(projectId, chapterId, reason) {
    if (!projectId || !chapterId) throw new Error("缺少作品或章节标识，无法重新打开终稿。");
    const cleanReason = String(reason || "").trim();
    if (!cleanReason) throw new Error("请填写重新打开终稿的原因。");
    const result = await apiPost(
      `/api/v1/projects/${projectId}/chapters/${chapterId}/reopen-final`,
      { reason: cleanReason },
    );
    this.invalidate(chapterId);
    return result;
  },

  /** 正文抽取结果只有经过作者裁决，才会成为后续生成可见的正史事实。 */
  async decideCanonCandidate(projectId, chapterId, candidateId, decision) {
    if (!projectId || !chapterId || !candidateId) throw new Error("缺少正史候选定位信息。");
    const result = await apiPost(
      `/api/v1/projects/${projectId}/canon/candidates/${candidateId}/decision`,
      decision,
    );
    this.invalidate(chapterId);
    const loaded = await this.refresh(chapterId);
    if (!loaded || loaded.status !== "ready") {
      throw new Error((loaded && loaded.error && loaded.error.message) || "候选已处理，但正史状态刷新失败。");
    }
    return result;
  },

  /** 所有候选裁决结束后，由作者显式确认整场事实清单已经核对完整。 */
  async verifySceneCanon(projectId, chapterId, sceneId, verification) {
    if (!projectId || !chapterId || !sceneId) throw new Error("缺少场景定位信息，无法确认正史。");
    const result = await apiPost(
      `/api/v1/projects/${projectId}/canon/scenes/${sceneId}/verify`,
      verification,
    );
    this.invalidate(chapterId);
    const loaded = await this.refresh(chapterId);
    if (!loaded || loaded.status !== "ready") {
      throw new Error((loaded && loaded.error && loaded.error.message) || "场景已确认，但正史状态刷新失败。");
    }
    return result;
  },

  /** 作者可从当前终稿的原文证据手工补录模型漏掉的持久事实。 */
  async createCanonCandidate(projectId, chapterId, sceneId, candidate) {
    if (!projectId || !chapterId || !sceneId) throw new Error("缺少场景定位信息，无法补录事实。");
    const result = await apiPost(
      `/api/v1/projects/${projectId}/canon/scenes/${sceneId}/candidates`,
      candidate,
    );
    this.invalidate(chapterId);
    const loaded = await this.refresh(chapterId);
    if (!loaded || loaded.status !== "ready") {
      throw new Error((loaded && loaded.error && loaded.error.message) || "事实已补录，但正史状态刷新失败。");
    }
    return result;
  },

  /** 作者主动请求一次真实模型提取；自动提取关闭/失败时仍有明确恢复入口。 */
  async extractSceneCanon(projectId, chapterId, sceneId) {
    if (!projectId || !chapterId || !sceneId) throw new Error("缺少场景定位信息，无法提取事实。");
    const result = await apiPost(
      `/api/v1/projects/${projectId}/canon/scenes/${sceneId}/extract`,
      {},
    );
    this.invalidate(chapterId);
    const loaded = await this.refresh(chapterId);
    if (!loaded || loaded.status !== "ready") {
      throw new Error((loaded && loaded.error && loaded.error.message) || "提取已完成，但正史状态刷新失败。");
    }
    return result;
  },

  /** 同步读：{ completion, assembled, aggregate, scenes:[{sceneId, paras, live, charCount}] } | null */
  body(chapterId) {
    const hit = manuCache[chapterId];
    if (!hit || hit.status !== "ready" || !hit.detail) return null;
    const detail = hit.detail;
    const scenes = (detail.scenes || []).map((entry) => {
      const finalScene = entry.final_scene;
      const live = !!(finalScene && (finalScene.content || "").trim());
      return {
        sceneId: entry.scene_id,
        sceneSeq: entry.scene_seq,
        title: entry.title || entry.scene_title || "",
        live,
        paras: live ? toParas(finalScene.content) : [],
        charCount: finalScene ? finalScene.char_count || 0 : 0,
      };
    });
    return {
      completion: detail.completion_status || "empty",
      assembled: detail.assembled || null,
      aggregate: detail.aggregate || null,
      canonContinuity: detail.canon_continuity || null,
      missingSceneIds: (detail.assembled && detail.assembled.missing_scene_ids) || [],
      scenes,
    };
  },

  /** 同步状态快照；视图只能在 ready 时把 body 当作服务端事实。 */
  snapshot(chapterId) {
    const hit = chapterId && manuCache[chapterId];
    if (!hit) return { status: "idle", body: null, error: null };
    return {
      status: hit.status,
      body: hit.status === "ready" ? this.body(chapterId) : null,
      error: hit.error || null,
    };
  },

  /** 作品切换/归档后失效重拉 */
  invalidate(chapterId) {
    if (chapterId) { delete manuCache[chapterId]; }
    else { Object.keys(manuCache).forEach((k) => delete manuCache[k]); }
  },
};

export { WsManuStore, manuscriptChapterEligible, manuscriptDisplayState };
