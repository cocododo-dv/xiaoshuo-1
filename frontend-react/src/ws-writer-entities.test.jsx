// 写作台的档案名高亮：档案来自资料库 store 的只读快照；档案晚到时的补标不动作者光标所在的那一段字。
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./ws-library-store.js", () => ({
  libLive: () => ({
    entries: [{ id: "e1", name: "林昭" }, { id: "e2", name: "雨城" }, { id: "e3", name: "旧" }],
    byId: {},
  }),
  useLibraryLive: () => ({ entries: [], byId: {} }),
}));

function editor(html) {
  const el = document.createElement("div");
  el.innerHTML = html;
  document.body.appendChild(el);
  return el;
}

afterEach(() => {
  window.getSelection().removeAllRanges();
  document.body.innerHTML = "";
});

describe("wrHighlightEntities", () => {
  it("把档案名标成 span.wr-entity（只认两个字以上的名字，已标过的、批注里的不再标）", async () => {
    const { wrHighlightEntities } = await import("./ws-writer-entities.jsx");
    const el = editor('<p>林昭在雨城读旧信。</p><p><mark class="wr-anno">林昭</mark>没说话。</p>');
    wrHighlightEntities(el);
    expect([...el.querySelectorAll(".wr-entity")].map((span) => span.getAttribute("data-lib-id"))).toEqual(["e1", "e2"]);
    expect(el.textContent).toBe("林昭在雨城读旧信。林昭没说话。");
    wrHighlightEntities(el);
    expect(el.querySelectorAll(".wr-entity")).toHaveLength(2);
  });

  it("keepSelection：光标所在的文本节点不动（光标不跳），其余段照标；不带它时整篇都标", async () => {
    const { wrHighlightEntities } = await import("./ws-writer-entities.jsx");
    const el = editor("<p>林昭来了。</p><p>雨城下雨了。</p>");
    const typing = el.querySelectorAll("p")[1].firstChild;
    const caret = document.createRange();
    caret.setStart(typing, 3);
    caret.collapse(true);
    window.getSelection().addRange(caret);

    wrHighlightEntities(el, { keepSelection: true });

    expect([...el.querySelectorAll(".wr-entity")].map((span) => span.getAttribute("data-lib-id"))).toEqual(["e1"]);
    const sel = window.getSelection();
    expect(sel.anchorNode).toBe(typing);
    expect(sel.anchorOffset).toBe(3);

    wrHighlightEntities(el);
    expect([...el.querySelectorAll(".wr-entity")].map((span) => span.getAttribute("data-lib-id"))).toEqual(["e1", "e2"]);
  });
});
