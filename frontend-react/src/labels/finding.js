/* ==========================================================
   labels/finding — 诊断发现的叫法（2026-10 从 ws-quality-model.js 挪出）
   ----------------------------------------------------------
   统一的发现记录（signal_id / label / dimension / severity / issue / recommendation……）在三处显示：
   文学质量、成稿中心的诊断页签、写作台深改抽屉。它们共用这里的词：
   · 严重程度的中文名与语气色（ws-ui 的 tone）；
   · 规则维度的中文名——与后端 literary_quality/dimensions.py 的 DIMENSION_LABELS 逐字相同
     （ws-quality.test.jsx 读后端源码比对）。服务端给了 finding.label 就用服务端的；
     AI 深评 / 通读的维度（information_rhythm 这类）一律由服务端给名字。
   纯数据与纯函数：不读 store、不碰 React、不写 window。
   ========================================================== */

/* 严重程度：从重到轻（文学质量「最低级别」下拉的顺序） */
export const FINDING_SEVERITY_ORDER = ["blocking", "revision", "taste", "info"];

export const FINDING_SEVERITY_META = {
  blocking: { label: "阻断", tone: "danger" },
  revision: { label: "修订", tone: "accent" },
  taste: { label: "审美", tone: "warn" },
  info: { label: "信息", tone: "info" },
};

/* 认不出的严重程度说「其他」（空值不说），语气按「信息」 */
export function findingSeverityLabel(severity) {
  const meta = FINDING_SEVERITY_META[severity];
  return meta ? meta.label : severity ? "其他" : "";
}

export function findingSeverityTone(severity) {
  return (FINDING_SEVERITY_META[severity] || FINDING_SEVERITY_META.info).tone;
}

/* 规则维度（后端 QUALITY_DIMENSIONS）的中文名，顺序与后端相同 */
export const RULE_DIMENSION_LABELS = {
  model_voice: "模型腔",
  image_homogeneity: "意象同质",
  repetitive_action: "动作重复",
  expository_dialogue: "说明式对白",
  no_choice_scene: "无抉择场景",
  summary_ending: "概述式收尾",
  choice_pressure: "抉择压力",
  ending_drive: "收束驱动",
  template_action_reuse: "模板动作复用",
  image_field_reuse: "意象场复用",
  syntax_monotony: "句式单调",
  false_clarity: "虚假清晰",
  painless_scene: "无痛场景",
  decorative_imagery: "装饰性意象",
  dialogue_as_report: "对白即汇报",
  over_explained_motive: "过度解释动机",
  false_poetic_closure: "伪诗意收束",
  perception_filter: "感知过滤",
  self_repetition: "自我重复",
  conflict_too_clean: "冲突过净",
};

/* 后端新加的维度前端还没有中文名时，不把英文键摊给作者 */
export function ruleDimensionLabel(dimension) {
  return RULE_DIMENSION_LABELS[dimension] || "其他维度";
}

/* 一条发现的名字：服务端给的 label 优先，其次规则维度的中文名 */
export function findingLabel(finding) {
  return (finding && finding.label) || ruleDimensionLabel(finding && finding.dimension);
}
