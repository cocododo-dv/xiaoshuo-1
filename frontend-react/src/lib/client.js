// FE-ALIGN Phase 2: 自 frontend/src/lib/api/client.js 移植（两端共享同一契约）。
// 信封 {ok,data,error,request_id} / X-Idempotency-Key / X-Operator-Ref /
// novel-system-api-base 逻辑保持一致；去掉 Vue 端的 cursorPagination 依赖。

const API_BASE_KEY = "novel-system-api-base";
const API_BASE_DEFAULT_KEY = "novel-system-api-base-default";
const FALLBACK_API_BASE = "http://127.0.0.1:8000";
const DEFAULT_API_BASE = (import.meta.env.VITE_NOVEL_SYSTEM_API_BASE || FALLBACK_API_BASE).trim();
const OPERATOR_REF_KEY = "novel-system-operator-ref";
const DEFAULT_OPERATOR_REF = "operator";
const REMOTE_ACCESS_TOKEN_KEY = "novel-system-remote-access-token";
const DEFAULT_REMOTE_ACCESS_TOKEN = (import.meta.env.VITE_NOVEL_SYSTEM_ACCESS_TOKEN || "").trim();
const memoryStorage = { local: new Map(), session: new Map() };
const storageStatus = {
  local: { available: true, errorName: null },
  session: { available: true, errorName: null },
};

function storageObject(scope) {
  if (typeof window === "undefined") return null;
  return scope === "session" ? window.sessionStorage : window.localStorage;
}

function markStorageResult(scope, error = null) {
  storageStatus[scope] = {
    available: !error,
    errorName: error ? String(error.name || "StorageError") : null,
  };
}

function safeStorageGet(scope, key) {
  try {
    const storage = storageObject(scope);
    if (storage) {
      const value = storage.getItem(key);
      markStorageResult(scope);
      if (value !== null) {
        memoryStorage[scope].set(key, value);
        return value;
      }
    }
  } catch (error) {
    markStorageResult(scope, error);
  }
  return memoryStorage[scope].get(key) ?? null;
}

function safeStorageSet(scope, key, value) {
  const normalized = String(value);
  memoryStorage[scope].set(key, normalized);
  try {
    const storage = storageObject(scope);
    if (storage) storage.setItem(key, normalized);
    markStorageResult(scope);
  } catch (error) {
    markStorageResult(scope, error);
  }
}

function safeStorageRemove(scope, key) {
  memoryStorage[scope].delete(key);
  try {
    const storage = storageObject(scope);
    if (storage) storage.removeItem(key);
    markStorageResult(scope);
  } catch (error) {
    markStorageResult(scope, error);
  }
}

export function getClientStorageStatus() {
  return {
    local: { ...storageStatus.local },
    session: { ...storageStatus.session },
  };
}

function timeoutValue(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

const DEFAULT_READ_TIMEOUT_MS = timeoutValue(import.meta.env.VITE_NOVEL_SYSTEM_READ_TIMEOUT_MS, 30_000);
const DEFAULT_MUTATION_TIMEOUT_MS = timeoutValue(
  import.meta.env.VITE_NOVEL_SYSTEM_MUTATION_TIMEOUT_MS,
  15 * 60_000,
);

function isLoopbackApiBase(value) {
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:") &&
      ["127.0.0.1", "localhost"].includes(url.hostname)
    );
  } catch {
    return false;
  }
}

function shouldUseInjectedDefault(stored, storedDefault) {
  if (!stored) {
    return true;
  }
  if (stored === FALLBACK_API_BASE) {
    return true;
  }
  if (storedDefault && stored === storedDefault) {
    return true;
  }
  return !storedDefault && isLoopbackApiBase(stored) && isLoopbackApiBase(DEFAULT_API_BASE);
}

export function getApiBase() {
  if (typeof window === "undefined") {
    return DEFAULT_API_BASE;
  }
  const stored = (safeStorageGet("local", API_BASE_KEY) || "").trim();
  const storedDefault = (safeStorageGet("local", API_BASE_DEFAULT_KEY) || "").trim();
  if (shouldUseInjectedDefault(stored, storedDefault)) {
    safeStorageSet("local", API_BASE_KEY, DEFAULT_API_BASE);
    safeStorageSet("local", API_BASE_DEFAULT_KEY, DEFAULT_API_BASE);
    return DEFAULT_API_BASE;
  }
  safeStorageSet("local", API_BASE_DEFAULT_KEY, DEFAULT_API_BASE);
  return stored;
}

export function setApiBase(value) {
  const normalized = value.trim() || DEFAULT_API_BASE;
  if (typeof window !== "undefined") {
    safeStorageSet("local", API_BASE_KEY, normalized);
    safeStorageSet("local", API_BASE_DEFAULT_KEY, DEFAULT_API_BASE);
  }
  return normalized;
}

export function getOperatorRef() {
  if (typeof window === "undefined") {
    return DEFAULT_OPERATOR_REF;
  }
  return safeStorageGet("local", OPERATOR_REF_KEY) || DEFAULT_OPERATOR_REF;
}

export function setOperatorRef(value) {
  const normalized = value.trim() || DEFAULT_OPERATOR_REF;
  if (typeof window !== "undefined") {
    safeStorageSet("local", OPERATOR_REF_KEY, normalized);
  }
  return normalized;
}

export function getRemoteAccessToken() {
  if (typeof window === "undefined") {
    return DEFAULT_REMOTE_ACCESS_TOKEN;
  }
  return (safeStorageGet("session", REMOTE_ACCESS_TOKEN_KEY) || DEFAULT_REMOTE_ACCESS_TOKEN).trim();
}

export function setRemoteAccessToken(value) {
  const normalized = String(value || "").trim();
  if (typeof window !== "undefined") {
    if (normalized) safeStorageSet("session", REMOTE_ACCESS_TOKEN_KEY, normalized);
    else safeStorageRemove("session", REMOTE_ACCESS_TOKEN_KEY);
  }
  return normalized;
}

function withAccessToken(headers = {}) {
  const result = { ...headers };
  const token = getRemoteAccessToken();
  if (token) result["X-Novel-Access-Token"] = token;
  return result;
}

export function buildUrl(path) {
  return `${getApiBase()}${path}`;
}

/* ---- 幂等键：操作意图级（审计 P-3）----
   旧实现每次请求都生成新键，后端的去重/重放/在途 409 机制被整体绕过
   （双击 = 两个键 = 两次执行）。现按「方法+路径+载荷」签名持键：
   - 请求在途或失败重试 → 复用同一键（后端可 409 拦截 / 重放缓存结果）；
   - 请求成功 → 丢弃签名（下一次同载荷调用视为新的用户意图，配新键）。 */
const IDEMPOTENCY_KEYS_MAX = 200;
const inflightIdempotencyKeys = new Map();

function requestSignature(method, path, body) {
  let payload = "";
  try {
    payload = body === undefined ? "" : JSON.stringify(body, (_key, value) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return value;
      return Object.keys(value).sort().reduce((sorted, key) => {
        sorted[key] = value[key];
        return sorted;
      }, {});
    });
  } catch {
    payload = String(body);
  }
  return `${method} ${path} ${payload}`;
}

function acquireIdempotencyKey(method, path, body) {
  const signature = requestSignature(method, path, body);
  let key = inflightIdempotencyKeys.get(signature);
  if (!key) {
    key = `${path}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    if (inflightIdempotencyKeys.size >= IDEMPOTENCY_KEYS_MAX) {
      const oldest = inflightIdempotencyKeys.keys().next().value;
      inflightIdempotencyKeys.delete(oldest);
    }
    inflightIdempotencyKeys.set(signature, key);
  }
  return { key, signature };
}

function releaseIdempotencyKey(signature) {
  inflightIdempotencyKeys.delete(signature);
}

function buildClientRequestId() {
  return `client_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export class ApiRequestError extends Error {
  constructor(message, {
    code = null,
    status = null,
    details = {},
    requestId = null,
    clientRequestId = null,
    retryable = false,
  } = {}) {
    super(message);
    this.name = "ApiRequestError";
    this.code = code;
    this.status = status;
    this.details = details || {};
    this.requestId = requestId;
    this.clientRequestId = clientRequestId;
    this.retryable = Boolean(retryable);
  }
}

export function buildQueryPath(path, filters = {}, aliases = {}) {
  const params = new URLSearchParams();
  Object.entries(filters || {}).forEach(([key, value]) => {
    if (value === null || value === undefined || value === "") {
      return;
    }
    params.set(aliases[key] || key, value);
  });
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

function normalizeRequestError(error, clientRequestId = null, timeout = null) {
  if (error instanceof ApiRequestError) {
    return error;
  }
  if (error && error.name === "AbortError") {
    if (timeout && timeout.didTimeout) {
      return new ApiRequestError("请求超时，请稍后重试。", {
        code: "REQUEST_TIMEOUT",
        status: 0,
        details: { retryable: true, reachedServer: null, timeoutMs: timeout.timeoutMs },
        clientRequestId,
        retryable: true,
      });
    }
    return new ApiRequestError("请求已取消。", {
      code: "REQUEST_ABORTED",
      status: 0,
      details: { retryable: true, reachedServer: null },
      clientRequestId,
      retryable: true,
    });
  }
  if (error instanceof Error) {
    if (error.code || error.status) {
      return error;
    }
    // Fetch rejects transport failures with a TypeError, but the message is
    // browser- and locale-specific (Failed to fetch / Load failed /
    // NetworkError when attempting to fetch resource / fetch failed).  The
    // request may already have reached the server, so every such rejection is
    // an uncertain, retryable mutation and must retain its idempotency key.
    if (error instanceof TypeError || error.name === "NetworkError") {
      return new ApiRequestError(`连接接口失败，请确认 API 地址和后端服务是否可用。当前 API 地址：${getApiBase()}`, {
        code: "NETWORK_ERROR",
        status: 0,
        details: { retryable: true, apiBase: getApiBase(), reachedServer: false },
        clientRequestId,
        retryable: true,
      });
    }
    return error;
  }
  return new ApiRequestError("请求失败。", {
    code: "REQUEST_FAILED",
    status: 0,
    details: { retryable: true, reachedServer: false },
    clientRequestId,
    retryable: true,
  });
}

async function parseEnvelope(response, clientRequestId = null) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok || payload?.ok === false) {
    const responseError = payload?.error || {};
    const fallbackMessage =
      responseError.code === "DATABASE_BUSY"
        ? "数据库正忙，请稍后重试。"
        : `请求失败：${response.status}`;
    const details = responseError.details || {};
    throw new ApiRequestError(responseError.message || fallbackMessage, {
      code: responseError.code || null,
      status: response.status,
      details,
      requestId: payload?.request_id || null,
      clientRequestId,
      retryable: Boolean(details.retryable || response.status === 429 || response.status >= 500),
    });
  }
  return payload?.data;
}

function requestAbortGuard(externalSignal, timeoutMs, defaultTimeoutMs) {
  const controller = new AbortController();
  const effectiveTimeoutMs = timeoutValue(timeoutMs, defaultTimeoutMs);
  let didTimeout = false;
  const forwardAbort = () => controller.abort(externalSignal && externalSignal.reason);
  if (externalSignal) {
    if (externalSignal.aborted) forwardAbort();
    else externalSignal.addEventListener("abort", forwardAbort, { once: true });
  }
  const timer = setTimeout(() => {
    didTimeout = true;
    controller.abort();
  }, effectiveTimeoutMs);
  return {
    signal: controller.signal,
    timeoutMs: effectiveTimeoutMs,
    didTimeout: () => didTimeout,
    cleanup() {
      clearTimeout(timer);
      if (externalSignal) externalSignal.removeEventListener("abort", forwardAbort);
    },
  };
}

async function requestEnvelope(
  path,
  init,
  { signal, timeoutMs } = {},
  clientRequestId,
  defaultTimeoutMs = DEFAULT_READ_TIMEOUT_MS,
) {
  const guard = requestAbortGuard(signal, timeoutMs, defaultTimeoutMs);
  try {
    const response = await fetch(buildUrl(path), { ...init, signal: guard.signal });
    return await parseEnvelope(response, clientRequestId);
  } catch (error) {
    throw normalizeRequestError(error, clientRequestId, {
      didTimeout: guard.didTimeout(),
      timeoutMs: guard.timeoutMs,
    });
  } finally {
    guard.cleanup();
  }
}

/* ---- 读取类请求共用骨架 ----
   两个读侧导出（apiGet/apiAdminGet）共享同一套「生成请求 id → 带访问令牌头 →
   读超时请求」流程，差异只有一点：adminToken 真值时附加 X-Admin-Token
   （空令牌不加头，保持无令牌 loopback 后端契约）。错误规范化由
   requestEnvelope 内部完成，这里不再包一层。 */
async function readRequest(path, { adminToken = "", signal, timeoutMs } = {}) {
  const clientRequestId = buildClientRequestId();
  const headers = withAccessToken({ "X-Client-Request-Id": clientRequestId });
  if (adminToken) headers["X-Admin-Token"] = adminToken;
  return requestEnvelope(
    path,
    { headers },
    { signal, timeoutMs },
    clientRequestId,
    DEFAULT_READ_TIMEOUT_MS,
  );
}

export async function apiGet(path, { signal, timeoutMs } = {}) {
  return readRequest(path, { signal, timeoutMs });
}

/* ---- 变更类请求共用骨架 ----
   六个 mutation 导出（apiPost/apiPatch/apiPut/apiDelete + 管理面两个）共享同一套
   「取幂等键 → 请求 → 成功释放 / 失败按可重试性释放」流程，差异只有三点：
   method、body 有无（undefined 表示无 body：不带 Content-Type/请求体，且幂等
   签名的载荷为空串）、adminToken 真值时附加 X-Admin-Token（空令牌不加头，
   保持无令牌 loopback 后端契约）。 */
async function mutationRequest(method, path, body, { adminToken = "", signal, timeoutMs, idempotencyKey = "" } = {}) {
  const clientRequestId = buildClientRequestId();
  // 调用方显式给键（2026-09-15：风格参考的合成 / 重新分类要用同一个键去轮询服务端进度）时
  // 直接用它，不进签名表；否则按「方法+路径+载荷」签名持键。
  const { key, signature } = idempotencyKey
    ? { key: String(idempotencyKey), signature: null }
    : acquireIdempotencyKey(method, path, body);
  try {
    const headers = withAccessToken({
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      "X-Idempotency-Key": key,
      "X-Operator-Ref": getOperatorRef(),
      "X-Client-Request-Id": clientRequestId,
    });
    if (adminToken) headers["X-Admin-Token"] = adminToken;
    const init = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    const data = await requestEnvelope(path, init, { signal, timeoutMs }, clientRequestId, DEFAULT_MUTATION_TIMEOUT_MS);
    if (signature) releaseIdempotencyKey(signature);
    return data;
  } catch (error) {
    const normalized = normalizeRequestError(error, clientRequestId);
    // 在途冲突/可重试失败保留键供重试重放；确定性失败（4xx 校验类）丢键防脏复用
    if (signature && !normalized.retryable && normalized.code !== "IDEMPOTENCY_REQUEST_IN_PROGRESS") {
      releaseIdempotencyKey(signature);
    }
    throw normalized;
  }
}

export async function apiPost(path, body = {}, options = {}) {
  return mutationRequest("POST", path, body, options);
}

export function cancelRunJob(jobId, options) {
  return apiPost(`/api/v1/run-jobs/${encodeURIComponent(jobId)}/cancel`, {}, options);
}

export function getLatestSceneRunJob(sceneId, options) {
  return apiGet(`/api/v1/scenes/${encodeURIComponent(sceneId)}/run/jobs/latest`, options);
}

export async function apiPatch(path, body = {}, options = {}) {
  return mutationRequest("PATCH", path, body, options);
}

export async function apiPut(path, body = {}, options = {}) {
  return mutationRequest("PUT", path, body, options);
}

/* ---- 管理面(X-Admin-Token)变体 ----
   系统配置写接口需要管理令牌;无令牌配置的本地后端对 loopback 放行,
   此时 adminToken 传空即可。令牌存取由调用方(WsAiProviders)负责。 */

export async function apiAdminGet(path, adminToken = "", options = {}) {
  return readRequest(path, { ...options, adminToken });
}

export async function apiAdminPost(path, body = {}, adminToken = "", options = {}) {
  return mutationRequest("POST", path, body, { ...options, adminToken });
}

export async function apiAdminDelete(path, adminToken = "", options = {}) {
  return mutationRequest("DELETE", path, undefined, { ...options, adminToken });
}

export async function apiDelete(path, options = {}) {
  return mutationRequest("DELETE", path, undefined, options);
}
