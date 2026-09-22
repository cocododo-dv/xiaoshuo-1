import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { ViewLoading } from "./ws-view-boundary.jsx";
import { HmBanner, HomeRing, WsAiSetupNotice, WsHomeDataNotice } from "./ws-home-parts.jsx";

/* ==========================================================
   主页在「还没有一本可看的书」时的四种版面：读取中、章节目录读不到、书架是空的、
   一部刚新建的空白作品。空白作品给引导性的起步清单，而不是一排空指标。
   ========================================================== */

function WsHomeLoading({ label }) {
  return <div className="ws-page ws-view hm"><ViewLoading label={label} reason="data" /></div>;
}

function WsHomeCatalogError({ work: p, remote, error }) {
  return (
    <div className="ws-page ws-view hm">
      <WsHomeDataNotice remote={remote} workId={p.id} />
      <header className="hm-top">
        <div className="hm-id">
          <h1 className="hm-title">{p.title}</h1>
          {p.sub ? <p className="hm-logline">{p.sub}</p> : null}
        </div>
      </header>
      <HmBanner role="alert" title="章节目录没有读到"
        action={(
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => { WsCatalog.reset(); }}>
            <I.Refresh size={13} /> 重新读取
          </button>
        )}>
        正文不受影响，可以重新读取。{error && error.message ? `（${error.message}）` : ""}
      </HmBanner>
    </div>
  );
}

function WsHomeNoWorks({ remote }) {
  const openNewWork = () => window.dispatchEvent(new CustomEvent("ws:new-work"));
  return (
    <div className="ws-page ws-view hm">
      <WsHomeDataNotice remote={remote} workId="" />
      <section className="hm-empty">
        <div className="hm-empty-mark" data-accent="slate">新</div>
        <h1 className="hm-empty-title">书架还是空的</h1>
        <p className="hm-empty-sub">先创建第一部作品，构思、资料、章节和正文才会有清晰且彼此隔离的归属。</p>
        <div className="hm-empty-actions">
          <button type="button" className="btn btn-accent btn-lg" data-testid="empty-create-work" onClick={openNewWork}>
            <I.Plus size={16} /> 创建第一部作品
          </button>
        </div>
      </section>
    </div>
  );
}

const HOME_START_STEPS = [
  { icon: "Snowflake", view: "snowflake", title: "雪花构思", desc: "从一句话故事开始，逐步长出人物与大纲。", cta: "开始十步" },
  { icon: "Library", view: "library", title: "建立资料库", desc: "登记人物、地点与设定，随写随查。", cta: "打开资料" },
  { icon: "Beaker", view: "styleref", title: "设定风格基调", desc: "给这部作品定一个叙述声音与语感。", cta: "去设定" },
];

function WsHomeBlank({ work: p, go, remote }) {
  return (
    <div className="ws-page ws-view hm">
      <WsHomeDataNotice remote={remote} workId={p.id} />
      <WsAiSetupNotice go={go} />
      <header className="hm-top">
        <div className="hm-id">
          <h1 className="hm-title">{p.title}</h1>
          <p className="hm-logline">{p.sub || "还没有简介——可以先用一句话，说清这部作品是关于什么的。"}</p>
        </div>
        <div className="hm-book" role="group" aria-label="全书进度">
          <HomeRing pct={0} size={66} />
          <div className="hm-book-meta">
            <div className="hm-book-lbl">全书进度</div>
            <div className="hm-book-val"><b>0</b> 章</div>
            <div className="hm-book-sub">目标 {(p.wordsTarget / 10000).toFixed(0)} 万字</div>
          </div>
        </div>
      </header>

      <section className="hm-empty">
        <div className="hm-empty-mark" data-accent={p.accent}>{p.mark}</div>
        <h2 className="hm-empty-title">这部作品还是一张白纸</h2>
        <p className="hm-empty-sub">先把念头落成结构，再开始写。<br />不知道从哪起步的话，雪花十步会一步步带着你走。</p>
        <div className="hm-empty-actions">
          <button type="button" className="btn btn-accent btn-lg" onClick={() => go("snowflake")}><I.Snowflake size={16} /> 开始雪花构思</button>
          <button type="button" className="btn btn-ghost btn-lg" onClick={() => go("writer")}><I.Pen size={16} /> 直接进入写作</button>
        </div>
      </section>

      <section className="hm-start">
        <div className="hm-chaps-head"><h2 className="hm-chaps-title">起步清单</h2></div>
        <div className="hm-start-grid">
          {HOME_START_STEPS.map(s => {
            const Ic = I[s.icon] || I.Dot;
            return (
              <button type="button" key={s.view} className="hm-start-card" onClick={() => go(s.view)}>
                <span className="hm-start-ic"><Ic size={20} /></span>
                <span className="hm-start-title">{s.title}</span>
                <span className="hm-start-desc">{s.desc}</span>
                <span className="hm-start-cta">{s.cta} <I.ArrowRight size={13} /></span>
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
}

export { WsHomeBlank, WsHomeCatalogError, WsHomeLoading, WsHomeNoWorks };
