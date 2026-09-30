/* ==========================================================
   labels/canon — 正史审核台的叫法（2026-10 从 ws-manuscripts-canon.jsx 挪出）
   ----------------------------------------------------------
   · CANON_EVENT_LABELS —— 候选事实的类型：键与后端 narrative/taxonomy.py 的 EVENT_TYPES 逐项相同
     （ws-manuscripts-compile.test.js 读后端源码比对）；
   · 提取结果 / 原因（后端 canon_continuity / prose_event_extractor 的枚举）与一场正史的状态 → 作者读得懂的话。
   纯数据：不读 store、不碰 React、不写 window。
   ========================================================== */

export const CANON_EVENT_LABELS = {
  character_state: "人物状态",
  character_learns: "人物获知",
  location_change: "位置变化",
  relation_change: "关系变化",
  item_change: "物品变化",
  foreshadow_plant: "伏笔埋设",
  foreshadow_reinforce: "伏笔强化",
  foreshadow_resolve: "伏笔兑现",
};

export const CANON_EXTRACTION_OUTCOMES = {
  not_invoked: "还没提取",
  completed_events: "已提取出候选事实",
  completed_empty: "模型没找到持久变化",
  facts_unchanged: "事实没有变化",
  parse_failed: "模型的回复无法解析",
  provider_failed: "模型调用失败",
  rejected_before_dispatch: "请求在发出前被拦下",
};

export const CANON_EXTRACTION_REASONS = {
  awaiting_extraction: "等待提取",
  runner_disabled: "自动提取没有开启",
  feature_disabled: "自动提取没有开启",
  offline_unsupported: "当前没有可用的模型",
  pre_dispatch_rejection: "额度或预算拦下了请求",
  provider_call_failed: "模型服务报错",
  invalid_llm_response: "回复格式不对",
};

export const CANON_STATUS_LABELS = {
  synced: "已提交正史",
  pending_review: "等待作者裁决",
  pending_extraction: "等待提取或人工确认",
  degraded: "自动提取降级",
  missing_final: "缺少终稿",
};
