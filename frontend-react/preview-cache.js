/* vite preview 给每个文件都标 Cache-Control: no-cache：每次刷新、每打开一个页面，浏览器都要把用到的每个文件
   逐个回源确认一遍。作者隔着 ~200 ms 的 SSH 隧道用工作台，这些往返就是「正在打开…」的大头。
   /assets/ 下的文件名带内容哈希（内容一变名字就变），可以让浏览器一直缓存、不再确认；index.html 不在
   /assets/ 下，仍每次确认，所以重新构建后刷新一次就换上新文件。只改真正的资源文件：缺失的 /assets/ 文件
   （重启后旧页面还在要上一版的块）会落到 SPA 兜底、以 200 回 index.html，这种 HTML 响应不缓存。 */

export const IMMUTABLE_ASSET_CACHE = "public, max-age=31536000, immutable";

/* 包住 res.writeHead：sirv 把 Cache-Control 放在 writeHead 的 headers 参数里，它会盖过先前的 setHeader，
   所以只能在这里替换。 */
export function markImmutableAsset(req, res) {
  if (!req.url || !req.url.startsWith("/assets/")) return;
  const writeHead = res.writeHead;
  res.writeHead = function writeHeadImmutable(status, ...rest) {
    const headerArgs = rest.filter((arg) => arg && typeof arg === "object");
    const headerValue = (wanted) => {
      for (const arg of headerArgs) {
        for (const name of Object.keys(arg)) if (name.toLowerCase() === wanted) return arg[name];
      }
      return this.getHeader?.(wanted);
    };
    if (status === 200 && !String(headerValue("content-type") || "").includes("text/html")) {
      for (const arg of headerArgs) {
        for (const name of Object.keys(arg)) {
          if (name.toLowerCase() === "cache-control") delete arg[name];
        }
      }
      this.setHeader("Cache-Control", IMMUTABLE_ASSET_CACHE);
    }
    return writeHead.call(this, status, ...rest);
  };
}

/* Vite 插件：只作用于 vite preview（开发服务器不经过 configurePreviewServer） */
export function immutablePreviewAssets() {
  return {
    name: "novel-system-immutable-preview-assets",
    configurePreviewServer(server) {
      server.middlewares.use((req, res, next) => {
        markImmutableAsset(req, res);
        next();
      });
    },
  };
}
