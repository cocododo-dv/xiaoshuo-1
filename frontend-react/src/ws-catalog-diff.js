import { apiPatch, apiPost } from "./lib/client.js";
import { emit } from "./lib/events.js";
import { catChapterPatch, catSceneCreateBody, catScenePatch } from "./ws-catalog-adapt.js";

/* ==========================================================
   目录 · 写入引擎（2026-09-29 从 ws-catalog.jsx 原样搬出）
   set() 给出一份新目录（乐观缓存已先行），这里按差异派发端点调用：删除合并成一次批量回收，
   字段变化 → PATCH，新增 → POST（等章建好再建场），场序 → scene-order，章序 → chapter-order；
   任何一步失败 → 提示并以服务端为准重拉。缓存与读取器由 ws-catalog.jsx 注入：
     catLoad(workId) / catActiveId() / catResolveScene(workId, sid) / catApiBase(workId)
     catWrite(workId, run)        = catLoader.write：写入期间回来的读取不写缓存，写完重读
     catRecover(error)            写失败的提示
     catPlanTitleHooks            章名写穿到章计划后要 await 的登记（雪花缓存接章表）
   返回 { dispatchDiff, backendChapterId, backendSceneId }。
   ========================================================== */
export function createCatalogWriter({ catLoad, catActiveId, catResolveScene, catApiBase, catWrite, catRecover, catPlanTitleHooks }) {
  const catPendingCreates = {}; // slug/sid → 创建中的 Promise（后端 id 待回填）

  /* 后端 id 解析（含等待乐观创建完成）。
     插在中间的新章用的是临时 id（ch-new-*）；前一次写入收尾时目录已经重拉，缓存里就只剩后端给的位置式 slug 了——
     接连新建两章时，第二次写入的章序要找第一章的后端 id，所以建好时把「作品 + 临时 id → 后端 id」记下来。
     只记临时 id、按作品分开、最后才查：接在书尾的新章用的是位置式 chNN，这个 id 删章之后会被重用、换一部作品也会重名，
     记下来就会把下一个同名新章的改名 / 章序发给回收站里的旧章或别的作品。 */
  const catCreatedChapterIds = {};
  const catCreatedKey = (workId, chId) => `${workId}::${chId}`;
  async function catBackendChapterId(chId) {
    const find = () => { const c = catLoad(catActiveId()).find(x => x.id === chId); return c && c.backendId; };
    let id = find();
    if (!id && catPendingCreates[chId]) { await catPendingCreates[chId]; id = find(); }
    return id || catCreatedChapterIds[catCreatedKey(catActiveId(), chId)];
  }
  async function catBackendSceneId(sid) {
    const lookup = () => catResolveScene(catActiveId(), sid);
    let hit = lookup();
    if (hit && !hit.scene.backendId && catPendingCreates[hit.chapter.id]) {
      await catPendingCreates[hit.chapter.id];
      hit = lookup();
    }
    if (hit && !hit.scene.backendId && catPendingCreates[sid]) {
      await catPendingCreates[sid];
      hit = lookup();
    }
    return hit && hit.scene.backendId;
  }

  function catCreateChapterViaApi(workId, nc) {
    const p = (async () => {
      /* 只上行这一章真有的字段：后端把收到的每个章级叙事字段原样存下来，
         送一个默认的张力 / 线索 / 「待定」占位，就等于替作者编了一条张力曲线（章节编排曾因此画回假弧线） */
      const narrative = {
        tension: nc.tension, pov: nc.pov, time_label: nc.time, place: nc.place,
        entry: nc.entry, exit: nc.exit, align: nc.align, promise: nc.promise, threads: nc.threads,
      };
      const body = {
        title: nc.title,
        state: nc.state === "active" ? "writing" : nc.state,
        current: !!nc.current,
        words_target: (nc.words && nc.words.target) || null,
        act: nc.act,
        drama: nc.drama || {},
        with_scene: false,
      };
      Object.entries(narrative).forEach(([key, value]) => { if (value !== undefined) body[key] = value; });
      const result = await apiPost(`${catApiBase(workId)}/chapters`, body);
      const created = result && result.chapter;
      if (!created) return;
      if (String(nc.id).startsWith("ch-new-")) catCreatedChapterIds[catCreatedKey(workId, nc.id)] = created.chapter_id;
      const mine = catLoad(workId).find(c => c.id === nc.id);
      if (mine) mine.backendId = created.chapter_id;
      for (let i = 0; i < (nc.scenes || []).length; i++) {
        const s = nc.scenes[i];
        const sres = await apiPost(
          `${catApiBase(workId)}/chapters/${created.chapter_id}/scenes`,
          catSceneCreateBody(s, i)
        );
        const mineScene = mine && (mine.scenes || []).find(x => x.sid === s.sid);
        if (mineScene && sres && sres.scene) mineScene.backendId = sres.scene.scene_id;
      }
    })();
    catPendingCreates[nc.id] = p;
    p.finally(() => { delete catPendingCreates[nc.id]; });
    return p;
  }

  function catCreateSceneViaApi(workId, chId, s, at) {
    const p = (async () => {
      const chapterId = await catBackendChapterId(chId);
      if (!chapterId) return;
      const res = await apiPost(`${catApiBase(workId)}/chapters/${chapterId}/scenes`, catSceneCreateBody(s, at));
      const chapter = catLoad(workId).find(x => x.id === chId);
      const mine = chapter && (chapter.scenes || []).find(x => x.sid === s.sid);
      if (mine && res && res.scene) mine.backendId = res.scene.scene_id;
    })();
    catPendingCreates[s.sid] = p;
    p.finally(() => { delete catPendingCreates[s.sid]; });
    return p;
  }

  /* 批量软删：后端逐条判定，把过不去的项放进 blocked 而不是抛错
     （已批准终稿、章下已有单删场景…）。静默丢弃会让作者以为删掉了、刷新后又冒出来，
     所以这里把 blocked 翻成异常，交由 catRecover 提示 + 以服务端为准重拉。 */
  async function catTrash(path, body) {
    const res = await apiPost(path, body);
    const blocked = (res && res.blocked) || [];
    if (blocked.length) {
      const seen = [];
      blocked.forEach((b) => {
        const msg = (b && (b.message || b.code)) || "未说明原因";
        if (!seen.includes(msg)) seen.push(msg);
      });
      throw new Error(`有 ${blocked.length} 项未能删除：${seen.slice(0, 3).join("；")}${seen.length > 3 ? "…" : ""}`);
    }
    return res;
  }

  /* set() 写穿点的 diff 拆解（视图层零修改的关键）：乐观缓存已先行，
     这里按差异派发端点调用；任何一步失败 → 整体重拉恢复。
     删除先于其它操作、且章/场各合并成一次批量调用：批量删 20 章不再打 20 个请求，
     场景删除也必须早于 scene-order（后端要求顺序集合覆盖章内全部在册场景）。 */
  function catDispatchDiff(workId, prev, next) {
    const ops = [];
    let planTitleSynced = false;
    const prevById = Object.fromEntries(prev.map(c => [c.id, c]));
    const nextIds = new Set(next.map(c => c.id));
    const trashChapterIds = [];
    const trashSceneIds = [];
    for (const c of prev) {
      if (!nextIds.has(c.id) && c.backendId) trashChapterIds.push(c.backendId);
    }
    for (const nc of next) {
      const pc = prevById[nc.id];
      if (!pc) continue;
      const nextSids = new Set((nc.scenes || []).map(s => s.sid));
      for (const s of pc.scenes || []) {
        if (!nextSids.has(s.sid) && s.backendId) trashSceneIds.push(s.backendId);
      }
    }
    if (trashChapterIds.length) ops.push(() => catTrash("/api/v1/chapters/trash", { chapter_ids: trashChapterIds }));
    if (trashSceneIds.length) ops.push(() => catTrash("/api/v1/scenes/trash", { scene_ids: trashSceneIds }));
    for (const nc of next) {
      const pc = prevById[nc.id];
      if (!pc) {
        ops.push(() => catCreateChapterViaApi(workId, nc));
        continue;
      }
      const patch = catChapterPatch(pc, nc);
      if (Object.keys(patch).length) {
        ops.push(async () => {
          const chapterId = await catBackendChapterId(nc.id);
          if (!chapterId) return;
          const res = await apiPatch(`${catApiBase(workId)}/chapters/${chapterId}`, patch);
          /* 阶段 Z「章名只有一个」：构思分出来的章在这里改了名，后端已经写穿到章计划（07 章节表 / 09 章头的
             服务端镜像跟着变了）。本机的雪花缓存必须立刻接过服务端这一版——它的合并规则是「本机为准」，
             不接的话下一次 07 上行会把旧章名当成作者的编辑同步回去。 */
          if (res && res.plan_title_synced) planTitleSynced = true;
        });
      }
      const prevScenes = pc.scenes || [];
      const nextScenes = nc.scenes || [];
      const prevBySid = Object.fromEntries(prevScenes.map(s => [s.sid, s]));
      const nextSids = new Set(nextScenes.map(s => s.sid));
      nextScenes.forEach((s, index) => {
        const ps = prevBySid[s.sid];
        if (!ps) {
          ops.push(() => catCreateSceneViaApi(workId, nc.id, s, index));
          return;
        }
        const scenePatch = catScenePatch(ps, s);
        if (Object.keys(scenePatch).length) {
          ops.push(async () => {
            const sceneId = await catBackendSceneId(s.sid);
            if (sceneId) await apiPatch(`${catApiBase(workId)}/scenes/${sceneId}`, scenePatch);
          });
        }
      });
      const prevOrder = prevScenes.map(s => s.sid).filter(sid => nextSids.has(sid));
      const nextOrder = nextScenes.map(s => s.sid).filter(sid => prevBySid[sid]);
      if (prevOrder.join("|") !== nextOrder.join("|")) {
        ops.push(async () => {
          const chapterId = await catBackendChapterId(nc.id);
          if (!chapterId) return;
          const sceneIds = [];
          for (const s of nextScenes) {
            const sceneId = await catBackendSceneId(s.sid);
            if (sceneId) sceneIds.push(sceneId);
          }
          if (sceneIds.length) {
            await apiPost(`/api/v1/chapters/${chapterId}/scene-order`, {
              scene_ids: sceneIds,
              last_scene_id: sceneIds[sceneIds.length - 1],
            });
          }
        });
      }
    }
    /* 章节拖拽过去只改了内存顺序，刷新即还原。顺序也是目录真相的一部分：
       所有增删/重排操作完成后，再用完整的服务端 chapter_id 集合一次性落 display_order。
       任何 id 无法解析都 fail-closed，避免把半份顺序写进后端。
       纯删除不改变存活章的相对次序，这里按「存活章」比对，免得每次删除都追发一次
       等价于现状的 chapter-order（后端要求提交全量在册集合）。 */
    const prevChapterOrder = prev.map((c) => c.id).filter((id) => nextIds.has(id));
    const nextChapterOrder = next.map((c) => c.id);
    if (next.length && prevChapterOrder.join("|") !== nextChapterOrder.join("|")) {
      ops.push(async () => {
        const chapterIds = [];
        for (const chapter of next) {
          const chapterId = await catBackendChapterId(chapter.id);
          if (!chapterId) throw new Error(`章节「${chapter.title || chapter.id}」尚未完成创建，顺序未保存。`);
          chapterIds.push(chapterId);
        }
        await apiPost(`${catApiBase(workId)}/chapter-order`, { chapter_ids: chapterIds });
      });
    }
    if (!ops.length) return false;
    /* 写入期间回来的读取一律不写缓存；写完（成功或失败）以服务端编号 / rollup 收敛——重读由 write 收尾时发 */
    catWrite(workId, async () => {
      try {
        for (const op of ops) await op();
        if (planTitleSynced) {
          for (const fn of catPlanTitleHooks) {
            try { await fn(workId); } catch (e) { console.warn("[WsCatalog] 本机雪花缓存接章表失败（下次打开构思时水合）:", e); }
          }
        }
        emit("ws:trash-changed");
      } catch (e) {
        catRecover(e);
      }
    });
    return true;
  }

  return { dispatchDiff: catDispatchDiff, backendChapterId: catBackendChapterId, backendSceneId: catBackendSceneId };
}
