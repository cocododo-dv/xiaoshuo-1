import { apiPatch, apiPost } from "./lib/client.js";
import { randomSuffix } from "./lib/ids.js";
import { hasAuthorText, sanitizeManuscriptHTML } from "./manuscript-html.js";
import { WsDiagnosis } from "./ws-diagnosis-summary.jsx";
import { wsNotify } from "./ws-notify.jsx";
import { adoptModuleListeners, emit, retireModuleListeners } from "./lib/events.js";
import { WsCatalog } from "./ws-catalog.jsx";
import { activeWorkId, recoveryCreate, recoveryList, recoveryRemove, recoveryRename } from "./wr-recovery-store.js";
import {
  cacheWrite, docText, draftTail, dropSceneKeys, pendingBase, pendingClear, pendingRead, pendingRestore, pendingSids, pendingStampScene,
  pendingWrite, readCache, readSlot, rememberInSession, renameSceneKeys, sameManuscriptText, textFingerprint, toDocHTML,
} from "./wr-doc-cache.js";

/* ==========================================================
   WrDocs — 写作器正文文档 store（FE-ALIGN Phase 3；2026-09-30 W1 起是一台保存状态机）
   ----------------------------------------------------------
   正文真相源 = author-drafts 主路径（POST ensure + PATCH /{draft_id}），字数统计与目录 rollup 由保存响应回流
   （words_rollup）。localStorage 的 wr-doc:<sid> 键是「同步读缓存」（本机这一层在 wr-doc-cache.js）：写作器在
   render / effect 里同步取文档，API 负责水合与持久化。sid = 目录 slug（阶段 X 起就是稳定的 scene_id）；后端 scene_id 经 WsCatalog 映射。

   保存按场（作品 id::sid）各一台状态机，住在模块里——换视图、写作台重新挂载都还在：
   · save(sid, html)：调用之内就把正文写进本机缓存 + 未同步标记（网络怎样都先在本机落地，刷新也丢不了），
     然后要么发出去，要么替换排队的那一稿：排队只留最新的一份，更早排队的被它取代。未同步标记先写、读缓存后写：标记记下
     读缓存里那一段字的指纹和它写在服务端哪一版上；浏览器存储满了、标记或读缓存写不进去时，这一稿不进共用读缓存（进去了却
     没有标记，下次打开会被当成过时的缓存静默盖掉），只留在这一页的内存里，存不上服务端时留进同步与恢复并提示（复核四
     W1-R4B-1 · W1-R4B-8）。
   · 同一场同一时刻只有一次 PATCH 在路上。它回来——
       存上了：有排队的，就带着新的修订号接着发；
       失败（断网 / 5xx / 401 / 403 / 429）：正文留在本机、仍是未同步，等下一次保存、显式 flush、窗口重新聚焦或联网时再发；
         再发的永远是最新的一稿，绝不把较旧的一稿补发到较新的后面。这一稿也许其实存上了（回包丢了）：记下它（unsure，
         同一个修订号上没等到回包的每一稿都记着——第一稿一直留着，最多再记最近的几十稿，复核三 W1-R3A-2）；
       409（服务端在别处改过）：进入冲突。撞上 409 的这一次和上一次没等到回包的用的是同一个修订号时，先核对
         （checking）：服务端眼下正好是那一稿、修订号只往前走了一步，那就是自己存上的——接上那个修订号，把最新的一稿
         接着发，不开冲突、不提示；不是就进入冲突。核对期间不发任何保存；核对读不到服务端（断网、读回来的比 409 说的
         还旧）不算冲突：保持核对、状态是保存失败，下一次保存 / flush / 窗口重新聚焦或联网 / 退避计时到了再读（复核二 W1-R2A-1）；
       服务端明确拒绝（章在别处批准锁定、这份作者稿已不是当前的一份、别的 4xx）：这一稿永远存不上——不停着、不重发。
         本机没存上的字（这一稿、排队的）先留进「同步与恢复」，读缓存和编辑器换回服务端的版本，告诉作者服务端拒绝了什么
         （复核三 W1-R3B-2）。章已批准锁定时顺手让目录重读一次，写作台随之只读。
   · 冲突：先把本机所有没上服务端的正文（撞上 409 的那一稿、排队的那一稿，去重；同步与恢复里已有一模一样的也不再放）
     放进「同步与恢复」（放不进本机存储时只留在本次会话里，并给出醒目的提示和入口），扔掉排队的，标上 conflictPending，
     再读服务端版本。读到之前，任何保存都拒绝（AUTHOR_DRAFT_CONFLICT）——绝不在作者没见过的修订号上保存；
     这期间存进来的字只进本机缓存，换掉读缓存之前一并放进同步与恢复。读服务端版本用的是冲突之后新发的 ensure
     （冲突之前发出、还没回来的那一次先等它落地，不拿它的回包）；409 说了服务端眼下是哪一版（current_revision_no）就只认
     不比它旧的回包，没说时只认比撞上的那一次新的——更旧的当没读到、马上再读。服务端几轮下来都一口咬定一个更旧的版本
     （库从备份恢复、历史往回走了），就信它（复核三 W1-R3B-1：过去这里会一直「读不到」，这一场从此存不上）。
     每一次 ensure 带自己的幂等键：回包丢了的那一次，服务端不会把它当时的旧快照重放给之后的读取。
     读到了：读缓存换成服务端版本，清掉 conflictPending 和 lastSaveError，订阅者收到 conflict-resolved
     （带服务端正文，写作台据此换掉编辑器里的字）；读不到：保持冲突，状态是保存失败，下一次保存 / 窗口重新
     聚焦或联网 / 退避计时到了再读。读到的服务端版本和本机留下的每一稿都是同一段字（两个标签页都把同一份工作稿传上去、
     先到的那一页存上了）：不是冲突——这一次多放的副本收回，不提示（复核五 W1-R5B-6）。
   · 规则：冲突时编辑器里一律是服务端版本，本机的字都在「同步与恢复」（作者拍板 #20c 的更严格做法：
     另一台设备的版本从不被覆盖，本机的版本也都留着）。
   · 水合总是读一次服务端（别的调用先前吸收的快照可能早就旧了，复核四 W1-R4B-6）。水合之前就有保存排着（作者在旧的读缓存上
     写了字）：服务端版本和作者写时编辑器里那份的文字（连同格式、空行、全角空格）不同，同样按冲突处理——除非作者写的那一份
     是上次会话没同步上的本机稿、未同步标记说的正是这一段字（指纹对得上）又说它写在服务端眼下这一版上：那作者接着写的字就是
     这一版的下一稿，照常保存（复核三 W1-R3A-3）。标记说的是另一段字（另一个标签页写进共用读缓存的、本机存储满了没写进去的）
     就不算数，照冲突处理（复核四 W1-R4A-2）；是上次会话留下的、服务端又往前走了，冲突的提示照实说「上次会话有没保存到
     服务端的本地正文」，不说「在别处被修改过」。服务端是新建的空稿（修订号 1）、本机有这一场的字时本机那份就是工作稿，
     按保存失败后停着的一稿记下（未同步、状态是保存失败，下一次保存 / flush / 重新聚焦或联网时传上去；水合本身不发请求）；
     章却已批准锁定时它永远存不上：留进同步与恢复、换上服务端的空稿并告诉作者——过去它被当成已存上的终稿显示，下一次 load 的
     后台复核又拿空稿把它静默盖掉（复核五 W1-R5B-1）。更高修订号上的空稿是在别处清空的，照常算服务端版本。
     上次会话（或另一个标签页）留下的本机稿和服务端版本不同时，它留进同步与恢复、编辑器换成服务端版本（loaded，reason pending）：
     编辑器里水合之前敲下、还没交出来的字并进同一条照实的提示，不说「在别处有更新」（复核五 W1-R5A-2）。
   · 已水合、没有本机改动的一场，每次 load 都在后台再问一次服务端（POST ensure 幂等）：修订号往前走了、或服务端的字和这一页
     的不一样（同一个修订号上换了字：库从备份恢复之后别处又存到了这个修订号，复核四 W1-R4B-2），就换读缓存、通知 loaded。
     要换掉的那一份若是还没同步上服务端的本机字（未同步标记说的正是它），先留进同步与恢复（复核五 W1-R5B-1）。
   · 章已批准锁定（目录说的）：WrDocs 不替它保存。交进来的字不写读缓存、不发请求，没存上的留进同步与恢复并提示，编辑器换回
     已存上的正文；失败后停着的一稿也一样（复核三 W1-R3B-3：写作台转只读那一刻还没交出去的半句，过去就这样没了）。
     那一刻这一场还有一次保存 / 核对 / 冲突读取 / 水合 / 采纳没有结果（已存上的是哪一版要等它）：交进来的字先留、提示说
     「编辑器随后换回」，排队的那一稿不再发；它有了结果再把停着 / 排队的留下、换回已存上的正文（lockPending，复核四
     W1-R4A-5 · W1-R4B-3）。显式 flush、窗口重新聚焦也不再重发停着的那一稿。那边有了结果时本章已在别处重新打开：锁定那一刻
     交进来的那一稿（编辑器里还是它）比排队的新，它就是下一次要存的——绝不在它后面补发排队的旧稿（复核五 W1-R5A-3 · W1-R5B-2）。
     那一刻这一场还没水合、第一次水合又没成：已存上的是哪一版要读了才知道——下一次保存 / flush / 窗口重新聚焦或联网时再读，
     读不到时等着的调用方收到失败，不一直「正在保存」（复核五 W1-R5A-4 · W1-R5B-3）。
     那一刻同一个修订号上还有一稿没等到回包（unsure：它也许存上了，批准的也许正是它）：换稿之前先读一次服务端（verifyUnsure）——
     是它就认作这一页自己存上的、编辑器换成它，不说那几句没存上；读不到就照这一页知道的换，提示照实说还没能确认，之后读到了照
     自己存上的换稿（reason own / locked），不说「在别处有更新」。服务端明确拒绝、同一个修订号上又有一稿没等到回包时照常马上换回
     （作者之后接着写的字不在那一次拒绝里），提示照实说还没能确认，随后的后台读取读到它就照自己存上的换稿（复核七 W1-R7A-2）。
     读到的是别处存上的一版（不是自己那几稿）、这一页还有没存上的字：不接它的修订号——之后换稿，或这期间本章重新打开了、接着发，
     发出去的都带着作者见过的旧修订号，撞上 409 走冲突，绝不接在作者没见过的一版后面把它盖掉（复核 I3-2）。
   · 提升（promote）只提升作者眼前的那一稿：调用方给出作者确认时编辑器里的那一稿（expectedText，确认框开着的时候水合 /
     复核可能落地、在后面把编辑器换掉）；还没水合的先水合；作者确认的那一稿、编辑器这一份、服务端存下的那一版三者文字
     不同就不提升（AUTHOR_DRAFT_CONFLICT：作者没看过它，复核二 W1-R2A-4 · W1-R2B-1，复核三 W1-R3A-1 · W1-R3B-5）。
     提升在路上时服务端的修订号往前走了、走的全是这一页自己存上的：说的是作者自己接着写了（AUTHOR_DRAFT_MOVED_BY_SELF），
     不说「在别处更新」（复核三 W1-R3B-8）；那一次自动保存的回包丢了（停着）时先把它再发一次再分，还是确认不了、或最新的一稿
     之后又没存上，就说草稿还没存上（AUTHOR_DRAFT_UNSAVED，复核四 W1-R4B-7，复核六 W1-R6A-3）。章已批准锁定的不提升；
     锁定那一刻交进来的一稿还没存上（那一次保存 / 采纳还没结果、本章这期间又重新打开了）：说它还没存上
     （AUTHOR_DRAFT_LOCK_PENDING），不说「在别处更新」（复核五 W1-R5A-3）。
   · 采纳归档（acceptCanonical）：服务端在一个事务里存下并提升了采纳的那一稿。这一页已经读到了那一版（后台复核先到）：
     作者在它上面接着写的字照常保存，只记下权威正文（复核四 W1-R4A-3）。否则还在路上的那一次保存就此作废（superseded）：
     它回来时——不管失败、409 还是别的——不补发、不停着、不开冲突、不提示；排队 / 停着 / 核对中 / 冲突中的本机稿不再发，
     和采纳的不一样又不在同步与恢复里的先留一份并告诉作者。采纳请求发出之前 beginAdoption，拿到的记号带着这一场（作者途中
     换了作品，收尾照样落在原来那一场上，复核四 W1-R4A-1）：采纳有结果之前不再发新的保存，路上那一次在这期间撞上的 409
     （多半就是采纳撞的）先按住，采纳成了就作废、都没成（endAdoption；同一场同时几次采纳时等最后一次）再照常核对 / 冲突——
     不为作者自己的采纳提示「在别处被修改」（复核三 W1-R3A-4 · W1-R3B-6）。采纳请求没等到回包：adoptionLanded 读一次服务端，
     存下的正是采纳的那一稿就照成了收尾（复核四 W1-R4A-4 · W1-R4B-5）；这一次也读不到（断网连着吞了回包和读取）：结果不明
     （adoptionUnsure）——之后按采纳之前的修订号撞上的 409 先核对是不是它，是就照采纳成了收尾，读不到时不提示，不说「在别处
     被修改」；起草台照实说没能确认（复核五 W1-R5B-4）。采纳前的预检 prepareAdoption：水合、读到冲突的
     服务端版本、把失败后停着的最新一稿再发一次（回包丢了的那一稿其实存上了时由核对接上修订号）、已水合的干净一场再读一次
     服务端，采纳带的修订号、预览和覆盖前的备份才是服务端眼下的（复核三 W1-R3B-4，复核四 W1-R4B-4）。作者确认覆盖的是
     预检交给他看过差异的那一稿（起草台 scnAdoptToDoc 比对；复核五 W1-R5A-1）。采纳被服务端按修订号拒绝、这期间草稿往前走的
     几版全是这一页自己存上的（写作台路上那一次先到了）：adoptionRefusalCause 说是（own），起草台不说「在别处更新」（复核五 W1-R5B-5）；
     提升和采纳被这样拒掉之后只按修订号链分：之后又有一稿没存上不相干，自己那一次的回包丢了的先再发一次，还是确认不了就说还没能
     确认（unsure），都不说「在别处」（refusalCause，复核六 W1-R6A-2 · W1-R6A-3）。采纳在路上时读到的服务端版本正是采纳的那一稿
     （后台复核赶在采纳的回包之前）：说的是采纳，不是「在别处有更新」（beginAdoption 记下采纳的那一稿，复核七 W1-R7B-6）；提升被作者
     自己的采纳赶在前面（采纳在一个事务里存下并提升了它）：等采纳有了结果，照实说换稿的是那次采纳（AUTHOR_DRAFT_ADOPTED；等着的
     保存被采纳作废时 flush 答 adopted，复核七 W1-R7B-5）。
   · replace(sid, html)（同步与恢复的「恢复」「重试同步」）：和 save 一样在调用之内落本机缓存并排进保存，同一个调用里
     就通知写作台换稿——编辑器、本机缓存和之后要同步的始终是同一稿，PATCH 失败时也是。章已批准锁定的场不替它存（拒绝）。
     排在作者自己的采纳后面、采纳先落了地：等着的调用方收到的冲突带 replacedByAdoption，「恢复」照实说是那次采纳（复核六 W1-R6A-1）。
     换掉的那一稿是交给 WrDocs、还没同步上服务端的字又不在同步与恢复里：先留一份再换（「恢复」照理先备份了；这是 WrDocs 自己的
     底线，复核七 W1-R7A-1）。
   · 一场一台状态机，按目录眼下的名字记：乐观新建的场先用临时 sid，后端建好、目录重拉之后换成稳定的 scene_id——状态机（路上 /
     排队 / 停着的一稿、等着的调用方）和本机这一层（会话内存、共用读缓存、未同步标记、恢复记录）一起搬到新名字下，按旧名字交进来
     的字经目录的别名进的也是这一台（renameMeta / followCatalog，复核六 W1-R6B-1）。这一场不在目录里了（建章没成、目录退回了
     服务端的版本；在别处移到了回收站）：写作台再也打不开它，本机还没同步上的字留进同步与恢复并提示、不再重发；上次会话留下的
     这种未同步标记在目录装载时同样处理（settleOrphan / sweepOrphanMarkers，复核六 W1-R6B-2）。未同步标记带着这一场的后端 scene_id：
     刷新之后（或另一个标签页里）目录不认得那个临时 sid、也没有别名时，凭它找到这一场，本机这一层跟过去、不提示；认不出是哪一场的
     临时 sid、标记又是刚写的（另一个标签页也许正在建它、等它的服务端）先不动；新建时写下的字照实说是新建时写的，不说这一场
     「不在目录里了」（它也许已经建好、只是这里认不出，复核七 W1-R7A-3 · W1-R7B-1 · W1-R7B-2）。收下孤儿场时这一页最后知道的服务端
     正文留在会话内存里：这一场从回收站回来时编辑器打开的是它，水合回来之前接着写的字不被当成「在别处被修改过」（复核七 W1-R7B-4）。
   · 上次会话把整场清空了、那一下没同步上（未同步标记说的正是那一份空稿）：清空时服务端就是眼下这一版就照停着的一稿记下、之后
     传上去，服务端又往前走了就换上服务端版本并照实提示——不一声不响换回旧稿（复核六 W1-R6B-5）。
   · 本机存储满了、这一页自己的冲突稿 / 拒掉的字只放进了会话内存：共用读缓存里标着未同步的那一份是它唯一的持久副本（slotOnlyCopy），
     之后的后台复核、冲突读取写服务端版本时同样先留、留不进就不盖，直到它持久地留下或作者又写了新的一稿（复核六 W1-R6B-3）。
     它持久地留下时按它自己那一次的原因留（会话里那一份随之换掉），不另说它是「另一个标签页（或上次打开时）」的（复核七 W1-R7A-4 ·
     W1-R7B-3）。
   · 同一浏览器开两个标签页：两份 store 共用 localStorage（读缓存、未同步标记、恢复记录），各有各的内存状态。
     保证的是——
       · 每个标签页读自己的那一份：这一页打开过、写过的场，之后 load / cachedHTML / 同步与恢复的比较读到的都是这一页自己的
         （会话内存；shown 是编辑器眼下在哪一份上），不是另一个标签页随后写进共用读缓存的字——另一页没同步上的字不会被这一页
         当成已保存的正文显示、提升（复核二 W1-R2B-1）。另一页存上的新版本由水合 / 复核读到，照常 loaded（没写字时换稿）或走冲突；
       · 共用读缓存里若装着这一页没有的、还没同步上的字（未同步标记还在），这一页往里写之前先把它留进同步与恢复——
         后写的一页不会静默盖掉先写的一页没同步上的字，那一页随后被关掉也一样（复核二 W1-R2B-3）；放不进本机存储时，这一页
         要写的若是服务端已有的版本就只写会话内存、不盖它（它可能是那段字唯一的持久副本，复核二 W1-R2B-2），要写的是作者
         刚写的字就照写，那段字只留在本次会话的同步与恢复里并醒目提示；
       · 未同步标记只在共用读缓存里的字都已在服务端上时才清，另一页随后写进去的没同步上的字留着标记；标记只说共用读缓存里
         那一份：这一页保存失败、要重新标未同步时，共用读缓存里若已换成别的字（另一页存上的一版、服务端版本），先把这一页
         没同步上的这一稿写回去再标（复核三 W1-R3A-5 · W1-R3B-7）；共用读缓存里是这一页自己上一次写进去的那一份
         （本机存储满了、之后的写没写进去）就不算别处的字（复核三 W1-R3A-6）；
       · 服务端按修订号拒绝后到的那一次（409），那个标签页走冲突副本、换成服务端版本，谁也盖不掉谁；冲突副本和恢复记录
         各有自己的键，两个标签页都看得到。
     不保证的是——同一场的读缓存键只有一个，后写的标签页覆盖先写的（先写的那段没同步上的字按上面留进同步与恢复）；
     一个标签页编辑器里还没交给 WrDocs 的字（不到自动保存、也没到切走 / 关页），只在那个标签页里。
   订阅（WrDocs.subscribe，fn(kind, detail)）：
     "state" { sid, workId, …state() }；"loaded" { sid, workId, html, reason, force, …state() }（读缓存换成了服务端 / 恢复 /
     采纳的正文；reason：server | pending（上次会话的本机稿换成服务端版本）| locked | refused | restore | adopt |
     own（读到的是这一页自己那一次回包丢了的保存）；force：服务端
     拒绝了本机的字 / 章已锁定 / 采纳时本机还有没存上的字，编辑器一定要换，作者正在写也一样）；
     "conflict-resolved" { sid, workId, html, reason }（冲突之后读到了服务端版本；reason：conflict | pending）。
   ========================================================== */

/* 作品id::sid → 一场的状态。必须带作品前缀：同名 slug（ch01s1）在每部作品都存在，裸 sid 会把
   PATCH 打到上一部作品的 draft（跨作品数据污染）。 */
const docMeta = {};

function metaKeyOf(workId, sid) {
  return `${workId}::${sid}`;
}

/* 目录眼下管这一场叫什么：乐观新建的场（「创建第一章」「加一场」）先用临时 sid（tmp_…），后端建好、目录重拉之后换成稳定的
   scene_id，临时 sid 经目录的别名仍解析到它；旧深链的位置式 sid 同样经目录解析。只认当前作品（目录只装着它）；目录里查不到
   就还是它自己 */
function catalogSid(workId, sid) {
  if (!sid || workId !== activeWorkId()) return sid;
  let hit = null;
  try { hit = WsCatalog.sceneById(sid); } catch (e) { hit = null; }
  return hit && hit.scene && hit.scene.sid ? hit.scene.sid : sid;
}

/* 一场只有一台状态机，按目录眼下的名字记（复核六 W1-R6B-1：过去临时 sid 和稳定 sid 各一台——写作台换到稳定 sid 时编辑器是空的、
   作者接着写的字撞上 409 说「在别处被修改过」，临时 sid 那一台停着的一稿之后在较新的一稿后面补发，刷新之后那段字只在一个
   谁也不读的本机键里）。这一场先前以别的名字起过状态机（临时 sid）：把它搬到现在的名字上（renameMeta），不另起一台 */
function metaFor(workId, sid) {
  const current = catalogSid(workId, sid);
  const key = metaKeyOf(workId, current);
  if (docMeta[key]) return docMeta[key];
  const earlier = (current !== sid && docMeta[metaKeyOf(workId, sid)]) || renamedMetaOf(workId, current);
  if (earlier) return renameMeta(earlier, current);
  return (docMeta[key] = newMeta(workId, current));
}

/* 这部作品里目录如今管它叫 sid、却还记在别的名字下的那一台（没有就是 null） */
function renamedMetaOf(workId, sid) {
  return Object.values(docMeta).find((m) => m.workId === workId && m.sid !== sid && currentSidOf(m) === sid) || null;
}

/* 后端 scene_id → 目录眼下管这一场叫什么（只认当前作品；查不到是 ""） */
function sidOfSceneId(workId, sceneId) {
  if (!sceneId || workId !== activeWorkId()) return "";
  try { return WsCatalog.sidForBackendId(sceneId) || ""; } catch (e) { return ""; }
}

/* 目录眼下管这一台状态机的那一场叫什么：同一次会话里有别名（乐观新建的临时 sid → 稳定的 scene_id）就是别名指的那一场；
   目录不认得这个名字、又没有别名时，凭记下的后端 scene_id 找（复核七 W1-R7A-3）；都找不到就还是它自己 */
function currentSidOf(m) {
  const aliased = catalogSid(m.workId, m.sid);
  if (aliased !== m.sid || !m.sceneId || !sceneGone(m)) return aliased;
  return sidOfSceneId(m.workId, m.sceneId) || m.sid;
}

/* 这一场的后端 scene_id，同步地知道多少算多少：目录里这一场已经有后端 id 就记下（不等进行中的新建）。未同步标记、恢复记录带上它——
   乐观新建的场先用临时 sid，刷新之后（或另一个标签页里）目录不再认得那个名字，凭它还能找到这一场（复核七 W1-R7A-3） */
function knownSceneId(m) {
  if (m.sceneId || !isActiveWork(m)) return m.sceneId || null;
  let id = null;
  try {
    const hit = WsCatalog.sceneById(m.sid);
    id = (hit && hit.scene && hit.scene.backendId) || null;
  } catch (e) { id = null; }
  if (id) m.sceneId = id;
  return id;
}

/* 写未同步标记（带上这一场的后端 scene_id，知道的话） */
function markPending(m, base, html) {
  knownSceneId(m);
  return pendingWrite(m, base, html);
}

/* 这一场换了名字：状态机（路上 / 排队 / 停着的一稿、等着的调用方，都是同一个对象）和本机这一层（会话内存、共用读缓存、
   未同步标记、恢复记录）一起搬到新名字下。新名字下已经有别处写进去的本机稿时不盖它：这一页写进共用读缓存的那一份
   不再算这一页的（slotOwn），旧名字下没同步上的那一份先留进同步与恢复 */
function renameMeta(m, sid) {
  const from = m.sid;
  delete docMeta[metaKeyOf(m.workId, from)];
  m.sid = sid;
  docMeta[metaKeyOf(m.workId, sid)] = m;
  const { moved, left } = renameSceneKeys(m.workId, from, sid);
  if (!moved) {
    m.slotOwn = undefined;
    m.slotOwnBase = null;
    m.slotOnlyCopy = false;
  }
  recoveryRename(m.workId, from, sid);
  if (left != null) {
    const { entry, created } = keepText(m, left, KEEP_REASONS.renamed, `场景 ${sid} · 未同步本地稿`, "unsynced");
    if (!entry || entry.durable !== false) dropSceneKeys({ workId: m.workId, sid: from });
    if (entry && created) {
      if (entry.durable !== false) recoveryNotice(m, NOTICE.otherTab);
      else recoveryNotice(m, NOTICE.otherTabVolatile, "danger");
    }
  }
  return m;
}

function newMeta(workId, sid) {
  return {
    workId,
    sid,
    sceneId: null,            // 后端 scene_id（解析过一次就记下：作品换了以后目录里查不到这一场）
    draftId: null,
    revision: 0,
    serverContent: "",
    currentFinalSceneRowId: null,
    lastPromotedRevisionNo: null,
    lastPromotedFinalSceneRowId: null,
    canonicalDirty: true,
    hydrated: false,
    hydrating: null,          // 进行中的水合（后来的调用方等同一次）
    revalidating: null,
    ownChainFrom: 0,          // 这个修订号之后到 revision 的每一版都是这一页自己存上的（提升被拒时分得清是谁动了草稿）
    preHydrateBase: undefined, // 水合之前的第一稿是在哪份正文上写的
    pendingAtLoad: false,
    pendingBaseAtLoad: null,  // 那时未同步标记说本机那一份写在服务端哪一版上 { draft, revision }
    shown: undefined,         // 这个标签页的编辑器眼下在哪一份正文上（见文件头「两个标签页」）
    slotOwn: undefined,       // 这一页上一次写进共用读缓存（本机存储）的那一份
    slotOwnBase: null,        // 它写在服务端哪一版上 { draftId | draft, revision, synced }；synced = 它就是服务端存下的一版
    slotOnlyCopy: false,      // 共用读缓存里这一页标着未同步的那一份是它唯一的持久副本（见 writeServerVersion）
    dirty: false,             // 本机有服务端还没确认的正文（路上 / 排队 / 失败待重发 / 核对中 / 冲突中）
    saveVersion: 0,
    savedVersion: 0,
    savedAt: null,
    lastSaveData: null,
    inFlight: null,           // 路上的那一次 { html, version, base, superseded }；html 为 null 时还在准备（水合）
    queued: null,             // 下一次要发的那一稿 { html, version }，只留最新的
    stalled: false,           // 上一次失败后 queued 停着，等下一次保存、显式 flush、重新聚焦或联网
    unsure: null,             // 发出去却没等到回包的几稿 { base, htmls }：它们也许已经存上了
    checking: null,           // 409 之后核对撞上的是不是自己那一稿（见 startOwnCheck）
    conflict: null,           // 409 之后、服务端版本读到之前（见 openConflict）
    lastEpisode: null,        // 上一次把本机稿放进同步与恢复的冲突 / 拒绝（写作台随之换稿时，编辑器里没交出的字并进它的提示）
    adoptions: [],            // 在路上的采纳请求，各一个记号 { m, base, html }（见 beginAdoption；起草台重新挂载后可能同时有几次）
    adoptionIdle: [],         // 等「这一场没有采纳在路上了」的调用方（提升被按修订号拒绝时先等采纳有结果，见 promoteRefusal）
    adoptedSeq: 0,            // 这一场落了地的采纳数（提升途中有采纳落了地 = 提升是被作者自己的采纳赶在了前面）
    lastLoadReason: null,     // 读缓存上一次换稿的原因（loaded 的 reason）
    adoptHeld: [],            // 采纳在路上时那一次保存撞上的 409 { flight, error }：采纳有了结果再定
    adoptionUnsure: null,     // 采纳没等到回包、读服务端也没读到：{ html, base }——结果不明，按那个修订号撞上的 409 先核对是不是它
    lockPending: null,        // 章已批准锁定时这一场还有结果没回来：{ kept, latest }（那一刻留下的字、交进来的最新一稿），有了结果再定（见 lockLocal）
    volatileWarned: false,    // 这一段「只留在本次会话里」的提示已经给过了（本机存储满了、又存不上服务端时只提示一次）
    waiters: [],
    lastSaveError: null,
    cacheError: null,
    localDurable: true,
  };
}

function meta(sid) {
  return metaFor(activeWorkId(), sid);
}

function isActiveWork(m) {
  return m.workId === activeWorkId();
}

function finalIdFromRef(ref) {
  if (typeof ref !== "string" || !ref.startsWith("final_scene:")) return null;
  return ref.slice("final_scene:".length) || null;
}

/* 吸收服务端回的草稿。own：这是这一页自己那一次保存存下的（保存回包、核对认出的自己那一稿）；别的来路（水合、复核、
   冲突之后读到的、采纳）带来了另一版（或另一份草稿）时，从这一版起重新数「这一页自己存上的」 */
function absorbServerState(m, data, { own = false } = {}) {
  const draft = data && data.draft;
  if (draft) {
    const before = { draftId: m.draftId, revision: m.revision };
    if (draft.draft_id) m.draftId = draft.draft_id;
    if (Number.isInteger(draft.revision_no)) m.revision = draft.revision_no;
    if (!own && (m.revision !== before.revision || m.draftId !== before.draftId)) m.ownChainFrom = m.revision;
    if (Object.prototype.hasOwnProperty.call(draft, "content")) m.serverContent = sanitizeManuscriptHTML(draft.content || "");
    if (Object.prototype.hasOwnProperty.call(draft, "last_promoted_revision_no")) {
      m.lastPromotedRevisionNo = draft.last_promoted_revision_no;
    }
    if (Object.prototype.hasOwnProperty.call(draft, "last_promoted_final_scene_row_id")) {
      m.lastPromotedFinalSceneRowId = draft.last_promoted_final_scene_row_id;
    }
    if (typeof draft.canonical_dirty === "boolean") m.canonicalDirty = draft.canonical_dirty;
    else if (Number.isInteger(draft.revision_no)) m.canonicalDirty = draft.revision_no !== m.lastPromotedRevisionNo;
  }
  if (data && Object.prototype.hasOwnProperty.call(data, "runtime_final_ref")) {
    m.currentFinalSceneRowId = finalIdFromRef(data.runtime_final_ref);
  }
}

/* 核对读不到服务端、等着再读的时候：不算「正在保存」（状态是保存失败） */
function checkWaiting(m) {
  return !!(m.checking && m.checking.failed);
}

function snapshotOf(m) {
  return {
    draftId: m.draftId,
    revision: m.revision,
    dirty: m.dirty,
    canonicalDirty: m.canonicalDirty,
    currentFinalSceneRowId: m.currentFinalSceneRowId,
    lastPromotedRevisionNo: m.lastPromotedRevisionNo,
    lastPromotedFinalSceneRowId: m.lastPromotedFinalSceneRowId,
    lastSaveError: m.lastSaveError,
    cacheError: m.cacheError,
    localDurable: m.localDurable,
    conflictPending: !!m.conflict,
    saving: (!!m.inFlight && !m.inFlight.superseded) || (!!m.queued && !m.stalled && !checkWaiting(m))
      || (!!m.checking && !checkWaiting(m)),
    savedAt: m.savedAt,
  };
}

/* ---- 通知 ---- */

const docListeners = new Set();
function notifyDoc(kind, detail) {
  docListeners.forEach((fn) => { try { fn(kind, detail); } catch (e) { /* 订阅者出错不打断保存 */ } });
}

function notifyState(m) {
  notifyDoc("state", { sid: m.sid, workId: m.workId, ...snapshotOf(m) });
}

/* 这个标签页在这一场的哪一份正文上（编辑器装的、交给 WrDocs 的、通知过的）：记在 shown，也放进这一页的会话内存——
   这一页之后读这一场（load、cachedHTML、同步与恢复的比较）读到的都是这一页自己的这一份，不是另一个标签页随后写进
   共用读缓存的字（那可能是它还没同步上的字，复核二 W1-R2B-1） */
function setShown(m, html) {
  m.shown = html;
  rememberInSession(m, html);
  // 章锁定那一刻交进来、还没存上的那一稿（lockPending.latest，见 lockLocal）只在编辑器还是它的时候才是「下一次要存的」：
  // 编辑器换成了别的正文（重新打开这一场、读缓存换稿），它就只在同步与恢复里了（复核五 W1-R5A-3：过去重新打开之后，
  // 编辑器是已存上的正文，锁定那一刻的字却在后台被存了上去）
  const lock = m.lockPending;
  if (lock && lock.latest && !sameManuscriptText(html, lock.latest.html)) lock.latest = null;
}

/* 读缓存换了一份正文：写作台随之换稿（或读到的就是作者正在写的底稿、接着写），这个标签页的编辑器从此在它上面。
   force：服务端不会收本机的字（拒绝了 / 章已锁定）——作者正在写、写的又正好是这份底稿，也要换 */
function notifyLoadedMeta(m, reason, { force = false } = {}) {
  const html = readCache(m.workId, m.sid);
  setShown(m, html);
  m.lastLoadReason = reason;
  notifyDoc("loaded", { sid: m.sid, workId: m.workId, ...snapshotOf(m), html, reason, force });
}

/* 本地稿被放进「同步与恢复」时告诉作者一声，并给一个直接打开它的按钮（入口在左侧导航栏底部）。
   外壳的提示层没挂上时（单测里单独加载 store）退回浏览器提示框。 */
function recoveryNotice(m, message, tone = "warn") {
  wsNotify({
    message,
    tone,
    timeout: tone === "danger" ? 20000 : 12000,
    action: {
      label: "打开同步与恢复",
      onClick: () => { emit("ws:recovery-open", { sid: m.sid }); },
    },
  });
}

const NOTICE = {
  conflict: "这份正文在别处被修改过，已加载服务端的最新版本。你本地没保存上的内容放进了「同步与恢复」，可以比较差异、恢复或导出。",
  conflictNothingKept: "这份正文在别处被修改过，已加载服务端的最新版本。",
  conflictVolatile: "这份正文在别处被修改过，编辑器已换成服务端的最新版本。你本机没保存上的正文因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。",
  conflictLoadFailed: "这份正文在别处被修改过，但暂时读不到服务端的最新版本。你本机的正文已放进「同步与恢复」，编辑器里的字也还在；连上服务器后会自动加载服务端版本，在那之前这一场不再保存。",
  conflictLoadFailedVolatile: "这份正文在别处被修改过，但暂时读不到服务端的最新版本；你本机的正文因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里。连上服务器后会自动加载服务端版本，在那之前这一场不再保存——刷新或关掉页面前请打开「同步与恢复」导出。",
  pendingAtLoad: "上次会话（或另一个标签页）有没保存到服务端的本地正文，已加载服务端版本。你的本地稿放进了「同步与恢复」，可以比较差异、恢复或导出。",
  pendingAtLoadVolatile: "上次会话（或另一个标签页）有没保存到服务端的本地正文。浏览器存储空间不足，它只放进了本次会话的「同步与恢复」（本机缓存里也还留着一份，直到这一场再保存）；编辑器显示的是服务端版本。请打开「同步与恢复」导出，或清理旧记录。",
  // 水合之前在上次会话留下的本机稿上接着写了，服务端版本和它对不上：照实说是上次会话的本机稿，不说「在别处被修改过」
  pendingConflict: "上次会话（或另一个标签页）有没保存到服务端的本地正文，编辑器已换成服务端上的版本。你的本地稿和刚写的几句都放进了「同步与恢复」，可以比较差异、恢复或导出。",
  pendingConflictVolatile: "上次会话（或另一个标签页）有没保存到服务端的本地正文，编辑器已换成服务端上的版本。浏览器存储空间不足，你的本地稿和刚写的几句只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。",
  locked: "这一章已批准锁定，改动不会保存：你刚写的几句放进了「同步与恢复」，编辑器换回了已存上的正文。要改写请先到成稿中心重新打开本章。",
  // 那一刻最后那一次保存的回包没回来、读服务端也没读到：说不准它存上了没有，照实说（复核七 W1-R7A-2）
  lockedUnconfirmed: "这一章已批准锁定，改动不会保存。你最后写的几句那一次保存的回包没回来，还没能确认存上了没有：它们放进了「同步与恢复」，编辑器先换回这台电脑上次确认存上的正文，连上服务器之后会换成服务端上的正文。要改写请先到成稿中心重新打开本章。",
  // 那一刻这一场还有一次保存（或读取）没有结果：编辑器等它有了结果再换，这里不说已经换了（复核四 W1-R4A-5 · W1-R4B-3）
  lockedPending: "这一章已批准锁定，改动不会保存：你刚写的几句放进了「同步与恢复」，编辑器随后换回已存上的正文。要改写请先到成稿中心重新打开本章。",
  lockedVolatile: "这一章已批准锁定，改动不会保存：你刚写的几句因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出。要改写请先到成稿中心重新打开本章。",
  // 打开时才知道：本机有这一场没同步上服务端的字（上次会话 / 另一个标签页留下的），章却已批准锁定（复核五 W1-R5B-1）
  lockedAtLoad: "这一章已批准锁定：这一场本机有没同步上服务端的正文（上次会话或另一个标签页留下的），它放进了「同步与恢复」，编辑器显示的是服务端上的正文。要改写请先到成稿中心重新打开本章。",
  lockedAtLoadVolatile: "这一章已批准锁定：这一场本机有没同步上服务端的正文。浏览器存储空间不足，它只放进了本次会话的「同步与恢复」（本机缓存里也还留着一份）——请打开它导出；编辑器显示的是服务端上的正文。要改写请先到成稿中心重新打开本章。",
  adoptKept: "这一场刚换成了采纳归档的 AI 稿：你在写作台里刚写、还没存上的几句放进了「同步与恢复」，可以比较差异、恢复或导出。",
  adoptKeptVolatile: "这一场刚换成了采纳归档的 AI 稿：你在写作台里刚写的几句因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。",
  unsyncedKept: "这一场刚写的几句没能保存到服务端，浏览器存储空间也不足、没写进本机缓存：它们放进了「同步与恢复」，连上之后会再保存。",
  unsyncedVolatile: "这一场刚写的几句没能保存到服务端，浏览器存储空间也不足：它们只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复；连上之后会再保存。",
  otherTab: "这一场在另一个标签页（或上次打开时）有没同步上服务端的正文，已放进「同步与恢复」，可以比较差异、恢复或导出。",
  otherTabVolatile: "这一场在另一个标签页（或上次打开时）有没同步上服务端的正文。浏览器存储空间不足，它只放进了本次会话的「同步与恢复」——刷新或关掉页面前请打开它导出或恢复。",
  otherTabVolatileKept: "这一场在另一个标签页（或上次打开时）有没同步上服务端的正文。浏览器存储空间不足，它只放进了本次会话的「同步与恢复」（本机缓存里也还留着一份，直到这一场再保存）。请打开「同步与恢复」导出，或清理旧记录。",
  // 这一场不在目录里了（乐观新建没能建到服务端、目录退回了服务端的版本；在别处移到了回收站），写作台再也打不开它（复核六 W1-R6B-2）
  gone: "这一场已经不在目录里了（新建没能存到服务端，或在别处移到了回收站）：你在这一场写的、还没同步上服务端的正文放进了「同步与恢复」，可以复制或导出。",
  goneVolatile: "这一场已经不在目录里了（新建没能存到服务端，或在别处移到了回收站）。你在这一场写的、还没同步上服务端的正文因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里（本机缓存里也还留着一份）——刷新或关掉页面前请打开它导出。",
  // 乐观新建的场（临时 sid）这里认不出了：也许新建没成，也许建好了、这里却不知道它现在是目录里的哪一场（建场的回包丢了、页面在建场途中
  // 刷新了）——不说它「不在目录里了」（复核七 W1-R7B-1）
  tmpGone: "新建这一场时写下、那一刻还没存到服务端的正文放进了「同步与恢复」（这里认不出它现在是目录里的哪一场）。这一场要是已经出现在目录里，打开它、从「同步与恢复」复制过去就行；也可以导出。",
  tmpGoneVolatile: "新建这一场时写下、那一刻还没存到服务端的正文因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里（本机缓存里也还留着一份）——刷新或关掉页面前请打开它复制或导出。",
  // 上次会话把整场清空了、没同步上，服务端之后又往前走了（复核六 W1-R6B-5：过去一声不响就换回了旧稿）
  clearedAtLoad: "上次会话（或另一个标签页）你把这一场整场清空了，那一下还没同步到服务端；服务端之后又有了新的一版，编辑器显示的是它。还要清空的话，在编辑器里再清一次。",
  clearedLocked: "这一章已批准锁定：上次会话（或另一个标签页）你把这一场整场清空了、还没同步到服务端，这一下存不上了，编辑器显示的是服务端上的正文。要改写请先到成稿中心重新打开本章。",
  server: "这一场在别处有更新，已加载服务端的最新版本。你刚才在旧版本上写的几句放进了「同步与恢复」，可以比较差异、恢复或导出。",
  // 读到的是这一页自己那一次回包丢了的保存（复核七 W1-R7A-2）：不是「在别处有更新」
  own: "编辑器换成了服务端上你先前存上的那一稿（那一次保存的回包当时没回来）。你刚才在旧版本上写的几句放进了「同步与恢复」，可以比较差异、恢复或导出。",
  replaced: "编辑器换成了新的正文，你刚才没保存的几句放进了「同步与恢复」，可以比较差异、恢复或导出。",
  // 「恢复」换掉的那一稿还没同步上服务端、又不在同步与恢复里（复核七 W1-R7A-1）
  restoreKept: "编辑器换成了恢复的正文：这一场先前交出去、还没同步上服务端的几句放进了「同步与恢复」，可以比较差异、恢复或导出。",
  volatile: "编辑器换成了新的正文。你刚才没保存的几句因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。",
};

/* 写作台换稿时编辑器里还没交出来的几句（keepLocalCopy）：按换稿的原因说（复核七 W1-R7A-2 · W1-R7B-6） */
const LOCAL_COPY_NOTICE = { server: NOTICE.server, own: NOTICE.own, adopt: NOTICE.adoptKept };

/* ---- 服务端明确拒绝的保存 ---- */

const REFUSED_SAVE_CODES = new Set(["CHAPTER_APPROVED_LOCKED", "AUTHOR_DRAFT_NOT_CURRENT", "AUTHOR_DRAFT_NOT_FOUND"]);
const TRANSIENT_STATUSES = new Set([401, 403, 408, 409, 429]); // 访问令牌、超时、其余 409（幂等请求还在跑……）、限流：再发会好

/* 这一稿服务端永远不会收（章已锁定、作者稿已不是当前的一份、别的 4xx）：再发也一样，不停着等重发。
   409 冲突不在这里（走核对 / 冲突），访问令牌、限流这类一时的也不在 */
function refusedSave(e) {
  if (!e || e.code === "AUTHOR_DRAFT_CONFLICT") return false;
  if (REFUSED_SAVE_CODES.has(e.code)) return true;
  const status = Number(e.status);
  return status >= 400 && status < 500 && !TRANSIENT_STATUSES.has(status) && !e.retryable;
}

/* 服务端拒绝这一稿的原因（作者读得懂的话；认不得的照实给代码）。同步与恢复的「恢复」被拒时也用它 */
function refusalReason(error) {
  const code = error && error.code;
  if (code === "CHAPTER_APPROVED_LOCKED") return "这一章已批准锁定，要先到成稿中心重新打开本章";
  if (code === "AUTHOR_DRAFT_NOT_CURRENT" || code === "AUTHOR_DRAFT_NOT_FOUND") return "服务端的这份作者稿已不是当前的一份";
  return code ? `服务端拒绝了这次保存（${code}）` : "服务端拒绝了这次保存";
}

/* unconfirmed：同一个修订号上最后那一次保存的回包没回来、读服务端也没读到——编辑器换回的是这一页上次确认存上的那一版，照实说（复核七 W1-R7A-2） */
function refusedNotice(error, { keptAny, durable, unconfirmed = false }) {
  const shown = unconfirmed
    ? "编辑器先换回这台电脑上次确认存上的正文（最后那一次保存的回包没回来，还没能确认它存上了没有；连上服务器之后会换成服务端上的正文）"
    : "编辑器换回了服务端上的正文";
  const head = `服务端没有接受这一场的保存：${refusalReason(error)}。${shown}`;
  if (!durable) return `${head}；你刚写的几句因为浏览器存储空间不足，只留在本次会话的「同步与恢复」里——刷新或关掉页面前请打开它导出或恢复。`;
  return keptAny ? `${head}，你刚写的几句放进了「同步与恢复」，可以比较差异、恢复或导出。` : `${head}。`;
}

/* 把一段本机正文放进「同步与恢复」。没有字的不放；已经一模一样持久地留着的不再放一份；只留在本次会话里的那一份，
   这次放得进本机存储就换成持久的，还是放不进就不再多放一份。
   → { entry, created }：entry.durable=false 表示只在本次会话里；created = 这次新放进去（要不要提示作者看它） */
function keepText(m, html, reason, label = `场景 ${m.sid} · 冲突本地稿`, type = "conflict") {
  if (!hasAuthorText(html)) return { entry: null, created: false };
  const text = docText(html);
  const same = recoveryList().filter((entry) => entry.sid === m.sid && entry.workId === m.workId && docText(entry.html) === text);
  const durable = same.find((entry) => entry.durable !== false);
  if (durable) return { entry: durable, created: false };
  const entry = recoveryCreate({ sid: m.sid, workId: m.workId, sceneId: knownSceneId(m), html, type, reason, label });
  if (entry.durable !== false) {
    same.forEach((old) => recoveryRemove(old.id)); // 会话里那一份这次放进了本机存储
    return { entry, created: true };
  }
  if (same.length) {
    recoveryRemove(entry.id); // 还是放不进本机存储：会话里已经有这一份，不再多放
    return { entry: same[0], created: false };
  }
  return { entry, created: true };
}

function keepCopy(m, html, reason, label) {
  return keepText(m, html, reason, label).entry;
}

/* ---- 共用读缓存（两个标签页 / 上次会话） ---- */

/* 共用读缓存里装着这一页没有的、还没同步上服务端的正文吗：未同步标记还在，字和这一页这一份、和要写进去的都不一样——
   另一个标签页写进去的（它的保存正失败着），上次会话留下的，或这一页只放进了会话内存的那一份本机稿（showServerVersion）。
   是这一页自己上一次写进去的那一份就不是：本机存储满了、这一页之后的写没写进去，它还停在那里（复核三 W1-R3A-6）——
   除非它是这一页那段字唯一的持久副本（slotOnlyCopy：同步与恢复里那一份只在本次会话里，复核六 W1-R6B-3）。
   是就返回它 */
function foreignSlotText(m, html) {
  if (pendingRead(m) == null) return null;
  const slot = readSlot(m);
  if (slot == null || !hasAuthorText(slot) || (slot === m.slotOwn && !m.slotOnlyCopy)) return null;
  const mine = m.shown === undefined ? readCache(m.workId, m.sid) : m.shown;
  if (sameManuscriptText(slot, mine) || sameManuscriptText(slot, html)) return null;
  return slot;
}

/* 把共用读缓存里那段别处没同步上的正文留进同步与恢复；新放进去的提示一次。serverWrite：这一页要写的是服务端已有的版本
   （放不进本机存储时本机缓存里那一份不动，提示照实说）。
   那一段是这一页自己的冲突稿 / 拒掉的字（slotOnlyCopy：当时同步与恢复放不下，它只进了本次会话，共用读缓存里这一份是唯一的持久副本）：
   按它自己那一次的原因、标签留（会话里那一份随之换成持久的），不说它是「另一个标签页（或上次打开时）」的、不再提示——那一次已经
   告诉过作者了（复核七 W1-R7A-4 · W1-R7B-3：过去作者腾出空间之后，下一次后台复核 / 保存把它说成另一个标签页留下的） */
function keepForeign(m, html, serverWrite) {
  if (m.slotOnlyCopy && html === m.slotOwn) return keepOwnSlotCopy(m, html);
  const { entry, created } = keepText(
    m, html, "另一个标签页（或上次打开时）留在本机、还没同步上服务端的正文", `场景 ${m.sid} · 未同步本地稿`, "unsynced",
  );
  if (entry && created) {
    if (entry.durable !== false) recoveryNotice(m, NOTICE.otherTab);
    else recoveryNotice(m, serverWrite ? NOTICE.otherTabVolatileKept : NOTICE.otherTabVolatile, "danger");
  }
  return entry;
}

/* 这一页自己只进了本次会话的那一稿（slotOnlyCopy），这次放得进本机存储：沿用会话里那一份记录的原因、标签、类型持久地留下 */
function keepOwnSlotCopy(m, html) {
  const text = docText(html);
  const session = recoveryList().find((entry) => entry.sid === m.sid && entry.workId === m.workId && entry.durable === false
    && docText(entry.html) === text);
  const { entry } = session
    ? keepText(m, html, session.reason, session.label, session.type)
    : keepText(m, html, KEEP_REASONS.conflict);
  return entry;
}

/* 服务端的一版进读缓存（这一页的会话内存 + 共用的那一份），它从此是这一页这一场的正文。
   durable=false：只进会话内存（本机稿没能持久留进同步与恢复，本机存储里那一份和未同步标记要留到刷新以后）。
   共用的那一份装着这一页没有的、没同步上的正文时先把它留进同步与恢复；留不进本机存储就同样只进会话内存、不盖它——
   它可能是那段正文唯一的持久副本（复核二 W1-R2B-2）。写进了本机存储，读缓存里的字就都在服务端上了：清未同步标记。
   本机存储里那一份这一次没盖、它又是这一页自己写进去的、标着未同步：记下它是那段字唯一的持久副本（slotOnlyCopy）——之后的
   后台复核、冲突读取写服务端版本时同样先留、留不进就不盖，直到它持久地留进同步与恢复或作者又写了新的一稿（复核六 W1-R6B-3：
   过去这一页自己的那一份不算「别处的字」，下一次后台复核就把它盖掉、清掉标记，刷新之后哪里都没有） */
function writeServerVersion(m, html, { durable = true } = {}) {
  let toStorage = durable;
  if (toStorage) {
    const foreign = foreignSlotText(m, html);
    if (foreign != null) {
      const entry = keepForeign(m, foreign, true);
      if (entry && entry.durable === false) toStorage = false;
    }
  }
  const written = cacheWrite(m, html, { durable: toStorage });
  m.localDurable = toStorage && written.ok;
  m.cacheError = toStorage
    ? written.error
    : Object.assign(new Error("本地恢复空间不足：服务端版本只在本次会话里，本机稿还留在本机缓存"), { code: "LOCAL_STORAGE_QUOTA" });
  if (toStorage && written.ok) {
    m.slotOwn = written.html;
    m.slotOwnBase = { draftId: m.draftId, revision: m.revision, synced: true };
    m.slotOnlyCopy = false;
    pendingClear(m);
  } else if (!toStorage && m.slotOwn !== undefined && pendingRead(m) != null && readSlot(m) === m.slotOwn) {
    m.slotOnlyCopy = true;
  }
  return written;
}

/* 这一页交给 WrDocs 的字写在服务端哪一版上：水合过就是眼下的修订号（冲突 / 核对中还是撞上 409 之前的那一版）；
   水合之前，作者若是在上次会话没同步上的那一稿上接着写的（未同步标记的指纹对得上，见 saveMeta），就还是标记说的那一版；
   别的都不知道（null） */
function textBase(m) {
  if (m.hydrated && m.draftId) return { draftId: m.draftId, revision: m.revision };
  return m.pendingBaseAtLoad || null;
}

/* 这一页存进来的正文写进读缓存：共用的那一份装着别处没同步上的字时先留进同步与恢复（作者最新的字照样要落地）。
   先标未同步、再写读缓存（浏览器若在请求完成前退出，下次水合会先留恢复副本，不会把服务端旧稿静默盖回本地）：
   标记写不进去（本机存储满了）时这一稿不进共用读缓存、只在这一页的会话内存里——进去了却没有标记，下次打开时它会被当成
   过时的读缓存、让服务端版本静默盖掉（复核四 W1-R4B-1）；标记写进去了、读缓存没写进去时，标记退回原来的样子：
   它说的还是那里原有的那一份（别处的一份、服务端版本都不能说成没同步上的，复核三 W1-R3A-6；复核四 W1-R4A-2）。
   两种情况这一稿都只在本次会话里（localDurable=false）：保存失败时 onFailed 把它留进同步与恢复并提示。
   repair：只是把这一页没同步上的那一稿写回共用读缓存、重新标上（markUnsynced）——修的是本机存储，编辑器没换稿：
   不改「这一页的编辑器在哪一份上」（shown），章锁定那一刻交进来的较新一稿（lockPending.latest）照旧是下一次要存的
   （复核六 W1-R6A-4：过去另一个标签页随后写过共用读缓存时，这一次重写把它丢了，之后补发的是较旧的一稿） */
function writeAuthorText(m, html, { repair = false } = {}) {
  const foreign = foreignSlotText(m, html);
  if (foreign != null) keepForeign(m, foreign, false);
  const base = textBase(m);
  const previous = pendingRead(m);
  const marked = markPending(m, base, html);
  const written = cacheWrite(m, html, { durable: marked.ok });
  if (marked.ok && !written.ok) pendingRestore(m, previous);
  if (!repair) {
    setShown(m, written.html);
    m.lastLoadReason = null; // 编辑器上的是作者自己交进来的这一稿
  }
  if (written.ok) {
    m.slotOwn = written.html;
    m.slotOwnBase = base ? { ...base, synced: false } : null;
    m.slotOnlyCopy = false;
    m.volatileWarned = false;
  }
  m.localDurable = written.ok;
  m.cacheError = written.ok ? null : (written.error || marked.error);
  return written;
}

/* 共用读缓存里是这一页先前写进去的一份、它又没存上服务端（不是服务端上的那一版）：标上未同步，说它写在它自己那一版上——
   不是眼下的修订号：这一页之后存上的几版它都没有（复核四 W1-R4A-2） */
function markSlotIfUnsynced(m) {
  const slot = readSlot(m);
  const own = m.slotOwnBase;
  if (slot != null && slot === m.slotOwn && !(own && own.synced) && !sameManuscriptText(slot, toDocHTML(m.serverContent || ""))) {
    markPending(m, own, slot);
  }
}

/* 这一页的 html 还没同步上：重新标未同步。标记说的是共用读缓存里那一份——那里已经换成了别的字（另一个标签页随后存上的
   一版、它水合时写进去的服务端版本）时，先把这一页的这一稿写回去再标，不让另一页存上的字被当成没同步上的
   （复核三 W1-R3A-5 · W1-R3B-7）；写回去之前那里若是别处没同步上的字，writeAuthorText 先把它留进同步与恢复 */
function markUnsynced(m, html) {
  const slot = readSlot(m);
  if (html != null && slot !== m.slotOwn && !sameManuscriptText(slot, html)) {
    writeAuthorText(m, html, { repair: true });
    return;
  }
  if (html != null && sameManuscriptText(slot, html)) markPending(m, textBase(m));
  else markSlotIfUnsynced(m);
}

/* 清未同步标记——只在共用读缓存里的是这一页自己的字（这一页写进去的 / 这一页这一份），或和 same 一样（服务端上的那一版）时：
   另一个标签页在这期间写进去的、还没同步上的字留着标记，这一页之后往里写之前会先把它留进同步与恢复 */
function clearPendingIfMine(m, same = null) {
  const slot = readSlot(m);
  if (slot == null || !hasAuthorText(slot) || slot === m.slotOwn || sameManuscriptText(slot, m.shown)
      || (same != null && sameManuscriptText(slot, same))) {
    pendingClear(m);
  }
}

/* ---- 错误 ---- */

function unavailableError() {
  return Object.assign(new Error("场景尚未就绪，草稿未保存到服务端"), { code: "AUTHOR_DRAFT_UNAVAILABLE" });
}
function holdError() {
  return Object.assign(new Error("这份正文在别处被修改过，服务端的最新版本还没读下来；这一稿先留在本机，暂不保存"), { code: "AUTHOR_DRAFT_CONFLICT" });
}
function staleBaseError() {
  return Object.assign(new Error("这一场在别处有更新：刚才是在这台电脑较旧的缓存上写的"), { code: "AUTHOR_DRAFT_CONFLICT" });
}
/* 采纳归档落了地、这一页等着的保存就此作废：代码照旧是冲突（flush 答「conflict」），replacedByAdoption 说明换稿的是作者自己
   在起草台的采纳，不是别处的修改——同步与恢复的「恢复」据此照实说（复核六 W1-R6A-1） */
function replacedError() {
  return Object.assign(new Error("这一场的正文刚被采纳归档替换；没存上的本机正文已放进「同步与恢复」"), {
    code: "AUTHOR_DRAFT_CONFLICT",
    replacedByAdoption: true,
  });
}
function staleReadError() {
  return Object.assign(new Error("读到的服务端版本比撞上冲突的那一次还旧"), { code: "AUTHOR_DRAFT_STALE_READ" });
}
function movedError() {
  return Object.assign(new Error("这一场刚读到了别处更新的版本，编辑器已换成它；看过之后再提升"), { code: "AUTHOR_DRAFT_CONFLICT" });
}
function notSyncedError() {
  return Object.assign(new Error("这一场本机的这一稿还没同步到服务端（服务端是一份新建的空稿）；下一次保存或离开这一场时会同步"), {
    code: "AUTHOR_DRAFT_NOT_SYNCED",
  });
}
function clearedError() {
  return Object.assign(new Error("这一场在本机整场清空了，那一下还没同步到服务端；下一次保存或离开这一场时会同步"), {
    code: "AUTHOR_DRAFT_NOT_SYNCED",
  });
}
function goneError() {
  return Object.assign(new Error("这一场已经不在目录里了；没同步上服务端的正文放进了「同步与恢复」"), {
    code: "AUTHOR_DRAFT_SCENE_GONE",
  });
}
function lockedError() {
  return Object.assign(new Error("这一章已批准锁定：请先到成稿中心重新打开本章，再改它的正文"), { code: "CHAPTER_APPROVED_LOCKED" });
}
function movedBySelfError(cause) {
  return Object.assign(new Error("提升途中你又改了几句、已经存上：这次没有提升，看过之后再点一次「提升」"), {
    code: "AUTHOR_DRAFT_MOVED_BY_SELF",
    cause,
  });
}
function lockPendingError() {
  return Object.assign(new Error("章锁定那一刻写的几句还没存上服务端（在「同步与恢复」里；路上那一次有了结果、本章重新打开着的话会接着保存），这次没有提升"), {
    code: "AUTHOR_DRAFT_LOCK_PENDING",
  });
}
function unsavedError(cause) {
  return Object.assign(new Error("提升途中你又改了几句，还没确认存上服务端：这次没有提升；存上之后再点一次「提升」"), {
    code: "AUTHOR_DRAFT_UNSAVED",
    cause,
  });
}
/* 提升被按修订号拒绝，本机已经没有要发的：最后那一次保存的回包丢了，服务端眼下多半就是那一稿（复核七 W1-R7A-2） */
function unconfirmedError(cause) {
  return Object.assign(new Error("最后那一次保存的回包没回来，还没能确认它存上了没有（服务端眼下可能就是那一稿）：这次没有提升；连上服务器、编辑器换成服务端上的正文之后再点一次「提升」"), {
    code: "AUTHOR_DRAFT_UNCONFIRMED",
    cause,
  });
}
/* 作者确认提升之后编辑器被这一页自己这边换了一版（章锁定 / 服务端拒绝之后换回、读到自己那一次回包丢了的保存、恢复……）：
   不是别处的修改（复核七 W1-R7A-2） */
function replacedHereError() {
  return Object.assign(new Error("编辑器刚换了一版正文，你确认的是换之前的那一稿：这次没有提升；看过之后再点一次「提升」"), {
    code: "AUTHOR_DRAFT_REPLACED",
  });
}
/* 提升途中作者自己在起草台的采纳落了地（它在一个事务里存下并提升了采纳的那一稿）：不是别处的修改（复核七 W1-R7B-5） */
function adoptedError(cause) {
  return Object.assign(new Error("这一场刚换成了你在 AI 起草台采纳归档的稿（它已经是权威正文），这次没有提升"), {
    code: "AUTHOR_DRAFT_ADOPTED",
    replacedByAdoption: true,
    cause,
  });
}

/* ---- 等待者：save() / flush() 等「这一稿（或更新的一稿）有了结果」 ----
   detailed 的等待者（replace 用）收到 { data, version, html }：带走它的那一次保存发的是哪一稿 */

function waitFor(m, version, detailed = false) {
  if (version <= m.savedVersion) {
    return Promise.resolve(detailed ? { data: m.lastSaveData, version: m.savedVersion, html: null } : m.lastSaveData);
  }
  const promise = new Promise((resolve, reject) => { m.waiters.push({ version, resolve, reject, detailed }); });
  promise.catch(() => {}); // 调用方不接失败时（离场冲刷、冒烟脚本）不算未处理的拒绝；接的照样收到
  return promise;
}

function resolveWaiters(m, version, data, html) {
  const rest = [];
  m.waiters.forEach((w) => {
    if (w.version <= version) w.resolve(w.detailed ? { data, version, html } : data);
    else rest.push(w);
  });
  m.waiters = rest;
}

function rejectWaiters(m, error, fromVersion = 0) {
  const rest = [];
  m.waiters.forEach((w) => { if (w.version > fromVersion) w.reject(error); else rest.push(w); });
  m.waiters = rest;
}

function outcomeOf(m, version) {
  return waitFor(m, version).then(
    () => "saved",
    (e) => {
      if (e && e.replacedByAdoption) return "adopted"; // 换稿的是作者自己在起草台的采纳（复核七 W1-R7B-5）
      if (e && e.code === "AUTHOR_DRAFT_CONFLICT") return "conflict";
      return e && e.refused ? "refused" : "failed";
    },
  );
}

/* 等这一场没有采纳在路上（acceptCanonical 收尾，或 endAdoption 收掉最后一个记号）：提升被按修订号拒绝时先等采纳有结果再分 */
function adoptionsSettled(m) {
  if (!m.adoptions.length) return Promise.resolve();
  return new Promise((resolve) => { m.adoptionIdle.push(resolve); });
}

function wakeAdoptionIdle(m) {
  if (m.adoptions.length) return;
  m.adoptionIdle.splice(0).forEach((resolve) => resolve());
}

/* ---- 服务端草稿 ---- */

async function sceneIdOf(m) {
  if (m.sceneId) return m.sceneId;
  if (!isActiveWork(m)) return null; // 目录只认当前作品
  let sceneId = null;
  try { sceneId = await WsCatalog.backendSceneId(m.sid); } catch (e) { sceneId = null; }
  if (sceneId) {
    m.sceneId = sceneId;
    // 未同步标记写下时还不知道它（乐观新建的场，建场的回包那时还没回来）：补进去，刷新之后凭它还能找到这一场（复核七 W1-R7A-3）
    pendingStampScene(m, sceneId);
  }
  return sceneId || null;
}

/* 这一场所在的章已批准锁定（目录说的；写作台据此只读、不自动保存）：WrDocs 也不替它发保存（恢复 / 重试同步） */
function approvedLocked(m) {
  if (!isActiveWork(m)) return false;
  try {
    const hit = WsCatalog.sceneById(m.sid);
    return !!(hit && hit.chapter && hit.chapter.state === "approved");
  } catch (e) {
    return false;
  }
}

/* 这一场已不在目录里：目录已从后端装载、查不到它（乐观新建的场没能建到服务端、目录退回了服务端的版本；在别处移到了回收站）。
   写作台再也打不开它。只认当前作品；目录还在装载时不算 */
function sceneGone(m) {
  if (!isActiveWork(m)) return false;
  try {
    return !!WsCatalog.ready() && !WsCatalog.sceneById(m.sid);
  } catch (e) {
    return false;
  }
}

// 作品id::sid → 正在进行的 ensure { seq, promise }。同一场同一时刻只发一次 POST ensure：版本列表、
// 某一版正文、draftId 常被同时调用（开发模式下 React 还会把挂载 effect 连跑两遍）。
// 请求结束（成功或失败）即移除，之后的调用重新请求。seq 按发出的先后递增：冲突之后要的是「冲突之后发出的」那一次。
const ensureInflight = new Map();
let ensureSeq = 0;

/* POST ensure（幂等，返回服务端当前的草稿）。只取回包，吸收与否由调用方决定。目录里还没有这一场时返回 null。
   minSeq：只接受第 minSeq 次（含）之后发出的 ensure——更早发出、还在路上的那一次先等它落地，再发一次新的。
   每一次都带自己的幂等键（复核二 W1-R2A-1）：客户端给回包丢了的请求留着键，同样的下一次 ensure 就会被服务端按那个键
   重放成当时的旧快照——核对自己那一稿、冲突之后读服务端版本读到的就都是冲突之前的样子。同一场同时只发一次由上面保证。 */
function requestDraft(m, minSeq = 0) {
  const key = metaKeyOf(m.workId, m.sid);
  const pending = ensureInflight.get(key);
  if (pending && pending.seq >= minSeq) return pending.promise;
  if (pending) return pending.promise.then(() => {}, () => {}).then(() => requestDraft(m, minSeq));
  const seq = ++ensureSeq;
  const request = (async () => {
    const sceneId = await sceneIdOf(m);
    if (!sceneId) return null;
    return apiPost(`/api/v1/author-drafts/scene/${sceneId}/ensure`, {}, {
      idempotencyKey: `wr-doc-ensure-${sceneId}-${Date.now()}-${randomSuffix(8)}`,
    });
  })();
  const entry = { seq, promise: null };
  entry.promise = request.finally(() => {
    if (ensureInflight.get(key) === entry) ensureInflight.delete(key);
  });
  ensureInflight.set(key, entry);
  return entry.promise;
}

/* 409 之后读服务端眼下的草稿：只认第 minSeq 次之后发出的 ensure；读回来的修订号比 floor（撞上 409 时服务端至少是的那一版）
   还旧（同一份草稿）就当没读到、马上再发一次，几次都这样才算读不到。没有草稿（目录里没有这一场）→ 读不到 */
async function readServerDraft(m, minSeq, floor) {
  let last = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const data = await requestDraft(m, attempt === 0 ? minSeq : ensureSeq + 1);
    const draft = data && data.draft;
    if (!draft || !draft.draft_id) throw unavailableError();
    const sameDraft = !m.draftId || draft.draft_id === m.draftId;
    if (!sameDraft || !Number.isInteger(draft.revision_no) || draft.revision_no >= floor) return data;
    last = data;
  }
  // 读到的是什么带上：几轮都是同一个更旧的版本时由调用方决定信它（trustedStaleRead）
  throw Object.assign(staleReadError(), { observed: last.draft, observedData: last });
}

async function ensureDraftMeta(m) {
  if (m.draftId) return m;
  const data = await requestDraft(m);
  if (data && !m.draftId) {
    absorbServerState(m, data);
    notifyState(m);
  }
  return m;
}

/* ---- 水合 ---- */

/* 水合：把读缓存和服务端草稿对齐（一场一次；冲突中 = 再读一次服务端版本）。进行中的水合，后来的调用方等同一次；出错抛给调用方。
   水合总是读一次服务端（正在路上的那一次 ensure 照常共用）：版本对比、draftId 先前吸收进来的快照可能是几个钟头之前的，
   拿它当服务端版本，打开时就显示旧稿、甚至把共用读缓存退回旧的一版（复核四 W1-R4B-6）。 */
function hydrateMeta(m) {
  if (m.conflict) return conflictLoad(m).then(() => { if (m.conflict) throw m.lastSaveError; return m; });
  if (m.hydrated) return Promise.resolve(m);
  if (m.hydrating) return m.hydrating;
  const run = (async () => {
    const data = await requestDraft(m);
    if (m.hydrated || m.conflict) return m;
    if (data && data.draft) {
      absorbServerState(m, data);
      notifyState(m);
    }
    if (!data || !data.draft || m.draftId == null) return m; // 目录里还没有这一场的后端 id：下次再水合
    settleHydrate(m);
    pump(m); // 水合期间章被锁定了（lockPending）：换回已存上的正文
    return m;
  })();
  const shared = run.finally(() => { if (m.hydrating === shared) m.hydrating = null; });
  m.hydrating = shared;
  // 章锁定那一刻这一场还没水合（lockPending）、这一次又没水合成：等着的调用方收到失败，停着等下一次保存 / flush / 重新聚焦或联网
  // 再读（见 pump）——过去这时谁也不再水合它，之后的保存、flush、提升一直停在「正在保存」（复核五 W1-R5A-4 · W1-R5B-3）
  shared.then(() => { if (!m.hydrated) parkLockHydrate(m, unavailableError()); }, (e) => parkLockHydrate(m, e));
  return shared;
}

function parkLockHydrate(m, error) {
  if (!m.lockPending || m.hydrated || m.conflict) return;
  m.stalled = true;
  m.lastSaveError = error;
  rejectWaiters(m, error);
  notifyState(m);
}

/* 服务端草稿到手之后决定读缓存怎么办 */
function settleHydrate(m) {
  const serverHTML = toDocHTML(m.serverContent || "");
  // 修订号 1 的空稿是 ensure 刚建的：本机缓存里的字就是工作稿。更高修订号上的空稿是在别处清空的，照常算服务端版本
  const freshBlank = !serverHTML && !(m.revision > 1);
  const current = m.shown === undefined ? readCache(m.workId, m.sid) : m.shown; // 这一页眼下的那一份
  const base = m.preHydrateBase === undefined ? current : m.preHydrateBase;
  const pendingAtLoad = m.pendingAtLoad;
  const pendingAt = m.pendingBaseAtLoad;
  m.preHydrateBase = undefined;
  m.pendingAtLoad = false;
  m.pendingBaseAtLoad = null;

  if (m.dirty) {
    // 水合之前就有保存排着：作者是在当时编辑器里那一份（base）上写的。服务端就是那一份（或是新建的空稿）——照常保存；
    // base 是上次会话没同步上的本机稿、未同步标记说它就写在服务端眼下这一版上——作者接着写的是这一版的下一稿，照常保存
    // （PATCH 带的就是那一版的修订号，服务端若已往前走会 409，谁也盖不掉谁；复核三 W1-R3A-3）；
    // 服务端在别处改过——本机写的字按冲突处理，服务端版本上屏，绝不带着服务端的修订号把它盖掉。
    const continuesPending = pendingAtLoad && !!pendingAt && pendingAt.draft === draftTail(m.draftId)
      && pendingAt.revision === m.revision;
    if (freshBlank || sameManuscriptText(base, serverHTML) || continuesPending) {
      m.hydrated = true;
      return;
    }
    const texts = [m.inFlight && m.inFlight.html, m.queued && m.queued.html];
    if (pendingAtLoad) texts.push(base);
    // 作者是在上次会话留下的本机稿上写的：提示照实说「上次会话有没保存到服务端的本地正文」，不说「在别处被修改过」
    openConflict(m, staleBaseError(), texts, { serverKnown: true, kind: pendingAtLoad ? "pending" : "conflict" });
    return;
  }

  if (pendingRead(m) != null) {
    // 上个会话（或另一个标签页）保存失败 / 没等到回包就关了页面：未同步标记说的是本机存储里的那一份，不得被静默覆盖
    const local = readSlot(m);
    const localText = local != null && hasAuthorText(local);
    if (localText && !freshBlank && !sameManuscriptText(local, serverHTML)) {
      const { entry, created } = keepText(m, local, "上次会话（或另一个标签页）未同步，服务端已有不同版本", `场景 ${m.sid} · 未同步本地稿`);
      const durable = !entry || entry.durable !== false;
      showServerVersion(m, serverHTML, durable);
      m.hydrated = true;
      notifyState(m);
      // 写作台随之换稿（reason pending）：编辑器里水合之前敲下、还没交出来的字经 keepLocalCopy 并进这一次，提示照实说是
      // 上次会话的本机稿——过去这里按 reason server 换稿，写作台另说一句「这一场在别处有更新」，服务端却从没在别处改过（复核五 W1-R5A-2）
      const episode = { kept: new Set([docText(local)]), keptAny: created, durable, resolving: true };
      m.lastEpisode = episode;
      notifyLoadedMeta(m, "pending");
      episode.resolving = false;
      const typed = episode.kept.size > 1; // 编辑器里还有刚敲的几句，也放进去了
      if (episode.durable) recoveryNotice(m, typed ? NOTICE.pendingConflict : NOTICE.pendingAtLoad);
      else recoveryNotice(m, typed ? NOTICE.pendingConflictVolatile : NOTICE.pendingAtLoadVolatile, "danger");
      return;
    }
    // 本机把这一场整场清空了、那一下还没同步上（未同步标记说的正是共用读缓存里这一份空稿）：它就是作者最新的一稿，
    // 不能让服务端上的旧稿一声不响地盖回去（复核六 W1-R6B-5：过去标记被当成「本机存储里没有稿」静默消费掉）
    if (!localText && local != null && hasAuthorText(serverHTML) && markerDescribes(m, local)) {
      settleClearedAtLoad(m, local, serverHTML);
      return;
    }
    // 内容一致（上次实际保上了）/ 本机存储里没有稿：静默消费标记。服务端是新建的空稿时标记留给下面的工作稿
    if (!localText || !freshBlank) pendingClear(m);
  }

  if (freshBlank) {
    m.hydrated = true;
    rememberInSession(m, current);
    // 服务端是 ensure 刚建的空稿、这一页有这一场的字：它就是工作稿，只是还没同步上——和保存失败后停着的一稿一样
    // （未同步标记、状态是保存失败），下一次保存 / flush / 重新聚焦或联网时传上去。过去它算「已保存」，
    // 这期间「提升」提升的是那份空稿（复核二 W1-R2B-1 同一类：只提升作者眼前的那一稿）。
    // 章已批准锁定：它永远存不上——留进同步与恢复、换上服务端的空稿（keepLockedCopy）
    if (current != null && hasAuthorText(current)) {
      if (approvedLocked(m)) keepLockedCopy(m, current, serverHTML);
      else holdWorkingCopy(m, current);
    }
    return;
  }

  // 服务端版本和这一页这一份不同（或和共用的那一份不同）：进读缓存；这一页这一份不是它时通知 loaded
  if (serverHTML !== (current || "") || serverHTML !== (readSlot(m) || "")) {
    writeServerVersion(m, serverHTML);
    m.hydrated = true;
    // 读到的正是作者自己在起草台正在采纳的那一稿（采纳的回包还没回来）：换稿的是那次采纳，不是「在别处有更新」（复核七 W1-R7B-6）
    if (serverHTML !== (current || "")) notifyLoadedMeta(m, adoptionInFlight(m, serverHTML) ? "adopt" : "server");
    return;
  }
  m.hydrated = true;
  rememberInSession(m, serverHTML); // 这一页从此读自己的这一份
}

/* 服务端版本进读缓存。durable=false（本机稿没能持久留进同步与恢复）时只进会话内存：本机存储里那份本机稿和未同步标记留着。 */
function showServerVersion(m, serverHTML, durable) {
  writeServerVersion(m, serverHTML, { durable });
}

/* 服务端是 ensure 刚建的空稿、这一页有这一场的字：本机这份就是工作稿，按「保存失败后停着的最新一稿」记下
   （不在水合里发请求：水合可能是别的台子为了读一眼触发的） */
function holdWorkingCopy(m, html, error = notSyncedError()) {
  m.dirty = true;
  m.canonicalDirty = true;
  markPending(m, { draftId: m.draftId, revision: m.revision });
  m.queued = { html: sanitizeManuscriptHTML(html), version: ++m.saveVersion };
  m.stalled = true;
  m.lastSaveError = error;
  notifyState(m);
}

/* 未同步标记说的是 html 这一段字吗（指纹对得上；没有指纹的旧版标记不知道说的是哪一段，当它说的就是共用读缓存里那一份） */
function markerDescribes(m, html) {
  const at = pendingBase(m);
  return !!at && (!at.fp || at.fp === textFingerprint(html));
}

/* 上次会话（或另一个标签页）把这一场整场清空了、那一下没同步上（见 settleHydrate）。清空时服务端就是眼下这一版（标记说的草稿、
   修订号都对得上）：空稿就是这一版的下一稿，按停着的一稿记下——编辑器照旧是空的，下一次保存 / flush / 重新聚焦或联网时传上去
   （和服务端刚建空稿、本机有字时一样）。服务端之后又往前走了（或标记不知道写在哪一版上）、章已批准锁定：清空落不到作者清空时的
   那一版上了——换上服务端版本，照实告诉作者（空稿没有字，不必留进同步与恢复） */
function settleClearedAtLoad(m, local, serverHTML) {
  const at = pendingBase(m);
  const locked = approvedLocked(m);
  if (!locked && at.draft && at.draft === draftTail(m.draftId) && at.revision === m.revision) {
    m.hydrated = true;
    rememberInSession(m, local);
    holdWorkingCopy(m, local, clearedError());
    return;
  }
  pendingClear(m);
  writeServerVersion(m, serverHTML);
  m.hydrated = true;
  notifyState(m);
  notifyLoadedMeta(m, locked ? "locked" : "pending", { force: locked });
  recoveryNotice(m, locked ? NOTICE.clearedLocked : NOTICE.clearedAtLoad);
}

/* 服务端是 ensure 刚建的空稿、这一页有这一场的字，章却已批准锁定：这份字永远存不上（WrDocs 不替锁定的章保存）。
   过去它不进工作稿、也不进同步与恢复，这一场就算已水合、没有本机改动——只读的编辑器把它当成已存上的终稿显示，下一次 load
   的后台复核又把空稿当成「同一个修订号上换了字」写进读缓存、清掉未同步标记，这段字从此哪里都没有（复核五 W1-R5B-1）。
   现在先留进同步与恢复（放不进本机存储时本机缓存里那一份和未同步标记留到刷新以后），再换上服务端的版本并告诉作者；
   写作台照 loaded(force, reason locked) 换稿，编辑器里还没交出来的字经 keepLocalCopy 并进这一次 */
function keepLockedCopy(m, html, serverHTML) {
  const { entry, created } = keepText(m, html, "这一章已批准锁定，本机还有没同步上服务端的正文", `场景 ${m.sid} · 未同步本地稿`, "unsynced");
  const durable = !entry || entry.durable !== false;
  writeServerVersion(m, serverHTML, { durable });
  notifyState(m);
  const episode = { kept: new Set(entry ? [docText(html)] : []), keptAny: created, durable, resolving: true };
  m.lastEpisode = episode;
  notifyLoadedMeta(m, "locked", { force: true });
  episode.resolving = false;
  if (episode.keptAny || !episode.durable) {
    recoveryNotice(m, episode.durable ? NOTICE.lockedAtLoad : NOTICE.lockedAtLoadVolatile, episode.durable ? "warn" : "danger");
  }
}

/* 这一页这一份是不是还没同步上服务端的本机字：未同步标记在、说的正是它（指纹对得上；没有指纹的旧版标记看共用读缓存里是不是它） */
function unsyncedCopy(m, html) {
  if (html == null || !hasAuthorText(html)) return false;
  const at = pendingBase(m);
  if (!at) return false;
  return at.fp ? at.fp === textFingerprint(html) : sameManuscriptText(readSlot(m), html);
}

function isClean(m) {
  return m.hydrated && !m.dirty && !m.inFlight && !m.queued && !m.conflict && !m.checking;
}

/* 作者在起草台正在采纳的就是这一稿（采纳请求在路上，beginAdoption 记下了它） */
function adoptionInFlight(m, html) {
  return m.adoptions.some((token) => token.html != null && sameManuscriptText(token.html, html));
}

/* 这一页有一稿写在眼下这个修订号上、发出去却没等到回包（unsure）：服务端眼下也许就是它 */
function unsureHere(m) {
  return !!(m.hydrated && m.draftId && m.unsure && m.unsure.base === m.revision && m.unsure.htmls.length);
}

/* 已水合、没有本机改动的一场，按刚读到的服务端草稿对齐（后台复核与采纳前的预检共用）。
   修订号往前走了：换成服务端版本，这一页这一份不是它时通知 loaded。修订号没动、字也一样：这一页这一份就是这个修订号上的
   正文（这一页读的是自己的那一份，不是另一个标签页写进共用读缓存的字），不动。修订号没动、字却不一样：服务端的历史被改写过
   （库从备份恢复之后别处又存到了同一个修订号）——和往前走了一样换上它；过去这里只收下服务端的字、不换稿，作者接着写的
   下一次保存带着同一个修订号过了比对，把另一台设备那一版静默盖掉（复核四 W1-R4B-2）。修订号更旧的不理：读缓存里的字
   不能被一份更旧的回包盖掉（库从备份恢复、历史往回走了时，作者接着写的第一次保存 409，走冲突：本机稿进同步与恢复、换成服务端版本）。
   这期间作者开始写了就整个不吸收——保存带的还是旧修订号，服务端动过就 409，走冲突。
   读到的正是这一页没等到回包的那一稿（unsure，修订号正好往前一步）：是这一页自己存上的——接上那个修订号，换稿说的是它（reason own；
   章已批准锁定时 locked），不说「在别处有更新」（复核七 W1-R7A-2）。读到的正是作者在起草台正在采纳（或结果不明）的那一稿：说的是
   采纳（reason adopt，复核五 W1-R5B-4 · 复核七 W1-R7B-6）。 → 换了稿返回 true */
function applyRefresh(m, data) {
  const draft = data && data.draft;
  if (!draft || !isClean(m)) return false;
  const replaced = !!(draft.draft_id && m.draftId && draft.draft_id !== m.draftId);
  if (!replaced && (!Number.isInteger(draft.revision_no) || draft.revision_no < m.revision)) return false;
  const current = m.shown === undefined ? readCache(m.workId, m.sid) : m.shown;
  const hasContent = Object.prototype.hasOwnProperty.call(draft, "content");
  const readHTML = hasContent ? toDocHTML(sanitizeManuscriptHTML(draft.content || "")) : null;
  const rewritten = !replaced && draft.revision_no === m.revision && hasContent && !sameManuscriptText(readHTML, current);
  const moved = replaced || draft.revision_no > m.revision || rewritten;
  const own = !replaced && hasContent && !!m.unsure && m.unsure.base === m.revision && draft.revision_no === m.unsure.base + 1
    && m.unsure.htmls.some((html) => sameManuscriptText(html, readHTML));
  absorbServerState(m, data, { own });
  if (own) m.unsure = null; // 回包丢了的那一稿确实存上了
  if (rewritten) m.ownChainFrom = m.revision; // 同一个修订号上换了一版：这一页自己存上的那几版不再算数
  if (moved) {
    const serverHTML = toDocHTML(m.serverContent || "");
    const differs = serverHTML !== (current || "");
    // 要换掉的这一份是还没同步上服务端的本机字（未同步标记说的正是它）：先留进同步与恢复（放不进本机存储时本机缓存里那一份和
    // 标记留着）——干净的一场按说不会是它，这是最后一道：过去这时它被当成「同一个修订号上换了字」静默盖掉（复核五 W1-R5B-1）
    let durable = true;
    if (differs && !sameManuscriptText(current, serverHTML) && unsyncedCopy(m, current)) {
      const entry = keepForeign(m, current, true);
      if (entry && entry.durable === false) durable = false;
    }
    if (differs || serverHTML !== (readSlot(m) || "")) writeServerVersion(m, serverHTML, { durable });
    // 读到的正是那一次结果不明的采纳（adoptionUnsure）或还在路上的那一次采纳：换上的是作者自己在起草台采纳的稿，不是「在别处有更新」
    // （复核五 W1-R5B-4 · 复核七 W1-R7B-6）
    const unsureAdoption = !!m.adoptionUnsure && sameManuscriptText(serverHTML, m.adoptionUnsure.html);
    if (unsureAdoption) m.adoptionUnsure = null;
    const adopted = unsureAdoption || adoptionInFlight(m, serverHTML);
    if (differs) {
      notifyState(m);
      const locked = own && approvedLocked(m);
      notifyLoadedMeta(m, adopted ? "adopt" : (own ? (locked ? "locked" : "own") : "server"), { force: locked });
      return true;
    }
  }
  notifyState(m);
  return false;
}

/* 已水合、没有本机改动的一场：后台再问一次服务端（F03-24：在别的设备上改过的场，重新打开时不再一直显示旧缓存） */
function revalidate(m) {
  if (!isClean(m) || m.revalidating || m.hydrating) return;
  const run = requestDraft(m).then((data) => { applyRefresh(m, data); })
    .catch(() => { /* 后台复核失败无妨：下次打开再问 */ })
    .finally(() => { if (m.revalidating === run) m.revalidating = null; });
  m.revalidating = run;
}

/* ---- 保存 ---- */

/* 本机缓存 + 未同步标记落地，然后发出去或替换排队的那一稿。返回这一稿的结果（detailed 见 waitFor） */
function saveMeta(m, html, detailed = false) {
  // 章已批准锁定（目录说的）：不替它保存（见 lockLocal）
  if (approvedLocked(m)) return lockLocal(m, html);
  if (!m.hydrated && !m.dirty && m.preHydrateBase === undefined) {
    // 水合之前的第一稿：记下作者是在编辑器里哪一份正文上写的（水合时与服务端版本比，文字不同就按冲突处理）；
    // 那一份若是上次会话没同步上的本机稿，也记下未同步标记说它写在服务端哪一版上。标记得说的正是这一段字（指纹对得上）
    // 才算数：共用读缓存里另一个标签页写进去的、本机存储满了没写进去的那一稿，都和作者写的这一份无关——那时拿它说的修订号
    // 当底，会在作者没见过的一版上保存（复核四 W1-R4A-2）。没有指纹的旧版标记（只有时刻）照旧当作说的就是它、底不知道
    m.preHydrateBase = m.shown === undefined ? readCache(m.workId, m.sid) : m.shown;
    const at = pendingBase(m);
    const describesBase = !!at && (!at.fp || at.fp === textFingerprint(m.preHydrateBase));
    m.pendingAtLoad = describesBase;
    m.pendingBaseAtLoad = describesBase && at.fp && at.draft ? at : null;
  }
  const written = writeAuthorText(m, html);
  m.dirty = true;
  m.canonicalDirty = true;
  const version = ++m.saveVersion;
  const outcome = waitFor(m, version, detailed);
  if (m.conflict) {
    // 服务端版本还没读到：这一稿只留在本机（读缓存 + 未同步标记；换掉读缓存之前放进同步与恢复），不发
    rejectWaiters(m, m.conflict.hold, version - 1);
    notifyState(m);
    void conflictLoad(m);
    return outcome;
  }
  m.lastSaveError = null;
  m.queued = { html: written.html, version };
  m.stalled = false;
  notifyState(m);
  if (m.checking) void checkRead(m, m.checking); // 核对读不到服务端时，这一次保存再读一次
  pump(m);
  return outcome;
}

/* 这一场接着往下走：章锁定那一刻还有结果没回来的（lockPending），这一场闲下来就换回已存上的正文（releaseLock）；
   否则把排队的那一稿发出去（同一场一次一个）。每一个结果落地的地方都调它 */
function pump(m) {
  if (m.lockPending) {
    if (m.inFlight || m.conflict || m.checking || m.adoptions.length) return;
    if (m.hydrated) releaseLock(m);
    // 还没水合（章锁定那一刻第一次水合没成）：已存上的是哪一版要读了才知道——读一次；读不到时停着（parkLockHydrate），
    // 等下一次保存、flush、窗口重新聚焦或联网
    else if (!m.hydrating && !m.stalled) hydrateMeta(m).catch(() => {});
    return;
  }
  // 采纳请求在路上（beginAdoption）：不再发新的保存，采纳有了结果再说——不和作者自己的采纳抢这一个修订号
  if (m.inFlight || m.conflict || m.checking || m.stalled || m.adoptions.length || !m.queued) return;
  const flight = { html: null, version: 0, base: null, superseded: false };
  m.inFlight = flight;
  void runFlight(m, flight);
}

async function runFlight(m, flight) {
  try {
    // 这一场第一次保存：先水合（可能发现作者是在旧缓存上写的 → 冲突，排队的已经进了同步与恢复）
    if (!m.hydrated) await hydrateMeta(m);
    if (m.conflict || m.adoptions.length || m.lockPending) { // 冲突了 / 水合期间开始采纳 / 章锁定了：排队的那一稿先不发
      if (m.inFlight === flight) m.inFlight = null;
      pump(m);
      return;
    }
    if (!m.draftId) throw unavailableError();
    const next = m.queued;
    if (!next) { // 排队的被收走了（采纳归档 / 水合时发现是在旧缓存上写的）：没有要发的
      if (m.inFlight === flight) m.inFlight = null;
      return;
    }
    m.queued = null;
    flight.html = next.html;
    flight.version = next.version;
    flight.base = m.revision;
    const data = await apiPatch(`/api/v1/author-drafts/${m.draftId}`, {
      content: flight.html,
      base_revision_no: flight.base,
    });
    onSaved(m, flight, data);
  } catch (e) {
    onFailed(m, flight, e);
  }
}

function onSaved(m, flight, data) {
  if (m.inFlight === flight) m.inFlight = null;
  if (flight.superseded) {
    settleSuperseded(m, data);
    return;
  }
  absorbServerState(m, data, { own: true });
  const draft = data && data.draft;
  if (!draft || !Object.prototype.hasOwnProperty.call(draft, "content")) m.serverContent = flight.html;
  m.unsure = null; // 这个修订号上存上了：之前没等到回包的那几稿都没存上
  m.savedVersion = Math.max(m.savedVersion, flight.version);
  m.lastSaveData = data;
  m.savedAt = Date.now();
  m.volatileWarned = false;
  // 共用读缓存里这一页写进去的那一份就是这次存上的：它从此是服务端上的一版，不再是没同步上的字
  if (m.slotOwnBase && !m.slotOwnBase.synced && m.slotOwn === flight.html) m.slotOwnBase = { ...m.slotOwnBase, synced: true };
  if (!m.queued) {
    // 最新的一稿存上了：才能消费跨会话的未同步标记（共用读缓存里另一个标签页随后写进去的字除外）
    m.dirty = false;
    m.lastSaveError = null;
    clearPendingIfMine(m, toDocHTML(m.serverContent || ""));
  }
  if (data && data.words_rollup && isActiveWork(m)) WsCatalog.applyWordsRollup(m.sid, data.words_rollup);
  /* 2026-09-22 场景诊断第三轮：正文一存，服务端把这一场 / 这一章开着的发现数带回来，角标随之更新 */
  if (data && data.diagnosis_rollup) {
    try { WsDiagnosis.applyRollup(data.diagnosis_rollup); } catch (e) {}
  }
  resolveWaiters(m, flight.version, data, flight.html);
  notifyState(m);
  pump(m); // 排队的那一稿带着新的修订号接着发
}

function onFailed(m, flight, e) {
  if (m.inFlight === flight) m.inFlight = null;
  if (flight.superseded) {
    settleSuperseded(m, null);
    return;
  }
  if (e && e.code === "AUTHOR_DRAFT_CONFLICT") {
    if (flight.html != null && m.adoptions.some((token) => token.base === flight.base)) {
      // 采纳请求在路上、服务端可能已经先存下了采纳的那一稿：这次 409 多半就是它撞的。先按住，采纳有了结果再定
      // （成了 → acceptCanonical 作废它；都没成 → endAdoption 再照常核对 / 冲突；复核三 W1-R3A-4 · W1-R3B-6）。
      // 按住的记在这一场上、不记在哪一次采纳上：同时有几次采纳时，先回来的那一次不会把它带走（复核四 W1-R4A-1）
      m.adoptHeld.push({ flight, error: e });
      notifyState(m);
      return;
    }
    if (flight.html != null && mayBeOwnSave(m, flight, e)) {
      startOwnCheck(m, flight, e);
      return;
    }
    openConflict(m, e, [flight.html, m.queued && m.queued.html], { minRevision: conflictFloor(flight, e) });
    return;
  }
  if (flight.html != null && refusedSave(e)) {
    // 服务端明确拒绝了这一稿（章在别处批准锁定……）：再发也一样——留进同步与恢复，换回服务端版本，不停着、不重发（复核三 W1-R3B-2）。
    // 同一个修订号上之前还有一稿没等到回包（unsure；这一次在比对修订号之前就被拒了）：服务端眼下也许就是它，换回的是这一页上次确认
    // 存上的一版——提示照实说还没能确认；refuseLocal 随后的后台读取读到它，就照自己存上的换稿（applyRefresh，reason own）。
    // 不等那一次读取再换：作者之后接着写的字不在这一次拒绝里，要照常存（复核七 W1-R7A-2）
    refuseLocal(m, e, [flight.html, m.queued && m.queued.html], { unconfirmed: unsureHere(m) });
    return;
  }
  if (flight.html == null && !m.queued) {
    // 还在准备（水合）时就失败了、又没有要发的（排队的被采纳收走了）：没有字要重发
    notifyState(m);
    pump(m);
    return;
  }
  // 断网 / 服务端出错：正文留在本机（读缓存 + 未同步标记），停着等下一次保存、显式 flush、重新聚焦或联网——
  // 再发的一定是最新的一稿
  if (flight.html != null) {
    if (mayHaveLanded(e)) rememberUnsure(m, flight);
    if (!m.queued) m.queued = { html: flight.html, version: flight.version };
  }
  // 这一场已不在目录里（建章没成、目录退回了服务端的版本；在别处移到了回收站）：停着等重发也永远发不出去——
  // 没同步上的字留进同步与恢复并告诉作者（复核六 W1-R6B-2）
  if (sceneGone(m) && settleOrphan(m)) {
    console.warn("[WrDocs] 这一场已不在目录里，没同步上的正文留进了同步与恢复:", m.sid, e);
    return;
  }
  m.stalled = true;
  m.lastSaveError = e;
  markUnsynced(m, m.queued && m.queued.html);
  if (!m.localDurable && m.queued) keepVolatileUnsynced(m, m.queued.html);
  console.warn("[WrDocs] 正文保存失败（本机已留底，下次保存再发）:", e);
  rejectWaiters(m, e);
  notifyState(m);
  pump(m); // 章锁定那一刻它还在路上（lockPending）：这时才把停着的这一稿留进同步与恢复、换回已存上的正文
}

/* 存不上服务端、本机存储又满了（这一稿没写进本机缓存）：它只在这个标签页的内存里——留进同步与恢复（放得进本机存储就持久地留）
   并告诉作者，只留在本次会话里时醒目提示、给出入口；同一段失败只提示一次（复核四 W1-R4B-8：过去只留一份会话记录、一句不说，
   刷新之后这段字就没了） */
function keepVolatileUnsynced(m, html) {
  const { entry } = keepText(m, html, "断网或服务端保存失败；浏览器缓存也不可用", `场景 ${m.sid} · 会话内未同步稿`, "unsynced");
  if (!entry || m.volatileWarned) return;
  m.volatileWarned = true;
  if (entry.durable === false) recoveryNotice(m, NOTICE.unsyncedVolatile, "danger");
  else recoveryNotice(m, NOTICE.unsyncedKept);
}

/* 采纳归档时还在路上的那一次回来了（acceptCanonical 把它标成 superseded）。它的字已经在采纳前的备份 / 同步与恢复里，
   等它的调用方在采纳时就收到了结果：失败、409 都不补发、不停着、不开冲突、不提示，只让采纳之后排队的那一稿接着发。 */
function settleSuperseded(m, data) {
  const draft = data && data.draft;
  if (draft && Number.isInteger(draft.revision_no) && draft.revision_no > m.revision && !m.dirty) {
    // 按说到不了这里（采纳用的就是这一次的修订号，服务端按修订号拒绝它）；真存上了，就当服务端有了新版本：编辑器跟着换
    absorbServerState(m, data);
    writeServerVersion(m, toDocHTML(m.serverContent || ""));
    notifyState(m);
    notifyLoadedMeta(m, "server");
    return;
  }
  notifyState(m);
  pump(m);
}

/* ---- 服务端不会收的字：服务端明确拒绝 / 章已批准锁定 ---- */

/* 本机没存上的这几稿留进同步与恢复（去重；和服务端版本一样的不留）→ { kept, created, durable }：
   created = 这次新放进去了（提示作者看它）；durable=false = 有的只留在本次会话里 */
function keepRefused(m, texts, reason) {
  const serverHTML = toDocHTML(m.serverContent || "");
  const kept = new Set();
  let created = false;
  let durable = true;
  texts.forEach((html) => {
    if (html == null) return;
    const text = docText(html);
    if (!text || kept.has(text) || (m.hydrated && sameManuscriptText(html, serverHTML))) return;
    const result = keepText(m, html, reason, `场景 ${m.sid} · 没存上的本机稿`, "unsynced");
    if (!result.entry) return;
    kept.add(text);
    if (result.created) created = true;
    if (result.entry.durable === false) durable = false;
  });
  return { kept, created, durable };
}

/* 服务端明确拒绝了本机的字（章在别处批准锁定、作者稿已不是当前的一份……），或章已锁定、WrDocs 不替它发（lockLocal）：
   它们永远存不上，不停着、不重发。先留进同步与恢复，读缓存换回服务端的版本（放不进本机存储时只换会话内存：本机缓存里那一份
   和未同步标记留到刷新以后），告诉作者；写作台照 loaded(force) 换稿——作者正在写也换，编辑器里还没交出来的字写作台经
   keepLocalCopy 先留（并进这一次的提示）。作者稿已不是当前的一份：丢掉旧的草稿 id 重新水合，读到的是眼下那一份；
   其余的在后台再问一次服务端（章在别处批准前又存过一版时，换上的是那一版）；章已锁定：让目录重读一次，写作台随之只读。
   unconfirmed：同一个修订号上还有一稿没等到回包，换稿之前读服务端也没读到（verifyUnsure）——换上的是这一页上次确认存上的那一版，
   那几句也许其实存上了：提示照实说还没能确认（复核七 W1-R7A-2） */
function refuseLocal(m, error, texts, { locked = false, unconfirmed = false } = {}) {
  const reason = locked ? KEEP_REASONS.locked : KEEP_REASONS.refused;
  const { kept, created, durable } = keepRefused(m, texts, reason);
  // 章锁定那一刻已经留进同步与恢复、提示过的几句（lockPending）：算这一次留过的，写作台补交的一样的字不再多留、多提示
  if (m.lockPending) m.lockPending.kept.forEach((text) => kept.add(text));
  m.lockPending = null;
  m.queued = null;
  m.stalled = false;
  // 没等到回包的那几稿（unsure）留着：被拒的这一次若是带着旧修订号去的（请求体太大在修订号比对之前就拒了），
  // 之前回包丢了的那一稿可能已经存上——下一次保存撞上 409 时照样认得出是自己那一稿，不说「在别处被修改」
  m.dirty = false;
  m.lastSaveError = null;
  m.canonicalDirty = !(Number.isInteger(m.lastPromotedRevisionNo) && m.lastPromotedRevisionNo === m.revision);
  writeServerVersion(m, toDocHTML(m.serverContent || ""), { durable });
  const staleDraft = !!error && (error.code === "AUTHOR_DRAFT_NOT_CURRENT" || error.code === "AUTHOR_DRAFT_NOT_FOUND");
  if (staleDraft) {
    m.draftId = null;
    m.hydrated = false;
  }
  rejectWaiters(m, Object.assign(error, { refused: true }));
  notifyState(m);
  const episode = { kept, keptAny: created, durable, resolving: true };
  m.lastEpisode = episode;
  notifyLoadedMeta(m, locked ? "locked" : "refused", { force: true });
  episode.resolving = false;
  if (locked) {
    if (episode.keptAny || !episode.durable) {
      const notice = unconfirmed ? NOTICE.lockedUnconfirmed : NOTICE.locked;
      recoveryNotice(m, episode.durable ? notice : NOTICE.lockedVolatile, episode.durable ? "warn" : "danger");
    }
  } else {
    recoveryNotice(m, refusedNotice(error, { ...episode, unconfirmed }), episode.durable ? "warn" : "danger");
  }
  if (staleDraft) hydrateMeta(m).catch(() => {});
  else revalidate(m);
  if (!locked && error.code === "CHAPTER_APPROVED_LOCKED" && isActiveWork(m)) {
    try { void Promise.resolve(WsCatalog.refresh(m.workId)).catch(() => {}); } catch (e) { /* 目录下次装载时再知道 */ }
  }
}

/* 章已批准锁定（目录说的）时交进来的一稿：不写读缓存、不发请求——和已存上的正文不一样就留进同步与恢复，编辑器换回已存上的
   正文（写作台转只读的那一刻还没交出去的半句，过去就这样没了，复核三 W1-R3B-3）；失败后停着的一稿一起留、不再发。
   路上 / 核对中 / 冲突中 / 还没水合 / 采纳在路上：那边自有结果，已存上的正文是哪一版要等它——这一稿先留进同步与恢复（提示照实说
   「编辑器随后换回」），记下 lockPending：排队的那一稿不再发，那边有了结果、这一场闲下来时（pump）再把停着 / 排队的留下、
   编辑器换回已存上的正文（releaseLock）。过去这里说「换回了」却没有换，排队的旧稿随后还会补发（复核四 W1-R4A-5 · W1-R4B-3）。
   交进来的这一稿也记下（lockPending.latest，after = 那一刻的 saveVersion）：那边有了结果时本章若已重新打开、编辑器里还是它
   （编辑器换过稿就作罢，见 setShown），它比排队的那一稿新（复核五 W1-R5A-3 · W1-R5B-2）。还没水合的，这就读一次服务端（pump）。
   同一个修订号上还有一稿没等到回包（unsure，这一场还停着没存上的字）：已存上的是哪一版同样要读了才知道——一样先留、记下 lockPending，
   releaseLock 先读一次服务端再换稿（复核七 W1-R7A-2）。
   → 被拒绝的 Promise（CHAPTER_APPROVED_LOCKED） */
function lockLocal(m, html) {
  const error = Object.assign(lockedError(), { refused: true });
  const rejected = Promise.reject(error);
  rejected.catch(() => {});
  const serverHTML = toDocHTML(m.serverContent || "");
  const unsure = m.dirty && unsureHere(m);
  if (m.inFlight || m.conflict || m.checking || !m.hydrated || m.adoptions.length || unsure) {
    const { kept, created, durable } = keepRefused(m, [html], KEEP_REASONS.locked);
    const lock = m.lockPending || { kept: new Set(), latest: null };
    kept.forEach((text) => lock.kept.add(text));
    lock.latest = { html: sanitizeManuscriptHTML(html == null ? "" : html), after: m.saveVersion };
    m.lockPending = lock;
    if (created) recoveryNotice(m, durable ? NOTICE.lockedPending : NOTICE.lockedVolatile, durable ? "warn" : "danger");
    if (!m.hydrated || unsure) pump(m);
    return rejected;
  }
  if (!m.dirty && sameManuscriptText(html, serverHTML)) return rejected; // 没有新写的字
  refuseLocal(m, error, [html, m.queued && m.queued.html], { locked: true });
  return rejected;
}

/* 章锁定那一刻这一场还有结果没回来（lockPending），现在闲下来了：还锁着——停着 / 排队的那一稿留进同步与恢复、编辑器换回
   已存上的正文（refuseLocal；那一刻已经留过、提示过的几句不再提示）；这期间重新打开了本章——照常接着发，发的是最新的一稿：
   锁定那一刻交进来的那一稿（编辑器里还是它）比排队的新，就排上它（它已在同步与恢复里）；排队的是重新打开之后才交的，
   就还是排队的那一稿。过去这里接着发排队的旧稿，状态随后说「草稿已保存」，编辑器里较新的字却只在同步与恢复里
   （复核五 W1-R5A-3 · W1-R5B-2）。
   还锁着、同一个修订号上又还有一稿没等到回包（unsure）：它也许存上了、批准的也许正是它——换稿之前先读一次服务端（verifyUnsure，
   一把锁只读一次）：是它就认作这一页自己存上的，编辑器换成它、不说它没存上；读不到就照这一页知道的换，提示照实说还没能确认
   （复核七 W1-R7A-2：过去马上换回这一页上次知道的那一版，说那几句没存上，其实存上、被批准的正是它；本章之后重新打开，作者在那一版上
   接着写，读取随后才回来，又被说成「在别处有更新」） */
function releaseLock(m) {
  const lock = m.lockPending;
  const latest = lock && lock.latest;
  if (!approvedLocked(m)) {
    m.lockPending = null;
    if (latest && !(m.queued && m.queued.version > latest.after)) queueLatest(m, latest.html);
    pump(m);
    return;
  }
  if (lock && !lock.verified && unsureHere(m)) {
    lock.verified = true;
    verifyUnsure(m, (read) => {
      lock.unconfirmed = !read;
      pump(m);
    });
    return;
  }
  refuseLocal(m, lockedError(), [m.queued && m.queued.html, latest && latest.html], { locked: true, unconfirmed: !!(lock && lock.unconfirmed) });
}

/* 把 html 排成下一次要发的一稿（本机缓存 + 未同步标记照 save 落地）。读缓存里已经是它（排队的、已存上的就是这一稿）就不必。
   失败后停着的照旧停着：等下一次保存、flush、重新聚焦或联网 */
function queueLatest(m, html) {
  if (sameManuscriptText(html, readCache(m.workId, m.sid))) return;
  const written = writeAuthorText(m, html);
  m.dirty = true;
  m.canonicalDirty = true;
  m.queued = { html: written.html, version: ++m.saveVersion };
  notifyState(m);
}

/* 章已批准锁定（目录说的）、这一场还停着一稿没存上（保存失败之后才知道锁了）：它永远存不上——和 lockLocal 一样留进
   同步与恢复、换回已存上的正文（写作台打开它、窗口重新聚焦时）。停着的这一稿（或同一个修订号上更早没等到回包的一稿）也许其实存上了
   （unsure）：交给 releaseLock，先读一次服务端再换稿（复核七 W1-R7A-2）。→ 处理了（或已交出去）返回 true */
function settleLockedStall(m) {
  if (!m.stalled || !m.queued || m.inFlight || m.conflict || m.checking || !m.hydrated || !approvedLocked(m)) return false;
  if (unsureHere(m)) {
    if (!m.lockPending) m.lockPending = { kept: new Set(), latest: null };
    m.stalled = false;
    pump(m);
    return true;
  }
  refuseLocal(m, lockedError(), [m.queued.html], { locked: true });
  return true;
}

/* ---- 不在目录里了的场、换了名字的场 ---- */

/* 这一场写作台再也打不开了（sceneGone；或换了名字、新名字下已另有一台状态机）。本机还没同步上的字——排队 / 失败后停着的那一稿、
   共用读缓存里标着未同步的那一份——先留进同步与恢复并告诉作者，这一场的状态机停下：不再重发，等着的调用方收到失败。都持久地
   留进去了，本机这一层（会话内存、共用读缓存、未同步标记）一并扔掉；有的只留在本次会话里（本机存储满了）就留着，刷新之后
   followCatalog 再留一次。过去这段字只在一个谁也不读的本机键里，刷新之后也找不回来（复核六 W1-R6B-2）。
   路上 / 核对中 / 冲突中 / 水合中 / 采纳在路上的先不动：那边有了结果再说（onFailed、下一次目录装载会再看）。
   乐观新建的场（临时 sid）：也许新建没成，也许建好了、这里却认不出它现在是目录里的哪一场（建场的回包丢了）——提示照实说是新建时
   写下的字，不说这一场「不在目录里了」（复核七 W1-R7B-1）。
   这一页最后知道的服务端正文（水合过才有）留在会话内存里：这一场若再回到目录（从回收站还原），打开时编辑器先显示它、不是一片空白——
   作者在水合回来之前接着写的字写在它上面，服务端没动过就照常存上，不被当成「在别处被修改过」（复核七 W1-R7B-4）。→ 处理了返回 true */
function settleOrphan(m) {
  if (m.inFlight || m.checking || m.conflict || m.hydrating || m.adoptions.length) return false;
  const marked = pendingRead(m) != null;
  if (!m.dirty && !m.queued && !marked) return false;
  const tmp = TMP_SID.test(m.sid);
  const { created, durable } = keepRefused(m, [m.queued && m.queued.html, marked ? readSlot(m) : null], tmp ? KEEP_REASONS.tmpGone : KEEP_REASONS.gone);
  const lastServer = m.draftId ? toDocHTML(m.serverContent || "") : null;
  m.queued = null;
  m.stalled = false;
  m.dirty = false;
  m.lastSaveError = null;
  m.lockPending = null;
  m.unsure = null;
  // 这一场若再回到目录（从回收站还原）：重新水合，读服务端眼下的那一版
  m.hydrated = false;
  m.draftId = null;
  m.sceneId = null;
  rejectWaiters(m, goneError());
  if (durable) {
    dropSceneKeys(m);
    m.shown = undefined;
    m.slotOwn = undefined;
    m.slotOwnBase = null;
    m.slotOnlyCopy = false;
    if (lastServer != null) rememberInSession(m, lastServer);
  }
  notifyState(m);
  if (created) {
    const notice = durable ? (tmp ? NOTICE.tmpGone : NOTICE.gone) : (tmp ? NOTICE.tmpGoneVolatile : NOTICE.goneVolatile);
    recoveryNotice(m, notice, durable ? "warn" : "danger");
  }
  return true;
}

/* 目录装载成功之后（wr-doc-store.jsx 登记）：换了名字的场（乐观新建的场建好了、目录重拉之后是稳定的 scene_id），状态机和本机
   这一层跟过去（renameMeta）；不在目录里了的，本机还没同步上的字留进同步与恢复（settleOrphan）。本机存储里标着未同步、这一页
   却没有状态机的那几场（上次会话 / 另一个标签页留下的）同样处理（sweepOrphanMarkers）。复核六 W1-R6B-1 · W1-R6B-2 */
function followCatalog(workId) {
  if (retired || !workId || workId !== activeWorkId()) return;
  let ready = false;
  try { ready = !!WsCatalog.ready(); } catch (e) { ready = false; }
  if (!ready) return;
  Object.values(docMeta).forEach((m) => {
    if (m.workId !== workId) return;
    const current = currentSidOf(m);
    if (current === m.sid) {
      if (sceneGone(m)) settleOrphan(m);
      return;
    }
    const other = docMeta[metaKeyOf(workId, current)];
    if (!other) {
      renameMeta(m, current);
      return;
    }
    // 新名字下已另有一台状态机（按说到不了这里：metaFor 总是先搬）：这一台没同步上的字留进同步与恢复，之后谁也不再找它
    if (settleOrphan(m) || (!m.dirty && !m.inFlight && !m.checking && !m.conflict && !m.hydrating && !m.adoptions.length)) {
      delete docMeta[metaKeyOf(workId, m.sid)];
    }
  });
  sweepOrphanMarkers(workId);
}

/* 乐观新建的场用的临时 sid（ws-catalog.jsx catStamp） */
const TMP_SID = /^tmp_/;
/* 认不出是哪一场的乐观新建、标记又是这么久之内写的：也许另一个标签页正在建它、等它的服务端，先不动（复核七 W1-R7B-2） */
const ORPHAN_GRACE_MS = 10 * 60 * 1000;

/* 本机存储里标着未同步、这一页却没有状态机的那几场：目录里有它的，打开时照常水合（settleHydrate）；换了名字的搬到新名字下——
   同一次会话里目录记得别名；刷新过了（或是另一个标签页新建的）凭标记里记下的后端 scene_id 找到它（复核七 W1-R7A-3：过去这时
   说这一场「不在目录里了」，那一段字留进同步与恢复时也不带 scene_id，「恢复」恢复不了）。新名字下另有本机稿、另有状态机的，那一段字
   留进同步与恢复（挂在新名字下），照实说是另一个标签页（或上次打开时）留下的。认不出是哪一场的：乐观新建的临时 sid、标记又是刚写的
   ——另一个标签页也许正在建它、等它的服务端，先不动（过去这一页把它说成「不在目录里了」，还删掉了那一页的未同步标记，复核七
   W1-R7B-2）；别的留进同步与恢复并告诉作者：正式编号的场不在目录里了（写作台再也打不开它），临时 sid 的照实说是新建时写下的字
   （复核七 W1-R7B-1） */
function sweepOrphanMarkers(workId) {
  let notice = null;
  pendingSids(workId).forEach((sid) => {
    if (docMeta[metaKeyOf(workId, sid)]) return; // 这一台还在：followCatalog 处理过，或还有结果没回来
    const scene = { workId, sid };
    const aliased = catalogSid(workId, sid);
    if (aliased === sid && !sceneGone(scene)) return;
    const at = pendingBase(scene) || {};
    const current = aliased !== sid ? aliased : sidOfSceneId(workId, at.sceneId);
    if (current && !docMeta[metaKeyOf(workId, current)]) {
      const { moved } = renameSceneKeys(workId, sid, current);
      if (moved) {
        recoveryRename(workId, sid, current);
        return;
      }
    }
    const tmp = TMP_SID.test(sid);
    if (!current && tmp && at.at != null && Date.now() - at.at < ORPHAN_GRACE_MS) return;
    const slot = readSlot(scene);
    if (slot == null || !hasAuthorText(slot)) {
      dropSceneKeys(scene);
      return;
    }
    const kind = current ? "renamed" : (tmp ? "tmpGone" : "gone");
    const target = { workId, sid: current || sid, sceneId: at.sceneId || null };
    const { entry, created } = keepText(target, slot, KEEP_REASONS[kind], `场景 ${target.sid} · 未同步本地稿`, "unsynced");
    if (!entry) return;
    if (entry.durable !== false) dropSceneKeys(scene);
    if (created && (!notice || entry.durable === false)) notice = { m: target, kind, durable: entry.durable !== false };
  });
  if (!notice) return;
  const tone = notice.durable ? "warn" : "danger";
  if (notice.kind === "renamed") recoveryNotice(notice.m, notice.durable ? NOTICE.otherTab : NOTICE.otherTabVolatile, tone);
  else if (notice.kind === "tmpGone") recoveryNotice(notice.m, notice.durable ? NOTICE.tmpGone : NOTICE.tmpGoneVolatile, tone);
  else recoveryNotice(notice.m, notice.durable ? NOTICE.gone : NOTICE.goneVolatile, tone);
}

/* ---- 回包丢了的那一稿 ---- */

/* 这一次也许其实存上了：断网 / 超时 / 5xx（服务端可能办完了、回包没回来）。4xx 是服务端明确拒绝的，没存上 */
function mayHaveLanded(e) {
  const status = Number(e && e.status);
  return !status || status >= 500 || !!(e && e.retryable);
}

/* 同一个修订号上最多记多少稿没等到回包的：第一稿一直留着（连接断掉的那一刻多半就是它存上了、回包没回来），
   其余的记最近的——断网写上几分钟，每一次自动保存都失败，过去只记最后四稿，真存上的那一稿被挤掉，
   连上之后自己那一稿被当成别处的修改（复核三 W1-R3A-2） */
const UNSURE_KEEP = 64;

/* 发出去却没等到回包（断网 / 5xx）：它也许已经存上了。同一个修订号上记几稿，修订号换了就重记 */
function rememberUnsure(m, flight) {
  if (!m.unsure || m.unsure.base !== flight.base) m.unsure = { base: flight.base, htmls: [] };
  const htmls = m.unsure.htmls;
  if (htmls.includes(flight.html)) return;
  m.unsure.htmls = htmls.length < UNSURE_KEEP
    ? [...htmls, flight.html]
    : [htmls[0], ...htmls.slice(2 - UNSURE_KEEP), flight.html];
}

/* 撞上 409 的这一次和没等到回包的那几稿用的是同一个修订号：可能撞上的是自己（服务端回的当前修订号若在，得正好往前一步） */
function mayBeOwnSave(m, flight, e) {
  const unsure = m.unsure && m.unsure.base === flight.base;
  const adoption = m.adoptionUnsure && m.adoptionUnsure.base === flight.base; // 结果不明的采纳也是这一页自己的（复核五 W1-R5B-4）
  if (!unsure && !adoption) return false;
  const current = e && e.details && e.details.current_revision_no;
  return !Number.isInteger(current) || current === flight.base + 1;
}

/* 409 之后读回来的服务端版本至少得是哪一版：409 说了服务端眼下是哪一版就是它——库从备份恢复、历史往回走了时它比撞上的
   那一次还旧，也照它（过去取两者大的，回退之后每一次读到的都「太旧」，这一场从此存不上，复核三 W1-R3B-1）；
   没说时是撞上的那一次的修订号 + 1 */
function conflictFloor(flight, e) {
  const current = e && e.details && e.details.current_revision_no;
  if (Number.isInteger(current)) return current;
  return Number.isInteger(flight.base) ? flight.base + 1 : 0;
}

/* 读回来的服务端版本一轮一轮都比门槛旧、又都是同一版（readServerDraft 每一轮连读三次、每次新发 ensure、各带各的幂等键）：
   不是重放的旧快照，是服务端的历史往回走了——几轮之后信它。holder：这一次冲突 / 核对。→ 信了返回读到的草稿 */
const STALE_ROUNDS_TRUSTED = 3;
function trustedStaleRead(holder, e) {
  if (!e || e.code !== "AUTHOR_DRAFT_STALE_READ" || !e.observed) {
    holder.staleRounds = 0;
    holder.staleKey = null;
    return null;
  }
  const key = `${e.observed.draft_id}#${e.observed.revision_no}`;
  holder.staleRounds = holder.staleKey === key ? (holder.staleRounds || 0) + 1 : 1;
  holder.staleKey = key;
  return holder.staleRounds >= STALE_ROUNDS_TRUSTED ? e.observedData : null;
}

const RETRY_BASE_MS = 2000;
const RETRY_MAX_MS = 60000;
let retired = false; // 模块被新实例取代（开发时热更新 / 单测 resetModules）后，旧实例的计时器不再动作

function retryDelay(attempts) {
  return Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** Math.max(0, attempts - 1));
}

/* 核对（不发任何保存）：服务端眼下若正好是没等到回包的那几稿之一、修订号只往前走了一步，就是自己存上的——
   接上那个修订号，把最新的一稿（排队的，或撞上 409 的这一稿）接着发，不开冲突、不提示。
   否则进入冲突（服务端版本已经读到，就地换上）。读不到服务端（断网、回来的比 409 说的还旧）不算冲突：保持核对，
   等着的调用方收到这次失败，状态是保存失败；下一次保存 / flush / 窗口重新聚焦或联网 / 退避计时到了再读（复核二 W1-R2A-1）。 */
function startOwnCheck(m, flight, error) {
  const check = {
    error,
    html: flight.html,
    base: flight.base,
    candidates: m.unsure && m.unsure.base === flight.base ? m.unsure.htmls.slice() : [],
    // 结果不明的那一次采纳（adoptionLanded 没读到服务端）：服务端眼下正是它，就是作者自己的采纳落了地
    adoption: m.adoptionUnsure && m.adoptionUnsure.base === flight.base ? m.adoptionUnsure : null,
    floor: conflictFloor(flight, error),
    minSeq: ensureSeq + 1,
    attempts: 0,
    timer: null,
    loading: null,
    failed: false,
  };
  m.checking = check;
  if (!m.queued) m.queued = { html: flight.html, version: flight.version };
  m.stalled = false;
  void checkRead(m, check);
  notifyState(m);
}

/* 换稿之前先弄清没等到回包的那几稿（unsure，写在眼下这个修订号上）是不是其实存上了（章在别处批准锁定时，见 releaseLock）：
   读一次服务端——眼下正是其中一稿、修订号正好往前一步，就是这一页自己存上的，接上那个修订号；
   是别的一版（没存上，或别处又存过）：这一页没有没存上的字时按它，有就不接它的修订号（见 finishVerify，复核 I3-2）；
   还停在那个修订号上就还没存上（unsure 留着：它也许还在路上）。读的期间这一场不发任何
   保存（m.checking，同核对）。读完（read=true）或读不到（read=false：断网、服务端出错；不重试，照这一页知道的换）调 then(read)
   （复核七 W1-R7A-2） */
function verifyUnsure(m, then) {
  const check = {
    verify: then,
    error: null,
    html: null,
    base: m.revision,
    candidates: m.unsure.htmls.slice(),
    adoption: null,
    floor: m.revision,
    minSeq: ensureSeq + 1,
    attempts: 0,
    timer: null,
    loading: null,
    failed: false,
  };
  m.checking = check;
  void checkRead(m, check);
  notifyState(m);
}

function finishVerify(m, check, data) {
  m.checking = null;
  clearTimeout(check.timer);
  const draft = data.draft;
  const sameDraft = !m.draftId || draft.draft_id === m.draftId;
  const own = sameDraft && draft.revision_no === check.base + 1
    && check.candidates.some((html) => sameManuscriptText(html, toDocHTML(draft.content || "")));
  // 服务端已经不在那个修订号上（别处存过 / 换了一份草稿），存下的又不是这一页自己那几稿
  const movedElsewhere = !own && (!sameDraft || draft.revision_no !== check.base);
  // 读到的是别处存上的一版、这一页还有没存上的字（停着 / 排队的一稿）：不吸收——修订号留在作者见过的那一版上。之后不管是换稿
  // （还锁着：留进同步与恢复，refuseLocal 的后台复核再换上别处那一版）还是接着发（这期间本章在别处重新打开了），发出去的都带着
  // 旧修订号，服务端 409，走冲突：本机稿进同步与恢复、编辑器换成服务端版本、照实提示。与 applyRefresh 同一条规矩（这一页有本机
  // 改动就不吸收，见 isClean）。复核 I3-2：过去这里接上了那一版，本章随后重新打开时排队的本机稿带着它发出去，把作者从没见过的
  // 另一台设备的正文静默盖掉——编辑器没显示过它，同步与恢复里没有，也没有提示
  if (!(movedElsewhere && (m.dirty || m.queued))) absorbServerState(m, data, { own });
  // 存上了，或服务端已经不在那个修订号上（别处存过 / 换了一份草稿：那几稿再也落不到那一版上了）：不必再记着它们
  if (own || movedElsewhere) m.unsure = null;
  check.verify(true);
}

/* 核对读一次服务端（同一次核对只有一次在路上） */
function checkRead(m, check) {
  if (check.loading) return check.loading;
  clearTimeout(check.timer);
  check.timer = null;
  check.failed = false;
  const run = readServerDraft(m, check.minSeq, check.floor).then(
    (data) => { if (m.checking === check) finishOwnCheck(m, check, data); },
    (e) => { if (m.checking === check) ownCheckFailed(m, check, e); },
  ).finally(() => { if (check.loading === run) check.loading = null; });
  check.loading = run;
  return run;
}

function finishOwnCheck(m, check, data) {
  if (check.verify) {
    finishVerify(m, check, data);
    return;
  }
  m.checking = null;
  clearTimeout(check.timer);
  const draft = data.draft;
  const sameDraft = !m.draftId || draft.draft_id === m.draftId;
  const next = sameDraft && draft.revision_no === check.base + 1;
  if (check.adoption) {
    if (m.adoptionUnsure === check.adoption) m.adoptionUnsure = null;
    if (next && promotedAtRevision(draft) && sameManuscriptText(check.adoption.html, toDocHTML(draft.content || ""))) {
      // 撞上 409 的是作者自己在起草台的采纳（它的回包、之后那一次核对都没收到）：照采纳成了收尾——本机没存上的留进
      // 同步与恢复、编辑器换成采纳的稿，提示照实说是采纳，不说「在别处被修改」（复核五 W1-R5B-4）
      acceptCanonicalMeta(m, check.adoption.html, landedAdoption(draft, data));
      return;
    }
  }
  if (next && check.candidates.some((html) => sameManuscriptText(html, toDocHTML(draft.content || "")))) {
    m.unsure = null;
    m.lastSaveError = null;
    absorbServerState(m, data, { own: true });
    notifyState(m);
    pump(m);
    return;
  }
  absorbServerState(m, data);
  openConflict(m, check.error, [check.html, m.queued && m.queued.html], { serverKnown: true });
}

function ownCheckFailed(m, check, e) {
  if (check.verify) { // 换稿之前的那一次读取没读到：不等、不重试，照这一页知道的换（提示照实说还没能确认）
    m.checking = null;
    clearTimeout(check.timer);
    check.verify(false);
    return;
  }
  const trusted = trustedStaleRead(check, e);
  if (trusted) { // 服务端几轮都说自己是同一个更旧的版本（历史往回走了）：信它，按它核对
    finishOwnCheck(m, check, trusted);
    return;
  }
  check.attempts += 1;
  check.failed = true;
  m.lastSaveError = e;
  console.warn("[WrDocs] 409 之后核对服务端版本失败，稍后再读:", e);
  rejectWaiters(m, e);
  notifyState(m);
  clearTimeout(check.timer);
  check.timer = setTimeout(() => {
    check.timer = null;
    if (!retired && m.checking === check) void checkRead(m, check);
  }, retryDelay(check.attempts));
}

/* ---- 冲突 ---- */

/* 409（或水合发现作者在旧缓存上写了字）：本机没上服务端的正文先进同步与恢复，扔掉排队的，标上冲突，再读服务端版本。
   minRevision：读回来的服务端版本至少得是这个修订号（更旧的是冲突之前的快照）。
   kind："conflict"（在别处被修改过）| "pending"（作者是在上次会话没同步上的本机稿上写的，提示照实说是它） */
function openConflict(m, error, texts, { serverKnown = false, minRevision = 0, kind = "conflict" } = {}) {
  const newest = m.queued ? m.queued.html : texts.find((html) => html != null);
  const reason = kind === "pending" ? "上次会话（或另一个标签页）没同步上的本机稿，和服务端版本对不上" : "服务端在别处更新（409 冲突）";
  const kept = new Set();
  const created = []; // 这一次新放进同步与恢复的记录：读到的服务端版本和它们是同一段字时收回（见 resolveConflict）
  let durable = true;
  let keptAny = false;
  texts.forEach((html) => {
    if (html == null) return;
    const text = docText(html);
    if (!text || kept.has(text)) return;
    const result = keepText(m, html, reason);
    if (!result.entry) return;
    kept.add(text);
    if (result.created) created.push(result.entry.id);
    keptAny = true;
    if (result.entry.durable === false) durable = false;
  });
  m.queued = null;
  m.stalled = false;
  if (m.checking) clearTimeout(m.checking.timer);
  m.checking = null;
  const episode = {
    error, hold: holdError(), kept, own: new Set(kept), created, keptAny, durable,
    attempts: 0, timer: null, loading: null, failNotified: false, resolving: false, minRevision, minSeq: ensureSeq + 1, kind,
  };
  m.conflict = episode;
  m.lastSaveError = episode.hold;
  m.dirty = true;
  // 本机稿都持久地进了同步与恢复：刷新后不必再备份一次（共用读缓存里若是另一个标签页随后写的字，标记留着）；
  // 只进了会话内存：共用读缓存里留着最新的本机稿、标着未同步，刷新后按跨会话的路径再留一次
  if (durable) clearPendingIfMine(m); else markUnsynced(m, newest);
  rejectWaiters(m, error);
  notifyState(m);
  if (serverKnown) resolveConflict(m, episode);
  else void conflictLoad(m);
}

/* 读服务端版本（同一次冲突只有一次在路上）：冲突之后新发的 ensure，回包比撞上的那一次还旧就当没读到、马上再读 */
function conflictLoad(m) {
  const episode = m.conflict;
  if (!episode) return Promise.resolve();
  if (episode.loading) return episode.loading;
  clearTimeout(episode.timer);
  episode.timer = null;
  const run = readServerDraft(m, episode.minSeq, episode.minRevision).then((data) => {
    if (m.conflict !== episode) return;
    absorbServerState(m, data);
    resolveConflict(m, episode);
  }).catch((e) => {
    if (m.conflict === episode) conflictLoadFailed(m, episode, e);
  }).finally(() => {
    if (episode.loading === run) episode.loading = null;
  });
  episode.loading = run;
  return run;
}

/* 这一次冲突 / 拒绝里再留一段本机正文（去重见 keepText）：记进这一次（同一条提示），新放进去的记下 id */
function keepInEpisode(m, episode, html, reason) {
  const { entry, created } = keepText(m, html, reason);
  if (!entry) return null;
  const text = docText(html);
  episode.kept.add(text);
  if (episode.own) episode.own.add(text);
  if (created && episode.created) episode.created.push(entry.id);
  episode.keptAny = true;
  if (entry.durable === false) episode.durable = false;
  return entry;
}

function resolveConflict(m, episode) {
  const serverHTML = toDocHTML(m.serverContent || "");
  // 冲突之后本机又存过（读到服务端版本之前作者接着写、离场冲刷）：读缓存里那一份换掉之前先留进同步与恢复
  const cached = readCache(m.workId, m.sid);
  const cachedText = docText(cached);
  if (cachedText && !episode.kept.has(cachedText) && !sameManuscriptText(cached, serverHTML)) {
    keepInEpisode(m, episode, cached, "服务端在别处更新（409 冲突）之后本机又写的正文");
  }
  clearTimeout(episode.timer);
  m.conflict = null;
  // 章锁定那一刻留下的几句（lockPending）：编辑器这就换成服务端版本，它们已在同步与恢复里
  if (m.lockPending) m.lockPending.kept.forEach((text) => episode.kept.add(text));
  m.lockPending = null;
  m.lastEpisode = episode;
  m.dirty = false;
  m.lastSaveError = null;
  m.unsure = null;
  m.hydrated = true;
  m.preHydrateBase = undefined;
  m.pendingAtLoad = false;
  m.pendingBaseAtLoad = null;
  showServerVersion(m, serverHTML, episode.durable);
  setShown(m, serverHTML);
  m.lastLoadReason = "conflict";
  notifyState(m);
  // 写作台在这里把编辑器换成服务端版本；编辑器里还没交出来的字经 keepLocalCopy 并进这一次（同一条提示）
  episode.resolving = true;
  notifyDoc("conflict-resolved", { sid: m.sid, workId: m.workId, html: serverHTML, reason: episode.kind === "pending" ? "pending" : "conflict" });
  episode.resolving = false;
  // 本机留下的每一稿都和读到的服务端版本是同一段字（两个标签页都把同一份工作稿传上去，先到的那一页存上了；复核五 W1-R5B-6）：
  // 不是冲突——没有哪一段字只在本机，这一次新放进同步与恢复的收回，不提示「在别处被修改过」
  const serverText = docText(serverHTML);
  if (episode.own.size && [...episode.own].every((text) => text === serverText)) {
    episode.created.forEach((id) => recoveryRemove(id));
    return;
  }
  if (episode.kind === "pending") {
    recoveryNotice(m, episode.durable ? NOTICE.pendingConflict : NOTICE.pendingConflictVolatile, episode.durable ? "warn" : "danger");
  } else if (!episode.durable) recoveryNotice(m, NOTICE.conflictVolatile, "danger");
  else recoveryNotice(m, episode.keptAny ? NOTICE.conflict : NOTICE.conflictNothingKept);
}

function conflictLoadFailed(m, episode, e) {
  const trusted = trustedStaleRead(episode, e);
  if (trusted) { // 服务端几轮都说自己是同一个更旧的版本（历史往回走了）：信它，换上它
    absorbServerState(m, trusted);
    resolveConflict(m, episode);
    return;
  }
  episode.attempts += 1;
  m.lastSaveError = episode.hold;
  notifyState(m);
  if (!episode.failNotified) {
    episode.failNotified = true;
    recoveryNotice(m, episode.durable ? NOTICE.conflictLoadFailed : NOTICE.conflictLoadFailedVolatile, episode.durable ? "warn" : "danger");
  }
  console.warn("[WrDocs] 冲突之后读取服务端版本失败，稍后重试:", e);
  clearTimeout(episode.timer);
  episode.timer = setTimeout(() => {
    episode.timer = null;
    if (!retired && m.conflict === episode) void conflictLoad(m);
  }, retryDelay(episode.attempts));
}

/* 窗口重新聚焦 / 重新联网：冲突中还没读到服务端版本的马上再读；核对读不到服务端的马上再核对；章锁定那一刻还没水合、
   水合又没成的（lockPending）再读一次；保存失败后停着的最新一稿再发一次（停着的就是最新的一稿，不会把较旧的补发上去）；
   章在这期间批准锁定了的，停着的那一稿不再发（settleLockedStall）。本机存储里没人管的未同步标记再看一次（sweepOrphanMarkers：
   刚写的、认不出是哪一场的乐观新建那时先放过了，复核七 W1-R7B-2） */
function onWake() {
  if (retired) return;
  try {
    const workId = activeWorkId();
    if (workId && WsCatalog.ready()) sweepOrphanMarkers(workId);
  } catch (e) { /* 目录下次装载时再看 */ }
  Object.values(docMeta).forEach((m) => {
    if (m.conflict) {
      if (!m.conflict.loading) void conflictLoad(m);
      return;
    }
    if (m.checking) {
      if (!m.checking.loading) void checkRead(m, m.checking);
      return;
    }
    if (m.lockPending && !m.hydrated) {
      if (!m.inFlight && !m.hydrating) {
        m.stalled = false;
        pump(m);
      }
      return;
    }
    if (m.stalled && m.queued && !m.inFlight) {
      if (settleLockedStall(m)) return;
      m.stalled = false;
      notifyState(m);
      pump(m);
    }
  });
}
retireModuleListeners("wr-doc-sync");
try {
  window.addEventListener("focus", onWake);
  window.addEventListener("online", onWake);
} catch (e) { /* 没有 window 的环境 */ }
adoptModuleListeners("wr-doc-sync", () => {
  retired = true;
  try {
    window.removeEventListener("focus", onWake);
    window.removeEventListener("online", onWake);
  } catch (e) {}
  Object.values(docMeta).forEach((m) => {
    if (m.conflict) clearTimeout(m.conflict.timer);
    if (m.checking) clearTimeout(m.checking.timer);
  });
});

/* ---- flush：等这一场本机的字有结果 ---- */

/* → "saved" | "conflict" | "adopted" | "refused" | "failed"（从不抛；refused：服务端明确拒绝、换回了服务端版本，作者已被告知；
   adopted：这一稿等着的时候，作者自己在起草台的采纳落了地、编辑器换成了采纳的稿——不是别处的修改，复核七 W1-R7B-5）。
   失败后停着的最新一稿，显式 flush 再发一次；核对读不到服务端的，再读一次；
   retry：路上那一次在等的时候失败了，再发一次最新的一稿（离场冲刷用：只一次）。冲突中顺手再读服务端版本。 */
async function flushMeta(m, { retry = false } = {}) {
  if (m.conflict) {
    void conflictLoad(m);
    return "conflict";
  }
  if (!m.dirty) return "saved";
  if (m.checking) void checkRead(m, m.checking);
  // 采纳在路上时按住的那一次（adoptHeld）还没有结果：等采纳收尾，不当成「存上了」（复核四 W1-R4A-1）
  if (!m.inFlight && !m.queued && !m.adoptHeld.length) return m.lastSaveError ? "failed" : "saved";
  // 停着的那一稿再发之前先看章锁了没有：锁了就留进同步与恢复、换回已存上的正文，不再发（复核四 W1-R4A-5）；
  // 同一个修订号上还有一稿没等到回包时先读一次服务端再换稿（复核七 W1-R7A-2）：等它有结果
  if (settleLockedStall(m)) return m.checking ? outcomeOf(m, m.saveVersion) : "refused";
  if (m.stalled && !m.inFlight) { m.stalled = false; pump(m); }
  let outcome = await outcomeOf(m, m.saveVersion);
  if (outcome === "failed" && retry && !m.conflict && m.dirty) {
    if (m.checking) void checkRead(m, m.checking);
    if (settleLockedStall(m)) return m.checking ? outcomeOf(m, m.saveVersion) : "refused";
    if (m.stalled && !m.inFlight) { m.stalled = false; pump(m); }
    if (m.inFlight || (m.queued && !m.stalled)) outcome = await outcomeOf(m, m.saveVersion);
  }
  return outcome;
}

/* 提升前等路上 / 排队 / 核对中的那一稿有结果；失败后停着的不再发（提升只提升已经存上的） */
async function settledMeta(m) {
  if (m.conflict || !m.dirty || m.stalled || (!m.inFlight && !m.queued)) return;
  if (m.checking) void checkRead(m, m.checking);
  await outcomeOf(m, m.saveVersion);
}

/* 提升 / 采纳被服务端按修订号拒绝（409 AUTHOR_DRAFT_CONFLICT）之后：这期间服务端往前走的几版是不是这一页自己存上的。
   只看修订号链：服务端那时的修订号（409 说的；没说时是这一页眼下的）落在这一页自己存上的那几版里（带去的修订号之后、到眼下，
   中间没吸收过别处的版本）就是自己的——之后又有一稿没存上（失败、停着）不相干（复核六 W1-R6A-2 · W1-R6A-3：过去它让这里答
   「在别处」）。先等路上 / 排队 / 核对中的那一稿有结果；自己那一次的回包丢了（unsure，服务端正好比那一次的底往前一步）时先把
   停着的最新一稿再发一次（客户端留着那一次的幂等键，服务端照当时的结果重放；不是这样也由核对认出自己那一稿）再分（复核四 W1-R4B-7）。
   本机眼下已经没有要发的（章锁定 / 服务端拒绝之后换了稿，unsure 留着）也一样：服务端正好比那一次的底往前一步，多半就是它（复核七 W1-R7A-2）。
   → "own"：全是自己存上的；"unsure"：多半是自己那一次、还没能确认；"other"：别处动过（或这一页正在冲突中） */
async function refusalCause(m, base, e) {
  const current = e && e.details && e.details.current_revision_no;
  const lostOwn = () => !!(m.unsure && m.unsure.base === m.revision && m.revision >= base && m.ownChainFrom <= base
    && (!Number.isInteger(current) || current === m.unsure.base + 1));
  if (lostOwn()) await flushMeta(m);
  else await settledMeta(m);
  if (m.conflict) return "other";
  const moved = Number.isInteger(current) ? current : m.revision;
  if (m.ownChainFrom <= base && moved > base && moved <= m.revision) return "own";
  return lostOwn() ? "unsure" : "other";
}

/* 提升被服务端按修订号拒绝：草稿往前走的全是作者在提升途中自己存上的——最新的一稿也存上了说 AUTHOR_DRAFT_MOVED_BY_SELF（又改了几句、
   已经存上，复核三 W1-R3B-8），还有没存上的（之后那一次失败了、回包丢了还没确认）说 AUTHOR_DRAFT_UNSAVED；本机已经没有要发的、只是
   最后那一次的回包丢了（章锁定 / 拒绝之后换回了上次确认存上的一版）说 AUTHOR_DRAFT_UNCONFIRMED，顺手再读一次服务端（是它的话编辑器
   换成它，applyRefresh；复核七 W1-R7A-2）；都不说「在别处更新」。
   提升途中作者自己在起草台的采纳（它在一个事务里存下并提升了采纳的那一稿）还在路上：先等它有结果；提升途中有采纳落了地——换稿的是那次
   采纳，说 AUTHOR_DRAFT_ADOPTED（复核七 W1-R7B-5）。adoptedBefore：提升发出那一刻这一场落了地的采纳数。
   真在别处更新了：原样交回 */
async function promoteRefusal(m, base, e, adoptedBefore = m.adoptedSeq) {
  if (!e || e.code !== "AUTHOR_DRAFT_CONFLICT") return e;
  if (m.adoptions.length) await adoptionsSettled(m);
  if (m.adoptedSeq !== adoptedBefore) return adoptedError(e);
  const cause = await refusalCause(m, base, e);
  if (cause === "own") return m.dirty ? unsavedError(e) : movedBySelfError(e);
  if (cause === "unsure") {
    if (m.dirty) return unsavedError(e);
    revalidate(m);
    return unconfirmedError(e);
  }
  return e;
}

const KEEP_REASONS = {
  conflict: "服务端在别处更新（409 冲突）时编辑器里还有没保存的改动",
  server: "这一场在别处有更新，编辑器换成服务端版本时还有没保存的改动",
  restore: "编辑器换成恢复的正文时还有没保存的改动",
  adopt: "编辑器换成采纳归档的正文时还有没保存的改动",
  refused: "服务端没有接受这一场的保存，编辑器换回服务端版本时还有没存上的正文",
  locked: "这一章已批准锁定，改动没有保存",
  pending: "上次会话没同步上的本机稿换成服务端版本时，编辑器里还有没保存的改动",
  gone: "这一场已经不在目录里了（新建没能存到服务端，或在别处移到了回收站），本机还有没同步上的正文",
  tmpGone: "新建这一场时写下、那一刻还没存到服务端的正文（这里认不出它现在是目录里的哪一场）",
  renamed: "新建的场换成正式编号时，新编号下已另有一份本机稿；这是新建时写下、还没同步上的正文",
  own: "编辑器换成这一页先前存上的那一稿（它的保存回包当时没回来）时还有没保存的改动",
  restoreUnsynced: "编辑器换成恢复的正文时，这一场先前交出去的这一稿还没同步上服务端",
};

function scopeMeta(sid, options) {
  return options && options.workId ? metaFor(options.workId, sid) : meta(sid);
}

/* 服务端这份草稿提升到的就是它眼下这一版（采纳在一个事务里存下并提升；没带提升信息的当作是） */
function promotedAtRevision(draft) {
  const promoted = Object.prototype.hasOwnProperty.call(draft, "last_promoted_revision_no")
    ? draft.last_promoted_revision_no
    : draft.revision_no;
  return promoted === draft.revision_no;
}

/* 读到的服务端草稿就是落了地的采纳：拼成和 adopt-current 回包一样形状的结果（recovered） */
function landedAdoption(draft, data) {
  const finalId = draft.last_promoted_final_scene_row_id || finalIdFromRef(data && data.runtime_final_ref);
  return {
    recovered: true,
    scene_status: "archived",
    final_scene_row_id: finalId || null,
    author_draft: { ...draft, last_promoted_revision_no: draft.revision_no, canonical_dirty: false },
  };
}

/* 采纳归档落了地（adopt-current 的回包 / 回包丢了之后读到的服务端版本 / 结果不明之后核对认出的那一版）：见 WrDocs.acceptCanonical */
function acceptCanonicalMeta(m, html, data) {
  const normalized = sanitizeManuscriptHTML(html || "");
  const serverDraft = data && data.author_draft;
  if (!serverDraft || !serverDraft.draft_id || !Number.isInteger(serverDraft.revision_no)) {
    throw Object.assign(new Error("归档响应缺少作者稿修订信息"), { code: "AUTHOR_DRAFT_ADOPTION_RESPONSE_INVALID" });
  }
  if (m.draftId && m.draftId !== serverDraft.draft_id) {
    throw Object.assign(new Error("归档响应属于另一份作者稿"), { code: "AUTHOR_DRAFT_ADOPTION_MISMATCH" });
  }
  const finalRowId = data.final_scene_row_id || null;
  m.adoptionUnsure = null; // 这一场的采纳有了结果
  const reached = m.hydrated && !m.conflict && !m.checking && !m.inFlight && !m.adoptHeld.length
    && m.draftId === serverDraft.draft_id
    && (m.revision > serverDraft.revision_no
      || (m.revision === serverDraft.revision_no && sameManuscriptText(toDocHTML(m.serverContent || ""), normalized)));
  m.adoptions.length = 0;
  m.adoptedSeq += 1;
  wakeAdoptionIdle(m);
  if (reached) {
    m.currentFinalSceneRowId = finalRowId;
    m.lastPromotedRevisionNo = serverDraft.revision_no;
    m.lastPromotedFinalSceneRowId = finalRowId;
    m.canonicalDirty = m.dirty || m.revision !== serverDraft.revision_no;
    notifyState(m);
    pump(m); // 采纳期间排着的那一稿（写在采纳的这一版上）接着发；章锁定了的换回已存上的正文
    return snapshotOf(m);
  }
  let episode = null;
  if (m.dirty) {
    // 本机缓存里的就是交给 WrDocs 的最新一稿（路上 / 排队 / 停着 / 冲突中写的）
    const local = readCache(m.workId, m.sid);
    if (local != null && hasAuthorText(local) && !sameManuscriptText(local, normalized)) {
      const { entry, created } = keepText(m, local, "采纳 AI 稿归档时本机还有没存上的正文");
      if (entry) episode = { kept: new Set([docText(local)]), keptAny: created, durable: entry.durable !== false, resolving: false };
    }
  }
  if (m.inFlight && m.inFlight.html != null) m.inFlight.superseded = true;
  m.adoptHeld.length = 0;
  m.queued = null;
  m.stalled = false;
  if (m.conflict) clearTimeout(m.conflict.timer);
  m.conflict = null;
  if (m.checking) clearTimeout(m.checking.timer);
  m.checking = null;
  m.unsure = null;
  m.lockPending = null;
  rejectWaiters(m, replacedError());
  m.serverContent = normalized;
  m.dirty = false;
  m.lastSaveError = null;
  m.hydrated = true;
  m.preHydrateBase = undefined;
  m.pendingAtLoad = false;
  m.pendingBaseAtLoad = null;
  absorbServerState(m, {
    draft: { ...serverDraft, content: normalized },
    runtime_final_ref: finalRowId ? `final_scene:${finalRowId}` : null,
  });
  // 本机那一稿只留进了本次会话（本机存储满了）：采纳的正文只进会话内存，本机缓存里那一份和未同步标记留到刷新以后
  writeServerVersion(m, normalized, { durable: !episode || episode.durable });
  notifyState(m);
  if (episode) {
    m.lastEpisode = episode;
    episode.resolving = true;
  }
  notifyLoadedMeta(m, "adopt", { force: !!episode && (episode.keptAny || !episode.durable) });
  if (episode) {
    // 写作台换稿时编辑器里还没交出来的字经 keepLocalCopy 并进这一次（同一条提示）
    episode.resolving = false;
    if (!episode.durable) recoveryNotice(m, NOTICE.adoptKeptVolatile, "danger");
    else if (episode.keptAny) recoveryNotice(m, NOTICE.adoptKept);
  }
  return snapshotOf(m);
}

/* 整篇换稿（replace）要换掉的那一稿：这一页交给 WrDocs、还没同步上服务端的字（路上 / 排队 / 失败待重发）又不在同步与恢复里——先留
   一份再换。「恢复」照理先备份了当前正文（一模一样的不会多留一份）；这是 WrDocs 自己的底线：换稿从不把作者没同步上的字悄悄扔掉
   （复核七 W1-R7A-1：过去「恢复」等水合时目录给这一场换了名字，它按旧名字读当前正文、读到的是空，没备份就把作者刚交出去的字换掉了） */
function keepSuperseded(m, html) {
  if (!m.dirty) return;
  const current = readCache(m.workId, m.sid);
  if (!hasAuthorText(current) || sameManuscriptText(current, html)) return;
  const { entry, created } = keepText(m, current, KEEP_REASONS.restoreUnsynced, `场景 ${m.sid} · 未同步本地稿`, "unsynced");
  if (!entry || !created) return;
  if (entry.durable === false) recoveryNotice(m, NOTICE.volatile, "danger");
  else recoveryNotice(m, NOTICE.restoreKept);
}

const WrDocs = {
  /* 解析 sid → 后端 author-draft draft_id（不存在则 ensure 建一份空稿）；
     供"AI 续写"等需要真实 draft_id 发起 LLM 调用的功能复用同一份映射缓存。 */
  async draftId(sid) {
    if (!sid) return null;
    const m = await ensureDraftMeta(meta(sid));
    return m.draftId || null;
  },
  /* 同步读：返回这一页这一场的那一份（这一页还没碰过这一场时读本机存储里共用的那一份；可能为 null = 从未写过），
     调用方（写作台）把它放进编辑器；后台水合（已水合且没有本机改动时后台复核），出错吞掉。
     章已批准锁定、还停着一稿没存上的：先把它留进同步与恢复、换回已存上的正文（settleLockedStall），读到的是终稿正文 */
  load(sid) {
    if (!sid) return null;
    const m = meta(sid);
    settleLockedStall(m);
    const html = readCache(m.workId, m.sid);
    setShown(m, html);
    if (m.checking && !m.checking.loading) void checkRead(m, m.checking);
    if (m.hydrated && !m.conflict) revalidate(m);
    else hydrateMeta(m).catch((e) => { console.warn("[WrDocs] 文档水合失败:", sid, e); });
    return html;
  },
  /* 显式等待服务端草稿水合（与进行中的水合共用一次；读不到服务器时抛错）；同步与恢复「恢复」之前用它和服务端对齐。 */
  async hydrate(sid) {
    if (!sid) return null;
    const m = meta(sid);
    await hydrateMeta(m);
    return readCache(m.workId, m.sid);
  },
  /* 采纳 AI 稿之前（AI 起草台的预检）：把这一场和服务端对齐，采纳请求带的修订号才是服务端眼下的——还没水合的先水合、
     冲突中的先读到服务端版本（读不到服务器时抛错）；保存失败后停着的最新一稿（或读不到服务端、停着的核对）再发 / 再读一次、
     等它有结果：回包丢了的那一稿其实存上了时，核对接上那个修订号（过去这时采纳一次次被拒「AUTHOR_DRAFT_CONFLICT」，
     直到窗口重新聚焦才好，复核三 W1-R3B-4）。已水合、没有本机改动的一场也再读一次服务端（别处存过的一版此刻才读到：预览、
     覆盖前的备份和采纳带的修订号都是服务端眼下的那一版；过去这时采纳同样一次次被拒，直到在写作台里重新打开这一场，
     复核四 W1-R4B-4）。路上那一次不等：采纳把它作废（beginAdoption）。→ 读缓存里这一场的正文 */
  async prepareAdoption(sid) {
    if (!sid) return null;
    const m = meta(sid);
    const settled = m.hydrated && !m.conflict; // 这一次的水合 / 冲突读取不会再去读服务端
    await hydrateMeta(m);
    if (m.dirty && !m.inFlight && (m.stalled || checkWaiting(m))) {
      await flushMeta(m);
      if (m.conflict) await hydrateMeta(m);
    }
    if (settled && isClean(m)) applyRefresh(m, await requestDraft(m, ensureSeq + 1));
    return readCache(m.workId, m.sid);
  },
  /* 采纳请求（adopt-current）发出之前调用，拿到的记号带着这一场（作者在采纳途中换了作品，收尾照样落在原来那一场上，
     复核四 W1-R4A-1）；采纳有了结果把记号交给 acceptCanonical（成了）或 endAdoption（没成、回包收不下）。
     采纳有结果之前不再发新的保存；路上那一次在这期间撞上的 409 先按住——采纳成了它就作废，没成再照常核对 / 冲突：
     不为作者自己的采纳提示「在别处被修改」（复核三 W1-R3A-4 · W1-R3B-6）。同一场同时有几次采纳（起草台重新挂载后防双击锁
     不在了）：按住的等最后一次也没成才走（复核四 W1-R4A-1）。options.html：采纳的那一稿（复核七 W1-R7B-6）；options.workId 同 save */
  beginAdoption(sid, options = {}) {
    if (!sid) return null;
    const m = scopeMeta(sid, options);
    // html：采纳的那一稿（起草台给出时记下）——后台复核 / 水合在采纳的回包之前读到了它，说的是采纳，不是「在别处有更新」（复核七 W1-R7B-6）
    const token = { m, base: m.revision, html: options.html != null ? sanitizeManuscriptHTML(options.html) : null };
    m.adoptions.push(token);
    return token;
  },
  endAdoption(sid, token) {
    if (!token || !token.m) return;
    const m = token.m;
    const at = m.adoptions.indexOf(token);
    if (at < 0) return; // acceptCanonical 已经收了尾
    m.adoptions.splice(at, 1);
    if (m.adoptions.length) return; // 还有别的采纳在路上：等它们
    m.adoptHeld.splice(0).forEach(({ flight, error }) => onFailed(m, flight, error));
    notifyState(m);
    pump(m);
    wakeAdoptionIdle(m);
  },
  /* 采纳请求没等到回包（断网、超时、服务端出错——它也许已经办完了）：读一次服务端。服务端眼下正是采纳的那一稿，存在采纳带的
     那一版或它的下一版上、提升到的也是它——采纳成了：交回一份和 adopt-current 回包一样形状的结果（recovered），调用方照
     采纳成了收尾（acceptCanonical）。不是 → null，照常当采纳没成（endAdoption）。只读，不动这一场的状态。
     过去这时起草台报「后端归档未通过」，写作台接着写的第一句 409，提示「在别处被修改过」（复核四 W1-R4A-4 · W1-R4B-5）。
     这一次也读不到服务端（一次断网连着把回包和这次读取都吞了）：结果不明——记下它（adoptionUnsure，token.unknown = true），
     返回 null；之后这一页按采纳之前的修订号撞上的 409 先核对是不是它（startOwnCheck），是就照采纳成了收尾，不说「在别处被
     修改」；起草台照实说没能确认（复核五 W1-R5B-4）。上一次结果不明、这一次采纳（同一稿）被按修订号拒绝：多半就是上一次落了地，
     同样读一次认它。 */
  async adoptionLanded(sid, token, html, error) {
    if (!token || !token.m) return null;
    const m = token.m;
    const normalized = sanitizeManuscriptHTML(html || "");
    const retried = !!(error && error.code === "AUTHOR_DRAFT_CONFLICT" && m.adoptionUnsure
      && sameManuscriptText(m.adoptionUnsure.html, normalized));
    if (!mayHaveLanded(error) && !retried) return null;
    const base = retried ? Math.min(token.base, m.adoptionUnsure.base) : token.base;
    let data = null;
    try {
      data = await requestDraft(m, ensureSeq + 1);
    } catch (e) {
      m.adoptionUnsure = { html: normalized, base };
      token.unknown = true;
      return null;
    }
    const draft = data && data.draft;
    if (!draft || !draft.draft_id || (m.draftId && draft.draft_id !== m.draftId)) return null;
    if (!Number.isInteger(draft.revision_no) || draft.revision_no < base || draft.revision_no > token.base + 1) return null;
    if (!sameManuscriptText(toDocHTML(draft.content || ""), normalized)) return null;
    if (!promotedAtRevision(draft)) return null;
    return landedAdoption(draft, data);
  },
  /* 采纳请求被服务端按修订号拒绝（409）之后（endAdoption 之后调用）：这期间草稿往前走的几版是不是这一页自己存上的——写作台
     路上那一次（离场冲刷、最后一次自动保存）先到了服务端，采纳带的还是它之前的修订号。先等路上 / 排队的那一稿有结果（它的回包
     可能比采纳的 409 晚到；回包丢了的先再发一次），再看服务端那时的修订号是不是落在这一页自己存上的那几版里（和提升同一个分法，
     见 refusalCause）。→ "own"：不是「在别处更新」，再采纳一次就在那一稿上（复核五 W1-R5B-5）；"unsure"：多半是写作台自己那一次、
     回包丢了还没能确认（复核六 W1-R6A-2）；"other"：别处动过 */
  async adoptionRefusalCause(sid, token, error) {
    if (!token || !token.m || !error || error.code !== "AUTHOR_DRAFT_CONFLICT") return "other";
    return refusalCause(token.m, token.base, error);
  },
  /* 写：调用之内本机缓存 + 未同步标记落地；PATCH 按场一次一个，排队的只留最新一稿。
     返回这一稿的结果（被更新的一稿取代时随它一起有结果）。options.workId：这一场所属的作品（离场冲刷时作品可能已换）。 */
  save(sid, html, options = {}) {
    if (!sid) return Promise.reject(Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" }));
    return saveMeta(scopeMeta(sid, options), html);
  },
  /* 把这一场整篇换成 html（同步与恢复的「恢复」「重试同步」）：和 save 一样在调用之内落本机缓存并排进保存，
     同一个调用里就通知写作台换稿（loaded，reason 默认 restore；编辑器里还没交出来的字写作台先留进同步与恢复）——
     编辑器、本机缓存和之后要同步的始终是同一稿，PATCH 失败时也是（停在本机，下一次保存 / 离场时再发）。
     章已批准锁定的场不换、不存（CHAPTER_APPROVED_LOCKED）：写作台对它只读，也不会替它自动保存。
     → Promise<{ data, carried }>：carried = 服务端存下的就是这一稿（没被作者随后在它上面接着写的更新一稿取代）。 */
  replace(sid, html, options = {}) {
    if (!sid) return Promise.reject(Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" }));
    const m = scopeMeta(sid, options);
    if (approvedLocked(m)) return Promise.reject(lockedError());
    keepSuperseded(m, html);
    const settled = saveMeta(m, html, true);
    const version = m.saveVersion;
    const wanted = readCache(m.workId, m.sid);
    notifyLoadedMeta(m, options.reason || "restore");
    return settled.then((result) => ({
      data: result.data,
      carried: result.version === version || (result.html != null && sameManuscriptText(result.html, wanted)),
    }));
  },
  /* 这一场所在的章是否已批准锁定（目录说的）：同步与恢复据此不恢复它 */
  locked(sid) {
    if (!sid) return false;
    return approvedLocked(meta(sid));
  },
  /* 等这一场本机的字有结果 → "saved" | "conflict" | "adopted" | "refused" | "failed"（从不抛；adopted 见 flushMeta）。
     options.retry：离场冲刷，失败时再发一次最新的一稿 */
  flush(sid, options = {}) {
    if (!sid) return Promise.resolve("saved");
    return flushMeta(scopeMeta(sid, options), options);
  },
  /* 当前草稿、保存与权威正文同步状态的只读快照。 */
  state(sid) {
    if (!sid) return null;
    return snapshotOf(meta(sid));
  },
  /* 写作台换稿（冲突、别处的新版本、恢复、采纳、服务端拒绝 / 章已锁定）时，编辑器里还没交给 WrDocs 的那几句经这里留进
     「同步与恢复」。与这一次已经留过的、与读缓存（新版本）一样的都不再留。冲突 / 拒绝里的并进那一条提示，其余的自己提示一句。 */
  keepLocalCopy(sid, html, options = {}) {
    if (!sid) return null;
    const m = scopeMeta(sid, options);
    const reason = options.reason || "conflict";
    const text = docText(html);
    if (!text) return null;
    const episode = m.conflict || (m.lastEpisode && m.lastEpisode.resolving ? m.lastEpisode : null);
    if (episode && episode.kept.has(text)) return null;
    if (sameManuscriptText(html, readCache(m.workId, m.sid))) return null;
    if (episode) return keepInEpisode(m, episode, html, KEEP_REASONS[reason] || KEEP_REASONS.conflict);
    const entry = keepCopy(m, html, KEEP_REASONS[reason] || KEEP_REASONS.conflict);
    if (!entry) return null;
    if (entry.durable === false) recoveryNotice(m, reason === "adopt" ? NOTICE.adoptKeptVolatile : NOTICE.volatile, "danger");
    else recoveryNotice(m, LOCAL_COPY_NOTICE[reason] || NOTICE.replaced);
    return entry;
  },
  /* 把已成功保存的场景草稿显式提升为权威正文。v1 仅支持“事实未变”。
     只提升作者眼前的那一稿：options.expectedText = 作者确认「只改了文字」时编辑器里的那一稿（确认框开着的时候，水合 /
     后台复核可能落地、在确认框后面把编辑器换成了另一台设备的版本；复核三 W1-R3A-1 · W1-R3B-5），没给就是这一页眼下这一份。
     打开时水合没成的先水合（读不到服务器时抛错）；作者确认的那一稿、这一页这一份、服务端存下的那一版文字不是同一段就拒绝
     （AUTHOR_DRAFT_CONFLICT：作者没看过它，复核二 W1-R2A-4 · W1-R2B-1）。
     服务端按修订号拒绝、这期间草稿往前走的几版全是这一页自己存上的（作者在提升途中接着写、自动保存先到）：
     AUTHOR_DRAFT_MOVED_BY_SELF，不说「在别处更新」（复核三 W1-R3B-8）。 */
  async promote(sid, options = {}) {
    if (!sid) throw Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" });
    const m = meta(sid);
    const asked = Object.prototype.hasOwnProperty.call(options, "expectedText") ? options.expectedText : m.shown;
    await settledMeta(m);
    if (m.conflict) throw m.conflict.hold;
    if (m.lastSaveError) throw m.lastSaveError;
    if (!m.hydrated) {
      await hydrateMeta(m);
      if (m.conflict) throw m.conflict.hold;
      if (m.lastSaveError) throw m.lastSaveError; // 服务端是新建的空稿、编辑器里的是还没同步上的工作稿
    }
    if (!m.draftId) {
      throw Object.assign(new Error("场景尚未就绪，无法提升权威正文"), { code: "AUTHOR_DRAFT_UNAVAILABLE" });
    }
    if (m.dirty) {
      throw Object.assign(new Error("草稿仍有未保存改动"), { code: "AUTHOR_DRAFT_UNSAVED" });
    }
    const serverHTML = toDocHTML(m.serverContent || "");
    // 章已批准锁定：WrDocs 不替它提升（写作台也不会这样调；服务端同样会拒绝）。锁定那一刻交进来的一稿还没存上（路上那一次保存 /
    // 采纳还没结果，本章这期间又重新打开了）：编辑器里是它、服务端上不是——不提升，照实说它还没存上，不说「在别处更新」
    // （复核五 W1-R5A-3：过去这时拿编辑器里的这一稿去比，报的是「草稿或权威正文已在别处更新」）
    if (approvedLocked(m)) throw lockedError();
    const lockText = m.lockPending && m.lockPending.latest;
    if (lockText && !sameManuscriptText(lockText.html, serverHTML)) throw lockPendingError();
    const shown = m.shown === undefined ? serverHTML : m.shown;
    if (!sameManuscriptText(shown, serverHTML) || (asked != null && !sameManuscriptText(asked, shown))) {
      // 编辑器被换过一版、作者确认的是换之前的那一稿。换稿的原因照实说（复核七 W1-R7B-5 · W1-R7A-2）：作者自己在起草台的采纳落了地
      // → 那次采纳；这一页自己这边的换稿（章锁定 / 服务端拒绝之后换回、读到自己那一次回包丢了的保存、恢复、上次会话的本机稿）
      // → 不说「在别处」；读到了别处存下的一版 / 冲突 → 别处更新
      const why = m.lastLoadReason;
      if (why === "adopt" && sameManuscriptText(shown, serverHTML)) throw adoptedError();
      if (why && why !== "server" && why !== "conflict") throw replacedHereError();
      throw movedError();
    }
    const expectedFinal = Object.prototype.hasOwnProperty.call(options, "expectedCurrentFinalSceneRowId")
      ? options.expectedCurrentFinalSceneRowId
      : m.currentFinalSceneRowId;
    const base = m.revision;
    const adoptedBefore = m.adoptedSeq; // 提升途中有采纳落了地：提升被拒是被作者自己的采纳赶在了前面
    let data;
    try {
      data = await apiPost(`/api/v1/author-drafts/${m.draftId}/promote-canonical`, {
        base_revision_no: base,
        expected_current_final_scene_row_id: expectedFinal == null ? null : expectedFinal,
        narrative_effect: options.narrativeEffect || "requires_reconcile",
        accepted_warning_codes: options.acceptedWarningCodes || [],
      });
    } catch (e) {
      throw await promoteRefusal(m, base, e, adoptedBefore);
    }
    m.currentFinalSceneRowId = data.final_scene_row_id;
    m.lastPromotedRevisionNo = data.draft_revision_no;
    m.lastPromotedFinalSceneRowId = data.final_scene_row_id;
    // 提升在路上时作者又存了一稿：提升的是较早的那个修订号，眼下的草稿仍待提升
    m.canonicalDirty = Boolean(data.canonical_dirty)
      || (Number.isInteger(data.draft_revision_no) && data.draft_revision_no !== m.revision)
      || m.dirty;
    notifyState(m);
    return data;
  },
  /* adopt-current 的 exact_author_draft 已在一个服务端事务内完成保存与提升。
     这里只吸收权威回包和刷新读缓存，绝不能再 PATCH 一次制造新修订。options.token：beginAdoption 给的记号——收尾落在采纳开始
     的那一场上（作者这期间换了作品也一样，复核四 W1-R4A-1）；同时在路上的别的采纳都会被服务端按修订号拒绝，一并收掉。
     · 这一页已经读到了采纳存下的那一版（或更新的一版：后台复核在采纳的回包之前就换上了它）：作者在它上面接着写的字就是它的
       下一稿——照常保存，只记下权威正文（复核四 W1-R4A-3）；
     · 否则服务端现在就是这一稿：路上那一次作废（它回来时不补发、不开冲突），采纳期间按住的 409 也作废，排队 / 失败后停着 /
       核对中 / 冲突中的本机稿都不再发——本机最新的那一稿和采纳的不一样、又不在同步与恢复里（采纳前的作者稿备份通常就是它）时
       先留一份并告诉作者，写作台随之换稿（force：状态不说「草稿已保存」，那几句没存上）。 */
  acceptCanonical(sid, html, data, options = {}) {
    if (!sid) throw Object.assign(new Error("缺少场景标识"), { code: "AUTHOR_DRAFT_SCENE_REQUIRED" });
    const token = options.token && options.token.m ? options.token : null;
    return acceptCanonicalMeta(token ? token.m : scopeMeta(sid, options), html, data);
  },
  /* 正文状态 / 读缓存变化的订阅：fn(kind, detail)，返回退订函数（见文件头） */
  subscribe(fn) {
    docListeners.add(fn);
    return () => { docListeners.delete(fn); };
  },
  /* 这一页读缓存里这一场的正文（不触发水合；可能是 null = 从未写过）。别的台子要读缓存时用它，
     不必自己拼 wr-doc: 键去读 localStorage（会绕过会话内存里这一页自己的那一份）。 */
  cachedHTML(sid) {
    if (!sid) return null;
    const workId = activeWorkId();
    return readCache(workId, catalogSid(workId, sid));
  },
  /* 目录眼下管这一场叫什么（乐观新建时的临时 sid 换成了稳定的 scene_id、旧深链的位置式 sid……；查不到就是它自己） */
  sceneSid(sid) {
    return catalogSid(activeWorkId(), sid);
  },
  /* 目录装载成功之后（wr-doc-store.jsx 登记）：换了名字的场跟过去，不在目录里了的场没同步上的字留进同步与恢复（见 followCatalog）。
     已被新实例取代的旧实例不再动作 */
  followCatalog(workId) {
    followCatalog(workId);
  },
  /* 当前在写场景预热（目录装载后调用）。已被新实例取代的旧实例（热更新 / 单测 resetModules）不再动作 */
  hydrateActive() {
    if (retired) return;
    try {
      const w = WsCatalog.writingScene();
      if (w && w.scene && w.scene.sid) hydrateMeta(meta(w.scene.sid)).catch(() => {});
    } catch (e) {}
  },
};

export { WrDocs, refusalReason };
