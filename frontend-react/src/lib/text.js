/* 文字小工具（2026-09-29 前端共享层）。纯函数，不碰 DOM、不写 window。 */

/* 字数：去掉所有空白后的字符数（中文按字计，标点也算一个）。null / undefined 当空串；
   其余值先转成字符串——需要把 0 / false 当空的调用方自己在调用点写 `value || ""`。 */
export function countChars(value) {
  return String(value == null ? "" : value).replace(/\s/g, "").length;
}
