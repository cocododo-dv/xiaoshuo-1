import React from "react";

/* 雪花编辑器共用的两个小输入件：会自己长高的多行框、视角人物选择器。 */

/* 自动长高的多行输入：内容多长框就多高，长节拍不再被两行框截住、也不再被单行框截成一截。
   支持 CSS field-sizing 的浏览器交给 CSS；其余在值变化后按 scrollHeight 量一次
   （scrollHeight 为 0 = 还没显示出来，比如在收起的 <details> 里——那时不量，免得量成 0 高）。 */
const S2_FIELD_SIZING = (() => { try { return typeof CSS !== "undefined" && !!CSS.supports && CSS.supports("field-sizing", "content"); } catch (e) { return false; } })();
export function S2AutoText({ value, className = "", minRows = 1, ...rest }) {
  const ref = React.useRef(null);
  React.useLayoutEffect(() => {
    if (S2_FIELD_SIZING) return;
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    if (el.scrollHeight > 0) el.style.height = `${el.scrollHeight + 2}px`;
    else el.style.height = "";
  }, [value]);
  return <textarea ref={ref} rows={minRows} className={`sf-autotext ${className}`.trim()} value={value || ""} {...rest} />;
}

/* POV 选择器：名册非空 → 下拉（value=角色id / label=姓名；名册对不上的旧值保留为
   独立项，不丢内容）；名册为空（04 还没建角色）→ 退化成自由文本框，避免卡死作者。 */
export function S2PovPick({ value, roster, onChange, className, placeholder, ariaLabel, id }) {
  if (!roster || !roster.length) {
    return <input id={id} className={className} value={value || ""} onChange={(e) => onChange(e.target.value)} placeholder={placeholder || "视角人物"} aria-label={ariaLabel} />;
  }
  const known = roster.find(r => r.id === value) || roster.find(r => r.name === value);
  const unknown = value && !known;
  return (
    <select id={id} className={className} value={known ? known.id : (value || "")} onChange={(e) => onChange(e.target.value)} aria-label={ariaLabel} title="视角人物（来自 04 角色名册）">
      <option value="">视角人物</option>
      {roster.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
      {unknown && <option value={value}>{value}</option>}
    </select>
  );
}
