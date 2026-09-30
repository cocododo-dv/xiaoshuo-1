/* 文字小工具（2026-09-29 前端共享层）。纯函数，不碰 DOM、不写 window。 */

/* 字数：去掉所有空白后的字符数（中文按字计，标点也算一个）。按 Unicode code point 数——与后端 count_words
   （services/writing_stats.py）同一口径：扩展区汉字、emoji 这些由两个 UTF-16 码元组成的字也只算一个
   （以前按 .length 数，一个就算两个，写作台、起草台的字数和后端记下的对不上，复核 Q3-R6）。
   null / undefined 当空串；其余值先转成字符串——需要把 0 / false 当空的调用方自己在调用点写 `value || ""`。 */
export function countChars(value) {
  return Array.from(String(value == null ? "" : value).replace(/\s/g, "")).length;
}
