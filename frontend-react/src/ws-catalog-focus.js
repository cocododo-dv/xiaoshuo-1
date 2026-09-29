/* ==========================================================
   目录 · 「现在在写哪一章 / 该写哪一场」（纯函数；2026-09-29 从 ws-catalog.jsx 拆出）
   主页的继续写作、写作台的落点、AI 起草台的落点共用这一条规则（后端 catalog.focus_scene_payload
   是它的镜像）。参数是目录视图形状的章列表（WsCatalog.get()）；不读缓存，主页的派生层也可以直接用。
   ========================================================== */

/* 当前章：标了 current 的 → 第一章「在写」的 → 最后一章 */
export function catalogCurrentChapter(chapters) {
  const chs = chapters || [];
  return chs.find(c => c.current) || chs.find(c => c.state === "writing") || chs[chs.length - 1] || null;
}

/* 当前章里在写的那一场 → 第一场没写完的 → 末场。
   过去三处各有各的规则——写作台会取全书任何一场「在写」的场，
   于是雪花刚整理完，它开在一张手建的空白占位场上，而主页指着雪花的第一场。
   当前章还没铺场时：从当前章往后找第一场没写完的（找到书尾再从头绕回来），而不是从全书第一章找起——
   那样会落回前面早已写完的章。全书都写完了，就停在当前章之前最近的那一场上（作者刚写完的地方）。
   返回 { chapter, scene, index } 或 null。 */
export function catalogFocusScene(chapters) {
  const pick = (c) => {
    const scenes = (c && c.scenes) || [];
    if (!scenes.length) return null;
    const s = scenes.find(x => x.state === "writing") || scenes.find(x => x.state !== "done") || scenes[scenes.length - 1];
    return { chapter: c, scene: s, index: scenes.indexOf(s) };
  };
  const all = chapters || [];
  const current = catalogCurrentChapter(all);
  const hit = pick(current);
  if (hit) return hit;
  const at = Math.max(0, all.indexOf(current));
  const forward = [...all.slice(at + 1), ...all.slice(0, at)];
  for (const c of forward) {
    const next = pick(c);
    if (next && next.scene.state !== "done") return next;
  }
  const backward = [...all.slice(0, at).reverse(), ...all.slice(at + 1)];
  for (const c of backward) { const next = pick(c); if (next) return next; }
  return null;
}
