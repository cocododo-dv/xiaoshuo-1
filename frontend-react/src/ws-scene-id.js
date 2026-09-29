import { WsCatalog } from "./ws-catalog.jsx";

/* ==========================================================
   目录 sid → 后端 scene id（写作台与起草台共用这一处）
   ----------------------------------------------------------
   sid 是目录 slug（阶段 X 起就是稳定的 scene_id）；乐观新建的场还带着临时 tmp_… sid，
   目录同步到后端之前没有后端 id。过去六处各自包一层 WsCatalog.__backendSceneId，兜底不一：
   深改那一处解析不到时直接拿 sid 去打后端，临时 id 换来一个 404。现在一律：解析不到就是 null，
   要发请求的地方用 requireSceneApiId 得到一句「还没同步」的错误（code SCENE_NOT_READY，
   wrAiError 翻成「稍等几秒再试」）。ESM 模块，不写 window。
   ========================================================== */

export async function sceneApiId(sid) {
  if (!sid) return null;
  try {
    return (await WsCatalog.__backendSceneId(sid)) || null;
  } catch (e) {
    return null;
  }
}

export function sceneNotSyncedError() {
  return Object.assign(new Error("这一场还没同步到服务器，稍等几秒再试。"), { code: "SCENE_NOT_READY" });
}

export async function requireSceneApiId(sid) {
  const sceneId = await sceneApiId(sid);
  if (!sceneId) throw sceneNotSyncedError();
  return sceneId;
}
