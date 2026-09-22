import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ContentSafetyReviewDialog, contentSafetyReviewFromError } from "./wr-content-safety-review.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const mounted = [];

const REVIEW = {
  findings: [
    {
      code: "sexual_content_with_minor_indicators",
      severity: "high",
      confidence: "heuristic",
      message: "请核对人物年龄和叙事目的。",
      evidenceTerms: ["性行为", "age:16"],
    },
    {
      code: "actionable_self_harm_detail",
      severity: "high",
      confidence: "heuristic",
      message: "请核对方法细节的必要性。",
      evidenceTerms: ["剂量"],
    },
  ],
  limitations: ["启发式不能可靠判断叙事语境。"],
};

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return { root, host };
}

async function click(node) {
  await act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  vi.restoreAllMocks();
});

describe("canonical 内容风险逐项确认", () => {
  it("只提取 review_required 的原始 finding code，不接受普通警告、已确认项或畸形代码", () => {
    const error = {
      code: "CONTENT_SAFETY_REVIEW_REQUIRED",
      details: {
        final_text_gate: {
          content_safety: {
            findings: [
              { code: "sexual_content_with_minor_indicators", review_required: true, acknowledged: false, message: "需复核", evidence_terms: ["age:16"] },
              { code: "graphic_violence", review_required: false, message: "普通提醒" },
              { code: "already_seen", review_required: true, acknowledged: true },
              { code: " content_safety_review:forged ", review_required: true },
              { code: "sexual_content_with_minor_indicators", review_required: true },
            ],
            limitations: ["仅为启发式。"],
          },
        },
      },
    };

    expect(contentSafetyReviewFromError(error)).toEqual({
      findings: [{
        code: "sexual_content_with_minor_indicators",
        severity: "unknown",
        confidence: "heuristic",
        message: "需复核",
        evidenceTerms: ["age:16"],
      }],
      limitations: ["仅为启发式。"],
    });
    expect(contentSafetyReviewFromError({ ...error, code: "SOURCE_SAFETY_BLOCKED" })).toBeNull();
  });

  it("默认零勾选；展示消息和命中词；全部逐项确认后只回传 exact codes", async () => {
    const onConfirm = vi.fn();
    await render(<ContentSafetyReviewDialog review={REVIEW} onCancel={vi.fn()} onConfirm={onConfirm} />);
    const dialog = document.querySelector(".wr-safety-dialog");
    const checks = [...dialog.querySelectorAll('input[type="checkbox"]')];
    const submit = dialog.querySelector('[data-testid="content-safety-confirm"]');

    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.textContent).toContain("请核对人物年龄和叙事目的");
    expect(dialog.textContent).toContain("age:16");
    expect(checks.every(node => node.checked === false)).toBe(true);
    expect(submit.disabled).toBe(true);

    await click(checks[0]);
    expect(submit.disabled).toBe(true);
    await click(checks[1]);
    expect(submit.disabled).toBe(false);
    await click(submit);

    expect(onConfirm).toHaveBeenCalledWith([
      "sexual_content_with_minor_indicators",
      "actionable_self_harm_detail",
    ]);
  });

  it("Esc 取消且焦点回到打开前元素", async () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();
    function Harness() {
      const [review, setReview] = React.useState(REVIEW);
      return review
        ? <ContentSafetyReviewDialog review={review} onCancel={() => setReview(null)} onConfirm={() => {}} />
        : null;
    }
    await render(<Harness />);
    // 焦点先进了对话框（不是停在打开它的按钮上）；Esc 从对话框里按下，像真实键盘那样冒泡
    const inside = document.activeElement;
    expect(document.querySelector(".wr-safety-dialog").contains(inside)).toBe(true);
    await act(async () => inside.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));

    expect(document.querySelector(".wr-safety-dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("重新校验进行中时，Esc 与点遮罩都不会关掉对话框", async () => {
    const onCancel = vi.fn();
    await render(<ContentSafetyReviewDialog review={REVIEW} busy onCancel={onCancel} onConfirm={() => {}} />);
    const inside = document.activeElement;
    await act(async () => inside.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    const scrim = document.querySelector(".wr-safety-scrim");
    await act(async () => scrim.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })));
    expect(onCancel).not.toHaveBeenCalled();
    expect(document.querySelector(".wr-safety-dialog")).not.toBeNull();
  });

  it("严重程度与判定方式用中文，不把内部风险代码印给作者", async () => {
    await render(<ContentSafetyReviewDialog review={REVIEW} onCancel={vi.fn()} onConfirm={vi.fn()} />);
    const text = document.querySelector(".wr-safety-dialog").textContent;
    expect(text).toContain("严重程度：高");
    expect(text).toContain("判定方式：启发式规则");
    expect(text).not.toContain("sexual_content_with_minor_indicators");
    expect(text).not.toMatch(/\bhigh\b|\bheuristic\b/);
  });
});
