// 生产构建只固定第三方依赖。业务页面的边界由 React.lazy 动态入口决定；
// 强行把相互调用的业务模块塞进手工 chunk 会制造循环依赖并把它们重新拉回首屏。
export function productionChunk(moduleId) {
  const id = String(moduleId || "").replaceAll("\\", "/");
  return id.includes("/node_modules/") ? "vendor" : undefined;
}
