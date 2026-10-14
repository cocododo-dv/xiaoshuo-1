/* ==========================================================
   labels/canon — 正史审核台的叫法（2026-10 从 ws-manuscripts-canon.jsx 挪出）
   ----------------------------------------------------------
   · CANON_EVENT_LABELS —— 候选事实的类型：键与后端 narrative/taxonomy.py 的 EVENT_TYPES 逐项相同
     （ws-manuscripts-compile.test.js 读后端源码比对）；
   · 提取结果 / 原因（后端 canon_continuity / prose_event_extractor 的枚举）与一场正史的状态 → 作者读得懂的话；
   · CANON_FACT_KIND_LABELS —— 硬质检认得的事实种类：键与后端 narrative/fact_vocabulary.py 的 FACT_KINDS 逐项相同
     （同一个测试读后端源码比对）。带槽的键写成「种类:槽」（has_item:铜钥匙），canonFactKeyLabel / canonFactValueLabel
     把它与值里的几个固定英文词（held / lost / dead …）翻成中文；别的键原样显示。
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

export const CANON_FACT_KIND_LABELS = {
  alive: "生死",
  location: "位置",
  has_item: "持有物品",
  missing_limb: "断肢",
  physical_state: "身体状况",
  appearance: "外貌",
  ability: "能力",
};

const CANON_FACT_VALUE_LABELS = { dead: "已死", alive: "活着", held: "持有", lost: "已失去", cannot: "不能", can: "能" };
const CANON_APPEARANCE_SLOT_LABELS = { hair: "发色", eyes: "瞳色" };

function splitFactKey(key) {
  const raw = String(key || "");
  const sep = raw.indexOf(":");
  const kind = (sep >= 0 ? raw.slice(0, sep) : raw).trim().toLowerCase();
  return { raw, kind, slot: sep >= 0 ? raw.slice(sep + 1).trim() : "" };
}

/* 事实键 → 给作者看的叫法：has_item:铜钥匙 → 持有物品 · 铜钥匙；不是硬质检的种类 → 原样 */
export function canonFactKeyLabel(key) {
  const { raw, kind, slot } = splitFactKey(key);
  const label = CANON_FACT_KIND_LABELS[kind];
  if (!label) return raw;
  if (!slot) return label;
  return `${label} · ${kind === "appearance" ? CANON_APPEARANCE_SLOT_LABELS[slot] || slot : slot}`;
}

/* 硬质检种类的固定取值翻成中文（held → 持有）；别的值原样 */
export function canonFactValueLabel(key, value) {
  const text = String(value ?? "");
  if (!CANON_FACT_KIND_LABELS[splitFactKey(key).kind]) return text;
  return CANON_FACT_VALUE_LABELS[text.trim().toLowerCase()] || text;
}

/* 手工补录时「事实键」的下拉建议：硬质检会核对的写法（值可以写中文，后端统一成规范写法） */
export const CANON_FACT_KEY_SUGGESTIONS = [
  { value: "alive", hint: "生死：值写「已死」" },
  { value: "location", hint: "位置：值写地名" },
  { value: "has_item:物品名", hint: "持有物品：把「物品名」换成东西的名字，值写「持有」或「失去」" },
  { value: "missing_limb", hint: "断肢：值写哪条肢体，如「右臂」" },
  { value: "physical_state", hint: "身体状况：如「昏迷」「失明」「苏醒」" },
  { value: "appearance:发色", hint: "外貌：发色或瞳色，值写颜色" },
  { value: "ability:能力名", hint: "能力：把「能力名」换成能力，值写「不能」或「恢复」" },
];

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
