import React from "react";
import { I } from "./icons.jsx";
import { WrRecovery } from "./wr-doc-store.jsx";
import { sanitizeManuscriptHTML } from "./manuscript-html.js";
import { WsDialog } from "./ws-dialog.jsx";
import { wsConfirm, wsToast } from "./ws-notify.jsx";

const { useEffect, useMemo, useRef, useState } = React;

/* 同步与恢复中心（2026-09-21 起入口在侧栏底部）。
   过去入口是一颗固定在右下角的悬浮按钮，在每个视图上都压住内容——AI 起草台 1100px 宽时
   正好盖住底栏的主按钮。现在：
   · 入口是侧栏底部的一项（图标 + 份数），类名 wrr-trigger 与 aria-label 措辞不变；
   · 任何视图派发 ws:recovery-open（detail 可带 { id } 或 { sid }）即可直接打开并选中对应记录；
   · 有新记录落进来时经外壳的提示层（ws-notify.jsx）给一条短暂的回执，带「打开」，
     不再靠常驻的悬浮按钮提醒；恢复 / 重试 / 删除前的确认也走应用内确认框。 */

const TYPE_LABEL = {
  conflict: "冲突副本",
  unsynced: "未同步稿",
  backup: "覆盖前备份",
  candidate: "AI 候选",
};

function plainText(html) {
  const node = document.createElement("div");
  node.innerHTML = sanitizeManuscriptHTML(html || "");
  return (node.textContent || "").trim();
}

function formatTime(value) {
  if (!value) return "时间未知";
  try {
    return new Date(value).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch (e) { return "时间未知"; }
}

/* 记录里存的是内部 id（场景 id、作品 id）；给作者看的一律换成标题，查不到就不显示 id。
   目录与书架是 window 上的过渡期 store，这里只读、不写。 */
function sceneTitleFor(sid) {
  if (!sid) return "";
  try {
    const hit = window.WsCatalog && window.WsCatalog.sceneById && window.WsCatalog.sceneById(sid);
    return (hit && hit.scene && hit.scene.title) || "";
  } catch (e) { return ""; }
}

function workTitleFor(workId) {
  if (!workId) return "";
  try {
    const works = (window.WsWorks && window.WsWorks.list && window.WsWorks.list()) || [];
    const hit = works.find((work) => work.id === workId);
    return (hit && hit.title) || "";
  } catch (e) { return ""; }
}

/* 记录标签的惯例是「场景 <sid> · 说明」：把 sid 换成场景标题；说明和类型标签重复时（「AI 候选」）不再重复一遍。 */
function entryTitle(entry) {
  if (!entry) return "";
  const label = entry.label || "";
  const sid = entry.sid || "";
  const prefix = `场景 ${sid}`;
  if (!sid || !label.startsWith(prefix)) return label || (sceneTitleFor(sid) ? `《${sceneTitleFor(sid)}》` : "未命名稿件");
  const title = sceneTitleFor(sid);
  const subject = title ? `《${title}》` : "当前目录里找不到的一场";
  const note = label.slice(prefix.length).replace(/^\s*·\s*/, "").trim();
  return note && note !== TYPE_LABEL[entry.type] ? `${subject} · ${note}` : subject;
}

function entryWhere(entry) {
  const work = workTitleFor(entry && entry.workId);
  return work ? `《${work}》` : "当前作品";
}

async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (e) {
      // 权限策略可能拒绝异步剪贴板；保留传统选择复制作为降级路径。
    }
  }
  const field = document.createElement("textarea");
  field.value = text;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  const copied = document.execCommand && document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("浏览器拒绝了剪贴板权限，请改用导出");
}

function exportEntry(entry) {
  const text = plainText(entry.html || "");
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  const name = (entryTitle(entry) || "恢复稿").replace(/[^\w\u4e00-\u9fa5-]/g, "-");
  link.download = `${name}-${entry.createdAt || Date.now()}.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function RecoveryDiff({ diff }) {
  if (!diff) return <div className="wrr-empty-detail">选择左侧记录查看正文与差异。</div>;
  if (!diff.adds && !diff.dels) {
    return <div className="wrr-same"><I.CheckCircle size={16} /> 这份记录与当前本地草稿内容一致。</div>;
  }
  return (
    <div className="wrr-diff" aria-label="恢复稿与当前草稿差异">
      <div className="wrr-diff-key">
        <span><i className="is-del" /> 当前草稿中将被替换的内容</span>
        <span><i className="is-add" /> 恢复稿中将写入的内容</span>
      </div>
      {diff.paras.map((para, index) => (
        <p key={`${para.p}-${index}`}>
          {para.segs.map((seg, segIndex) => (
            <span key={`${seg.t}-${segIndex}`} className={`is-${seg.t}`}>{seg.text}</span>
          ))}
        </p>
      ))}
    </div>
  );
}

function pickEntry(list, detail) {
  if (!detail) return null;
  if (detail.id && list.some((item) => item.id === detail.id)) return detail.id;
  if (detail.sid) {
    const hit = list.find((item) => item.sid === detail.sid);
    if (hit) return hit.id;
  }
  return null;
}

/* 侧栏入口 + 对话框（组合导出，单测直接渲染它）。 */
function WrRecoveryCenter() {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState(() => WrRecovery.list());
  const [selectedId, setSelectedId] = useState(() => (entries[0] && entries[0].id) || null);
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const triggerRef = useRef(null);
  const closeRef = useRef(null);
  const openedFromTrigger = useRef(false);
  const openRef = useRef(false);
  openRef.current = open;

  const refresh = () => {
    const next = WrRecovery.list();
    setEntries(next);
    setSelectedId((current) => next.some(item => item.id === current) ? current : ((next[0] && next[0].id) || null));
    return next;
  };

  const openCenter = (detail, fromTrigger = false) => {
    const next = refresh();
    const wanted = pickEntry(next, detail);
    if (wanted) setSelectedId(wanted);
    openedFromTrigger.current = fromTrigger;
    setMessage("");
    setOpen(true);
  };

  useEffect(() => {
    const onChange = (event) => {
      refresh();
      const detail = (event && event.detail) || {};
      // 中心开着时作者自己就在看（恢复前的自动备份也会走到这里），不再弹回执。
      if (detail.action !== "created" || openRef.current || !detail.entry) return;
      const entry = detail.entry;
      wsToast({
        message: `${TYPE_LABEL[entry.type] || "恢复稿"}已放入「同步与恢复」：${entryTitle(entry)}`,
        action: { label: "打开", onClick: () => openCenter({ id: entry.id }) },
      });
    };
    const onOpen = (event) => openCenter((event && event.detail) || null);
    window.addEventListener("ws:recovery-changed", onChange);
    window.addEventListener("ws:recovery-open", onOpen);
    return () => {
      window.removeEventListener("ws:recovery-changed", onChange);
      window.removeEventListener("ws:recovery-open", onOpen);
    };
    // openCenter / refresh 只读 setState 与 store，挂载一次即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const close = () => setOpen(false);

  // WsDialog 会把焦点还给打开前的焦点；鼠标点入口在个别浏览器里不聚焦按钮，这里兜底回到入口。
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open) { wasOpen.current = true; return; }
    if (wasOpen.current && openedFromTrigger.current) triggerRef.current?.focus();
    wasOpen.current = false;
  }, [open]);

  const selected = entries.find(item => item.id === selectedId) || null;
  const diff = useMemo(() => selected ? WrRecovery.diff(selected.id) : null, [selected, entries]);
  const volatileCount = entries.filter(item => item.durable === false).length;
  const count = entries.length;

  const run = async (kind, action) => {
    if (!selected || busy) return;
    setBusy(kind);
    setMessage("");
    try {
      await action();
      refresh();
      setMessage(kind === "retry" ? "已同步到服务端，并移出恢复列表。" : "已恢复为当前草稿并同步到服务端。恢复记录仍保留，确认无误后可删除。");
    } catch (error) {
      setMessage(`操作未完成：${(error && error.message) || "请检查网络后重试"}`);
    } finally { setBusy(""); }
  };

  const restore = async () => {
    if (!selected) return;
    const target = selected;
    const ok = await wsConfirm({
      title: `把${entryTitle(target)}恢复为当前草稿？`,
      body: "恢复后会同步到服务端。当前草稿若不同，系统会先自动留下一份可撤销的备份。",
      confirmLabel: "恢复为当前草稿",
    });
    if (ok) void run("restore", () => WrRecovery.restore(target.id));
  };
  const retry = async () => {
    if (!selected) return;
    const target = selected;
    const ok = await wsConfirm({
      title: "用这份记录重试同步？",
      body: "同步成功后，它会从恢复列表移除。",
      confirmLabel: "重试同步",
    });
    if (ok) void run("retry", () => WrRecovery.retry(target.id));
  };
  const remove = async () => {
    if (!selected) return;
    const target = selected;
    const ok = await wsConfirm({
      title: "永久删除这份恢复记录？",
      body: "它只存在这台电脑的浏览器里，删除后无法撤销。",
      confirmLabel: "永久删除",
      tone: "danger",
    });
    if (!ok) return;
    WrRecovery.remove(target.id);
    setMessage("恢复记录已删除。");
    refresh();
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`wrr-trigger ws-foot-btn ${count ? "has-items" : ""}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`打开同步与恢复中心${count ? `，有 ${count} 份恢复记录` : ""}`}
        title="同步与恢复"
        onClick={() => openCenter(null, true)}
      >
        <span className="ws-item-ic"><I.Save size={18} />{count > 0 && <i className="ws-foot-dot" aria-hidden="true" />}</span>
        <span className="ws-foot-label">同步与恢复</span>
        {count > 0 && <b className="ws-foot-count" aria-hidden="true">{count}</b>}
      </button>

      <WsDialog
        open={open}
        onClose={close}
        labelledBy="wrr-title"
        size="xl"
        className="wrr-panel"
        initialFocus={closeRef}
      >
        <header className="wrr-head">
          <div className="wrr-seal" aria-hidden="true"><I.Save size={19} /></div>
          <div className="wrr-head-text">
            <h2 id="wrr-title">同步与恢复中心</h2>
            <p>冲突、断网和覆盖前的稿件都留在这里；先比较，再决定。</p>
          </div>
          <button ref={closeRef} type="button" className="wrr-close" onClick={close} aria-label="关闭同步与恢复中心"><I.X size={18} /></button>
        </header>

        {volatileCount > 0 && (
          <div className="wrr-warning" role="alert">
            <I.AlertTriangle size={15} /> {volatileCount} 份记录因浏览器空间不足仅保留在本次会话。请立即复制或导出，刷新页面后它们会消失。
          </div>
        )}

        <div className="wrr-body">
          <aside className="wrr-list" aria-label="恢复记录">
            <div className="wrr-list-head"><span>待处理校样</span><b>{count}</b></div>
            {/* 没有记录时只说一次：右边的详情区给那一句，这一栏不再重复一个空态 */}
            {entries.map((entry) => {
              const preview = plainText(entry.html || "").slice(0, 54);
              return (
                <button
                  type="button"
                  key={entry.id}
                  className={`wrr-row ${selectedId === entry.id ? "is-active" : ""}`}
                  onClick={() => { setSelectedId(entry.id); setMessage(""); }}
                  aria-pressed={selectedId === entry.id}
                >
                  <span className={`wrr-type is-${entry.type || "conflict"}`}>{TYPE_LABEL[entry.type] || "恢复稿"}</span>
                  <strong>{entryTitle(entry)}</strong>
                  <span className="wrr-row-meta">{formatTime(entry.createdAt)} · {entryWhere(entry)}</span>
                  <span className="wrr-row-preview">{preview || "（空白稿件）"}</span>
                  {!entry.durable && <span className="wrr-volatile">仅本次会话</span>}
                </button>
              );
            })}
          </aside>

          <section className="wrr-detail" aria-label="记录详情">
            {selected ? (
              <>
                <div className="wrr-detail-head">
                  <div>
                    <span className="wrr-detail-kicker">{TYPE_LABEL[selected.type] || "恢复稿"} · {entryWhere(selected)}</span>
                    <h3>{entryTitle(selected)}</h3>
                    <p>{selected.reason || "系统在可能丢稿前留下的本地副本。"}</p>
                  </div>
                  <div className="wrr-delta" aria-label={`新增 ${diff ? diff.adds : 0} 句，替换 ${diff ? diff.dels : 0} 句`}>
                    <span className="is-add">+{diff ? diff.adds : 0}</span>
                    <span className="is-del">−{diff ? diff.dels : 0}</span>
                  </div>
                </div>
                <RecoveryDiff diff={diff} />
                {/* 一组动作只有一个实心主按钮：还没同步上去的稿件主动作是「重试同步」，其余（冲突、覆盖前的底稿、候选）是「恢复为当前草稿」 */}
                <div className="wrr-actions" role="group" aria-label="恢复操作">
                  <button type="button" className={`btn ${selected.type === "unsynced" ? "btn-ghost" : "btn-accent"}`} onClick={restore} disabled={!!busy}>
                    <I.Refresh size={14} /> {busy === "restore" ? "恢复中…" : "恢复为当前草稿"}
                  </button>
                  <button type="button" className={`btn ${selected.type === "unsynced" ? "btn-accent" : "btn-ghost"}`} onClick={retry} disabled={!!busy}>
                    <I.UploadCloud size={14} /> {busy === "retry" ? "同步中…" : "重试同步"}
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={async () => {
                    try {
                      await copyText(plainText(selected.html || ""));
                      setMessage("正文已复制到剪贴板。");
                    } catch (error) { setMessage((error && error.message) || "复制失败，请改用导出"); }
                  }}>
                    <I.FileText size={14} /> 复制正文
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={() => { exportEntry(selected); setMessage("已导出为纯文本文件。"); }}>
                    <I.Download size={14} /> 导出
                  </button>
                  <button type="button" className="btn btn-quiet wrr-delete" onClick={remove}><I.Trash size={14} /> 删除</button>
                </div>
              </>
            ) : count === 0 ? (
              <div className="wrr-empty-detail"><I.CheckCircle size={22} /><strong>没有待恢复稿件</strong><span>本地草稿与服务端目前没有已知冲突。</span></div>
            ) : <div className="wrr-empty-detail"><I.FileText size={23} />在左边选一条记录，这里会并排比较它和当前草稿。</div>}
            <div className="wrr-live" role="status" aria-live="polite">{message}</div>
          </section>
        </div>
      </WsDialog>
    </>
  );
}

export { WrRecoveryCenter };
