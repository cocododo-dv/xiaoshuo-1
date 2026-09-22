import React from "react";
import { I } from "./icons.jsx";
import { WsWorks, useActiveWork, useWorksStatus, wsKey } from "./ws-works.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { useDiagnosisSummary } from "./ws-diagnosis-summary.jsx";
import { RV_KINDS, rvMarkResolved, useReviewOpenItems } from "./ws-review.jsx";
import { Notice } from "./ws-ui.jsx";
import { LEGACY_DRAFT_PLACEHOLDER, hasAuthorText, stripLegacyDraftPlaceholder } from "./manuscript-html.js";
import {
  hmBookProgress, hmChapterWindow, hmDeriveSpine, hmFocusModel, hmResumeModel, hmSnowLoadState, hmSnowSummary,
} from "./ws-home-derive.js";
import { HomeRing, WsAiSetupNotice, WsHomeDataNotice } from "./ws-home-parts.jsx";
import { HmChapters } from "./ws-home-chapters.jsx";
import { WsHomeBlank, WsHomeCatalogError, WsHomeLoading, WsHomeNoWorks } from "./ws-home-states.jsx";

/* ==========================================================
   WsHome — 项目主页
   一进来就回答一个问题：「现在该写哪一场？」其余都是安静的背景。
   版面：抬头（书名 + 全书进度）→ 焦点卡（这一场 + 上次写下的句子）→ 雪花 / 待办 → 全书章节
   （进度脊 + 当前章附近的几章，ws-home-chapters.jsx）。
   单一真相：
   · 焦点场景 = WsCatalog.focusScene()（写作台、AI 起草台落点同一条规则），进度脊的「前线」就是它所在的章；
   · 章的阶段与叫法 = ws-labels（与成稿中心同一份）；
   · 雪花卡只读服务端 dashboard 的十步状态；
   · 待办读收件箱 store（useReviewOpenItems：列表 + 是否已装载 + 装载失败）；
   · 「上次写到这里」优先读写作台落盘的正文缓存，服务端 dashboard 作兜底；两边都先去掉旧草稿开头的空白页占位。
   纯派生在 ws-home-derive.js；还没有一本可看的书时（读取中 / 目录读不到 / 书架空 / 空白作品）
   的版面在 ws-home-states.jsx，两边共用的进度环与提示条在 ws-home-parts.jsx。
   ========================================================== */

/* —— 待办速览的数据源 ——
   收件箱 store（ws-review.jsx）的订阅式读取 useReviewOpenItems：ready = 当前作品的收件箱已从后端拉回来过，
   error = 最近一次拉取失败（失败不广播 ws:review-changed，只通知这类订阅）。
   没拉回来之前说「正在读取待办…」，拉回来确实是空的才说「都处理完了」；还没拉到过就失败才说读不到——
   拉到过之后的失败保留旧列表，不打扰。
   不能在主页一键划掉的三种卡，与收件箱的 rvNeedsChoice 同一条规则：实时派生项（live，只能去源头改好，
   后端拒绝直接划掉）、带真实效果的动作、带候选项的卡——这些只给「去收件箱」一个出口。 */
function useHomeTodos(limit = 3) {
  const review = useReviewOpenItems();
  const items = review.items
    .slice().sort((a, b) => a.priority - b.priority).slice(0, limit)
    .map(it => ({
      id: it.id,
      kind: it.kind,
      title: String(it.title || "").trim() || "（未命名待办）",
      live: !!it.live,
      needsChoice: !!(it.live || (it.actions || []).some(a => a && a.effect) || it.options),
    }));
  return { items, ready: review.ready, error: review.error };
}

/* 行按钮的悬停说明：说清点下去去哪、为什么这一行没有「标记已处理」 */
function hmTodoOpenTitle(it) {
  if (it.kind === "decision") return "需要你拍板：去收件箱选一个选项";
  if (it.live) return "去待办收件箱处理：这一条在源头改好后会自己消失";
  if (it.needsChoice) return "去待办收件箱处理：这一条要在那里选怎么处理";
  return "去待办收件箱处理";
}

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
  React.useLayoutEffect(() => {
    const measure = () => {
      const el = ref.current;
      if (!el || open) return;
      setClamped(el.scrollHeight - el.clientHeight > 2);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [v, open]);
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

/* 待办速览卡：与「待办收件箱」同源（store），取优先级最高的几条。
   在这里「标记已处理」会真实落盘（store 乐观移除并广播），徽标与收件箱同步消失。 */
function HmTodoCard({ go }) {
  const { items: todos, ready, error } = useHomeTodos(3);
  const decisionsLeft = todos.filter(t => t.kind === "decision").length;
  const listRef = React.useRef(null);
  const allRef = React.useRef(null);
  const pendingFocus = React.useRef(null);

  /* 「标记已处理」让这一行连同拿着焦点的按钮一起消失。等这一行真从列表里下去了，
     把焦点交给顶上来的那一行（划掉的是最后一行就交给上一行），列表空了交给「全部」——
     键盘和读屏用户不会被甩回页面开头。焦点已经在别处（作者点了别的）就不抢。 */
  React.useLayoutEffect(() => {
    const pending = pendingFocus.current;
    if (!pending || todos.some(t => t.id === pending.id)) return;
    pendingFocus.current = null;
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    const opens = listRef.current ? listRef.current.querySelectorAll(".home-todo-open") : [];
    const target = opens.length ? opens[Math.min(pending.index, opens.length - 1)] : allRef.current;
    if (target) target.focus();
  });

  const markResolved = (it, index) => {
    pendingFocus.current = { id: it.id, index };
    rvMarkResolved([it.id]);
  };

  return (
    <article className="home-card">
      <div className="home-card-head">
        <h2 className="home-card-title"><span className="ic"><I.Inbox size={17} /></span> 待办收件箱</h2>
        <button type="button" ref={allRef} className="home-card-go home-card-go-btn" onClick={() => go("review")}>全部 <I.ArrowRight size={13} /></button>
      </div>
      <div className="home-todo-list" ref={listRef}>
        {todos.length === 0 && !ready && error && (
          <Notice tone="warn" className="home-todo-error" testId="home-todo-error" title="待办暂时读不出来"
            actions={(
              <button type="button" className="btn btn-ghost btn-sm" data-testid="home-todo-open-review" onClick={() => go("review")}>
                去待办里重试
              </button>
            )}>
            后端可能没连上。待办存在后端，不会丢。
          </Notice>
        )}
        {todos.length === 0 && !ready && !error && (
          <div className="home-todo-empty is-loading" role="status">正在读取待办…</div>
        )}
        {todos.length === 0 && ready && (
          <div className="home-todo-empty"><I.CheckCircle size={18} /> 待办都处理完了，回去继续写吧。</div>
        )}
        {todos.map((it, index) => {
          const m = RV_KINDS[it.kind] || { tone: "slate", label: "待办" };
          // 决策项、实时派生项、带效果 / 候选项的卡只能去收件箱处理，不给一键划掉
          const canResolve = it.kind !== "decision" && !it.needsChoice;
          return (
            <div className="home-todo" key={it.id}>
              <button type="button" className="home-todo-open" onClick={() => go("review")} title={hmTodoOpenTitle(it)}>
                <span className={`pill pill-${m.tone} text-xs`}><span className="pill-dot" />{m.label}</span>
                <span className="home-todo-text">{it.title}</span>
              </button>
              {canResolve && (
                <button type="button" className="home-todo-go" title="标记已处理" aria-label={`标记已处理：${it.title}`}
                  onClick={() => markResolved(it, index)}>
                  <I.Check size={14} />
                </button>
              )}
            </div>
          );
        })}
        {todos.length > 0 && (
          <div className="home-todo-foot">
            {decisionsLeft
              ? <>其中 <b>{decisionsLeft}</b> 条需要你拍板；其余在收件箱里。</>
              : "这里只列最紧要的几条，其余在收件箱里。"}
          </div>
        )}
      </div>
    </article>
  );
}

function WsHome({ go, mode = "writer" }) {
  const work = useActiveWork();
  const remote = useWorksStatus(work && work.id);
  const chapters = useCatalogChapters();

  if (work.id === "__loading__" || (!work.id && remote.projects.phase === "loading")) {
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
  const hero = hmFocusModel(WsCatalog.focusScene(), home);
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
  const resume = hmResumeModel(hmReadCachedDraft(hero.sid), home.resume, hero.scene ? hero.scene.words : undefined);
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

        <HmTodoCard go={go} />
      </section>

      {/* ===== whole book: per-chapter progress spine + chapters around the front ===== */}
      <HmChapters spine={spine} windowed={windowed} allChapters={allChapters} openScene={openScene} diagnosis={diagnosis} />
    </div>
  );
}

export { WsHome };
