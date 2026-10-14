import React from "react";
import { I } from "./icons.jsx";
import { LIB_chapterLabel, LIB_groupConnections, LIB_relLabel } from "./ws-library-derive.js";
import { LIB_CAT_BY_ID, LibEntryRow, LibGlyph, libAccClass, libCatLabel } from "./ws-library-parts.jsx";
import { CharacterStory, EventStory, ProfileSections, SceneRows, WorldStory } from "./ws-library-story.jsx";
import { navigateWithViewIntent, writerIntents } from "./ws-view-intents.js";
import { IconButton, SectionLabel, Tag } from "./ws-ui.jsx";

/* ==========================================================
   资料 · 一份档案的阅读视图（Dossier）与它上面的导航条（DossierNav）。
   编辑 / 新建表单在 ws-library-form.jsx；从故事长出来的几块（构思档案、写到现在、场次）在 ws-library-story.jsx。
   派生条目（目录排成的大事记、场景地点、物品）只读：没有置顶 / 编辑 / 删除；地点与物品可以「收进资料」——
   新建一份同名的世界条目，它随即收下这一条（后端按名字归并）。
   ========================================================== */

/* 导航条：回总览 / 当前类别，以及沿当前可见列表翻上一条、下一条 */
function DossierNav({ entry, pos, total, prev, next, onHome, onCat, onOpen }) {
  const prevLabel = prev ? "上一条：" + prev.name : "已是第一条";
  const nextLabel = next ? "下一条：" + next.name : "已是最后一条";
  return (
    <nav className="dossier-nav" aria-label="档案导航">
      <button type="button" className="dossier-nav-link is-home" onClick={onHome}><I.Activity size={13} aria-hidden="true" /> 总览</button>
      <span className="dossier-nav-sep" aria-hidden="true">/</span>
      <button type="button" className="dossier-nav-link" onClick={onCat}>{libCatLabel(entry.cat)}</button>
      <span className="dossier-nav-spacer" />
      <IconButton icon="ChevronLeft" label={prevLabel} variant="ghost" disabled={!prev} onClick={() => prev && onOpen(prev.id)} />
      {pos >= 0 && <span className="dossier-nav-pos">{pos + 1} / {total}</span>}
      <IconButton icon="ChevronRight" label={nextLabel} variant="ghost" disabled={!next} onClick={() => next && onOpen(next.id)} />
    </nav>
  );
}

function Dossier({ entry: e, conns, byId, chapters, pending, onNav, onEdit, onDelete, onTogglePin, onAdopt, onGo }) {
  const meta = LIB_CAT_BY_ID[e.cat] || {};
  const Ic = I[meta.icon] || I.Dot;
  const all = conns || [];
  const isEvent = e.cat === "events";
  const derived = !!e.derived;
  /* 连过来的派生大事记是「场次」，单列一块（按目录顺序）；关系网络只放人物 / 世界 / 作者写的大事记 */
  const sceneConns = isEvent ? [] : all.filter(c => byId[c.id] && byId[c.id].derived && byId[c.id].cat === "events");
  const cs = isEvent ? all : all.filter(c => !sceneConns.includes(c));
  const relGroups = LIB_groupConnections(cs);
  const go = onGo || ((view, intents) => navigateWithViewIntent(view, (intents[0] || {}).type, (intents[0] || {}).detail));
  /* 说明行：人物写角色定位，世界写类型；大事记的时间放进下面的信息格 */
  const kindLine = e.cat === "people" ? e.role : e.cat === "world" ? e.kind : "";
  const factRows = isEvent
    ? [
        { k: "时间", v: e.timeLabel || "未定时间" },
        { k: "所在章", v: LIB_chapterLabel(e.chapterRef, chapters) || "未关联章节" },
        ...(derived ? [{ k: "地点", v: e.location || "未定" }, { k: "正文", v: e.realized ? "已写成" : "还没写" }] : []),
      ]
    : (e.facts || []).filter(f => f && (f.k || f.v));
  const hasProfile = !!(e.profile && e.profile.sections && e.profile.sections.length);
  /* 自定义编号只有和类别名不同时才显示，否则就是重复 */
  const showCode = e.code && e.code !== meta.label;
  return (
    <div className={`dossier ${libAccClass(e.accent)}`}>
      <header className="dossier-head">
        <LibGlyph entry={e} size="xl" />
        <div className="dossier-head-main">
          {showCode && <div className="dossier-code">{e.code}</div>}
          <h2 className="dossier-name">{e.name}</h2>
          {kindLine && <div className="dossier-kind"><Ic size={13} aria-hidden="true" />{kindLine}</div>}
          {e.summary && <p className="dossier-summary">{e.summary}</p>}
        </div>
        {derived && (
          <div className="dossier-head-actions">
            <Tag tone="info" title={isEvent ? "目录里规划的一场，改它去章节编排或构思" : "从目录的场景地点 / 已写正文里整理出来的"}>来自故事</Tag>
          </div>
        )}
        {!isEvent && !derived && (
          <div className="dossier-head-actions">
            <button
              type="button"
              className={`dossier-pin ${e.pinned ? "is-on" : ""}`}
              aria-pressed={!!e.pinned}
              onClick={onTogglePin}
              title={e.pinned ? "取消置顶" : "置顶：在列表和总览里排在最前"}
            >
              <I.Star size={14} /> {e.pinned ? "已置顶" : "置顶"}
            </button>
          </div>
        )}
      </header>

      <div className="dossier-body">
        {e.blurb ? (
          <section className="dossier-section">
            <SectionLabel>{isEvent ? "经过" : derived || !hasProfile ? "简述" : "你的笔记"}</SectionLabel>
            <p className="dossier-blurb">{e.blurb}</p>
          </section>
        ) : derived ? (
          <p className="lib-quiet">
            {isEvent
              ? "这是目录里规划的一场，跟着构思与章节编排走；写成之后，正文落下的事实会列在下面。"
              : e.entityKind === "item"
                ? "这件物品是从已写正文里整理出来的（谁拿着、谁丢了）。想补充它，点下面的「收进资料」新建一份同名档案；名字里含着它的物品也会归到那一份下面。"
                : "这一处是目录里某一场的场景地点。想补充它，点下面的「收进资料」新建一份同名档案——可以改成更概括的名字（比如全书反复出现的那个地方），名字出现在别的场景地点里的也会归到那一份下面。"}
          </p>
        ) : !hasProfile ? (
          <button type="button" className="dossier-newhint" onClick={onEdit}>
            <I.Edit size={14} /> 这份档案还没有{isEvent ? "写经过" : "简述"}。点这里补上{isEvent ? "时间、所在章和经过" : "简述、关键信息与关联"}。
          </button>
        ) : null}

        <ProfileSections entry={e} pending={pending} onGo={go} />
        {e.cat === "people" && <CharacterStory entry={e} chapters={chapters} />}
        {e.cat === "world" && <WorldStory entry={e} byId={byId} onNav={onNav} />}
        {isEvent && <EventStory entry={e} byId={byId} />}

        {factRows.length > 0 && (
          <section className="dossier-section">
            <SectionLabel>{isEvent ? "时间与位置" : "关键信息"}</SectionLabel>
            <dl className="dossier-facts">
              {factRows.map((f, i) => (
                <div key={i} className="dossier-fact"><dt className="k">{f.k || "未命名"}</dt><dd className="v">{f.v || "—"}</dd></div>
              ))}
            </dl>
          </section>
        )}

        {e.tags && e.tags.length > 0 && (
          <section className="dossier-section">
            <SectionLabel>标签</SectionLabel>
            <div className="dossier-tags">
              {e.tags.map(t => <Tag key={t}>{t}</Tag>)}
            </div>
          </section>
        )}

        <SceneRows
          title={e.cat === "people" ? "出场的场次" : "在这里发生"}
          icon={e.cat === "people" ? "Users" : "MapPin"}
          conns={sceneConns} byId={byId} chapters={chapters} onNav={onNav}
        />

        {cs.length > 0 && (
          <section className="dossier-section">
            <SectionLabel aside={cs.length}>{isEvent && derived ? "这一场里的人与地方" : "关系网络"}</SectionLabel>
            <div className="rel-groups">
              {relGroups.map(g => {
                const Ti = I[g.type.icon] || I.Dot;
                return (
                  <div key={g.type.id} className={`rel-group acc-${g.type.accent}`}>
                    <div className="rel-group-h">
                      <span className="rel-group-ic" aria-hidden="true"><Ti size={12} /></span>
                      <span className="rel-group-label">{g.type.label}</span>
                      <span className="rel-group-hint">{g.type.hint}</span>
                      <span className="rel-group-n">{g.items.length}</span>
                    </div>
                    <div className="lib-rows">
                      {g.items.map((l, i) => {
                        const t = byId[l.id];
                        if (!t) return null;
                        return (
                          <LibEntryRow
                            key={i}
                            entry={t}
                            variant="card"
                            glyphSize="sm"
                            chevron
                            onClick={() => onNav(l.id)}
                            sub={(
                              <>
                                {l.dir === "in" && <span className="lib-row-flag" title="由对方那一头建立的关联">对方关联</span>}
                                {LIB_relLabel(l)}
                              </>
                            )}
                            aside={<span className="lib-row-aside">{libCatLabel(t.cat)}</span>}
                          />
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}
      </div>

      <footer className="dossier-foot">
        {!derived && <button type="button" className="btn btn-ghost btn-sm" onClick={onEdit}><I.Edit size={13} /> 编辑档案</button>}
        {derived && !isEvent && onAdopt && (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onAdopt}
            title="新建一份同名的世界档案，之后可以改名、写简述、建关联；名字出现在别的场景地点 / 物品里的也会归到它下面">
            <I.Plus size={13} /> 收进资料
          </button>
        )}
        {derived && isEvent && e.sceneId ? (
          <button type="button" className="btn btn-quiet btn-sm" onClick={() => go("writer", writerIntents(e.sceneId))}>
            <I.Pen size={13} /> 到写作台打开这一场
          </button>
        ) : (
          <button type="button" className="btn btn-quiet btn-sm"
            onClick={() => navigateWithViewIntent("writer", "ws:writer-locate", e.id)}
            title="到写作台里找这个名字出现的地方；当前这一场没有时，会换到正在写的那一场再找">
            <I.Pen size={13} /> 在正文中查找
          </button>
        )}
        <span className="spacer" />
        {!derived && <button type="button" className="btn btn-danger btn-sm" onClick={onDelete}><I.Trash size={13} /> 删除</button>}
      </footer>
    </div>
  );
}

export { Dossier, DossierNav };
