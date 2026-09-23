/* 后端的原话能不能直接给作者看（风格参考与对照检查共用一个口径）：要是中文句子——有汉字、汉字不少于英文字母，
   也不是 Python 异常串（作业边界记的是「TypeError: …」这种，夹几个汉字也不算中文说明）。 */
const CJK_ALL = /[㐀-鿿]/g;
const LATIN_ALL = /[A-Za-z]/g;

export function isChineseMessage(text) {
  const s = String(text || "").trim();
  if (!s) return false;
  if (/^[A-Za-z_.]*(Error|Exception)\b/.test(s)) return false;
  const cjk = (s.match(CJK_ALL) || []).length;
  if (!cjk) return false;
  return cjk >= (s.match(LATIN_ALL) || []).length;
}
