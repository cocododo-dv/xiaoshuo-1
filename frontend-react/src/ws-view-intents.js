const MAX_PENDING_PER_VIEW = 24;
const INTENT_TTL_MS = 30_000;
const pendingByView = new Map();
const readyViews = new Set();

function validName(value) {
  return typeof value === "string" && value.trim().length > 0;
}

function queueViewIntent(view, type, detail) {
  if (!validName(view) || !validName(type)) return false;
  const key = view.trim();
  const queue = pendingByView.get(key) || [];
  queue.push({ type: type.trim(), detail, queuedAt: Date.now() });
  if (queue.length > MAX_PENDING_PER_VIEW) queue.splice(0, queue.length - MAX_PENDING_PER_VIEW);
  pendingByView.set(key, queue);
  return true;
}

function queueViewIntents(view, intents) {
  const list = Array.isArray(intents) ? intents : (intents ? [intents] : []);
  return list.reduce((count, intent) => {
    if (!intent || !queueViewIntent(view, intent.type, intent.detail)) return count;
    return count + 1;
  }, 0);
}

function flushViewIntents(view, target = typeof window !== "undefined" ? window : null, options = {}) {
  if (!validName(view) || !target || typeof target.dispatchEvent !== "function") return 0;
  const key = view.trim();
  if (options.onlyWhenReady && !readyViews.has(key)) return 0;
  const queue = pendingByView.get(key) || [];
  pendingByView.delete(key);
  const now = Date.now();
  const EventCtor = target.CustomEvent || globalThis.CustomEvent;
  if (typeof EventCtor !== "function") return 0;

  let dispatched = 0;
  for (const intent of queue) {
    if (now - intent.queuedAt > INTENT_TTL_MS) continue;
    target.dispatchEvent(new EventCtor(intent.type, { detail: intent.detail }));
    dispatched += 1;
  }
  return dispatched;
}

function setViewIntentTargetReady(view, ready = true, target = typeof window !== "undefined" ? window : null) {
  if (!validName(view)) return 0;
  const key = view.trim();
  if (!ready) {
    readyViews.delete(key);
    return 0;
  }
  readyViews.add(key);
  return flushViewIntents(key, target);
}

function clearViewIntents(view) {
  if (validName(view)) pendingByView.delete(view.trim());
  else pendingByView.clear();
}

/* 进写作台的一组视图意图（章节编排、成稿中心、文学质量、待办、AI 起草台去写作台都用它）：
   · sid：先定位到这一场（ws:writer-scene）；
   · deep / signalId：进这一场的深改姿态，signalId 让深改面板到了诊断就选中这一条（隐含 deep）。没有 sid
     （章级结果没有单一场景可去）时不带姿态，是空数组：只回写作台；
   · posture：调用方点名的姿态（待办卡动作上带的、起草台「去写作台」给的），没有 sid 也照样带上。 */
function writerIntents(sid, { deep = false, signalId = "", posture = "" } = {}) {
  const intents = sid ? [{ type: "ws:writer-scene", detail: sid }] : [];
  if (sid && signalId) intents.push({ type: "ws:writer-posture", detail: { posture: "deep", signal_id: signalId } });
  else if (sid && deep) intents.push({ type: "ws:writer-posture", detail: "deep" });
  else if (posture) intents.push({ type: "ws:writer-posture", detail: posture });
  return intents;
}

function navigateWithViewIntent(view, type, detail) {
  if (!queueViewIntent(view, type, detail) || typeof window === "undefined") return false;
  const hash = `#${view.trim()}`;
  if (window.location.hash !== hash) {
    window.location.hash = hash;
    return true;
  }
  const schedule = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 0));
  schedule(() => flushViewIntents(view, window, { onlyWhenReady: true }));
  return true;
}

export {
  clearViewIntents,
  flushViewIntents,
  navigateWithViewIntent,
  queueViewIntent,
  queueViewIntents,
  setViewIntentTargetReady,
  writerIntents,
};
