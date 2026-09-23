// ws-labels：章 / 场的叫法与章节状态词表（纯函数）。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  LLM_NODE_LABELS,
  PARAGRAPH_TYPE_LABELS,
  STYLE_DIMENSIONS,
  STYLE_DIMENSION_LABELS,
  STYLE_LAYER_ORDER,
  STYLE_MOOD_TAGS,
  STYLE_SITUATION_TAGS,
  WINDOW_POSITION_LABELS,
  CHAPTER_STATE_META,
  CHAPTER_STATE_ORDER,
  SCENE_STATE_META,
  SCENE_STATE_ORDER,
  accountingStatusMeta,
  chapterLabel,
  chapterHeading,
  chapterLabelById,
  chapterOwnTitle,
  chapterStateMeta,
  isPlaceholderChapterTitle,
  llmNodeLabel,
  manuscriptStage,
  preferenceHintLabel,
  reviewSourceLabel,
  sceneLabel,
  sceneLabelById,
  sceneNoLabel,
  sceneStateMeta,
  styleDimensionLabel,
  styleJobKindLabel,
  styleLayerOf,
} from "./ws-labels.js";

const BOOK = [
  {
    id: "ch01", backendId: "P_CH01", n: "01", title: "第 1 章", state: "planned", words: { cur: 3200 },
    scenes: [{ sid: "s-a", backendId: "P_CH01_SC01", title: "码头交班", state: "done" }, { sid: "s-b", backendId: "P_CH01_SC02", title: "夜渡", state: "todo" }],
  },
  { id: "ch02", backendId: "P_CH06", n: "02", title: "雾里的灯", state: "todo", words: { cur: 0 }, scenes: [] },
];

describe("章节状态词表", () => {
  it("覆盖后端全部六个目录状态，认不出的状态回落到规划中而不是抛错", () => {
    CHAPTER_STATE_ORDER.forEach((state) => expect(CHAPTER_STATE_META[state].label).toBeTruthy());
    expect(chapterStateMeta("todo").label).toBe("待写");
    expect(chapterStateMeta("plan")).toBe(CHAPTER_STATE_META.planned);
    expect(chapterStateMeta("something-new")).toBe(CHAPTER_STATE_META.planned);
  });

  it("成稿阶段：有字的规划 / 待写章算写作中，空章保持原状", () => {
    expect(manuscriptStage(BOOK[0])).toBe("writing");
    expect(manuscriptStage(BOOK[1])).toBe("todo");
    expect(manuscriptStage({ state: "planned", words: { cur: 0 }, scenes: [{ state: "archived" }] })).toBe("writing");
    expect(manuscriptStage({ state: "planned", words: { cur: 0 }, scenes: [] })).toBe("planned");
    expect(manuscriptStage({ state: "review" })).toBe("review");
    expect(manuscriptStage({ state: "approved", words: { cur: 0 } })).toBe("approved");
  });

  it("一个状态只有一个叫法：没有第二列「短称」（主页图例以前叫在写 / 定稿，别处叫写作中 / 已定稿）", () => {
    CHAPTER_STATE_ORDER.forEach((state) => expect(Object.keys(CHAPTER_STATE_META[state]).sort()).toEqual(["label", "tone"]));
    expect(CHAPTER_STATE_META.approved.label).toBe("已定稿");
    expect(CHAPTER_STATE_META.writing.label).toBe("写作中");
  });
});

describe("场景状态词表", () => {
  it("三态各一个词；写作台的 active 与起草台的 archived 按同一个词显示，认不出的回落到待写", () => {
    expect(SCENE_STATE_ORDER.map((state) => SCENE_STATE_META[state].label)).toEqual(["已完成", "写作中", "待写"]);
    expect(SCENE_STATE_ORDER.map((state) => SCENE_STATE_META[state].tone)).toEqual(["ok", "accent", "neutral"]);
    expect(sceneStateMeta("active")).toBe(SCENE_STATE_META.writing);
    expect(sceneStateMeta("archived")).toBe(SCENE_STATE_META.done);
    expect(sceneStateMeta("something-new")).toBe(SCENE_STATE_META.todo);
    expect(sceneStateMeta(undefined)).toBe(SCENE_STATE_META.todo);
  });
});

describe("章 / 场的叫法", () => {
  it("章号去掉前导零；占位章名不重复拼接", () => {
    expect(chapterLabel(BOOK[0])).toBe("第 1 章");
    expect(chapterLabel(BOOK[1])).toBe("第 2 章 · 雾里的灯");
    expect(chapterLabel(BOOK[1], { withTitle: false })).toBe("第 2 章");
    expect(isPlaceholderChapterTitle("第三章")).toBe(true);
    expect(isPlaceholderChapterTitle(" 第 12 章 ")).toBe(true);
    expect(isPlaceholderChapterTitle("第三章的灯")).toBe(false);
    expect(isPlaceholderChapterTitle("")).toBe(false);
  });

  it("占位章名还包括手建章的「未命名 / 未命名章节」和 07 章表的「（待补）」；列表行的两段：真章名旁放章号，占位时只写章号", () => {
    ["未命名", "未命名章节", "（待补）"].forEach((t) => expect(isPlaceholderChapterTitle(t)).toBe(true));
    expect(isPlaceholderChapterTitle("未命名的信")).toBe(false);
    expect(chapterOwnTitle(BOOK[0])).toBe("");
    expect(chapterOwnTitle(BOOK[1])).toBe("雾里的灯");
    expect(chapterHeading(BOOK[0])).toEqual({ num: "第 1 章", title: "" });
    expect(chapterHeading({ n: "07", title: "未命名章节" })).toEqual({ num: "第 7 章", title: "" });
    expect(chapterHeading(BOOK[1])).toEqual({ num: "第 2 章", title: "雾里的灯" });
  });

  it("场的坐标只有一种写法「第 N 章 · 第 M 场」，没有 CH / SC 缩写；场号缺失时不编一个「第 0 场」", () => {
    expect(sceneLabel(BOOK[0], 2)).toBe("第 1 章 · 第 3 场");
    expect(sceneLabel({ n: "12" }, -1)).toBe("第 12 章");
    expect(sceneNoLabel(0)).toBe("第 1 场");
    expect(sceneNoLabel(undefined)).toBe("");
  });

  it("按后端 id 找章（不是前端 slug）；找不到时不回显原始 id", () => {
    expect(chapterLabelById(BOOK, "P_CH06")).toBe("第 2 章 · 雾里的灯");
    expect(chapterLabelById(BOOK, "ch02")).toBe("已不在目录里的章");
    expect(chapterLabelById(BOOK, "")).toBe("未关联章节");
  });

  it("按后端场景 id 给出章内位置", () => {
    expect(sceneLabelById(BOOK, "P_CH01_SC02")).toBe("第 1 章 · 第 2 场");
    expect(sceneLabelById(BOOK, "P_CH01_SC01", { withTitle: true })).toBe("第 1 章 · 第 1 场「码头交班」");
    expect(sceneLabelById(BOOK, "NOPE")).toBe("已不在目录里的场");
  });
});

describe("其余中文名", () => {
  it("待办来源、写作偏好、记账状态、模型节点都不把英文键直接给作者", () => {
    expect(reviewSourceLabel("author_preference_profile")).toBe("写作偏好");
    expect(reviewSourceLabel("some_internal_key")).toBe("系统");
    expect(reviewSourceLabel("成稿中心")).toBe("成稿中心");
    expect(preferenceHintLabel("prefer_expansion")).toBe("偏好扩写");
    expect(preferenceHintLabel("prefer_longer_paragraphs")).toBe("偏好更长的段落");
    expect(accountingStatusMeta("settled").label).toBe("已结算");
    expect(accountingStatusMeta("weird").label).toBe("其他");
    expect(llmNodeLabel("style_draft")).toBe("风格稿");
    expect(llmNodeLabel("unknown_node")).toBe("");
  });
});

/* 风格参考的词表只有一张：16 维的名字、段落类型、场面 / 情绪标签都与后端源码逐字相同（读后端文件比对，
   任何一边改名而另一边没改，这里就红）。 */
const BACKEND_STYLE_REF = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../backend/src/novel_system/services/style_reference",
);
const readBackend = (name) => fs.readFileSync(path.join(BACKEND_STYLE_REF, name), "utf8");
/* 取一段 Python 字面量 / 类体：head 以 { 或 ( 结尾时取到配对的右括号，否则取到下一个顶层 class */
const pyBlock = (source, head) => {
  const start = source.indexOf(head);
  expect(start, `后端源码里找不到 ${head}`).toBeGreaterThanOrEqual(0);
  const body = start + head.length;
  const last = head.trim().slice(-1);
  if (last === "{" || last === "(") return source.slice(body, source.indexOf(last === "{" ? "}" : ")", body));
  const next = source.indexOf("\nclass ", body);
  return source.slice(body, next < 0 ? undefined : next);
};

describe("风格参考词表与后端一致", () => {
  it("16 维的键、顺序与名字和 card.DIMENSION_LABELS 逐字相同", () => {
    const block = pyBlock(readBackend("card.py"), "DIMENSION_LABELS: dict[str, str] = {");
    const backend = [...block.matchAll(/"([a-z_]+\.[a-z_]+)":\s*"([^"]+)"/g)].map((m) => [m[1], m[2]]);
    expect(backend).toHaveLength(16);
    expect(Object.entries(STYLE_DIMENSION_LABELS)).toEqual(backend);
    expect(STYLE_DIMENSIONS).toEqual(backend.map(([key]) => key));
  });

  it("场面 / 情绪标签与 tags.py 的 SITUATION_TAGS / MOOD_TAGS 同一套词、同一顺序", () => {
    const source = readBackend("tags.py");
    const words = (head) => [...pyBlock(source, head).matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect(STYLE_SITUATION_TAGS).toEqual(words("SITUATION_TAGS: tuple[str, ...] = ("));
    expect(STYLE_MOOD_TAGS).toEqual(words("MOOD_TAGS: tuple[str, ...] = ("));
  });

  it("段落类型覆盖后端 ParagraphType 的 8 类（外加分类作业还没轮到的「未分类」）", () => {
    const block = pyBlock(readBackend("schemas.py"), "class ParagraphType(str, Enum):");
    const backend = [...block.matchAll(/=\s*"([a-z_]+)"/g)].map((m) => m[1]);
    expect(backend).toHaveLength(8);
    expect(Object.keys(PARAGRAPH_TYPE_LABELS).sort()).toEqual([...backend, "unclassified"].sort());
  });

  it("层、维、位置、作业的叫法都给中文，认不出的不硬造", () => {
    expect(STYLE_LAYER_ORDER.map((layer) => STYLE_DIMENSIONS.filter((d) => styleLayerOf(d) === layer).length)).toEqual([4, 4, 4, 4]);
    expect(styleDimensionLabel("scene.dialogue")).toBe("对话写法");
    expect(styleLayerOf("bogus.key")).toBe("");
    expect(WINDOW_POSITION_LABELS.opening).toBe("章首");
    expect(WINDOW_POSITION_LABELS.closing).toBe("章末");
    expect(styleJobKindLabel("classify")).toBe("段落分类");
    expect(styleJobKindLabel("learn")).toBe("学习文风");
    expect(styleJobKindLabel("check")).toBe("对照检查");
    expect(styleJobKindLabel("nope")).toBe("");
  });

  it("参考书的模型节点：新节点有名字，删掉的节点不留名字", () => {
    expect(LLM_NODE_LABELS.style_ref_protected_terms).toBe("参考书 · 识别本书专名");
    expect(LLM_NODE_LABELS.style_ref_tag_windows).toBe("参考书 · 给片段打标签");
    for (const gone of ["style_ref_supplement_evidence", "style_ref_preview_generate", "style_ref_rag_rerank"]) {
      expect(LLM_NODE_LABELS[gone], gone).toBeUndefined();
    }
  });
});
