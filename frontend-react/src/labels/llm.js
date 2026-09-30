/* ==========================================================
   labels/llm — 模型记账状态与模型节点的中文叫法（2026-09-29 从 ws-labels.js 拆出）。纯函数，不写 window。
   ========================================================== */

/* ---------- 成本看板 ---------- */

const ACCOUNTING_STATUS_LABELS = {
  settled: { label: "已结算", tone: "neutral" },
  reserved: { label: "预留中", tone: "info" },
  failed: { label: "失败", tone: "danger" },
  released: { label: "已释放", tone: "neutral" },
  rejected: { label: "发出前被拦下", tone: "warn" },
  usage_exceeds_reservation: { label: "用量超出预留", tone: "warn" },
};

export function accountingStatusMeta(status) {
  return ACCOUNTING_STATUS_LABELS[status] || { label: status ? "其他" : "—", tone: "neutral" };
}

/* 模型节点（后端 llm_node_registry 的 node_id）的中文名——唯一的一张表（2026-09-30 批准 #19：以成本看板这套为底，
   设置 · 高级路由也读它；以前设置另有一份叫法不同的表，同一个节点在两页叫两个名字）。与后端注册表逐个对上
   （ws-labels.test.js 读后端源码比对）；批准 #24b 删掉的四个保留节点不再留名字。认不出的节点返回空串，
   调用方回落到等宽显示原始 id（机器标识，放在 title 里也行）。 */
export const LLM_NODE_LABELS = {
  extraction: "通用抽取",
  snowflake_step_candidates: "构思 · 方向候选",
  snowflake_step_generate: "构思 · 生成本步",
  snowflake_workspace_assistant: "构思 · 教练",
  snowflake_scene_triage: "构思 · 场景分诊",
  snowflake_chapter_plan: "构思 · 分章建议",
  style_ref_paragraph_classify_anchor: "参考书 · 锚定段落分类",
  style_ref_paragraph_classify_bulk: "参考书 · 段落分类",
  style_ref_extract_language: "参考书 · 语言层抽取",
  style_ref_extract_narrative: "参考书 · 叙事层抽取",
  style_ref_extract_scene: "参考书 · 场景层抽取",
  style_ref_extract_theme: "参考书 · 主题层抽取",
  style_ref_synthesize_profile: "参考书 · 写文风卡",
  style_ref_protected_terms: "参考书 · 识别本书专名",
  style_ref_tag_windows: "参考书 · 给片段打标签",
  scene_blueprint: "场景蓝图",
  character_pressure_blueprint: "人物压力蓝图",
  chapter_story_architecture: "章节故事架构",
  chapter_scene_plan_candidates: "章节场景候选",
  chapter_scene_plan_fill: "章节场景补全",
  chapter_plan_review: "章节规划评审",
  neutral_draft: "初稿",
  style_draft: "风格稿",
  style_patch: "风格修补",
  scene_literary_rewrite: "文学改写",
  hard_qc: "硬质检",
  soft_qc: "软质检",
  near_final_acceptance_review: "准定稿评审",
  chapter_near_final_review: "章节准定稿评审",
  writer_passage_patch: "写作台 · 段落修补",
  writer_deep_review: "写作台 · 深度审读",
  author_proposal_generate: "写作台 · 修改提案",
};

export function llmNodeLabel(nodeId) {
  return LLM_NODE_LABELS[nodeId] || "";
}
