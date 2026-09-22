// 导航模型（ws-nav.js）单测：侧栏、命令面板、路由校验共用这一份。
import { describe, expect, it } from "vitest";
import {
  WS_ALL_VIEWS, WS_NAV_ITEMS, WS_SNOW_STEPS, WS_VIEW_ALIAS, WS_VIEW_LABELS,
  isAdvancedView, isKnownView, navGroupsForMode, snowStepByBackendKey, systemNavGroup,
} from "./ws-nav.js";

describe("ws-nav", () => {
  it("全部页面：侧栏与命令面板读同一份（含文学质量 / 成本看板）", () => {
    expect(WS_ALL_VIEWS).toEqual([
      "home", "snowflake", "writer", "styleref", "review", "library",
      "author", "scene", "manuscripts", "quality", "cost", "settings", "trash",
    ]);
    expect(WS_VIEW_LABELS.cost).toBe("成本看板");
    WS_NAV_ITEMS.forEach(it => { expect(it.desc).toBeTruthy(); expect(it.kw).toBeTruthy(); });
  });

  it("高级页面只在高级模式出现；系统组固定在底部，不进滚动区", () => {
    expect(["author", "scene", "manuscripts", "quality", "cost"].every(isAdvancedView)).toBe(true);
    expect(["home", "writer", "settings", "trash"].some(isAdvancedView)).toBe(false);
    expect(navGroupsForMode("writer").map(g => g.id)).toEqual(["daily"]);
    expect(navGroupsForMode("advanced").map(g => g.id)).toEqual(["daily", "production", "ops"]);
    expect(systemNavGroup().items.map(it => it.id)).toEqual(["settings", "trash"]);
  });

  it("旧路由别名只重定向到已知页面", () => {
    Object.values(WS_VIEW_ALIAS).forEach(v => expect(isKnownView(v)).toBe(true));
    expect(isKnownView("deepdesk")).toBe(false);
  });

  it("雪花十步：后端 step_key ↔ 前端步骤键一一对应", () => {
    expect(WS_SNOW_STEPS).toHaveLength(10);
    expect(new Set(WS_SNOW_STEPS.map(s => s.key)).size).toBe(10);
    expect(snowStepByBackendKey("scene_details")).toMatchObject({ key: "planning", num: "10" });
    expect(snowStepByBackendKey("nope")).toBeNull();
  });
});
