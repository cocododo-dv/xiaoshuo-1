/* 看页面可见性的轮询（2026-09-29 前端数据层；审计 F01-22）。
   作业轮询（对照检查、场景 / 章运行、参考书活动）原来在后台标签页里照样每 1–5 秒问一次后端。
   这里的规矩：页面隐藏时改用 hiddenInterval（默认 30 秒；给 null 就整个暂停），回到前台立刻问一次，
   之后恢复原来的节奏。纯 ESM，不 import store、不写 window；没有 document 的环境（node 单测）当作一直可见。

   createPoller({ run, interval, hiddenInterval })
     run()               问一次；返回（或 resolve 为）false 表示不用再问了（作业到了终态）。抛错 / reject 照常继续。
     interval            毫秒数，或 () => 毫秒数（每轮重算：跑久了可以放慢）。
     hiddenInterval      页面隐藏时的间隔；null = 隐藏时不问，回到前台再问。
   返回 { start(delay?), stop(), active() }：start 在 delay（默认 interval）之后问第一次；已经在跑就重排。 */

const HIDDEN_POLL_MS = 30_000;

function pageHidden() {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function createPoller({ run, interval, hiddenInterval = HIDDEN_POLL_MS } = {}) {
  let timer = null;
  let running = false;   // start 之后、stop 之前
  let inFlight = false;  // run 正在跑（这时不另排一次）

  const intervalNow = () => {
    const value = typeof interval === "function" ? interval() : interval;
    return Number(value) > 0 ? Number(value) : 1000;
  };

  function clear() {
    if (timer != null) clearTimeout(timer);
    timer = null;
  }

  function schedule(delay) {
    clear();
    if (!running) return;
    if (pageHidden()) {
      if (hiddenInterval == null) return; // 暂停：回到前台时由 visibilitychange 接着问
      delay = Math.max(delay, hiddenInterval);
    }
    timer = setTimeout(tick, delay);
  }

  async function tick() {
    timer = null;
    if (!running || inFlight) return;
    inFlight = true;
    let keepGoing = true;
    try { keepGoing = (await run()) !== false; } catch (e) { keepGoing = true; }
    inFlight = false;
    if (!running) return;
    if (!keepGoing) { stop(); return; }
    schedule(intervalNow());
  }

  function onVisibility() {
    if (!running || pageHidden() || inFlight) return;
    clear();
    tick();
  }

  function start(delay) {
    if (!running) {
      running = true;
      if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisibility);
    }
    schedule(delay == null ? intervalNow() : delay);
  }

  function stop() {
    running = false;
    clear();
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibility);
  }

  return { start, stop, active: () => running };
}
