/* index.html 的 CSP 只放行回环后端（http://127.0.0.1:* / http://localhost:*）。
   构建时把 VITE_NOVEL_SYSTEM_API_BASE 指到别处（远程模式的内网地址、https 反向代理）时，
   浏览器会按 connect-src 拦下每一个 API 请求，页面只剩加载失败。
   这里在 transformIndexHtml 里把那个后端的 origin 补进 connect-src；回环地址、相对地址不改一个字。 */

// index.html 里已经放行的 http 回环主机
const LOOPBACK_HTTP_HOSTS = new Set(["127.0.0.1", "localhost"]);

/* 需要额外放行的 origin；已被默认策略覆盖（http 回环）、相对地址或写错的地址返回 null。 */
export function apiConnectOrigin(apiBase) {
  const raw = String(apiBase || "").trim();
  if (!raw) return null;
  let url;
  try {
    url = new URL(raw);
  } catch (e) {
    return null; // 相对地址（同源）或写错：'self' 管同源，写错的地址放行了也连不上
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  if (url.protocol === "http:" && LOOPBACK_HTTP_HOSTS.has(url.hostname)) return null;
  return url.origin;
}

/* 在 CSP meta 的 connect-src 末尾补上 apiBase 的 origin；不需要补时原样返回。 */
export function withApiConnectSrc(html, apiBase) {
  const origin = apiConnectOrigin(apiBase);
  if (!origin) return html;
  return html.replace(/(connect-src\s[^;"]*)/, (directive) => (
    directive.split(/\s+/).includes(origin) ? directive : `${directive} ${origin}`
  ));
}

/* Vite 插件：dev 与 build 都会经过 transformIndexHtml */
export function cspConnectSrc(apiBase) {
  return {
    name: "novel-system-csp-connect-src",
    transformIndexHtml(html) {
      return withApiConnectSrc(html, apiBase);
    },
  };
}
