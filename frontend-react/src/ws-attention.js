import { useCatalogChapters } from "./ws-catalog.jsx";
import { chapterLabel, chapterStage, chapterStarted, sceneLabel } from "./labels/catalog.js";

/* ==========================================================
   等你处理 —— 从目录现算的两类提醒（2026-10-21 取代待办收件箱）
   ----------------------------------------------------------
   · AI 稿被管线停下、等你处理的场：目录场的 work.runStatus（后端 scene_run_states.scene_status）是下面的停点之一，
     而且这一场还没写完（state ≠ done：作者在写作台采纳归档了就不再提醒）。去处：AI 起草台的那一场（ws:scene-enqueue）。
   · 写齐了、等你批准的章：chapterStage = review，成稿中心左栏也收这一章（与成稿中心的「等你批准」同一条规则）。
     去处：成稿中心的那一章（ws:manu-chapter）。按章依次定稿：只有当前章能批，别的章到了成稿中心会说先批哪一章。
   没有请求、没有缓存：主页「等你处理」与侧栏徽标都读 WsCatalog，目录一变就跟着变；源头处理完自己消失，没有「划掉」。
   收件箱为什么退役：早就没有生产者往里投卡，剩下的实时卡要么与主页、构思、章节编排重复，要么在去掉「送审」之后
   失效（「待你批准」读的是没人再写的目录状态），还有一类拿错了字数口径、成了误报。
   ========================================================== */

/* 管线的停点 → 标签。进行中的步位（bundle_built、near_final…）与归档不在这里；
   tone：等你拍板的是 accent，要重写 / 重新规划的是 warn */
const ATTN_SCENE_STOPS = {
  awaiting_candidate_selection: { label: "等你终选", tone: "accent" },
  trial_draft_ready: { label: "试写稿待读", tone: "accent" },
  quality_warning_pending_acceptance: { label: "等你采纳", tone: "accent" },
  human_review_required: { label: "等你审阅", tone: "accent" },
  hard_qc_partial_rewrite_required: { label: "要局部重写", tone: "warn" },
  hard_qc_full_rewrite_required: { label: "要整场重写", tone: "warn" },
  soft_qc_patch_required: { label: "要小修", tone: "warn" },
  needs_replan: { label: "要重新规划", tone: "warn" },
};

/* 被管线停下、还没写完的场，按书序 */
function attentionScenes(chapters) {
  const out = [];
  (chapters || []).forEach((chapter) => {
    ((chapter && chapter.scenes) || []).forEach((scene, index) => {
      const stop = ATTN_SCENE_STOPS[scene && scene.work && scene.work.runStatus];
      if (!stop || scene.state === "done") return;
      out.push({
        key: `scene:${scene.sid}`,
        sid: scene.sid,
        label: stop.label,
        tone: stop.tone,
        text: sceneLabel(chapter, index, scene, { withTitle: true }),
      });
    });
  });
  return out;
}

/* 写齐了、等你批准的章，按书序（成稿中心左栏收不收一章：不在「规划中」，或者「规划中」但已动笔） */
function attentionChapters(chapters) {
  return (chapters || [])
    .filter((chapter) => chapter && chapterStage(chapter) === "review"
      && (chapter.state !== "planned" || chapterStarted(chapter)))
    .map((chapter) => ({
      key: `chapter:${chapter.id}`,
      id: chapter.id,
      current: !!chapter.current,
      text: chapterLabel(chapter),
    }));
}

function attentionOf(chapters) {
  const scenes = attentionScenes(chapters);
  const approvals = attentionChapters(chapters);
  return { scenes, chapters: approvals, total: scenes.length + approvals.length };
}

/* 侧栏徽标：数字挂在处理它的那一页上（AI 起草台 = 停下的场，成稿中心 = 等你批准的章）；
   作家模式的侧栏里没有这两页，合计挂在主页上——主页的「等你处理」把它们列出来 */
function attentionBadges(att, mode) {
  const badges = {};
  if (att.scenes.length) badges.scene = String(att.scenes.length);
  if (att.chapters.length) badges.manuscripts = String(att.chapters.length);
  if (mode !== "advanced" && att.total) badges.home = String(att.total);
  return badges;
}

/* 订阅目录：当前作品的「等你处理」 */
function useAttention() {
  return attentionOf(useCatalogChapters());
}

export { ATTN_SCENE_STOPS, attentionBadges, attentionChapters, attentionOf, attentionScenes, useAttention };
