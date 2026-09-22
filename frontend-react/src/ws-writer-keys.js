/* 写作台的快捷键提示：Mac 上是 ⌘，Windows / Linux 上是 Ctrl。
   实现只有一份，在 lib/platform.js（外壳的侧栏、命令面板用的也是它）；这里保留写作台用惯的两个名字。
   纯 ESM，不写 window。 */

export { modKeyLabel as modKey, modShortcut as modCombo } from "./lib/platform.js";
