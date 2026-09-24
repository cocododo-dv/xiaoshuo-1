// 「像不像」store（ws-fidelity-store.js）：一场 / 一部作品的读数按键缓存（force 才重读，失败保留旧数据）；
// 对照检查：发起 → 轮询到终态 → 成功后那一场 / 那部作品重读；发起就被拒（没有模型）、作业失败（评审失败）、
// 轮询时网络抖动（放慢再问）、离开页面后回来接着轮询。合成数据。
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({ apiGet: vi.fn(), apiPost: vi.fn() }));

let client;
let store;

const SCENE_URL = "/api/v1/scenes/sc-1/style-fidelity";
const PROJECT_URL = "/api/v1/projects/w1/style-fidelity";
const CHECKS = "/api/v2/style-reference/checks";

function job(status, extra = {}) {
  return { key: `job:job-1`, job_id: "job-1", kind: "check", status, phase: status === "running" ? "judge" : status, percent: status === "running" ? 33.3 : null, ...extra };
}

const READING = { reading_id: "r-9", scene_id: "sc-1", project_id: "w1", percentile: 41, within_range: true, reliable: true };

beforeEach(async () => {
  vi.resetModules();
  client = await import("./lib/client.js");
  client.apiGet.mockReset();
  client.apiPost.mockReset();
  store = await import("./ws-fidelity-store.js");
  store.fidResetForTests();
});

afterEach(() => {
  store.fidResetForTests();
  vi.useRealTimers();
});

describe("读数缓存", () => {
  it("一场的读数读过就不再读；force 重读；读失败保留上一次的数据", async () => {
    client.apiGet.mockResolvedValueOnce({ scene_id: "sc-1", bound: true, readings: { final: READING } });
    await store.fidLoadScene("sc-1");
    await store.fidLoadScene("sc-1");
    expect(client.apiGet).toHaveBeenCalledTimes(1);
    expect(client.apiGet).toHaveBeenCalledWith(SCENE_URL);
    expect(store.fidScene("sc-1")).toMatchObject({ phase: "ready", data: { bound: true } });

    client.apiGet.mockRejectedValueOnce(Object.assign(new Error("down"), { code: "NETWORK_ERROR" }));
    await store.fidLoadScene("sc-1", { force: true });
    expect(store.fidScene("sc-1").phase).toBe("error");
    expect(store.fidScene("sc-1").data.readings.final.reading_id).toBe("r-9");
  });

  it("同一个键同时只有一个请求；force 撞上在途的请求，等它回来再读一次", async () => {
    let release;
    client.apiGet.mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve({ project_id: "w1", reading_count: 1 }); }));
    client.apiGet.mockResolvedValueOnce({ project_id: "w1", reading_count: 2 });
    const first = store.fidLoadProject("w1");
    const again = store.fidLoadProject("w1");
    const forced = store.fidLoadProject("w1", { force: true });
    expect(client.apiGet).toHaveBeenCalledTimes(1);
    release();
    await Promise.all([first, again, forced]);
    expect(client.apiGet).toHaveBeenCalledTimes(2);
    expect(client.apiGet.mock.calls.every(([url]) => url === PROJECT_URL)).toBe(true);
    expect(store.fidProject("w1").data.reading_count).toBe(2);
  });
});

describe("对照检查", () => {
  it("发起：文字 + 画像的请求体；轮询到成功，记下读数，那一场与作品重读", async () => {
    vi.useFakeTimers();
    // 先读过那一场与作品，成功后它们应被重读
    client.apiGet.mockResolvedValueOnce({ scene_id: "sc-1", readings: {} });
    client.apiGet.mockResolvedValueOnce({ project_id: "w1", reading_count: 0 });
    await store.fidLoadScene("sc-1");
    await store.fidLoadProject("w1");

    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", state: "queued", job: job("queued"), reading: null });
    const entry = await store.fidStartCheck("book:bk-a", { sceneId: "sc-1", projectId: "w1" });
    expect(client.apiPost).toHaveBeenCalledWith(CHECKS, { scene_id: "sc-1", project_id: "w1" });
    expect(entry).toMatchObject({ phase: "running", jobId: "job-1" });
    expect(store.fidCheckActive("book:bk-a")).toBe(true);

    client.apiGet.mockImplementation((url) => {
      if (url === `${CHECKS}/job-1`) return Promise.resolve({ job: job("running"), reading: null });
      return Promise.resolve({});
    });
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(store.fidCheck("book:bk-a")).toMatchObject({ phase: "running", job: { phase: "judge" } });

    client.apiGet.mockImplementation((url) => {
      if (url === `${CHECKS}/job-1`) return Promise.resolve({ job: job("succeeded", { finished_at: "2026-09-23T10:00:00" }), reading: READING });
      if (url === SCENE_URL) return Promise.resolve({ scene_id: "sc-1", readings: { manual: READING } });
      if (url === PROJECT_URL) return Promise.resolve({ project_id: "w1", reading_count: 1 });
      return Promise.resolve({});
    });
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(store.fidCheck("book:bk-a")).toMatchObject({ phase: "done", reading: { reading_id: "r-9" } });
    expect(store.fidCheckActive("book:bk-a")).toBe(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(store.fidScene("sc-1").data.readings.manual.reading_id).toBe("r-9");
    expect(store.fidProject("w1").data.reading_count).toBe(1);

    // 终态之后不再轮询
    const calls = client.apiGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 4);
    expect(client.apiGet.mock.calls.length).toBe(calls);
  });

  it("贴的文字：请求体是 text + profile_id", async () => {
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("queued") });
    await store.fidStartCheck("book:bk-a", { text: "一段文字", profileId: "pf-a" });
    expect(client.apiPost).toHaveBeenCalledWith(CHECKS, { text: "一段文字", profile_id: "pf-a" });
  });

  it("发起就被拒（没有模型）：条目记下错误，不抛、不轮询", async () => {
    vi.useFakeTimers();
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("no llm"), { code: "STYLE_REFERENCE_LLM_REQUIRED", status: 409 }));
    const entry = await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    expect(entry).toMatchObject({ phase: "failed", error: { code: "STYLE_REFERENCE_LLM_REQUIRED" } });
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 3);
    expect(client.apiGet).not.toHaveBeenCalled();
  });

  it("作业失败（评审失败）：条目带作业的错误码与可重试标记", async () => {
    vi.useFakeTimers();
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    client.apiGet.mockResolvedValue({
      job: job("failed", { error: { code: "STYLE_REFERENCE_CHECK_JUDGE_FAILED", message: "judge failed", retryable: true } }),
      reading: null,
    });
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(store.fidCheck("scene:sc-1")).toMatchObject({ phase: "failed", error: { code: "STYLE_REFERENCE_CHECK_JUDGE_FAILED", retryable: true } });
  });

  it("轮询时网络抖动：放慢一点接着问，最后照样拿到结果", async () => {
    vi.useFakeTimers();
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    client.apiGet.mockRejectedValueOnce(Object.assign(new Error("down"), { code: "NETWORK_ERROR" }));
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(store.fidCheck("scene:sc-1").phase).toBe("running");
    client.apiGet.mockResolvedValue({ job: job("succeeded"), reading: READING });
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(store.fidCheck("scene:sc-1").phase).toBe("running");
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(store.fidCheck("scene:sc-1").phase).toBe("done");
  });

  it("又发起了一次：上一次作业迟到的结果不覆盖新的", async () => {
    vi.useFakeTimers();
    let slow;
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    client.apiGet.mockImplementationOnce(() => new Promise((resolve) => { slow = resolve; }));
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    client.apiPost.mockResolvedValueOnce({ job_id: "job-2", job: { ...job("running"), job_id: "job-2" } });
    await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    slow({ job: job("failed", { error: { code: "X" } }), reading: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(store.fidCheck("scene:sc-1")).toMatchObject({ phase: "running", jobId: "job-2" });
  });

  it("关掉条目：停止轮询", async () => {
    vi.useFakeTimers();
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    store.fidDismissCheck("scene:sc-1");
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 3);
    expect(client.apiGet).not.toHaveBeenCalled();
    expect(store.fidCheck("scene:sc-1")).toBeNull();
  });
});

describe("对照检查 · 取消、认领、放慢轮询（2026-09-24 清理 C1）", () => {
  it("取消进行中的检查：POST …/checks/{id}/cancel，条目清掉、不再轮询", async () => {
    vi.useFakeTimers();
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("book:bk-a", { text: "一段文字", profileId: "pf-a" });
    expect(store.fidCheckActive("book:bk-a")).toBe(true);
    client.apiPost.mockResolvedValueOnce({ job: job("cancelled") });
    expect(await store.fidCancelCheck("book:bk-a")).toEqual({ ok: true });
    expect(client.apiPost).toHaveBeenLastCalledWith(`${CHECKS}/job-1/cancel`, {});
    expect(store.fidCheck("book:bk-a")).toBeNull();
    expect(store.fidCheckActive("book:bk-a")).toBe(false);
    const gets = client.apiGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 3);
    expect(client.apiGet.mock.calls.length).toBe(gets);
    // 没有在跑的：取消什么也不做
    expect(await store.fidCancelCheck("book:bk-a")).toEqual({ ok: false, error: null });
  });

  it("发起请求还在路上就点了取消：拿到作业 id 立刻取消并清掉，不留一条在跑的", async () => {
    vi.useFakeTimers();
    let release;
    client.apiPost.mockImplementationOnce(() => new Promise((resolve) => { release = () => resolve({ job_id: "job-1", job: job("queued") }); }));
    const starting = store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    await vi.advanceTimersByTimeAsync(0);
    expect(store.fidCheck("scene:sc-1").phase).toBe("starting");
    expect(await store.fidCancelCheck("scene:sc-1")).toEqual({ ok: true, pending: true });
    client.apiPost.mockResolvedValueOnce({ job: job("cancelled") });
    release();
    await starting;
    await vi.advanceTimersByTimeAsync(0);
    expect(client.apiPost).toHaveBeenLastCalledWith(`${CHECKS}/job-1/cancel`, {});
    expect(store.fidCheck("scene:sc-1")).toBeNull();
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 2);
    expect(client.apiGet).not.toHaveBeenCalled();
  });

  it("取消时作业已经结束（409 STYLE_REFERENCE_CHECK_NOT_ACTIVE）：把结果拿回来，条目变成完成；作业不在了就记成失败", async () => {
    vi.useFakeTimers();
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("book:bk-a", { text: "一段文字", profileId: "pf-a" });
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("not active"), { code: "STYLE_REFERENCE_CHECK_NOT_ACTIVE", status: 409 }));
    client.apiGet.mockResolvedValue({ job: job("succeeded"), reading: READING });
    const result = await store.fidCancelCheck("book:bk-a");
    expect(result.ok).toBe(false);
    expect(result.error.code).toBe("STYLE_REFERENCE_CHECK_NOT_ACTIVE");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.fidCheck("book:bk-a")).toMatchObject({ phase: "done", reading: { reading_id: "r-9" } });

    client.apiPost.mockResolvedValueOnce({ job_id: "job-2", job: { ...job("running"), job_id: "job-2" } });
    await store.fidStartCheck("book:bk-a", { text: "一段文字", profileId: "pf-a" });
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("gone"), { code: "STYLE_REFERENCE_CHECK_NOT_FOUND", status: 404 }));
    expect((await store.fidCancelCheck("book:bk-a")).ok).toBe(false);
    expect(store.fidCheck("book:bk-a")).toMatchObject({ phase: "failed", error: { code: "STYLE_REFERENCE_CHECK_NOT_FOUND" } });
  });

  it("按作业 id 认领（活动清单里「打开」一次在起草台发起的检查）：GET 一次，在跑就接着轮询到终态；起草台记着目标就借过来", async () => {
    vi.useFakeTimers();
    client.apiGet.mockResolvedValue({ job: job("running"), reading: null });
    const entry = store.fidAdoptJob("book:bk-a", "job-1");
    expect(entry).toMatchObject({ key: "book:bk-a", jobId: "job-1", phase: "running", adopted: true, target: null });
    await vi.advanceTimersByTimeAsync(0);
    expect(client.apiGet).toHaveBeenCalledWith(`${CHECKS}/job-1`);
    expect(store.fidCheck("book:bk-a")).toMatchObject({ phase: "running", job: { phase: "judge" } });
    client.apiGet.mockResolvedValue({ job: job("succeeded"), reading: READING });
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(store.fidCheck("book:bk-a")).toMatchObject({ phase: "done", reading: { reading_id: "r-9" }, adopted: true });
    // 同一个作业再认领一次：不重发请求、结果不动
    const gets = client.apiGet.mock.calls.length;
    store.fidAdoptJob("book:bk-a", "job-1");
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS);
    expect(client.apiGet.mock.calls.length).toBe(gets);
    expect(store.fidCheck("book:bk-a").phase).toBe("done");

    // 起草台（scene: 键）发起的作业：认领到书的键下时把目标借过来，「再查一次」才有的用
    client.apiPost.mockResolvedValueOnce({ job_id: "job-2", job: { ...job("running"), job_id: "job-2" } });
    await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    expect(store.fidCheckByJob("job-2").key).toBe("scene:sc-1");
    expect(store.fidCheckByJob("nope")).toBeNull();
    client.apiGet.mockResolvedValue({ job: { ...job("running"), job_id: "job-2" }, reading: null });
    expect(store.fidAdoptJob("book:bk-a", "job-2")).toMatchObject({ jobId: "job-2", target: { sceneId: "sc-1" }, phase: "running" });
  });

  it("认领的作业已经不在了（404 STYLE_REFERENCE_CHECK_NOT_FOUND）：条目记成失败，不再轮询", async () => {
    vi.useFakeTimers();
    client.apiGet.mockRejectedValue(Object.assign(new Error("gone"), { code: "STYLE_REFERENCE_CHECK_NOT_FOUND", status: 404 }));
    store.fidAdoptJob("book:bk-a", "job-9");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.fidCheck("book:bk-a")).toMatchObject({ phase: "failed", error: { code: "STYLE_REFERENCE_CHECK_NOT_FOUND" } });
    const gets = client.apiGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 4);
    expect(client.apiGet.mock.calls.length).toBe(gets);
  });

  it("按作业 id 取消（活动清单里的一条）：记着它的每个键都清掉；取消失败原样抛给调用方", async () => {
    vi.useFakeTimers();
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("scene:sc-1", { sceneId: "sc-1" });
    client.apiGet.mockResolvedValue({ job: job("running"), reading: null });
    store.fidAdoptJob("book:bk-a", "job-1");
    await vi.advanceTimersByTimeAsync(0);
    client.apiPost.mockResolvedValueOnce({ job: job("cancelled") });
    await store.fidCancelCheckJob("job-1");
    expect(client.apiPost).toHaveBeenLastCalledWith(`${CHECKS}/job-1/cancel`, {});
    expect(store.fidCheck("scene:sc-1")).toBeNull();
    expect(store.fidCheck("book:bk-a")).toBeNull();
    const gets = client.apiGet.mock.calls.length;
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 3);
    expect(client.apiGet.mock.calls.length).toBe(gets);
    client.apiPost.mockRejectedValueOnce(Object.assign(new Error("gone"), { code: "STYLE_REFERENCE_CHECK_NOT_FOUND" }));
    await expect(store.fidCancelCheckJob("job-1")).rejects.toMatchObject({ code: "STYLE_REFERENCE_CHECK_NOT_FOUND" });
  });

  it("跑了 3 分钟还没完：轮询从 1.5 s 放慢到 5 s（有取消可用，不再无上限地紧问）", async () => {
    vi.useFakeTimers();
    client.apiPost.mockResolvedValueOnce({ job_id: "job-1", job: job("running") });
    await store.fidStartCheck("book:bk-a", { text: "一段文字", profileId: "pf-a" });
    client.apiGet.mockResolvedValue({ job: job("running"), reading: null });
    await vi.advanceTimersByTimeAsync(store.FID_POLL_SLOW_AFTER_MS);
    const at3min = client.apiGet.mock.calls.length;
    expect(at3min).toBe(store.FID_POLL_SLOW_AFTER_MS / store.FID_POLL_MS);
    await vi.advanceTimersByTimeAsync(store.FID_POLL_MS * 2);
    expect(client.apiGet.mock.calls.length).toBe(at3min);
    await vi.advanceTimersByTimeAsync(store.FID_POLL_SLOW_MS - store.FID_POLL_MS * 2);
    expect(client.apiGet.mock.calls.length).toBe(at3min + 1);
    expect(store.fidCheck("book:bk-a").phase).toBe("running");
  });
});
