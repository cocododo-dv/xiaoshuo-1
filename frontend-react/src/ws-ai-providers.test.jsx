// WsAiProviders store 层单测:管理面「写后重拉」契约。
// 覆盖:refresh 只拉 /llm 单接口(adminConfigured 来自 overview.runtime,不再全量拉
// /system-config 历史)、saveProvider/deleteProvider 写后重拉、删除失败上抛且 busy 复位。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiAdminGet: vi.fn(),
  apiAdminPost: vi.fn(),
  apiAdminDelete: vi.fn(),
}));

const OVERVIEW = {
  runtime: { admin_configured: true, secret_configured: true },
  providers: { my_openai: { provider_type: "openai", base_url: "https://api.openai.example/v1" } },
  readiness: { ready: true },
};

async function loadStore() {
  const client = await import("./lib/client.js");
  client.apiGet.mockResolvedValue(OVERVIEW);
  const mod = await import("./ws-ai-providers.jsx");
  return { client, mod };
}

describe("WsAiProviders store(AI 模型接入)", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => vi.restoreAllMocks());

  it("refresh:只拉 /llm 一个接口,adminConfigured 取自 overview.runtime", async () => {
    const { client, mod } = await loadStore();
    await mod.WsAiProviders.refresh();
    expect(client.apiGet).toHaveBeenCalledTimes(1);
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/system-config/llm");
    const state = mod.WsAiProviders.state();
    expect(state.loaded).toBe(true);
    expect(state.adminConfigured).toBe(true);
    expect(state.overview.providers.my_openai).toBeTruthy();
  });

  it("saveProvider:POST 后重拉 overview(写后重拉,无乐观更新)", async () => {
    const { client, mod } = await loadStore();
    client.apiAdminPost.mockResolvedValueOnce({ provider: { provider_id: "my_openai" } });
    await mod.WsAiProviders.saveProvider({ provider_id: "my_openai", provider_type: "openai" });
    expect(client.apiAdminPost).toHaveBeenCalledWith(
      "/api/v1/system-config/llm/providers",
      expect.objectContaining({ provider_id: "my_openai" }),
      expect.any(String),
    );
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/system-config/llm");
    expect(mod.WsAiProviders.state().busy["save:my_openai"]).toBeUndefined();
  });

  it("saveProvider:只发后端收的字段——overview 项里只读的 secret 不上行,表单填的 api_key 照发", async () => {
    // 卡片上的「启用」开关拿 overview 里那一项原样改 enabled 再存;后端请求模型 extra=forbid,带着 secret 就 422
    const { client, mod } = await loadStore();
    client.apiAdminPost.mockResolvedValue({ provider: { provider_id: "relay_a" } });
    const overviewEntry = {
      provider_id: "relay_a", provider_type: "openai", account_id: null, base_url: "http://relay.example/v1",
      enabled: true, credential_mode: "api_key", api_mode: "responses", models: ["model-x"], provider_options: {},
      secret: { configured: true, decryptable: true, hint: "sk-...WXYZ" },
    };
    await mod.WsAiProviders.saveProvider({ ...overviewEntry, enabled: false });
    const [url, body, token] = client.apiAdminPost.mock.calls[0];
    expect(url).toBe("/api/v1/system-config/llm/providers");
    expect(body).toEqual({
      provider_id: "relay_a", provider_type: "openai", account_id: null, base_url: "http://relay.example/v1",
      enabled: false, credential_mode: "api_key", api_mode: "responses", models: ["model-x"], provider_options: {},
    });
    expect(body).not.toHaveProperty("secret");
    expect(typeof token).toBe("string");
    expect(mod.WsAiProviders.state().busy["save:relay_a"]).toBeUndefined();

    await mod.WsAiProviders.saveProvider({ provider_id: "relay_a", provider_type: "openai", api_key: "sk-new", enabled: true, models: [] });
    expect(client.apiAdminPost.mock.calls[1][1]).toEqual({
      provider_id: "relay_a", provider_type: "openai", api_key: "sk-new", enabled: true, models: [],
    });
  });

  it("deleteProvider:DELETE 正确地址 + 清本地探活结果 + 重拉 overview", async () => {
    const { client, mod } = await loadStore();
    client.apiAdminPost.mockResolvedValueOnce({ ok: true, message: "pong" });
    await mod.WsAiProviders.probe("my_openai").catch(() => {});
    expect(mod.WsAiProviders.state().probes.my_openai).toBeTruthy();

    client.apiAdminDelete.mockResolvedValueOnce({
      deleted_provider_id: "my_openai",
      orphaned_route_node_ids: ["project_outline_plan"],
    });
    const result = await mod.WsAiProviders.deleteProvider("my_openai");
    expect(client.apiAdminDelete).toHaveBeenCalledWith(
      "/api/v1/system-config/llm/providers/my_openai",
      expect.any(String),
    );
    expect(result.orphaned_route_node_ids).toEqual(["project_outline_plan"]);
    expect(mod.WsAiProviders.state().probes.my_openai).toBeUndefined();
    expect(client.apiGet).toHaveBeenCalledWith("/api/v1/system-config/llm");
    expect(mod.WsAiProviders.state().busy["delete:my_openai"]).toBeUndefined();
  });

  it("deleteProvider 失败:错误上抛供视图 flash,busy 复位,不触发重拉", async () => {
    const { client, mod } = await loadStore();
    const callsBefore = client.apiGet.mock.calls.length;
    client.apiAdminDelete.mockRejectedValueOnce(Object.assign(new Error("not found"), { code: "CONFIG_PROVIDER_NOT_FOUND" }));
    await expect(mod.WsAiProviders.deleteProvider("ghost")).rejects.toThrow("not found");
    expect(mod.WsAiProviders.state().busy["delete:ghost"]).toBeUndefined();
    expect(client.apiGet.mock.calls.length).toBe(callsBefore);
  });

  it("管理令牌只读 sessionStorage：旧版 localStorage 里的那一份不再搬过来，但照旧抹掉（批准 #25，重评 R16）", async () => {
    window.localStorage.setItem("novel-system-admin-token", "legacy-token");
    const { mod } = await loadStore();

    expect(mod.WsAiProviders.adminToken()).toBe("");
    expect(window.localStorage.getItem("novel-system-admin-token")).toBeNull();
    expect(window.sessionStorage.getItem("novel-system-admin-token")).toBeNull();

    mod.WsAiProviders.setAdminToken("session-token");
    expect(mod.WsAiProviders.adminToken()).toBe("session-token");
    expect(window.sessionStorage.getItem("novel-system-admin-token")).toBe("session-token");
    expect(window.localStorage.getItem("novel-system-admin-token")).toBeNull();

    // 会话里有令牌时，localStorage 里再冒出来的旧值同样抹掉、不取代会话里的
    window.localStorage.setItem("novel-system-admin-token", "stale-token");
    expect(mod.WsAiProviders.adminToken()).toBe("session-token");
    expect(window.localStorage.getItem("novel-system-admin-token")).toBeNull();
  });
});
