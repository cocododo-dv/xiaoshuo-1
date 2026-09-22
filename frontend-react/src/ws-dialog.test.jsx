import React from "react";
import { act } from "react";
import ReactDOMClient from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WsDialog, isImeComposing, focusableIn } from "./ws-dialog.jsx";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let host;
let root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = ReactDOMClient.createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  document.body.innerHTML = "";
});

function Harness({ onClose, onBeforeClose, dismissOnBackdrop }) {
  const [open, setOpen] = React.useState(false);
  return (
    <div>
      <button type="button" data-testid="opener" onClick={() => setOpen(true)}>打开</button>
      <WsDialog
        open={open}
        label="测试对话框"
        testId="dlg"
        dismissOnBackdrop={dismissOnBackdrop}
        onBeforeClose={onBeforeClose}
        onClose={(reason) => { onClose && onClose(reason); setOpen(false); }}
      >
        <button type="button" data-testid="first">第一</button>
        <input data-testid="middle" />
        <button type="button" data-testid="last">最后</button>
      </WsDialog>
    </div>
  );
}

async function openDialog(props = {}) {
  await act(async () => root.render(<Harness {...props} />));
  const opener = host.querySelector("[data-testid=opener]");
  opener.focus();
  await act(async () => opener.click());
  return opener;
}

function press(key, init = {}) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  document.activeElement.dispatchEvent(event);
  return event;
}

describe("WsDialog", () => {
  it("renders an aria-modal dialog in a portal and moves focus to the first control", async () => {
    await openDialog();
    const dialog = document.querySelector("[data-testid=dlg]");
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-label")).toBe("测试对话框");
    expect(host.contains(dialog)).toBe(false);
    expect(document.activeElement.getAttribute("data-testid")).toBe("first");
  });

  it("keeps Tab inside the dialog in both directions", async () => {
    await openDialog();
    document.querySelector("[data-testid=last]").focus();
    const forward = press("Tab");
    expect(forward.defaultPrevented).toBe(true);
    expect(document.activeElement.getAttribute("data-testid")).toBe("first");
    const backward = press("Tab", { shiftKey: true });
    expect(backward.defaultPrevented).toBe(true);
    expect(document.activeElement.getAttribute("data-testid")).toBe("last");
  });

  it("closes on Escape and returns focus to the opener", async () => {
    const onClose = vi.fn();
    const opener = await openDialog({ onClose });
    await act(async () => { press("Escape"); });
    expect(onClose).toHaveBeenCalledWith("escape");
    expect(document.querySelector("[data-testid=dlg]")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });

  it("ignores Escape while an input method is composing", async () => {
    const onClose = vi.fn();
    await openDialog({ onClose });
    await act(async () => { press("Escape", { isComposing: true }); });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("lets onBeforeClose veto every close path (dirty guard)", async () => {
    const onClose = vi.fn();
    const onBeforeClose = vi.fn(() => false);
    await openDialog({ onClose, onBeforeClose });
    await act(async () => { press("Escape"); });
    const scrim = document.querySelector(".ws-dialog-scrim");
    await act(async () => { scrim.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); });
    expect(onBeforeClose).toHaveBeenCalledTimes(2);
    expect(onClose).not.toHaveBeenCalled();
    expect(document.querySelector("[data-testid=dlg]")).not.toBeNull();
  });

  it("closes on a backdrop press unless dismissOnBackdrop is false", async () => {
    const onClose = vi.fn();
    await openDialog({ onClose, dismissOnBackdrop: false });
    const scrim = document.querySelector(".ws-dialog-scrim");
    await act(async () => { scrim.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); });
    expect(onClose).not.toHaveBeenCalled();
    await act(async () => {
      document.querySelector("[data-testid=dlg]").dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    });
    expect(onClose).not.toHaveBeenCalled();
  });

  it("a backdrop press never lets focus fall out of the dialog", async () => {
    await openDialog({ dismissOnBackdrop: false });
    const scrim = document.querySelector(".ws-dialog-scrim");
    const down = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    await act(async () => { scrim.dispatchEvent(down); });
    expect(down.defaultPrevented).toBe(true);
    const inside = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    await act(async () => { document.querySelector("[data-testid=middle]").dispatchEvent(inside); });
    expect(inside.defaultPrevented).toBe(false);
  });
});

describe("WsDialog under React.StrictMode (the dev server runs effects twice)", () => {
  function MountedOpen({ onClose }) {
    const [open, setOpen] = React.useState(false);
    return (
      <div>
        <button type="button" data-testid="opener" onClick={() => setOpen(true)}>打开</button>
        {open && (
          <WsDialog label="严格模式" testId="strict" onClose={() => { onClose(); setOpen(false); }}>
            <button type="button" data-testid="first">第一</button>
            <button type="button" data-testid="last">最后</button>
          </WsDialog>
        )}
      </div>
    );
  }

  it("still focuses the first control on open and returns focus to the opener on close", async () => {
    const onClose = vi.fn();
    await act(async () => root.render(<React.StrictMode><MountedOpen onClose={onClose} /></React.StrictMode>));
    const opener = host.querySelector("[data-testid=opener]");
    opener.focus();
    await act(async () => opener.click());
    expect(document.activeElement.getAttribute("data-testid")).toBe("first");
    await act(async () => { press("Escape"); });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.querySelector("[data-testid=strict]")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

describe("WsDialog stacking and in-place mode", () => {
  function Nested({ onParentClose, onChildClose }) {
    const [child, setChild] = React.useState(false);
    const [, force] = React.useState(0);
    return (
      <WsDialog label="父" testId="parent" onClose={() => onParentClose()}>
        <button type="button" data-testid="open-child" onClick={() => setChild(true)}>子</button>
        <button type="button" data-testid="rerender" onClick={() => force((n) => n + 1)}>重绘</button>
        <WsDialog open={child} label="子" testId="child" onClose={() => { onChildClose(); setChild(false); }}>
          <button type="button" data-testid="child-btn" onClick={() => force((n) => n + 1)}>子按钮</button>
        </WsDialog>
      </WsDialog>
    );
  }

  it("Escape closes only the top dialog, even after the parent re-renders with new callbacks", async () => {
    const onParentClose = vi.fn();
    const onChildClose = vi.fn();
    await act(async () => root.render(<Nested onParentClose={onParentClose} onChildClose={onChildClose} />));
    await act(async () => document.querySelector("[data-testid=open-child]").click());
    // 子对话框打开后父组件重绘：父的 onClose 换了身份
    await act(async () => document.querySelector("[data-testid=child-btn]").click());
    await act(async () => { press("Escape"); });
    expect(onChildClose).toHaveBeenCalledTimes(1);
    expect(onParentClose).not.toHaveBeenCalled();
    expect(document.querySelector("[data-testid=child]")).toBeNull();
    await act(async () => { press("Escape"); });
    expect(onParentClose).toHaveBeenCalledTimes(1);
  });

  it("portal={false} renders inside the host container", async () => {
    await act(async () => root.render(<WsDialog portal={false} label="就地" testId="inplace"><button type="button">x</button></WsDialog>));
    expect(host.querySelector("[data-testid=inplace]")).not.toBeNull();
    expect(host.querySelector(".ws-dialog-scrim")).not.toBeNull();
  });
});

describe("dialog helpers", () => {
  it("isImeComposing recognises composing and keyCode 229 events", () => {
    expect(isImeComposing({ isComposing: true })).toBe(true);
    expect(isImeComposing({ keyCode: 229 })).toBe(true);
    expect(isImeComposing({ nativeEvent: { isComposing: true } })).toBe(true);
    expect(isImeComposing({ key: "Enter" })).toBe(false);
  });

  it("focusableIn skips disabled and hidden controls", () => {
    const box = document.createElement("div");
    box.innerHTML = '<button>a</button><button disabled>b</button><input type="hidden"><a href="#x">c</a><span tabindex="-1">d</span>';
    expect(focusableIn(box).map((node) => node.textContent || node.tagName)).toEqual(["a", "c"]);
  });
});
