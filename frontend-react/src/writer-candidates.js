import { htmlToPlainText } from "./manuscript-html.js";

export function wrSentences(html) {
  return String(html || "").split(/(?<=[。！？!?])/g).filter((sentence) => sentence.trim());
}

/* 候选里的一句 HTML → 纯文字（与恢复中心同一份实现，住在 manuscript-html.js） */
export { htmlToPlainText as wrPlainText };

export function wrPickedText(sentences, picked) {
  return picked
    .slice()
    .sort((left, right) => left - right)
    .map((index) => htmlToPlainText(sentences[index]))
    .join("");
}
