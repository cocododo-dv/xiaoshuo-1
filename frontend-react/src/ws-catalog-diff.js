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
   返回 { dispatchDiff, backendChapterId, backendSceneId, readStarted, reconcileCreates }
   （后两个是目录读取器的开头与收尾：认回包丢了的新建场，见 unconfirmedCreates）。
   ========================================================== */

/* 建场请求失败了，场景却也许建好了：断网 / 超时（没有状态码）、5xx、服务端说可以重试的（同一个幂等键的原请求还在跑）。
   与 wr-doc-sync.js 的 mayHaveLanded 同一口径；其余 4xx 是服务端明确拒绝的，没建成 */
function createMayHaveLanded(e) {
  const status = Number(e && e.status);
  return !status || status >= 500 || !!(e && e.retryable);
}

export function createCatalogWriter({ catLoad, catActiveId, catResolveScene, catApiBase, catWrite, catRecover, catPlanTitleHooks }) {
  const catPendingCreates = {}; // slug/sid → 创建中的 Promise（后端 id 待回填）
  /* 创建中的 Promise 登记在 catPendingCreates 里，建完就摘掉。摘的那条链只管摘，吞掉它自己的拒绝：失败照旧交给
     等这个 Promise 的调用方（写入收尾时提示并以服务端为准重拉），不再多出一条没人接的拒绝（复核 I3：
     建章 / 建场失败时浏览器报 unhandled rejection）。 */
  const trackCreate = (key, p) => {
    catPendingCreates[key] = p;
    p.finally(() => { delete catPendingCreates[key]; }).catch(() => {});
    return p;
  };
  /* 新建一场没拿到回包（后端建好了、回包在路上丢了：后端重启、连接断开、超时）：乐观缓存里那一场一直是临时 sid、没有后端 id，
     正文 store 找不到它建好之后的样子，作者的头几句就留在临时 sid 下。这里把它记成「待认」，写入收尾重读目录时（reconcileCreates）
     认出它来就记成它的别名，写作台跟着把那几句挪过去（复核 W1-R7B-1）。认错了比认不出糟得多：写作台会把这一场写下的字挪进
     别的场、存上服务端，同步与恢复里什么也没有（复核 Q1c-R1）。所以只认「确实是它」的那一场：
       · 只记也许建成了的失败（createMayHaveLanded，或回包里没有 scene）；服务端明确拒绝的没建成，不记；
       · 只等它记下之后发出的第一次目录读取，而且那一次要写进缓存：读失败了（后端重启时多半如此）、回来时作废了（本机另一笔
         目录写入还没写完，或在路上时目录被要求以服务端为准重读——回收站恢复、方案落地、物化、重新同步）都就此作罢（readStarted），
         不拿一份过时的「建之前」去比之后别处、别的建场、这些事加进来的场（复核 Q1c-R4）。代价：回包丢了、那一次又没写进缓存的，
         认不出，照实进同步与恢复；
       · 那一章重读时多出来的场里，除掉建之前就有的、这一页已经认得的（别的建场拿到了回包、缓存里已有后端 id 的）——剩下恰好一场；
       · 它就在乐观时的位置上，题名与形态正是这一页发出去的那样（服务端照存题名，空题名存成「新场景」）；
       · 同一章里不止一次待认：认不准哪一场是哪一份，都不认。
     认不准就不记：写作台照旧把那几句留进同步与恢复，照实说是新建这一场时写下的字。剩下的窗口：建场失败到那一次读取在服务端
     读完之间，另一个标签页恰好在这个位置上加了同名同形态的一场——要第二个人在一次读取的往返里动手，接受。 */
  let unconfirmedCreates = [];   // { workId, sid, chapterId, at, before, title, kind, readNo }
  const catReadNos = {};         // workId → 目录读取已发出几次（readStarted 计数；待认记下时的读数在 readNo）
  const catAnsweredSceneIds = {}; // workId → 这一页从建场回包里认得的 scene_id（目录被 reset 清掉缓存时也还认得）
  const noteAnswered = (workId, sceneId) => {
    (catAnsweredSceneIds[workId] || (catAnsweredSceneIds[workId] = new Set())).add(sceneId);
  };

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
      /* 只上行这一章真有的字段。章级的张力 / 线索 / 视角 / 时间 / 地点 / 入口出口 / 衔接没有任何地方能填，
         后端也不再收（批准 #17a，重评 R10）；章承诺 promise 是戏剧卡 promise 的镜像，照旧（有才上行） */
      const body = {
        title: nc.title,
        state: nc.state === "active" ? "writing" : nc.state,
        current: !!nc.current,
        words_target: (nc.words && nc.words.target) || null,
        act: nc.act,
        drama: nc.drama || {},
        with_scene: false,
      };
      if (nc.promise !== undefined) body.promise = nc.promise;
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
        if (sres && sres.scene && sres.scene.scene_id) noteAnswered(workId, sres.scene.scene_id);
        if (mineScene && sres && sres.scene) mineScene.backendId = sres.scene.scene_id;
      }
    })();
    return trackCreate(nc.id, p);
  }

  function catCreateSceneViaApi(workId, chId, s, at) {
    const p = (async () => {
      const chapterId = await catBackendChapterId(chId);
      if (!chapterId) return;
      const sceneMine = () => {
        const chapter = catLoad(workId).find(x => x.id === chId);
        return chapter && (chapter.scenes || []).find(x => x.sid === s.sid);
      };
      const before = new Set();
      const known = catLoad(workId).find(x => x.id === chId);
      ((known && known.scenes) || []).forEach((x) => { if (x.backendId) before.add(x.backendId); });
      let res = null;
      let failure = null;
      try {
        res = await apiPost(`${catApiBase(workId)}/chapters/${chapterId}/scenes`, catSceneCreateBody(s, at));
      } catch (e) {
        failure = e;
        throw e;
      } finally {
        const sceneId = res && res.scene && res.scene.scene_id;
        if (sceneId) {
          noteAnswered(workId, sceneId);
          const mine = sceneMine();
          if (mine) mine.backendId = sceneId;
        } else if (!failure || createMayHaveLanded(failure)) {
          unconfirmedCreates.push({
            workId, sid: s.sid, chapterId, at, before,
            title: String(s.title || "").trim() || "新场景",
            kind: s.kind === "反应" ? "反应" : "主动",
            readNo: catReadNos[workId] || 0,
          });
        }
      }
    })();
    return trackCreate(s.sid, p);
  }

  /* 目录读取器（ws-catalog.jsx）每发出一次读取先调 readStarted，拿到这一次的编号（写进缓存时 reconcileCreates 凭它认）。
     发出之前先摘掉「第一次读取已经发出过」的待认：那一次写进了缓存的已经认过、摘掉了（reconcileCreates）；还留着的，那一次就是
     失败了或回来时作废了——就此作罢，不留给之后的读取去认（见 unconfirmedCreates，复核 Q1c-R1 / R4）。
     同一部作品的读取一次接一次（读取器按作品合并在飞请求，上一次结束之后才发下一次），编号就是先后。 */
  function readStarted(workId) {
    const prior = catReadNos[workId] || 0;
    unconfirmedCreates = unconfirmedCreates.filter((c) => c.workId !== workId || c.readNo >= prior);
    catReadNos[workId] = prior + 1;
    return catReadNos[workId];
  }

  /* 目录重读之后（ws-catalog.jsx 的装载收尾，第 readNo 次读取；known = 这一次写进缓存之前的那一份）：没拿到回包的新建场
     认不认得出来——认得出来的给 [临时 sid, 现在的 sid]。在这一次读取之前记下的每一条只认这一次：认不出就是没建成，
     或者建成了却认不准。规则见 unconfirmedCreates。 */
  function reconcileCreates(workId, chapters, known, readNo) {
    const ours = unconfirmedCreates.filter((c) => c.workId === workId);
    const due = ours.filter((c) => c.readNo < readNo);
    if (!due.length) return [];
    unconfirmedCreates = unconfirmedCreates.filter((c) => !due.includes(c));
    const knownIds = new Set(catAnsweredSceneIds[workId] || []);
    (known || []).forEach((c) => (c.scenes || []).forEach((x) => { if (x.backendId) knownIds.add(x.backendId); }));
    const aliases = [];
    due.forEach((c) => {
      if (ours.filter((x) => x.chapterId === c.chapterId).length > 1) return;
      const chapter = (chapters || []).find((x) => x.backendId === c.chapterId);
      if (!chapter) return;
      const scenes = chapter.scenes || [];
      const added = scenes.filter((x) => x.backendId && !c.before.has(x.backendId) && !knownIds.has(x.backendId));
      if (added.length !== 1) return;
      const hit = added[0];
      if (scenes.indexOf(hit) !== c.at || hit.title !== c.title || hit.kind !== c.kind) return;
      if (hit.sid && hit.sid !== c.sid) aliases.push([c.sid, hit.sid]);
    });
    return aliases;
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

  return {
    dispatchDiff: catDispatchDiff, backendChapterId: catBackendChapterId, backendSceneId: catBackendSceneId,
    readStarted, reconcileCreates,
  };
}
