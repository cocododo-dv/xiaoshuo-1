import React from "react";
import { I } from "./icons.jsx";
import { canonFactKeyLabel, canonFactValueLabel } from "./labels/canon.js";
import { LIB_chapterLabel } from "./ws-library-derive.js";
import { LibEntryRow } from "./ws-library-parts.jsx";
import { snowStepByBackendKey } from "./snow-steps.js";
import { Notice, SectionLabel, Tag } from "./ws-ui.jsx";

/* ==========================================================
   资料 · 档案里「从故事长出来的」几块（只读；数据见 ws-library-store.js 模块说明与后端 services/library_story.py）
   · ProfileSections —— 人物在构思 04 / 06 / 08 确认过的那一版；构思里有更新的没确认时提醒，并能直达那一步；
   · CharacterStory —— 人物写到现在：出场、现状、知道的事、各章摘要里的变化；
   · WorldStory —— 地点收下的场景地点与来过的人，物品的持有人，正文记在它名下的事实；
   · EventStory —— 目录里的一场：时间、章、地点、写没写成，写成了的场落下的事实；
   · SceneRows —— 连到这份档案的场次（人物的「出场的场次」、地点的「在这里发生」）。
   ========================================================== */

const { useMemo } = React;

/* 事实一行：硬质检认得的种类翻成中文（持有物品 · 铜钥匙：持有），别的键（模型起的英文键）只显示值 */
function factLine(fact) {
  const key = String((fact && fact.key) || "");
  const value = String((fact && fact.value) || "");
  const label = canonFactKeyLabel(key);
  if (label === key) return { label: "", value };
  return { label, value: canonFactValueLabel(key, value) };
}

function FactList({ facts, nameOf }) {
  return (
    <ul className="dossier-story-list">
      {facts.map((f, i) => {
        const line = factLine(f);
        const who = nameOf ? nameOf(f.entity_id) : "";
        return (
          <li key={i}>
            {who && <span className="dossier-story-who">{who}</span>}
            {line.label && <span className="dossier-story-k">{line.label}</span>}
            <span>{line.value}</span>
          </li>
        );
      })}
    </ul>
  );
}

function ProfileSections({ entry, pending, onGo }) {
  const sections = (entry.profile && entry.profile.sections) || [];
  if (!sections.length) return null;
  const toStep = (stepKey) => {
    const step = snowStepByBackendKey(stepKey);
    onGo("snowflake", step ? [{ type: "ws:snow-step", detail: step.key }] : []);
  };
  const steps = Array.from(new Set(sections.map(s => s.step_key)));
  const waiting = (pending || []).filter(p => steps.includes(p.step_key));
  return (
    <section className="dossier-section" data-testid="lib-profile">
      <SectionLabel icon="Snowflake"
        aside={<button type="button" className="btn btn-quiet btn-sm" onClick={() => toStep(steps[0])}><I.Edit size={12} /> 去构思里改</button>}>
        构思里的设定
      </SectionLabel>
      {waiting.length > 0 && (
        <Notice tone="warn" testId="lib-profile-pending"
          actions={<button type="button" className="btn btn-ghost btn-sm" onClick={() => toStep(waiting[0].step_key)}>去构思确认</button>}>
          构思里{waiting.map(p => `「${p.step_label}」`).join("")}
          {waiting.some(p => p.status === "stale") ? "上游改过、等你复核，或" : ""}有一版新的还没确认。这里显示的是你确认过的那一版。
        </Notice>
      )}
      {sections.map((section, i) => (
        <div key={`${section.step_key}-${i}`} className="dossier-profile">
          <div className="dossier-profile-h">
            {section.title}
            {section.step_label !== section.title && <span className="dossier-profile-src">{section.step_label}</span>}
          </div>
          <dl className="dossier-profile-rows">
            {section.rows.map((row, j) => (
              <div key={j} className="dossier-profile-row"><dt>{row.label}</dt><dd>{row.value}</dd></div>
            ))}
          </dl>
        </div>
      ))}
    </section>
  );
}

function CharacterStory({ entry, chapters }) {
  const story = entry.story || {};
  const pov = (story.pov_scene_ids || []).length;
  const onstage = (story.onstage_scene_ids || []).length;
  const state = story.state || [];
  const learned = story.learned || [];
  const changes = story.changes || [];
  if (!pov && !onstage && !state.length && !learned.length && !changes.length) return null;
  return (
    <section className="dossier-section" data-testid="lib-character-story">
      <SectionLabel icon="Activity" aside={pov || onstage ? `视角 ${pov} 场 · 在场 ${onstage} 场` : null}>写到现在</SectionLabel>
      {state.length > 0 && (
        <div className="dossier-profile">
          <div className="dossier-profile-h">现状</div>
          <FactList facts={state} />
        </div>
      )}
      {learned.length > 0 && (
        <div className="dossier-profile">
          <div className="dossier-profile-h">知道的事</div>
          <FactList facts={learned} />
        </div>
      )}
      {changes.length > 0 && (
        <div className="dossier-profile">
          <div className="dossier-profile-h">各章的变化</div>
          <dl className="dossier-profile-rows">
            {changes.map((c, i) => (
              <div key={i} className="dossier-profile-row"><dt>{LIB_chapterLabel(c.chapter_id, chapters) || "某一章"}</dt><dd>{c.text}</dd></div>
            ))}
          </dl>
        </div>
      )}
      {(state.length > 0 || learned.length > 0) && (
        <p className="lib-quiet">现状与知道的事来自已写正文的自动抽取（还没核对的也算，与起草时模型看到的同一份）；核对在成稿中心的「正史」。</p>
      )}
    </section>
  );
}

function WorldStory({ entry, byId, onNav }) {
  const story = entry.story || {};
  const spots = story.spots || [];
  const people = (story.character_ids || []).map(id => byId[id]).filter(Boolean);
  /* 派生物品的持有人已经是它的关联（关系网络里那几行）；作者的条目收下的物品才在这里列 */
  const holders = entry.derived ? [] : story.holders || [];
  const facts = story.facts || [];
  if (!spots.length && !people.length && !holders.length && !facts.length) return null;
  const nameOf = (id) => (byId[id] && byId[id].name) || id;
  /* 作者的条目收下的场景地点才值得列：派生条目自己就是那一处 */
  const showSpots = spots.length > 1 || (spots.length === 1 && spots[0] !== entry.name);
  return (
    <section className="dossier-section" data-testid="lib-world-story">
      <SectionLabel icon="MapPin">故事里</SectionLabel>
      {showSpots && (
        <div className="dossier-profile">
          <div className="dossier-profile-h">归在这里的场景地点</div>
          <p className="dossier-story-text">{spots.join("、")}</p>
        </div>
      )}
      {people.length > 0 && (
        <div className="dossier-profile">
          <div className="dossier-profile-h">来过的人物</div>
          <div className="dossier-tags">
            {people.map(p => (
              <button key={p.id} type="button" className="btn btn-quiet btn-sm" onClick={() => onNav(p.id)}>{p.name}</button>
            ))}
          </div>
        </div>
      )}
      {holders.length > 0 && (
        <div className="dossier-profile">
          <div className="dossier-profile-h">持有人</div>
          <ul className="dossier-story-list">
            {holders.map((h, i) => (
              <li key={i}>
                <span className="dossier-story-who">{nameOf(h.character_id)}</span>
                <span>{canonFactValueLabel("has_item", h.value)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {facts.length > 0 && (
        <div className="dossier-profile">
          <div className="dossier-profile-h">正文里记下的</div>
          <FactList facts={facts} />
        </div>
      )}
    </section>
  );
}

function EventStory({ entry, byId }) {
  const facts = entry.storyFacts || [];
  if (!facts.length) return null;
  const nameOf = (id) => (byId[id] && byId[id].name) || id;
  return (
    <section className="dossier-section" data-testid="lib-event-story">
      <SectionLabel icon="CheckCircle" aside={facts.length}>这一场落下的事实</SectionLabel>
      <FactList facts={facts} nameOf={nameOf} />
    </section>
  );
}

/* 连到这份档案的场次（派生大事记），按目录顺序；副标题 = 章 · 时间，右侧 = 在这一场里的身份（视角 / 在场 / 地点） */
function SceneRows({ title, icon, conns, byId, chapters, onNav }) {
  const rows = useMemo(() => conns.map(c => ({ c, e: byId[c.id] })).filter(x => x.e), [conns, byId]);
  if (!rows.length) return null;
  const written = rows.filter(x => x.e.realized).length;
  return (
    <section className="dossier-section" data-testid="lib-scene-rows">
      <SectionLabel icon={icon} aside={`${rows.length} 场${written ? ` · 已写 ${written}` : ""}`}>{title}</SectionLabel>
      <div className="lib-rows">
        {rows.map(({ c, e }) => (
          <LibEntryRow
            key={e.id}
            entry={e}
            variant="card"
            glyphSize="sm"
            chevron
            className={e.realized ? "" : "is-planned"}
            onClick={() => onNav(e.id)}
            sub={[LIB_chapterLabel(e.chapterRef, chapters), e.timeLabel].filter(Boolean).join(" · ")}
            aside={(
              <span className="lib-row-aside">
                {c.rel}{e.realized ? "" : <Tag tone="neutral">没写</Tag>}
              </span>
            )}
          />
        ))}
      </div>
    </section>
  );
}

export { CharacterStory, EventStory, ProfileSections, SceneRows, WorldStory };
