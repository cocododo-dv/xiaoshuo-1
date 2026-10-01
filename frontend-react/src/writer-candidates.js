import { htmlToPlainText, sanitizeManuscriptHTML } from "./manuscript-html.js";

/* 续写候选的句子与段落（托盘、抽屉的候选卡，写作台采纳时用）。候选按段给（已转义的 HTML），
   作者可以点句子只挑几句；挑出来的句子按原来的段落拼回去，采纳成连续的几段。 */

export function wrSentences(html) {
  return String(html || "").split(/(?<=[。！？!?])/g).filter((sentence) => sentence.trim());
}

/* 候选里的一句 HTML → 纯文字（与恢复中心同一份实现，住在 manuscript-html.js） */
export { htmlToPlainText as wrPlainText };

/* 候选的各段 → 按句拆开，记下每句在第几段：[{ para, html }]（句子的序号就是它在这张表里的位置） */
export function wrCandSentences(paras) {
  const sentences = [];
  (paras || []).forEach((html, para) => {
    wrSentences(html).forEach((sentence) => sentences.push({ para, html: sentence }));
  });
  return sentences;
}

/* 一句 HTML → 它的字，原样（不修首尾空白）：句与句之间的空格跟在后一句的开头，修掉了英文句子就粘成一串 */
function sentenceText(html) {
  const node = document.createElement("div");
  node.innerHTML = sanitizeManuscriptHTML(html || "");
  return node.textContent || "";
}

/* 挑中的那几句 → 按段落拼回纯文字：同一段的句子连在一起，不同段各成一段（顺序按原文）。
   每句原样拼，只修掉拼好的一段首尾的空白（W1-R6B-6：过去逐句修，「Run! Go!」挑出来成了「Run!Go!」） */
export function wrPickedParas(sentences, picked) {
  const byPara = new Map();
  picked
    .slice()
    .sort((left, right) => left - right)
    .forEach((index) => {
      const sentence = sentences[index];
      if (!sentence) return;
      byPara.set(sentence.para, (byPara.get(sentence.para) || "") + sentenceText(sentence.html));
    });
  return [...byPara.keys()]
    .sort((left, right) => left - right)
    .map((para) => byPara.get(para).trim())
    .filter(Boolean);
}
