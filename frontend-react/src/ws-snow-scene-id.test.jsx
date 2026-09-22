import { describe, expect, it } from "vitest";

// 纯函数住在叶子模块里（不 import 任何东西），不必再为了它拉起整张雪花视图、mock 目录与作品层
import { s2NextSceneRowId } from "./ws-snow-model.js";

/* 09 场景行的 id 会作为 row_uid 上行，是场景计划的不可变身份锚。
   旧写法 `"S" + (list.length + 1)` 在「删掉中间一场再新增」时会铸出与幸存场同号的 id，
   后端按 row_uid 对位时后者直接覆盖前者 —— 作者写好的一场无声消失。 */
describe("s2NextSceneRowId — 场景行身份铸造", () => {
  const rows = (...ids) => ids.map((id) => ({ id }));

  it("空列表从 S01 开始", () => {
    expect(s2NextSceneRowId([])).toBe("S01");
    expect(s2NextSceneRowId(undefined)).toBe("S01");
  });

  it("连续列表续下一号", () => {
    expect(s2NextSceneRowId(rows("S01", "S02", "S03"))).toBe("S04");
  });

  it("删掉中间一场后新增，不得撞上仍然存活的号", () => {
    // 回归：旧写法在这里返回 "S04"，与幸存的 S04 撞号
    const afterDelete = rows("S01", "S02", "S04");
    const minted = s2NextSceneRowId(afterDelete);
    expect(minted).toBe("S05");
    expect(afterDelete.map((r) => r.id)).not.toContain(minted);
  });

  it("反复删删加加也始终不重号", () => {
    let list = rows("S01", "S02", "S03", "S04");
    list = list.filter((r) => r.id !== "S02");           // 删中间
    list = [...list, { id: s2NextSceneRowId(list) }];    // 加一场
    list = list.filter((r) => r.id !== "S03");           // 再删中间
    list = [...list, { id: s2NextSceneRowId(list) }];    // 再加一场
    const ids = list.map((r) => r.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toEqual(["S01", "S04", "S05", "S06"]);
  });

  it("非 S 前缀 / 脏 id 不会让铸号退化成重号", () => {
    const dirty = [{ id: "" }, { id: null }, { id: "怪东西" }, { id: "S07" }];
    expect(s2NextSceneRowId(dirty)).toBe("S08");
  });

  it("最大号被占用但有空洞时，跳过占用号而不是填洞", () => {
    // 填洞会让「已删除又复活」的语义变脏；宁可留洞也不复用号
    expect(s2NextSceneRowId(rows("S01", "S05"))).toBe("S06");
  });
});
