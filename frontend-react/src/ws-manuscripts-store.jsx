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
import { adoptModuleListeners, retireModuleListeners } from "./lib/events.js";
import { createSubscribers } from "./lib/store-utils.js";
import { chapterStarted } from "./labels/catalog.js";

// chapterBackendId → { status: idle|loading|ready|error, detail, error, fetchedAt }
const manuCache = {};
const manuInflight = {};

/* 状态变化只通知 WsManuStore.subscribe 的订阅者（成稿中心各处都订阅它；以前同一条消息还发一个没人听的
   ws:manuscripts-loaded 窗口事件，复核 Q4-R4 删掉）。 */
const manuSubs = createSubscribers();
function dispatchManuscriptState() {
  manuSubs.notify();
}

/* 别处改了正文（AI 起草台采纳并归档、写作台提升为权威正文、运行本章跑完……）之后目录都会变（字数、场的状态），
   目录每次变化都广播 ws:catalog-changed：听它把已读到的快照都记成「可能过时」（只清读取时刻，照旧显示）——
   下一次带 maxAgeMs 的读取就会重读，而不是拿 30 秒内的旧快照充数（审计 F04-08）。 */
retireModuleListeners("ws-manuscripts-store");
const manuOnCatalogChanged = () => {
  Object.values(manuCache).forEach((hit) => { if (hit) hit.fetchedAt = 0; });
};
window.addEventListener("ws:catalog-changed", manuOnCatalogChanged);
adoptModuleListeners("ws-manuscripts-store", () => {
  window.removeEventListener("ws:catalog-changed", manuOnCatalogChanged);
});

/* 读失败的形状刻意不用 store-kit 的 toStoreError：成稿中心按错误码分支，码只取后端给的 error.code
   （没有就是 MANUSCRIPT_LOAD_FAILED，不拿 HTTP 状态顶替），也不关心离线标志。 */
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

/* 成稿中心左栏收不收这一章：不在「规划中」就收；「规划中」的章动了笔（labels/catalog.js 的 chapterStarted：
   有字，或者有一场在写 / 写完了）也收——与主页、章节编排读作「写作中」是同一条规则（复核 Q4-R2：以前只认
   写完 / 归档的场，一场正在写的规划中章在页头读作写作中，左栏里却没有它）。 */
function manuscriptChapterEligible(chapter) {
  return !!chapter && (chapter.state !== "planned" || chapterStarted(chapter));
}

function manuscriptDisplayState(state) {
  return state === "planned" ? "plan" : state;
}

const WsManuStore = {
  /**
   * 拉取权威章节聚合。状态是显式的，不再把“未加载”和“服务端失败”折成同一个 null。
   * 并发请求仍幂等去重；重试会从 error 重新进入 loading。
   * options.maxAgeMs：已读到的快照不超过这么久、之后目录也没变过（见 manuOnCatalogChanged）就直接用，不重读；
   * 不给（动作之后、生成导出前）就总是重读。
   */
  async refresh(chapterId, { maxAgeMs = 0 } = {}) {
    if (!chapterId) return null;
    if (manuInflight[chapterId]) return manuInflight[chapterId];
    const hit = manuCache[chapterId];
    if (maxAgeMs > 0 && hit && hit.status === "ready" && hit.fetchedAt && Date.now() - hit.fetchedAt < maxAgeMs) return hit;
    manuCache[chapterId] = { status: "loading", detail: null, error: null, fetchedAt: 0 };
    dispatchManuscriptState();
    manuInflight[chapterId] = (async () => {
      try {
        const detail = await apiGet(`/api/v1/chapter-manuscripts/${chapterId}`);
        manuCache[chapterId] = { status: "ready", detail, error: null, fetchedAt: Date.now() };
      } catch (e) {
        manuCache[chapterId] = { status: "error", detail: null, error: normalizedLoadError(e), fetchedAt: 0 };
      } finally {
        delete manuInflight[chapterId];
        dispatchManuscriptState();
      }
      return manuCache[chapterId];
    })();
    return manuInflight[chapterId];
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

  /** 终稿批准是项目级权威操作，不允许由目录 PATCH 冒充。
   *  「已通读」与「确认定稿」一次提交（批准 #10）：带上作者读到的那一份正文的哈希（成稿中心读到的章节聚合的
   *  body_hash）；读完之后正文又在别处变了，服务端 409，不会把没读过的那一版定下来。以前先单独 POST read-confirm，
   *  再 approve-final。没读到过这一章的聚合（没有哈希）就不提交。 */
  async approveFinal(projectId, chapterId, { readNote = "", revisionNotes = "" } = {}) {
    if (!projectId || !chapterId) throw new Error("缺少作品或章节标识，无法批准终稿。");
    const hit = manuCache[chapterId];
    const bodyHash = hit && hit.status === "ready" && hit.detail ? hit.detail.body_hash : "";
    if (!bodyHash) throw new Error("还没读到这一章的服务端正文，请刷新后再批准。");
    const result = await apiPost(
      `/api/v1/projects/${projectId}/chapters/${chapterId}/approve-final`,
      { revision_notes: revisionNotes, read_confirmation: { body_hash: bodyHash, note: readNote } },
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

  /** 同步读：{ completion, assembled, canonContinuity, missingSceneIds, scenes:[{sceneId, paras, live, charCount}] } | null */
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
  /* 任何一章的读取状态变了就通知；返回退订函数 */
  subscribe(fn) { return manuSubs.subscribe(fn); },
  invalidate(chapterId) {
    if (chapterId) { delete manuCache[chapterId]; }
    else { Object.keys(manuCache).forEach((k) => delete manuCache[k]); }
  },
};

export { WsManuStore, manuscriptChapterEligible, manuscriptDisplayState };
