import { useRef, useSyncExternalStore } from "react";
import { createSubscribers } from "./store-utils.js";

/* ==========================================================
   store-kit — store 的共用骨架（2026-09-29 前端共享层）
   · createKeyedLoader —— 按键（作品 id 等）合并的读取器，带写入序号与失效重读；
   · createStore —— 一份状态 + 订阅 + useSyncExternalStore 的 hook；
   · toStoreError —— store 对视图报错的统一形状 { code, message, offline, … }。
   各 store 原来各写一遍「在飞就复用同一个请求」：目录、回收站、待办、书架、资料、设计同步、诊断、成稿……
   其中目录 / 回收站 / 待办的写法会让旧结果盖住新状态（审计 F01-05）：
     · 写入之后的补读被并进写入之前发出的那次读取，回来的是写入前的服务端状态，第二次改名在屏上退回去；
     · 回收站 / 待办的在飞请求不看作品：拉取途中换了作品，新作品的读取并进上一部的请求。
   这里的规矩：只按键合并；写入之前（或写入进行中）发出的读取回来时不写缓存；在飞时失效就作废它、
   结束后恰好再读一次。纯 ESM，不读 store、不写 window。
   ========================================================== */

/* 按键合并的读取器。
   options:
     fetch(key, ...args) → Promise<data>   发请求（可以顺带做纯映射）；
     apply(key, data)                        结果可信时写进 store（只在这里写缓存、发通知）；
     onError(key, error)                     可信的那次读取失败（或 apply 抛错）时调用；
     isCurrent(key) → boolean                可选：只存「当前作品」一份的 store 用它丢掉换作品之前的结果。
   返回：
     load(key, ...args)        在飞就复用这个键的请求，否则发一次；
     invalidate(key, ...args)  在飞的那次作废、结束后恰好再读一次（多次失效只多读一次，参数取最近一次给的）；
                               空闲时立刻读；
     noteWrite(key)            本机刚改了这个键的状态：此前发出、还没回来的读取一律不写缓存；
     write(key, run, { reload = true })
                               把一次写入（run 返回 Promise）包起来：开始与结束各记一次写入序号，
                               进行中结束的读取不写缓存；结束后（成功或失败）若没有别的写入在进行就补读一次，
                               以服务端为准收敛。返回 run 的结果，run 的失败照旧抛给调用方（由它提示）。
     inflight(key) / writing(key)
   load / invalidate 的 Promise 解析为 true = 这次（或它被作废后的重读）的结果已经写进 store，
   false = 没写（作废、不是当前键、失败）；从不 reject。 */
export function createKeyedLoader(options) {
  const opts = options || {};
  const slots = new Map();         // key → 在飞的那一次 { args, dirty, promise }
  const writeSeqs = new Map();     // key → 本机写入序号
  const pendingWrites = new Map(); // key → 进行中的 write() 个数

  const seqOf = (key) => writeSeqs.get(key) || 0;
  const pendingOf = (key) => pendingWrites.get(key) || 0;

  function current(key) {
    if (!opts.isCurrent) return true;
    try { return !!opts.isCurrent(key); } catch (e) { return false; }
  }

  function report(key, error) {
    if (!opts.onError) return;
    try { opts.onError(key, error); } catch (e) { /* 与 createSubscribers 同一口径：store 的处理函数出错不打断读取器 */ }
  }

  function start(key, args) {
    const slot = { args, dirty: false, promise: null };
    const startedAt = seqOf(key);
    slots.set(key, slot);
    slot.promise = (async () => {
      let data;
      let error = null;
      let failed = false;
      try { data = await opts.fetch(key, ...slot.args); } catch (e) { failed = true; error = e; }
      slots.delete(key);
      if (!current(key)) return false;
      // 在飞时被失效：这次的结果（与失败）都作废，按最近一次给的参数再读一次
      if (slot.dirty) return start(key, slot.args).promise;
      if (failed) { report(key, error); return false; }
      // 发出之后本机写过、或者还有写入没结束：服务端回来的状态早于本机的新状态
      if (seqOf(key) !== startedAt || pendingOf(key) > 0) return false;
      try {
        opts.apply(key, data);
      } catch (e) {
        report(key, e);
        return false;
      }
      return true;
    })();
    return slot;
  }

  function load(key, ...args) {
    const slot = slots.get(key);
    return slot ? slot.promise : start(key, args).promise;
  }

  function invalidate(key, ...args) {
    const slot = slots.get(key);
    if (!slot) return start(key, args).promise;
    slot.dirty = true;
    if (args.length) slot.args = args;
    return slot.promise;
  }

  function noteWrite(key) {
    writeSeqs.set(key, seqOf(key) + 1);
  }

  async function write(key, run, { reload = true } = {}) {
    noteWrite(key);
    pendingWrites.set(key, pendingOf(key) + 1);
    try {
      return await run();
    } finally {
      const left = pendingOf(key) - 1;
      if (left > 0) pendingWrites.set(key, left);
      else pendingWrites.delete(key);
      noteWrite(key);
      if (reload && left <= 0) invalidate(key);
    }
  }

  return {
    load,
    invalidate,
    noteWrite,
    write,
    inflight: (key) => slots.has(key),
    writing: (key) => pendingOf(key) > 0,
  };
}

const isRecord = (value) => !!value && typeof value === "object" && !Array.isArray(value);

/* 一份状态 + 订阅。
   set(patch)：对象状态浅合并（每个键都没变就什么都不做）；set(fn)：fn(旧状态) 的返回值整体替换；
   非对象状态直接替换。状态真的变了才通知订阅者（订阅者抛错被吞掉，见 createSubscribers）。
   useStore(selector?)：选中的值（Object.is）变了才重渲；选择器可以内联、可以返回新对象——
   同一份状态、同一个选择器只算一次，不会触发 useSyncExternalStore 的「快照没缓存」死循环。 */
export function createStore(initial) {
  let state = initial;
  const subs = createSubscribers();

  function set(update) {
    let next;
    if (typeof update === "function") next = update(state);
    else if (isRecord(state) && isRecord(update)) {
      next = Object.keys(update).some((key) => !Object.is(state[key], update[key])) ? { ...state, ...update } : state;
    } else next = update;
    if (Object.is(next, state)) return state;
    state = next;
    subs.notify();
    return state;
  }

  function useStore(selector) {
    const memo = useRef(null);
    const snapshot = () => {
      const last = memo.current;
      if (last && last.state === state && last.selector === selector) return last.value;
      const value = selector ? selector(state) : state;
      memo.current = { state, selector, value };
      return value;
    };
    return useSyncExternalStore(subs.subscribe, snapshot, snapshot);
  }

  return { get: () => state, set, subscribe: subs.subscribe, useStore };
}

/* store 对视图报错的统一形状。code：错误码，没有就用 HTTP 状态，再没有用兜底码（extra.code，默认 NETWORK_ERROR）；
   message：错误原话，没有就用 fallback；offline：浏览器此刻是否报告离线。extra 的其余字段（例如雪花同步的 scope）
   原样并进来。书架的 wsErrorShape、雪花同步的 snowErrorShape 是它的两个特例。 */
export function toStoreError(error, fallback, extra) {
  const { code: fallbackCode = "NETWORK_ERROR", ...rest } = extra || {};
  return {
    code: (error && (error.code || error.status)) || fallbackCode,
    message: (error && error.message) || fallback,
    offline: typeof navigator !== "undefined" && navigator.onLine === false,
    ...rest,
  };
}
