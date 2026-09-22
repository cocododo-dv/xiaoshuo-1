import React from "react";
import { I } from "./icons.jsx";
import { LIB_CATS } from "./ws-library-data.jsx";

/* ==========================================================
   资料 · 共用小件：字块（LibGlyph）与条目行（LibEntryRow）。
   以前「字块 + 名字 + 一行说明 + 箭头」在目录、总览、档案关联、时间线、图谱面板里各写一遍，
   每一处都有自己的一套 CSS；现在都从这里出，样式在 ws-library.css 的 .lib-glyph / .lib-row。
   空态统一用 ws-ui 的 EmptyState，不再单独包一层。纯 ESM，不写 window。
   ========================================================== */

const cx = (...parts) => parts.filter(Boolean).join(" ");

/* 类别 id → 类别定义（名字、图标、强调色） */
const LIB_CAT_BY_ID = LIB_CATS.reduce((m, c) => { m[c.id] = c; return m; }, {});
const libCatLabel = (id) => (LIB_CAT_BY_ID[id] || {}).label || "";

/* 条目强调色 → .acc-*（给出 --acc / --acc-wash / --acc-ink） */
const libAccClass = (accent) => `acc-${accent || "ink"}`;

/* 字块：条目名字的第一个字，底色取条目的强调色。
   size：xs 20 · sm 26 · md 30 · lg 40 · xl 56；round 用于挂在别处的小圆点。 */
function LibGlyph({ entry, glyph, accent, size = "md", round = false, className, title }) {
  const text = glyph != null ? glyph : (entry && entry.glyph) || "";
  const acc = accent || (entry && entry.accent);
  return (
    <span
      className={cx("lib-glyph", `is-${size}`, round && "is-round", libAccClass(acc), className)}
      aria-hidden={title ? undefined : "true"}
      title={title}
    >
      {text}
    </span>
  );
}

/* 条目行：字块 + 名字（可带置顶星）+ 一行说明 + 右侧附注 + 箭头。
   有 onClick 时渲染成按钮（type=button），否则是 div；其余属性（data-*、tabIndex、aria-*）原样透传。
   variant：plain（列表里的行，悬停变底色）· card（有边框的一块）· chip（胶囊）。 */
function LibEntryRow({
  entry, name, sub, glyphSize = "md", variant = "plain", active = false, pinned = false,
  aside, chevron = false, as, className, onClick, children, ...rest
}) {
  const Tag = as || (onClick ? "button" : "div");
  const label = name != null ? name : (entry && entry.name) || "";
  return (
    <Tag
      {...(Tag === "button" ? { type: "button" } : null)}
      className={cx("lib-row", `is-${variant}`, libAccClass(entry && entry.accent), active && "is-active", className)}
      onClick={onClick}
      {...rest}
    >
      <LibGlyph entry={entry} size={glyphSize} round={variant === "chip"} />
      <span className="lib-row-main">
        <span className="lib-row-name">
          <span className="lib-row-name-text">{label}</span>
          {/* 图标一律 aria-hidden，挂在它身上的 aria-label 读屏听不到；置顶状态用一段只给读屏的文字说 */}
          {pinned && <><I.Star className="lib-row-pin" size={11} /><span className="ws-sr-only">（已置顶）</span></>}
        </span>
        {sub ? <span className="lib-row-sub">{sub}</span> : null}
      </span>
      {aside}
      {children}
      {chevron && <I.ChevronRight className="lib-row-chev" size={15} aria-hidden="true" />}
    </Tag>
  );
}

export { LIB_CAT_BY_ID, libCatLabel, libAccClass, LibGlyph, LibEntryRow };
