import React from "react";
import { I } from "./icons.jsx";
import { WsWorks, useActiveWork, useWorksStatus, wsKey } from "./ws-works.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { useDiagnosisSummary } from "./ws-diagnosis-summary.jsx";
import { attentionOf } from "./ws-attention.js";
import { Tag } from "./ws-ui.jsx";
import { useWindowEvents } from "./lib/events.js";
import { LEGACY_DRAFT_PLACEHOLDER, hasAuthorText, stripLegacyDraftPlaceholder } from "./manuscript-html.js";
import {
  hmBookProgress, hmChapterWindow, hmDeriveSpine, hmFocusModel, hmResumeModel, hmSnowLoadState, hmSnowSummary,
} from "./ws-home-derive.js";
import { HomeRing, WsAiSetupNotice, WsHomeDataNotice } from "./ws-home-parts.jsx";
import { HmChapters } from "./ws-home-chapters.jsx";
import { WsHomeBlank, WsHomeCatalogError, WsHomeLoading, WsHomeNoWorks } from "./ws-home-states.jsx";
import { LOADING_WORK_ID } from "./lib/work-id.js";

/* ==========================================================
   WsHome — 项目主页
   一进来就回答一个问题：「现在该写哪一场？」其余都是安静的背景。
   版面：抬头（书名 + 全书进度）→ 焦点卡（这一场 + 上次写下的句子）→ 雪花 / 等你处理 → 全书章节
   （进度脊 + 当前章附近的几章，ws-home-chapters.jsx）。
   单一真相：
   · 焦点场景 = WsCatalog.focusScene()（写作台、AI 起草台落点同一条规则），进度脊的「前线」就是它所在的章；
     全书还没有一场时焦点卡是空的（不再拿 dashboard 缓存里的旧场顶上）；
   · 章的阶段与叫法 = ws-labels（与成稿中心同一份）；
   · 雪花卡只读服务端 dashboard 的十步状态；
   · 「等你处理」从目录现算（ws-attention.js：被管线停下的场、写齐了等你批准的章），与侧栏徽标同一份；
   · 「上次写到这里」优先读写作台落盘的正文缓存，服务端 dashboard 作兜底；两边都先去掉旧草稿开头的空白页占位。
   纯派生在 ws-home-derive.js；还没有一本可看的书时（读取中 / 目录读不到 / 书架空 / 空白作品）
   的版面在 ws-home-states.jsx，两边共用的进度环与提示条在 ws-home-parts.jsx。
   ========================================================== */

/* 写作台落盘的正文缓存是 HTML；用 DOMParser 在惰性文档里解析（不会执行任何东西，也不挂到页面上）。
   旧版本草稿开头可能留着空白页占位句，先按写作台同一条规则去掉，再取段落。
   返回 { lines, placeholderOnly }：placeholderOnly = 缓存里的字全是开头那句旧占位（交给 hmResumeModel 决定怎么说）。 */
function hmCachedDraft(raw) {
  if (raw == null || typeof DOMParser === "undefined") return null;
  const parse = (html) => new DOMParser().parseFromString(html, "text/html");
  const lines = Array.from(parse(stripLegacyDraftPlaceholder(raw)).querySelectorAll("p, li"))
    .map(x => (x.textContent || "").trim()).filter(Boolean);
  const placeholderOnly = !lines.length && !hasAuthorText(raw)
    && String(parse(raw).body.textContent || "").replace(/\s/g, "").includes(LEGACY_DRAFT_PLACEHOLDER);
  return { lines, placeholderOnly };
}

/* 站点数据被禁用时读 localStorage 会抛错；读不到就走 dashboard 兜底 */
function hmReadCachedDraft(sid) {
  if (!sid) return null;
  try {
    return hmCachedDraft(localStorage.getItem(wsKey("wr-doc:" + sid)));
  } catch (e) { return null; }
}

/* 三拍里的某一拍可能很长（多轮冲突），默认最多三行，需要时展开——否则 1100×760 下按钮被挤出首屏。 */
function HmBeat({ k, tone, v }) {
  const ref = React.useRef(null);
  const [open, setOpen] = React.useState(false);
  const [clamped, setClamped] = React.useState(false);
  const measure = () => {
    const el = ref.current;
    if (!el || open) return;
    setClamped(el.scrollHeight - el.clientHeight > 2);
  };
  React.useLayoutEffect(measure, [v, open]);
  useWindowEvents({ resize: measure });
  return (
    <div className="hm-gos-row">
      <span className={`hm-gos-k t-${tone}`}>{k}</span>
      <div className="hm-gos-body">
        <span ref={ref} className={`hm-gos-v ${open ? "is-open" : ""}`}>{v}</span>
        {(clamped || open) && (
          <button type="button" className="hm-gos-more" aria-expanded={open} onClick={() => setOpen(o => !o)}>
            {open ? "收起" : "展开"}
          </button>
        )}
      </div>
    </div>
  );
}

/* 「等你处理」：AI 稿被管线停下的场、写齐了等你批准的章（ws-attention.js 从目录现算，侧栏徽标同一份；
   取代 2026-10-21 退役的待办收件箱）。一行一件事，点了直达处理它的那一页的那一章 / 那一场；源头处理完自己消失，
   所以没有「标记已处理」。章在前（按章依次定稿，批了才轮到下一章），场在后，最多列 HM_ATTN_ROWS 行。 */
const HM_ATTN_ROWS = 4;

function HmAttentionCard({ chapters, go }) {
  const att = attentionOf(chapters);
  const rows = [
    ...att.chapters.map((c) => ({
      key: c.key, kind: "chapter", tag: "等你批准", tone: "warn", text: c.text,
      title: "这一章各场都有终稿了：去成稿中心通读，批准为终稿",
      open: () => go("manuscripts", { type: "ws:manu-chapter", detail: c.id }),
    })),
    ...att.scenes.map((s) => ({
      key: s.key, kind: "scene", tag: s.label, tone: s.tone, text: s.text,
      title: "AI 起草台把这一场的稿子停下了：去起草台打开这一场处理",
      open: () => go("scene", { type: "ws:scene-enqueue", detail: { sid: s.sid } }),
    })),
  ];
  const shown = rows.slice(0, HM_ATTN_ROWS);
  const hidden = rows.slice(HM_ATTN_ROWS);
  const hiddenScenes = hidden.filter((r) => r.kind === "scene").length;
  const hiddenChapters = hidden.length - hiddenScenes;
  const hiddenWhere = [
    hiddenChapters ? `成稿中心 ${hiddenChapters} 章` : "",
    hiddenScenes ? `AI 起草台 ${hiddenScenes} 场` : "",
  ].filter(Boolean).join("、");

  return (
    <article className="home-card" aria-label="等你处理" data-testid="home-attention">
      <div className="home-card-head">
        <h2 className="home-card-title"><span className="ic"><I.Inbox size={17} /></span> 等你处理</h2>
      </div>
      <div className="home-todo-list">
        {shown.length === 0 && (
          <div className="home-todo-empty">
            <I.CheckCircle size={18} /> 现在没有等你处理的事。AI 稿被管线停下、或者一章写齐了等你批准，会列在这里。
          </div>
        )}
        {shown.map((row) => (
          <div className="home-todo" key={row.key}>
            <button type="button" className="home-todo-open" onClick={row.open} title={row.title}>
              <Tag tone={row.tone} dot>{row.tag}</Tag>
              <span className="home-todo-text">{row.text}</span>
            </button>
          </div>
        ))}
        {hidden.length > 0 && (
          <div className="home-todo-foot">还有 <b>{hidden.length}</b> 项没列出：{hiddenWhere}。</div>
        )}
      </div>
    </article>
  );
}

function WsHome({ go, mode = "writer" }) {
  const work = useActiveWork();
  const remote = useWorksStatus(work && work.id);
  const chapters = useCatalogChapters();

  if (work.id === LOADING_WORK_ID || (!work.id && remote.projects.phase === "loading")) {
    return <WsHomeLoading label="书架" />;
  }
  if (!work.id) return <WsHomeNoWorks remote={remote} />;
  if (chapters.length === 0) {
    // 目录还没读到时不能先说「这部作品还是一张白纸」
    const catalogError = WsCatalog.loadError();
    if (catalogError) return <WsHomeCatalogError work={work} remote={remote} error={catalogError} />;
    if (!WsCatalog.ready()) return <WsHomeLoading label="章节目录" />;
    return <WsHomeBlank work={work} go={go} remote={remote} />;
  }
  return <WsHomeFull work={work} go={go} mode={mode} chapters={chapters} remote={remote} />;
}

/* ===== full home — a work with momentum ===== */
function WsHomeFull({ work: p, go, mode, chapters, remote }) {
  const home = p.home || {};

  /* 雪花卡读服务端 dashboard：回到主页时拉一次，构思里刚确认的步骤就能反映出来。
     作品列表还在读时先不拉——列表读完 store 会顺带拉当前作品的 dashboard，这时再走到这里会并进那一次在途请求。 */
  const projectsLoading = Boolean(remote && remote.projects && remote.projects.phase === "loading");
  React.useEffect(() => {
    if (!p.id || projectsLoading) return;
    void WsWorks.retry("dashboard", p.id);
  }, [p.id, projectsLoading]);

  /* —— 焦点场景（单一真相源）：WsCatalog.focusScene()，进度脊的前线就是它所在的章
     （全书还没有一场时 hero.chapter 为空，hmDeriveSpine 自己退回当前章）—— */
  const hero = hmFocusModel(WsCatalog.focusScene());
  const spine = hmDeriveSpine(chapters, hero.chapter);
  const windowed = hmChapterWindow(chapters, spine);
  /* 各章还开着的诊断发现数（写作台深改面板的同一份）：章卡角标 + 进度脊一句 */
  const diag = useDiagnosisSummary();
  const diagnosis = { byChapter: {}, totals: diag.loaded() ? diag.totals() : null };
  chapters.forEach((c) => { const counts = c && c.backendId ? diag.chapterCounts(c.backendId) : null; if (counts) diagnosis.byChapter[c.id] = counts; });
  const openScene = (sid) => {
    if (sid) go("writer", { type: "ws:writer-scene", detail: sid });
    else go("writer");
  };
  const enterWriter = () => openScene(hero.sid);

  /* —— 进度（与切换器 / 成稿中心同源）—— */
  const book = hmBookProgress(WsCatalog.totals(), p);
  const dayPct = Math.min(100, Math.round((p.wordsToday / Math.max(1, p.wordsTargetDay)) * 100));

  const snow = hmSnowSummary(home.snow);
  const snowLoad = hmSnowLoadState(remote);
  const snowNext = () => {
    if (!snow.next) return go("snowflake");
    if (snow.next.action === "write") return enterWriter();
    return go("snowflake", snow.next.key ? { type: "ws:snow-step", detail: snow.next.key } : undefined);
  };

  /* —— 「上次写到这里」：优先读写作器落盘的真实正文（取末两段），服务端 dashboard 作兜底；
     两边都先去掉旧草稿开头的空白页占位（规则见 hmResumeModel）。「暂停于」只配服务端的句子 —— */
  /* 服务端那几行按当前章算；焦点场不是那一场（当前章写完、焦点挪到下一章）时不拿来顶 */
  const serverResume = home.resume && home.resume.sceneId && hero.scene && hero.scene.backendId
    && home.resume.sceneId !== hero.scene.backendId ? null : home.resume;
  const resume = hmResumeModel(hmReadCachedDraft(hero.sid), serverResume, hero.scene ? hero.scene.words : undefined);
  const resumeLines = resume.lines;

  const allChapters = mode === "advanced"
    ? { label: "章节编排", title: "在章节编排里看全部章节", onClick: () => go("author") }
    : { label: "全部章节", title: "在写作台的大纲里看全部章节", onClick: () => go("writer") };

  return (
    <div className="ws-page ws-view hm">
      <WsHomeDataNotice remote={remote} workId={p.id} />
      {/* ===== masthead — identity + book progress ===== */}
      <header className="hm-top">
        <div className="hm-id">
          <h1 className="hm-title">{p.title}</h1>
          {p.sub ? <p className="hm-logline">{p.sub}</p> : null}
        </div>
        <div className="hm-book" role="group" aria-label="全书进度">
          <HomeRing pct={book.pct} size={66} />
          <div className="hm-book-meta">
            <div className="hm-book-lbl">全书进度</div>
            <div className="hm-book-val" title="已经有正文的章 / 目录里的章">动笔 <b>{book.written}</b> / {book.planned} 章</div>
            <div className="hm-book-sub">{book.wordsWan} 万 / {book.targetWan} 万字</div>
          </div>
        </div>
      </header>

      {/* ===== focus hero — the one scene to write now ===== */}
      <section className="hm-hero" aria-label="现在该写的一场">
        <div className="hm-hero-main">
          <div className="hm-hero-bar">
            <span className="hm-locator">
              {hero.slug ? <span className="hm-slug">{hero.slug}</span> : null}
              {hero.kind ? <span className="hm-kind">{hero.kind}</span> : null}
            </span>
            <span className="hm-today" title="今日写作目标">
              <span className="hm-today-txt">今日 <b>{p.wordsToday.toLocaleString()}</b> / {p.wordsTargetDay.toLocaleString()} 字</span>
              <span className="hm-today-bar" aria-hidden="true"><i style={{ width: dayPct + "%" }} /></span>
              {p.streak > 0 ? <span className="hm-today-streak"><I.Activity size={12} /> 连续 {p.streak} 天</span> : null}
            </span>
          </div>
          <h2 className="hm-scene">{hero.title || "—"}</h2>
          <div className="hm-gos">
            {hero.beats.map(g => <HmBeat key={g.k} k={g.k} tone={g.tone} v={g.v} />)}
          </div>
          <div className="hm-hero-actions">
            <button type="button" className="btn btn-accent btn-lg" data-testid="home-enter-writer" onClick={enterWriter}><I.Pen size={16} /> 进入写作房间</button>
            <button type="button" className="btn btn-ghost btn-lg" onClick={() => go("snowflake")}><I.Snowflake size={16} /> 回到构思</button>
            {/* 放在按钮行里而不是页顶：状态晚到时不会把整页往下推 */}
            <WsAiSetupNotice go={go} compact />
          </div>
        </div>

        <article className="hm-resume" aria-label="上次写到这里">
          <div className="hm-resume-head"><I.Quote size={12} /> 上次写到这里</div>
          <div className="hm-resume-body">
            {resumeLines.length === 0 && <p>这一场还没有正文——进去写下第一段，它会出现在这里。<span className="hm-caret" /></p>}
            {resumeLines.map((ln, i) => (
              <p key={i}>{ln}{i === resumeLines.length - 1 ? <span className="hm-caret" /> : null}</p>
            ))}
          </div>
          <div className="hm-resume-foot">
            <span className="hm-resume-words">本场 {resume.words.toLocaleString()} 字</span>
            {resume.pausedAgo ? <span className="hm-resume-ago"><I.Clock size={11} /> 暂停于 {resume.pausedAgo}</span> : null}
            <button type="button" className="btn btn-quiet btn-sm hm-resume-go" onClick={enterWriter}>接着写</button>
          </div>
        </article>
      </section>

      {/* ===== secondary: snowflake + todo ===== */}
      <section className="home-row">
        <article className="home-card">
          <div className="home-card-head">
            <h2 className="home-card-title"><span className="ic"><I.Snowflake size={17} /></span> 雪花构思</h2>
            <button type="button" className="home-card-go home-card-go-btn" onClick={() => go("snowflake")}>打开 <I.ArrowRight size={13} /></button>
          </div>
          {snow.total > 0 ? (
            <>
              <div className="home-snow-track" aria-hidden="true">
                {snow.steps.map((s, i) => <span key={i} className={`home-snow-tick s-${s.s}`} title={s.name} />)}
              </div>
              <div className="home-snow-now">
                <div className="home-snow-now-main">
                  <div className="home-snow-now-label">{snow.allDone ? "构思" : "下一步"}</div>
                  <div className="home-snow-now-name">{snow.now}</div>
                </div>
                <div className="home-snow-count"><b>{snow.done}</b> <span className="text-muted text-sm">/ {snow.total} 已确认</span></div>
              </div>
              {snow.next && (
                <button type="button" className="btn btn-ghost btn-sm home-snow-next" onClick={snowNext}>
                  {snow.next.action === "write" ? <I.Pen size={13} /> : <I.Compass size={13} />} {snow.next.label}
                </button>
              )}
            </>
          ) : snowLoad === "loading" ? (
            <div className="home-todo-empty is-loading" role="status">正在读取构思进度…</div>
          ) : snowLoad === "error" ? (
            <div className="home-todo-empty">构思进度暂时读不到，点「打开」直接去构思里看。</div>
          ) : (
            <div className="home-todo-empty">还没有构思进度，点「打开」从第一步开始。</div>
          )}
        </article>

        <HmAttentionCard chapters={chapters} go={go} />
      </section>

      {/* ===== whole book: per-chapter progress spine + chapters around the front ===== */}
      <HmChapters spine={spine} windowed={windowed} allChapters={allChapters} openScene={openScene} diagnosis={diagnosis} />
    </div>
  );
}

export { WsHome };
