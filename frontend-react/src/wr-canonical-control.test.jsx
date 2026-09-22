import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WrCanonicalControl } from "./wr-canonical-control.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const mounted = [];

async function renderControl(props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<WrCanonicalControl {...props} />));
  return { host, root };
}

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
});

describe("WrCanonicalControl", () => {
  it("把草稿保存与权威正文同步显示为两个独立状态", async () => {
    const onPromote = vi.fn();
    const { host } = await renderControl({
      saveStatus: "草稿已保存",
      canonicalStatus: "dirty",
      onPromote,
    });

    expect(host.querySelector('[data-testid="draft-save-status"]').textContent).toContain("草稿已保存");
    expect(host.querySelector('[data-testid="canonical-status"]').textContent).toBe("权威正文待更新");
    expect(host.querySelector('[data-testid="draft-save-status"]').getAttribute("aria-live")).toBe("polite");
    expect(host.querySelector('[data-testid="canonical-status"]').getAttribute("role")).toBe("status");
    const button = host.querySelector("button");
    expect(button.textContent).toBe("提升为权威正文");
    expect(button.disabled).toBe(false);
    await act(async () => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onPromote).toHaveBeenCalledTimes(1);
  });

  it("提升中与已同步状态禁止重复提交", async () => {
    const { host, root } = await renderControl({
      saveStatus: "草稿已保存",
      canonicalStatus: "promoting",
      onPromote: vi.fn(),
    });
    expect(host.querySelector('[data-testid="canonical-status"]').textContent).toBe("正在提升权威正文…");
    expect(host.querySelector("button").disabled).toBe(true);

    await act(async () => root.render(
      <WrCanonicalControl saveStatus="草稿已保存" canonicalStatus="current" onPromote={vi.fn()} />,
    ));
    expect(host.querySelector('[data-testid="canonical-status"]').textContent).toBe("权威正文已更新");
    expect(host.querySelector("button").disabled).toBe(true);
  });

  it("空白场（none）：不显示「待更新」，也不给提升按钮", async () => {
    const onPromote = vi.fn();
    const { host } = await renderControl({ saveStatus: "草稿已加载", canonicalStatus: "none", onPromote });
    const status = host.querySelector('[data-testid="canonical-status"]');
    expect(status.textContent).toBe("还没有可提升的正文");
    expect(status.textContent).not.toContain("待更新");
    // 状态只留给读屏；提升按钮仍是第一个 <button>，但隐藏且禁用
    expect(status.classList.contains("ws-sr-only")).toBe(true);
    const button = host.querySelector("button");
    expect(button.classList.contains("wr-canonical-promote")).toBe(true);
    expect(button.hidden).toBe(true);
    expect(button.disabled).toBe(true);
    await act(async () => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(onPromote).not.toHaveBeenCalled();
  });

  it("等待作者内容风险复核时禁止从底层按钮重复提升", async () => {
    const { host } = await renderControl({
      saveStatus: "草稿已保存",
      canonicalStatus: "review",
      onPromote: vi.fn(),
    });

    expect(host.querySelector('[data-testid="canonical-status"]').textContent).toBe("内容风险待作者复核");
    expect(host.querySelector("button").disabled).toBe(true);
  });
});
