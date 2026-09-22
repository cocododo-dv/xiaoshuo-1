import React from "react";
import ReactDOM from "react-dom";
import { WsDialog } from "./ws-dialog.jsx";
import { UndoToast, registerToastStack } from "./ws-undo-toast.jsx";
import { registerAlertSink } from "./lib/store-utils.js";

/* ==========================================================
   外壳的提示与确认层（2026-09-21）
   过去外壳没有自己的提示层：store 失败弹浏览器的 alert，删除类动作用浏览器的 confirm，
   每个视图各挂一个 toast。这里给整个应用一份：
   · <WsToastHost /> 在 App 里挂一次；
   · wsToast({ message, tone, action: { label, onClick }, timeout }) —— 一条短暂的提示；
     提示摞成一列（最多 TOAST_MAX 条），新的不顶掉还没到时的旧的；同一句话重复来只留一条（重新计时）。
     视图自己的回执（UndoToast）也进同一列（ws-undo-toast.jsx 的提示栈），不再和这里的提示叠在同一块像素上；
   · wsConfirm({ title, body, confirmLabel, cancelLabel, tone }) → Promise<boolean> ——
     应用内的确认框（WsDialog：焦点陷阱、Esc / 遮罩等于取消、关闭后焦点回到原处）；
   · store 的 storeAlert 在提示层挂载时改走这里（lib/store-utils.js 的 registerAlertSink）。
   提示层没挂载时（单测里单独渲染某个组件）：wsConfirm 退回 window.confirm，
   storeAlert 退回 window.alert，wsToast 返回 false——调用方可以自己兜底。
   纯 ESM，不写 window。
   ========================================================== */

const { useEffect, useLayoutEffect, useRef, useState, useCallback } = React;

const TOAST_MS = 6000;
const ALERT_TOAST_MS = 9000;
const TOAST_MAX = 4;   // 同时最多几条；超了先让最早的非失败类提示退场，失败提示（danger）最后才让

let hostApi = null;

function wsNotifyReady() {
  return !!hostApi;
}

function wsToast(input) {
  const opts = typeof input === "string" ? { message: input } : (input || {});
  if (!hostApi || !opts.message) return false;
  hostApi.toast(opts);
  return true;
}

function wsConfirm(input) {
  const opts = typeof input === "string" ? { title: input } : (input || {});
  if (!hostApi) {
    let ok = false;
    try { ok = window.confirm([opts.title, opts.body].filter(Boolean).join("\n")); } catch (e) { ok = false; }
    return Promise.resolve(!!ok);
  }
  return hostApi.confirm(opts);
}

function ConfirmDialog({ opts, onSettle }) {
  const cancelRef = useRef(null);
  const okRef = useRef(null);
  const titleId = React.useId();
  const bodyId = React.useId();
  const danger = opts.tone === "danger";
  return (
    <WsDialog
      onClose={() => onSettle(false)}
      labelledBy={titleId}
      describedBy={opts.body ? bodyId : undefined}
      size="sm"
      className="ws-confirm"
      testId="ws-confirm"
      zIndex="var(--z-modal)"
      initialFocus={danger ? cancelRef : okRef}
    >
      <div className="ws-dialog-head">
        <div>
          <h2 className="ws-dialog-title" id={titleId}>{opts.title || "确定继续吗？"}</h2>
          {opts.body ? <p className="ws-dialog-desc ws-confirm-body" id={bodyId}>{opts.body}</p> : null}
        </div>
      </div>
      <div className="ws-dialog-foot">
        <button ref={cancelRef} type="button" className="btn btn-ghost" data-testid="ws-confirm-cancel" onClick={() => onSettle(false)}>
          {opts.cancelLabel || "取消"}
        </button>
        <button ref={okRef} type="button" className={`btn ${danger ? "btn-danger" : "btn-accent"}`} data-testid="ws-confirm-ok" onClick={() => onSettle(true)}>
          {opts.confirmLabel || "确定"}
        </button>
      </div>
    </WsDialog>
  );
}

/* 挂在 App 里一次。提示摞成一列（与 UndoToast 同一外观与 testid，进同一个提示栈）；确认框排队，一次一个。 */
function WsToastHost() {
  const [toasts, setToasts] = useState([]);
  const [confirms, setConfirms] = useState([]);
  const [stackEl] = useState(() => (typeof document === "undefined" ? null : document.createElement("div")));
  const toastsRef = useRef([]);
  const timersRef = useRef(new Map());
  const seqRef = useRef(0);
  const confirmsRef = useRef([]);
  confirmsRef.current = confirms;

  const commitToasts = useCallback((next) => {
    toastsRef.current = next;
    setToasts(next);
  }, []);

  const dismissToast = useCallback((key) => {
    const timer = timersRef.current.get(key);
    if (timer) { window.clearTimeout(timer); timersRef.current.delete(key); }
    if (!toastsRef.current.some((item) => item.key === key)) return;
    commitToasts(toastsRef.current.filter((item) => item.key !== key));
  }, [commitToasts]);

  /* 提示栈的容器：挂在 <body> 下（视图容器可能带 transform，fixed 会跟着它跑），登记给 UndoToast */
  useLayoutEffect(() => {
    if (!stackEl) return undefined;
    stackEl.className = "ws-toast-stack";
    stackEl.setAttribute("data-testid", "ws-toast-stack");
    document.body.appendChild(stackEl);
    const unregister = registerToastStack(stackEl);
    return () => {
      unregister();
      stackEl.remove();
    };
  }, [stackEl]);

  useEffect(() => {
    const timers = timersRef.current;
    const api = {
      toast(opts) {
        const action = opts.action || null;
        const item = {
          key: ++seqRef.current,
          text: opts.message,
          tone: opts.tone || "neutral",
          actionLabel: action && action.label,
          onAction: action && action.onClick,
        };
        // 同一句话（同一色调）已经在屏上：换成新的这条、重新计时，不摞两条一样的
        let next = toastsRef.current.filter((t) => {
          const same = t.text === item.text && t.tone === item.tone;
          if (same) { const timer = timers.get(t.key); if (timer) window.clearTimeout(timer); timers.delete(t.key); }
          return !same;
        });
        next = [...next, item];
        while (next.length > TOAST_MAX) {
          const idx = next.findIndex((t) => t.tone !== "danger");
          const [gone] = next.splice(idx >= 0 ? idx : 0, 1);
          const timer = timers.get(gone.key);
          if (timer) window.clearTimeout(timer);
          timers.delete(gone.key);
        }
        commitToasts(next);
        if (!next.includes(item)) return; // 满屏都是失败提示：这条较轻的不挤掉它们
        timers.set(item.key, window.setTimeout(() => {
          timers.delete(item.key);
          commitToasts(toastsRef.current.filter((t) => t.key !== item.key));
        }, opts.timeout || TOAST_MS));
      },
      confirm(opts) {
        return new Promise((resolve) => {
          setConfirms((queue) => [...queue, { id: ++seqRef.current, opts, resolve }]);
        });
      },
    };
    hostApi = api;
    const unregister = registerAlertSink((message) => {
      api.toast({ message, tone: "danger", timeout: ALERT_TOAST_MS });
      return true;
    });
    return () => {
      if (hostApi === api) hostApi = null;
      unregister();
      timers.forEach((timer) => window.clearTimeout(timer));
      timers.clear();
      // 卸载时还没回答的确认一律按「取消」结算，调用方不会永远等下去
      confirmsRef.current.forEach((item) => item.resolve(false));
    };
  }, [commitToasts]);

  const current = confirms[0] || null;
  const settle = (value) => {
    if (!current) return;
    current.resolve(!!value);
    setConfirms((queue) => queue.filter((item) => item !== current));
  };

  return (
    <>
      {stackEl && toasts.length > 0 && ReactDOM.createPortal(
        toasts.map((item) => (
          <UndoToast key={item.key} toast={item} inline onClose={() => dismissToast(item.key)} />
        )),
        stackEl,
      )}
      {current && <ConfirmDialog key={current.id} opts={current.opts} onSettle={settle} />}
    </>
  );
}

export { WsToastHost, wsConfirm, wsNotifyReady, wsToast };
