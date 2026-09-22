import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsDialog, isImeComposing, topModalLayer } from "./ws-dialog.jsx";
import { WS_NAV_ITEMS, WS_SNOW_STEPS } from "./ws-nav.js";
import { modShortcut } from "./lib/platform.js";
import { sceneLabel } from "./ws-labels.js";

/* ==========================================================
   WsPalette — 全局命令面板（⌘K / Ctrl+K；提示文字由 lib/platform.js 按平台给）
   跳到任何一个页面、任何一场、雪花的任何一步，或者执行一个动作。
   · 页面清单来自 ws-nav.js（和侧栏同一份；过去手抄的清单漏了文学质量 / 成本看板）；
   · 没输入时场景只列前几条（在写的排前面），输入后在全书所有场景里找（过去只在前 12 场里找）；
   · 命令列表只在面板打开时构建一次；
   · 对话框语义：WsDialog（焦点陷阱、Esc、关闭后焦点回到原处）+ 输入框是 combobox、
     结果是 listbox / option（aria-activedescendant 指向当前项）；
   · 输入法组字时的回车 / 方向键不当命令。
   纯 ESM，不写 window。
   ========================================================== */

const { useState, useEffect, useRef, useMemo } = React;

const SCENES_WHEN_EMPTY = 8;
const SCENES_WHEN_SEARCHING = 40;

/* ---- fuzzy subsequence match w/ light scoring ---- */
function fuzzy(q, text) {
  if (!q) return { ok: true, score: 0 };
  q = q.toLowerCase(); const t = text.toLowerCase();
  if (t.includes(q)) return { ok: true, score: 100 - t.indexOf(q) };
  let qi = 0, score = 0, prev = -2;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) { score += (i === prev + 1 ? 5 : 1); prev = i; qi++; }
  }
  return { ok: qi === q.length, score };
}

/* 全书场景（与大纲 / 主页同源：WsCatalog）。在写的排前面。 */
function catalogScenes() {
  try {
    const out = [];
    WsCatalog.get().forEach(c => (c.scenes || []).forEach((s, i) => {
      out.push({ ch: c.n, chTitle: c.title, where: sceneLabel(c, i), id: s.sid, title: s.title || "未命名场景", state: s.state === "writing" ? "active" : (s.state || "todo") });
    }));
    return out.sort((a, b) => (a.state === "active" ? -1 : 0) - (b.state === "active" ? -1 : 0));
  } catch (e) { return []; }
}

function buildCommands(theme, run) {
  const cmds = [];

  // 作品 — 切换 / 新建
  const works = WsWorks ? WsWorks.list() : [];
  const activeId = WsWorks ? WsWorks.activeId() : null;
  works.forEach(w => {
    if (w.id === activeId) return;
    cmds.push({ g: "作品", icon: "BookOpen", label: `切换到《${w.title}》`, hint: w.genre,
      kw: `work zuopin qiehuan ${w.title} ${w.genre}`, run: () => run({ type: "work", workId: w.id }) });
  });
  cmds.push({ g: "作品", icon: "Plus", label: "新建作品", kw: "new work xinjian zuopin xinshu", run: () => run({ type: "new-work" }) });

  // 页面 — 与侧栏同一份导航模型
  WS_NAV_ITEMS.forEach(it => {
    cmds.push({ g: "页面", icon: it.icon, label: it.label, hint: it.desc, kw: `${it.id} ${it.kw || ""}`,
      run: () => run({ type: "go", view: it.id }) });
  });

  // 动作
  cmds.push({ g: "动作", icon: "Sparkles", label: "AI 续写当前场景", hint: modShortcut("J"), kw: "ai xuxie sparkles", run: () => run({ type: "writer-action", action: "ai" }) });
  cmds.push({ g: "动作", icon: "Eye", label: "进入沉浸写作", hint: modShortcut("."), kw: "immersion chenjin zhuanzhu", run: () => run({ type: "writer-action", action: "immersion" }) });
  cmds.push({ g: "动作", icon: "Microscope", label: "深改当前场景", hint: "写作台", kw: "deepdesk shengai shenxiu", run: () => run({ type: "writer-action", action: "deep" }) });
  cmds.push({ g: "动作", icon: "Sliders", label: "排版与舒适度", kw: "tweaks shushidu paiban ziti hangju", run: () => run({ type: "tweaks" }) });
  const night = theme === "night";
  cmds.push({ g: "动作", icon: night ? "Sun" : "Moon", label: night ? "切换到白昼主题" : "切换到夜灯主题", kw: "theme zhuti yedeng baizhou", run: () => run({ type: "theme", value: night ? "day" : "night" }) });
  if (theme !== "dusk") {
    cmds.push({ g: "动作", icon: "Type", label: "切换到暮色主题", kw: "theme dusk muse", run: () => run({ type: "theme", value: "dusk" }) });
  }

  // 构思 — 雪花十步
  WS_SNOW_STEPS.forEach(s => cmds.push({
    g: "构思", icon: "Compass", label: `${s.num} ${s.name}`, hint: "雪花十步",
    kw: `${s.name} ${s.num} xuehua`, run: () => run({ type: "step", key: s.key }),
  }));
  return cmds;
}

function sceneCommands(scenes, run) {
  return scenes.map(s => ({
    g: "场景", icon: "FileText", label: s.title, hint: s.where, state: s.state,
    kw: `${s.title} ${s.chTitle} ch${s.ch}`, run: () => run({ type: "scene", sceneId: s.id }),
  }));
}

/* 结果按分组排列（组的先后以第一条命中的位置为准） */
function groupResults(results) {
  const order = []; const map = {};
  results.forEach(c => { if (!map[c.g]) { map[c.g] = []; order.push(c.g); } map[c.g].push(c); });
  return order.map(g => ({ g, items: map[g] }));
}

function WsPalette({ open, ...rest }) {
  if (!open) return null;
  return <PaletteDialog {...rest} />;
}

function PaletteDialog({ onClose, run, theme }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef(null);
  const listRef = useRef(null);
  const runRef = useRef(run);
  runRef.current = run;
  const baseId = React.useId();
  const listId = `${baseId}-list`;
  const optionId = (i) => `${baseId}-opt-${i}`;

  // 面板打开期间只建一次（App 每次重渲都会换一个 run，这里经 ref 调用最新的）
  const commands = useMemo(() => buildCommands(theme, (cmd) => runRef.current(cmd)), [theme]);
  const scenes = useMemo(() => sceneCommands(catalogScenes(), (cmd) => runRef.current(cmd)), []);

  const groups = useMemo(() => {
    const query = q.trim();
    if (!query) {
      return groupResults([...commands.filter(c => c.g !== "构思"), ...scenes.slice(0, SCENES_WHEN_EMPTY), ...commands.filter(c => c.g === "构思")]);
    }
    const score = (c) => ({ c, ...fuzzy(query, `${c.label} ${c.kw || ""} ${c.hint || ""}`) });
    const hitScenes = scenes.map(score).filter(x => x.ok).sort((a, b) => b.score - a.score).slice(0, SCENES_WHEN_SEARCHING);
    const hits = [...commands.map(score).filter(x => x.ok), ...hitScenes].sort((a, b) => b.score - a.score);
    return groupResults(hits.map(x => x.c));
  }, [q, commands, scenes]);

  const flat = useMemo(() => groups.flatMap(gr => gr.items), [groups]);

  useEffect(() => { if (sel >= flat.length) setSel(Math.max(0, flat.length - 1)); }, [flat.length, sel]);

  useEffect(() => {
    const el = listRef.current && listRef.current.querySelector(`[data-i="${sel}"]`);
    if (el && typeof el.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
  }, [sel]);

  const choose = (c) => {
    if (!c) return;
    onClose();
    c.run();
  };

  const onKeyDown = (e) => {
    if (isImeComposing(e)) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setSel(s => Math.min(flat.length - 1, s + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel(s => Math.max(0, s - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); choose(flat[sel]); }
  };

  let running = -1;
  return (
    <WsDialog onClose={onClose} label="命令面板" size="lg" className="pal" scrimClassName="pal-wrap" initialFocus={inputRef}>
      <div className="pal-search">
        <I.Search size={18} />
        <input ref={inputRef} className="pal-input" value={q} placeholder="跳到页面、场景或构思步骤，或输入命令…"
          role="combobox" aria-expanded="true" aria-controls={listId} aria-autocomplete="list"
          aria-activedescendant={flat.length ? optionId(sel) : undefined} aria-label="搜索命令、页面或场景"
          onChange={(e) => { setQ(e.target.value); setSel(0); }} onKeyDown={onKeyDown} spellCheck={false} />
        <kbd className="pal-esc">Esc</kbd>
      </div>

      <div className="pal-list" ref={listRef} role="listbox" id={listId} aria-label="结果">
        {groups.length === 0 && (
          <div className="pal-empty" role="presentation"><I.Search size={22} /><span>没有匹配「{q}」的结果</span></div>
        )}
        {groups.map(gr => {
          const headId = `${baseId}-g-${gr.g}`;
          return (
            <div className="pal-group" key={gr.g} role="group" aria-labelledby={headId}>
              <div className="pal-group-h" id={headId}>{gr.g}</div>
              {gr.items.map(c => {
                running++;
                const i = running;
                const Ic = I[c.icon] || I.Dot;
                return (
                  <div key={i} id={optionId(i)} data-i={i} role="option" aria-selected={sel === i}
                    className={`pal-item ${sel === i ? "is-sel" : ""}`}
                    onMouseMove={() => { if (sel !== i) setSel(i); }}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => choose(c)}>
                    <span className="pal-item-ic"><Ic size={17} /></span>
                    <span className="pal-item-label">{c.label}</span>
                    {c.state && <span className={`pal-dot s-${c.state}`} aria-hidden="true" />}
                    {c.hint && <span className="pal-item-hint">{c.hint}</span>}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>

      <div className="pal-foot" aria-hidden="true">
        <span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>
        <span><kbd>↵</kbd> 打开</span>
        <span><kbd>Esc</kbd> 关闭</span>
        <span className="pal-foot-spacer" />
        <span className="pal-foot-tip">随时按 <kbd>{modShortcut("K")}</kbd> 唤出</span>
      </div>
    </WsDialog>
  );
}

/* ⌘K / Ctrl+K：开关命令面板（App 挂一次）。别的模态层（确认框、续写托盘、作品切换、抽屉……）开着时不开：
   面板会压在它的遮罩底下看不见却拿走焦点，回车就把整个应用跳到别页，确认框还悬着。
   面板自己在最上层时照旧是开关。仍然 preventDefault：不让浏览器把焦点抢去地址栏的搜索。 */
function usePaletteShortcut(setPalette) {
  useEffect(() => {
    const onKey = (e) => {
      if (!(e.metaKey || e.ctrlKey) || isImeComposing(e) || String(e.key || "").toLowerCase() !== "k") return;
      e.preventDefault();
      const top = topModalLayer();
      if (top && !(top.classList && top.classList.contains("pal"))) return;
      setPalette((open) => !open);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPalette]);
}

export { WsPalette, fuzzy, usePaletteShortcut };
