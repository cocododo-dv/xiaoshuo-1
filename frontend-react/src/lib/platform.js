/* ==========================================================
   平台差异（纯函数，不写 window）
   快捷键提示按平台写：Mac 上是 ⌘，Windows / Linux 上是 Ctrl。
   键盘处理本来就同时接受 metaKey 与 ctrlKey；只是提示过去一直印着 ⌘，
   而这个产品主要在 Windows 上启动（start-dev.cmd）。
   写作台（ws-writer-keys.js）和外壳（侧栏、命令面板）过去各有一份判断，
   连格式都不一样（Ctrl+J 与 Ctrl K）——现在只有这一份，格式统一为 Windows 的惯例「Ctrl+K」。
   nav 参数只给测试用：不传就读当前浏览器的 navigator；显式传 null 当作非 Mac。
   ========================================================== */

function currentNavigator() {
  return typeof navigator !== "undefined" ? navigator : null;
}

export function isMacPlatform(nav = currentNavigator()) {
  if (!nav) return false;
  try {
    const platform = String((nav.userAgentData && nav.userAgentData.platform) || nav.platform || nav.userAgent || "");
    return /mac|iphone|ipad|ipod/i.test(platform);
  } catch (e) {
    return false;
  }
}

/* 修饰键本身："⌘" / "Ctrl" */
export function modKeyLabel(nav) {
  return isMacPlatform(nav) ? "⌘" : "Ctrl";
}

/* 组合键：modShortcut("K") → "⌘K"（Mac）/ "Ctrl+K"（其余） */
export function modShortcut(key, nav) {
  return isMacPlatform(nav) ? `⌘${key}` : `Ctrl+${key}`;
}

/* 修饰键 + 回车：「⌘↵」（Mac）/「Ctrl+Enter」（其余）——Windows 键帽上印的是 Enter，不是 ↵ */
export function modEnterShortcut(nav) {
  return isMacPlatform(nav) ? "⌘↵" : "Ctrl+Enter";
}
