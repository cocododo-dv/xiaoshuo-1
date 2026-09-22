import React from "react";
import { I } from "./icons.jsx";
import { WsWorks, useActiveWork, useWorks } from "./ws-works.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { AISettings } from "./ws-settings-ai.jsx";
import { Section, Row, Field, Toggle } from "./ws-settings-shared.jsx";
import { PageHeader, Segmented } from "./ws-ui.jsx";
import { WS_LINE_HEIGHT_PRESETS, WS_PREFS, lineHeightPreset } from "./ws-prefs.js";
import { WS_NAV_GROUPS } from "./ws-nav.js";
import { setViewIntentTargetReady } from "./ws-view-intents.js";
import { wsConfirm } from "./ws-notify.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { WR_ANNO_KEY_PREFIX, wrAnnoLoad } from "./ws-writer-annotations.js";

const { useState: useSt6, useEffect: useEf6, useLayoutEffect: useLayout6, useRef: useRef6 } = React;

/* ==========================================================
   Settings — 设置
   页签：项目 · AI 模型 · 外观 · 数据与安全
   ----------------------------------------------------------
   · 项目页读写 WsWorks（当前作品），状态由 WsCatalog 实时汇总
   · 外观是全局外观偏好的完整入口，按 ws-prefs.js 的同一份 schema 生成；
     侧栏底部「排版与舒适度」是快捷面板，两处读写同一份 ws_tweaks_v1
   · 数据页：导出、同步与恢复中心、危险区都是真实操作
   · 其它页面跳来时用 ws:settings-tab 视图指令直接落到某个页签（「AI 尚未就绪，去配置」→ AI 模型）
   2026-09-21：删除「写作偏好」页签和「AI 行为 · 生成候选数」——它们只写 localStorage，没有任何代码读取。
   ========================================================== */

const S_TABS = [
  { id: "project", label: "项目", icon: "Folder" },
  { id: "ai",      label: "AI 模型", icon: "Sparkles" },
  { id: "appear",  label: "外观",  icon: "Type" },
  { id: "data",    label: "数据与安全", icon: "ShieldCheck" },
];
const S_TAB_IDS = S_TABS.map(t => t.id);
/* 同一个浏览器会话里记住上次停在哪个页签 */
const S_TAB_KEY = "ws_settings_tab_v1";

function readSessionTab() {
  try {
    const v = sessionStorage.getItem(S_TAB_KEY);
    return S_TAB_IDS.includes(v) ? v : "project";
  } catch (e) { return "project"; }
}

function WsSettings({ go, t, setTweak }) {
  const [tab, setTabState] = useSt6(readSessionTab);
  const setTab = (id) => {
    if (!S_TAB_IDS.includes(id)) return;
    setTabState(id);
    try { sessionStorage.setItem(S_TAB_KEY, id); } catch (e) {}
  };

  /* 视图指令：detail 是页签 id（"ai"），或 { tab: "ai" }。先挂监听再宣布就绪（就绪时会同步投递排队的指令）；
     用 layout effect，页签在首帧绘制前就切过去，不会先闪一下「项目」。 */
  useLayout6(() => {
    const onTab = (e) => {
      const d = e && e.detail;
      setTab(typeof d === "string" ? d : (d && d.tab));
    };
    window.addEventListener("ws:settings-tab", onTab);
    setViewIntentTargetReady("settings");
    return () => {
      setViewIntentTargetReady("settings", false);
      window.removeEventListener("ws:settings-tab", onTab);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const current = S_TABS.find(x => x.id === tab) || S_TABS[0];

  return (
    <div className="page" data-screen-label="settings">
      <div className="page-narrow">
        <PageHeader
          title="设置"
          description="「项目」只影响当前作品，其余是全局设置。改动即时保存。"
        />

        <div className="settings-cols">
          <nav className="settings-nav" aria-label="设置分类">
            {S_TABS.map(x => {
              const Ic = I[x.icon] || I.Dot;
              const on = tab === x.id;
              return (
                <button type="button" key={x.id} className={`settings-nav-btn ${on ? "is-active" : ""}`}
                  aria-current={on ? "page" : undefined} onClick={() => setTab(x.id)}>
                  <Ic size={15} /><span>{x.label}</span>
                </button>
              );
            })}
          </nav>

          <section className="settings-body" aria-label={current.label}>
            {tab === "project" && <ProjectSettings />}
            {tab === "ai" && <AISettings />}
            {tab === "appear" && <AppearSettings t={t} setTweak={setTweak} />}
            {tab === "data" && <DataSettings go={go} />}
          </section>
        </div>
      </div>
    </div>
  );
}

/* ===== 项目 — 读写当前作品（WsWorks），状态来自目录汇总 ===== */

/* 一个即时保存的字段：失焦或回车提交、Esc 放弃并恢复原值，保存后在旁边说一声「已保存」。
   WsWorks.update 是乐观写 + 失败回滚（回滚时它自己弹错误提示）；回滚后这里没在编辑就跟着恢复原值，
   不会留着一个没存上的值。数字字段填 0 / 空、必填字段清空，都恢复原值并说明，不再被静默忽略。 */
function ProjectField({ label, hint, value, onSave, type = "text", step, stacked = false, placeholder, multiline = false, required = false }) {
  const stored = value == null ? "" : String(value);
  const [draft, setDraft] = useSt6(stored);
  const [status, setStatus] = useSt6(null); /* { tone: ok|warn, text } */
  const editingRef = useRef6(false);
  const cancelRef = useRef6(false);
  const savedRef = useRef6(null);
  const timerRef = useRef6(null);

  const flash = (next) => {
    setStatus(next);
    if (timerRef.current) clearTimeout(timerRef.current);
    if (next && next.tone === "ok") timerRef.current = setTimeout(() => setStatus(null), 2400);
  };
  useEf6(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  useEf6(() => {
    if (savedRef.current != null) {
      if (stored === savedRef.current) {
        flash({ tone: "ok", text: "已保存" });
      } else {
        flash({ tone: "warn", text: "没保存上，已恢复原来的值" });
        setDraft(stored);
      }
      savedRef.current = null;
      return;
    }
    if (!editingRef.current) setDraft(stored);
  }, [stored]); // eslint-disable-line react-hooks/exhaustive-deps

  const commit = () => {
    editingRef.current = false;
    const raw = draft.trim();
    if (type === "number") {
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n) || n <= 0) {
        setDraft(stored);
        flash({ tone: "warn", text: "请填一个大于 0 的整数，已恢复原来的值" });
        return;
      }
      if (String(n) === stored) { setDraft(stored); return; }
      savedRef.current = String(n);
      setDraft(String(n));
      onSave(n);
      return;
    }
    if (raw === stored) { setDraft(stored); return; }
    if (required && !raw) {
      setDraft(stored);
      flash({ tone: "warn", text: `${label}不能为空，已恢复原来的值` });
      return;
    }
    savedRef.current = raw;
    onSave(raw);
  };

  const Control = multiline ? "textarea" : "input";
  return (
    <Row label={label} hint={hint} stacked={stacked}>
      <Field
        as={Control}
        className={`${multiline ? "textarea" : "input"} set-field ${stacked ? "is-wide" : ""}`}
        type={multiline ? undefined : type}
        inputMode={type === "number" ? "numeric" : undefined}
        step={step}
        rows={multiline ? 2 : undefined}
        value={draft}
        placeholder={placeholder}
        onFocus={() => { editingRef.current = true; cancelRef.current = false; }}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          /* Esc 触发的失焦是「撤销」，不是提交：blur 会同步触发 onBlur，而这时 commit 闭包里的 draft
             还是刚才敲的值（setDraft 还没渲染），不拦住就会把想放弃的内容存上去。 */
          if (cancelRef.current) { cancelRef.current = false; return; }
          commit();
        }}
        onKeyDown={(e) => {
          if (isImeComposing(e)) return;   // 输入法组词时的回车 / Esc 属于输入法
          if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.blur(); }
          if (e.key === "Escape") {
            cancelRef.current = true;
            editingRef.current = false;
            setDraft(stored);
            e.currentTarget.blur();
          }
        }}
      />
      <span className={`set-field-status ${status ? `is-${status.tone}` : ""}`} role="status" aria-live="polite">
        {status ? status.text : ""}
      </span>
    </Row>
  );
}

function ProjectSettings() {
  const work = useActiveWork ? useActiveWork() : { id: "", title: "", genre: "", sub: "", wordsTarget: 0, wordsTargetDay: 0, streak: 0 };
  useCatalogChapters();   // 订阅目录：字数 / 章数变化时这里跟着刷新
  const totals = WsCatalog ? WsCatalog.totals() : { words: 0, written: 0, planned: 0, today: 0 };
  const save = (patch) => { if (WsWorks && work.id) WsWorks.update(work.id, patch); };
  const pct = work.wordsTarget ? Math.min(100, Math.round((totals.words / work.wordsTarget) * 100)) : 0;

  return (
    <>
      <Section title="项目信息" desc={`当前作品《${work.title}》。失焦或回车即保存，书架和主页会同步更新。`}>
        <ProjectField key={work.id + ":t"} label="书名" hint="显示在书架、主页和导出的成稿里。" value={work.title} required
          onSave={(v) => save({ title: v })} />
        <ProjectField key={work.id + ":g"} label="题材" value={work.genre} placeholder="如 悬疑、成长" onSave={(v) => save({ genre: v })} />
        <ProjectField key={work.id + ":s"} label="一句话简介" hint="主页和书架上的那一行。" value={work.sub} stacked multiline
          placeholder="用一句话说这本书讲什么" onSave={(v) => save({ sub: v })} />
        <ProjectField key={work.id + ":w"} label="目标字数" type="number" step="10000" value={work.wordsTarget || ""}
          onSave={(n) => save({ wordsTarget: n })} />
        <ProjectField key={work.id + ":d"} label="每日目标" hint="主页「今日」进度条的分母。" type="number" step="100" value={work.wordsTargetDay || ""}
          onSave={(n) => save({ wordsTargetDay: n })} />
      </Section>
      <Section title="项目状态" desc="由章节目录实时汇总，与主页、书架同源。">
        <Row label="完成度" readonly>
          <div className="set-readonly">{pct}%（{totals.words.toLocaleString()} 字）</div>
        </Row>
        <Row label="已动笔的章" readonly>
          <div className="set-readonly">{totals.written} / {totals.planned} 章</div>
        </Row>
        <Row label="今日已写" readonly>
          <div className="set-readonly">{(totals.today || 0).toLocaleString()} 字，连续 {work.streak || 0} 天</div>
        </Row>
      </Section>
    </>
  );
}

/* ===== 外观 — 全局外观偏好的完整入口 =====
   和侧栏底部「排版与舒适度」快捷面板是同一份设置（ws_tweaks_v1）；取值范围、选项都来自 ws-prefs.js，
   两处永远一致。面板里还有稿纸宽度、专注等写作台细项。 */
const ADVANCED_VIEWS = WS_NAV_GROUPS.filter(g => g.advanced).flatMap(g => g.items.map(it => it.label));

function AppearSettings({ t, setTweak }) {
  const get = (key) => (t && t[key] !== undefined ? t[key] : WS_PREFS[key].default);
  const set = (k, v) => setTweak && setTweak(k, v);
  const fontSpec = WS_PREFS.fontSize;
  const fontSize = get("fontSize");
  const lineHeight = get("lineHeight");
  const lhPreset = lineHeightPreset(lineHeight);
  const lhExact = !WS_LINE_HEIGHT_PRESETS.some(p => Math.abs(p.lineHeight - Number(lineHeight)) < 0.001);
  /* 细调过的行距仍高亮最近的一档，而分段控件不响应点已选中的那一档——所以另给一个「改回」按钮 */
  const lhActive = WS_LINE_HEIGHT_PRESETS.find(p => p.value === lhPreset);

  return (
    <>
      <Section title="主题与界面" desc="全局生效。侧栏底部的昼夜按钮切的也是这里的主题。">
        <Row label={WS_PREFS.theme.label}>
          <Segmented label={WS_PREFS.theme.label} value={get("theme")} onChange={(v) => set("theme", v)} options={WS_PREFS.theme.options} />
        </Row>
        <Row label={WS_PREFS.texture.label} hint="页面底色上一层很淡的纸纹。">
          <Toggle on={get("texture") !== false} onChange={(v) => set("texture", v)} />
        </Row>
        <Row label={WS_PREFS.motion.label} hint="打开、收起、切换时的过渡动画。容易晕的话选「关」。">
          <Segmented label={WS_PREFS.motion.label} value={get("motion")} onChange={(v) => set("motion", v)} options={WS_PREFS.motion.options} />
        </Row>
        <Row label={WS_PREFS.mode.label} hint={`「高级」会在侧栏多出${ADVANCED_VIEWS.join("、")}。`}>
          <Segmented label={WS_PREFS.mode.label} value={get("mode")} onChange={(v) => set("mode", v)} options={WS_PREFS.mode.options} />
        </Row>
      </Section>

      <Section title="正文排版" desc="写作台正文的字号和行距。侧栏底部「排版与舒适度」改的是同一份设置，那里还能调稿纸宽度、专注等细项。">
        <Row label={fontSpec.label} hint={`${fontSpec.min}–${fontSpec.max}px`}>
          <Field
            type="range" className="range"
            min={fontSpec.min} max={fontSpec.max} step={fontSpec.step}
            value={fontSize}
            onChange={(e) => set("fontSize", parseInt(e.target.value, 10))}
          />
          <output className="set-range-value" aria-hidden="true">{fontSize}px</output>
        </Row>
        <Row label={WS_PREFS.lineHeight.label} hint={lhExact ? `当前 ${Number(lineHeight).toFixed(2)}，是在快捷面板里细调的。` : undefined}>
          <Segmented
            label={WS_PREFS.lineHeight.label}
            value={lhPreset}
            onChange={(v) => { const p = WS_LINE_HEIGHT_PRESETS.find(x => x.value === v); if (p) set("lineHeight", p.lineHeight); }}
            options={WS_LINE_HEIGHT_PRESETS.map(p => ({ value: p.value, label: p.label }))}
          />
          {lhExact && lhActive && (
            <button type="button" className="btn btn-quiet btn-sm" onClick={() => set("lineHeight", lhActive.lineHeight)}>
              改回{lhActive.label}（{lhActive.lineHeight.toFixed(2)}）
            </button>
          )}
        </Row>
      </Section>
    </>
  );
}

/* ===== 数据与安全 — 真实动作 ===== */

/* 「清除本机缓存」清哪些键：这部作品在本浏览器里的键都以 ::<作品 id> 结尾。
   批注例外——它只存在本机（不进正文、不上服务器，ws-writer-annotations.js），清掉就再也找不回来，
   所以不在清除范围里；annotations 是保留下来的批注条数，确认框据此告诉作者。 */
export function wsWorkCachePurgePlan(workId, storage = localStorage) {
  const suffix = "::" + workId;
  const doomed = [];
  let annotations = 0;
  for (let i = 0; i < storage.length; i++) {
    const k = storage.key(i);
    if (!k || k.slice(-suffix.length) !== suffix) continue;
    if (k.startsWith(WR_ANNO_KEY_PREFIX)) { annotations += wrAnnoLoad(k).length; continue; }
    doomed.push(k);
  }
  return { doomed, annotations };
}

function DataSettings({ go }) {
  const works = useWorks ? useWorks() : [];
  const work = WsWorks ? WsWorks.active() : { id: "", title: "—" };
  const worksN = works.length || 1;

  const clearLocalCache = async () => {
    let annotations = 0;
    try { annotations = wsWorkCachePurgePlan(work.id).annotations; } catch (e) {}
    const ok = await wsConfirm({
      title: `清除《${work.title}》的本机缓存？`,
      body: "不会删除服务端作品、章节或正文；页面会刷新并重新从服务端读取。"
        + "还没同步的本机恢复记录、构思「历史」里的本机回滚快照会一起删掉且无法撤销——仍需保留的内容请先手工复制。"
        + (annotations ? `这部作品在本机的 ${annotations} 条批注不在清除范围内，会保留。` : "本机批注不在清除范围内，会保留。"),
      confirmLabel: "清除并刷新",
      tone: "danger",
    });
    if (!ok) return;
    try {
      wsWorkCachePurgePlan(work.id).doomed.forEach(k => localStorage.removeItem(k));
    } catch (e) {}
    location.reload();
  };

  const deleteWork = async () => {
    const ok = await wsConfirm({
      title: `删除《${work.title}》？`,
      body: "整部作品会连同全部章节、正文与设定移进回收站，可以在回收站里整体恢复。",
      confirmLabel: "移进回收站",
      tone: "danger",
    });
    if (!ok) return;
    WsWorks.remove(work.id);
    if (go) go("home");
  };

  return (
    <>
      <Section title="导出与备份" desc="导出成稿、查看本机缓存快照、备份数据库是三件不同的事。完整的数据库备份由运维在停机时执行（带完整性与校验和检查），不在浏览器里做。">
        <Row label="导出服务端成稿" hint="逐章核验服务端的定稿正文，可导出 Markdown、TXT 或 Word。" readonly>
          <button type="button" className="btn btn-ghost" onClick={() => go && go("manuscripts")}>去成稿中心 <I.ArrowRight size={13} /></button>
        </Row>
        <Row label="浏览器缓存快照" hint="冲突副本、未同步稿和覆盖前备份，供诊断和人工找回；不含服务端数据库，不能用来迁移项目。" readonly>
          <button type="button" className="btn btn-ghost" onClick={() => window.dispatchEvent(new CustomEvent("ws:recovery-open"))}>
            <I.Database size={13} /> 打开同步与恢复
          </button>
        </Row>
      </Section>
      <Section title="危险区" desc="清缓存不改服务端数据；删除的作品可以从回收站恢复。">
        <Row label="清除本机缓存" hint="删除当前作品在这个浏览器里的缓存与未同步恢复记录，随后从服务端重新载入；本机批注会保留。" readonly>
          <button type="button" className="btn btn-danger" onClick={clearLocalCache}>清除缓存</button>
        </Row>
        <Row label="删除本作品" hint={worksN <= 1 ? "至少要保留一部作品，这是最后一部。" : "整部移进回收站，可以恢复。"} readonly>
          <button type="button" className="btn btn-danger" disabled={worksN <= 1} onClick={deleteWork}>删除作品</button>
        </Row>
      </Section>
    </>
  );
}

export { WsSettings };
