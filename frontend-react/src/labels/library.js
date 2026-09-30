/* ==========================================================
   labels/library — 资料库（故事圣经）的类别与世界条目类型（2026-09-30 从 ws-library-data.jsx 挪出）
   资料库的 store、派生层、图谱布局、表单与写作台的悬停卡都要这几张表；放在叶子模块里，纯函数与布局
   不必为了一张常量表牵出整个 store（store 一 import 就要作品列表）。纯 ESM，不写 window。
   ========================================================== */

/* 2026-09-21：原型里还有「参考 / 风格 / 知识」三类，适配层从来不产出它们（风格参考有自己的页面，
   知识簇已在 2026-09 减法里删除），在这些类别里新建会以「概念」落进世界。只保留真有数据的三类。 */
export const LIB_CATS = [
  { id: "people", label: "人物",   icon: "Users",  accent: "crimson", noun: "位角色" },
  { id: "world",  label: "世界",   icon: "MapPin", accent: "gold",    noun: "处设定" },
  { id: "events", label: "大事记", icon: "Clock",  accent: "slate",   noun: "起事件" },
];

/* 世界条目的类型：后端枚举 ↔ 中文。编辑时反查，写回 entity.kind。 */
export const LIB_KIND_LABEL = { location: "地点", item: "物品", faction: "机构", concept: "概念" };
export const LIB_KIND_OPTIONS = Object.keys(LIB_KIND_LABEL).map(value => ({ value, label: LIB_KIND_LABEL[value] }));
