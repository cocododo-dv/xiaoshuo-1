/* 本地临时 id / 幂等键的随机部分（2026-09-29 前端共享层）。纯函数，不写 window。 */

/* Math.random 的 36 进制小数部分取前 n 位（偶尔更短）。不是安全随机数：只用来让本机生成的
   临时 id、幂等键彼此不撞；各调用点的前缀、时间戳、分隔符照旧各写各的（它们的格式是各自的契约）。 */
export function randomSuffix(n) {
  return Math.random().toString(36).slice(2, 2 + n);
}
