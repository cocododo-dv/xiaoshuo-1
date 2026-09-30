/* 时间与数字文案的唯一住处（2026-09-29 前端共享层）。纯函数，不碰 DOM、不写 window。
   同一家族里口径不同的几版各有明确的名字（写在每个函数上方），不要把它们合成一个——合了就会改掉
   作者在某个视图上看到的字。视图里原来的名字（srFormatPct、fmtInt……）照旧从各自模块转出。 */

/* ---------- 相对时间 ---------- */

/* 相对时间文案：毫秒时间戳 → 「刚刚 / N 分钟前 / N 小时前 / N 天前」。
   不做入参守卫（undefined 会得到 NaN 文案）——需要兜底的调用方
   自行在调用点补 `|| 0` 之类的前置守卫。 */
export function agoLabel(t) {
  const m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return "刚刚";
  if (m < 60) return m + " 分钟前";
  const h = Math.floor(m / 60);
  if (h < 24) return h + " 小时前";
  return Math.floor(h / 24) + " 天前";
}

/* 天级绝对时间文案：ISO 串或时间戳 → 今天「今天 HH:MM」、跨天「M 月 D 日 HH:MM」。
   非法入参返回空串——需要占位符的调用方自行在调用点补 `|| "—"`。 */
export function dayTimeLabel(t) {
  try {
    const d = new Date(t);
    if (isNaN(d.getTime())) return "";
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    return d.toDateString() === new Date().toDateString() ? `今天 ${hm}` : `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${hm}`;
  } catch (e) { return ""; }
}

/* 一小时内说「刚刚 / N 分钟前」，更早走 dayTimeLabel（雪花历史的口径；与 agoLabel 不同，不说「N 小时前」）。 */
export function recentOrDayTimeLabel(t) {
  const diff = Date.now() - t;
  if (diff < 60000) return "刚刚";
  if (diff < 3600000) return Math.floor(diff / 60000) + " 分钟前";
  return dayTimeLabel(t);
}

/* ---------- 绝对时间（本地时区）---------- */

/* 「M 月 D 日 HH:MM」，不看是不是今天。空值 / 非法值返回空串。 */
export function formatMonthDayTime(value) {
  const d = value ? new Date(value) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/* 浏览器 zh-CN 口径的「HH:MM」（toLocaleTimeString，两位小时 / 分钟）。不做入参守卫。 */
export function formatClockTime(value) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

/* 浏览器 zh-CN 口径的「M/D HH:MM」（toLocaleString，月日不补零）。空值 / 非法值返回空串。 */
export function formatLocaleMonthDayTime(value) {
  const d = value ? new Date(value) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/* 「09-29 16:30」：带时区的 ISO 串（后端记的是 UTC 时间戳）换算成浏览器所在时区再显示；空值 / 非法值返回空串。
   成本看板调用明细的时间列（审计 F05-04：以前直接截取 UTC 串，北京时间 15:00 的调用显示成「07:00」）。 */
export function formatLocalMonthDayTime(value) {
  const d = value ? new Date(value) : null;
  if (!d || Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* 日期串直接截取：「2026-09-29」→「09-29」。只给本身就是一天的串用（成本看板的日桶是后端按 UTC 分的，照原样标）。 */
export function isoMonthDay(iso) { return (iso || "").slice(5); }

/* 本地日历日的键「2026-09-29」（Date / 时间戳 / ISO 串，缺省为现在）：按作者所在的时区换日。
   toISOString().slice(0, 10) 是 UTC 日——东八区早上 8 点以前还算「昨天」（审计 F05-05）。非法值返回空串。 */
export function localDayKey(value) {
  const d = value == null ? new Date() : new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/* ---------- 数字 ---------- */

/* 整数千分位；null / undefined 给「—」（成本看板的口径）。 */
export function formatIntOrDash(v) { return v === null || v === undefined ? "—" : Number(v).toLocaleString(); }

/* 比例 → 百分比，四舍五入到整数；null / undefined 给「—」（成本看板的口径）。 */
export function formatPercentRounded(v) { return v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`; }

/* 比例 → 百分比：不到 10% 留一位小数（去掉 .0），其余取整；非数给「—」（风格参考的口径）。 */
export function formatPercent(value) {
  const n = Number(value) * 100;
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  const text = abs > 0 && abs < 10 ? n.toFixed(1).replace(/\.0$/, "") : String(Math.round(n));
  return `${text}%`;
}

/* 秒 → 「m:ss」 */
export function formatDurationClock(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* 分钟：<1 说「不到 1 分钟」，≥60 说「约 N 小时 M 分钟」 */
export function formatMinutesApprox(minutes) {
  const m = Number(minutes);
  if (!Number.isFinite(m) || m < 1) return "不到 1 分钟";
  if (m < 60) return `约 ${Math.round(m)} 分钟`;
  const h = Math.floor(m / 60);
  const rest = Math.round(m - h * 60);
  return rest ? `约 ${h} 小时 ${rest} 分钟` : `约 ${h} 小时`;
}

/* ---------- 万 ---------- */

/* 以万为单位的定点数字符串（不带「万」字）：wanFixed(43800, 1) →「4.4」。各视图的取舍位数不同
   （目标字数取整、书架一位小数、参考书按大小变），位数由调用方给。 */
export function wanFixed(count, digits) { return (count / 10000).toFixed(digits); }

/* token / 字数：≥1 万写「N 万」（≥10 万取整，其余一位小数、去掉 .0），不到 1 万写千分位；负数 / 非数给「—」。 */
export function formatCountWan(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n >= 10000) return `${(n / 10000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")} 万`;
  return n.toLocaleString("zh-CN");
}

/* 字数：「980 字」「4.4 万字」（万后面不再空一格） */
export function formatCharsWan(value) {
  const text = formatCountWan(value);
  if (text === "—") return text;
  return text.endsWith("万") ? `${text}字` : `${text} 字`;
}
