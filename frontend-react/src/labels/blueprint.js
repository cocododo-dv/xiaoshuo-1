/* ==========================================================
   labels/blueprint.js — AI 写前笔记的叫法（场景蓝图与人物压力的格子、每一格的来源）
   后端字段见 services/scene_blueprint.py（SCENE_BLUEPRINT_FIELDS / SCENE_BLUEPRINT_FACT_FIELDS）与
   services/near_final_planning.py（CHARACTER_PRESSURE_FIELDS）；来源判法见 services/scene_blueprint_view.py。
   ========================================================== */

export const BLUEPRINT_FIELD_LABELS = Object.freeze({
  visible_desire: "看得见的想要",
  forced_choice: "被迫的选择",
  price_paid: "付出的代价",
  information_release: "放出的信息",
  relationship_turn: "关系转折",
  image_anchor: "意象",
  ending_action: "收尾动作",
  ending_function: "结尾要完成什么",
  next_scene_pull: "下一场的牵引",
  anti_summary_rule: "收尾禁忌",
});

export const PRESSURE_FIELD_LABELS = Object.freeze({
  surface_goal: "表面目标",
  hidden_fear: "藏着的恐惧",
  wrong_belief: "错误信念",
  shame_point: "羞耻点",
  avoidance_strategy: "回避方式",
  relationship_debt: "关系欠账",
  current_mask: "此刻的面具",
});

/* 每一格从哪来：plan = 第 10 步写下的那一格落地而来；ai = 第 10 步里没有，AI 加的；author = 你的版本 */
export const BLUEPRINT_ORIGIN_LABELS = Object.freeze({
  plan: "照第 10 步",
  ai: "AI 加的",
  author: "我的版本",
});

export function blueprintFieldLabel(key) {
  return BLUEPRINT_FIELD_LABELS[key] || PRESSURE_FIELD_LABELS[key] || key;
}
