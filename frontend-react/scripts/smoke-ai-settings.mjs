// AI 模型接入冒烟:设置 → AI 模型(WsAiProviders + system-config LLM 接口)。
// 全链不触外网:用「自定义 OpenAI 兼容」免密钥 + 手填模型,走 loopback 免令牌后端。
// 运行:cd frontend && node ../frontend-react/scripts/smoke-ai-settings.mjs [BASE] [API]
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:5174/";
const API = process.argv[3] || "http://127.0.0.1:8009";
let failed = 0;
const errors = [];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.setDefaultTimeout(20_000);
page.on("pageerror", (e) => errors.push(`[pageerror] ${e.message}`));
page.on("dialog", (d) => d.accept());

async function check(label, fn) {
  try { await fn(); console.log("ok:", label); }
  catch (e) { failed++; console.log("FAIL:", label, "—", e.message.split("\n")[0]); }
}

const api = async (p) => (await page.evaluate(async (u) => (await fetch(u)).json(), API + p)).data;
const PROVIDER_ID = "smoke_local_ai";

await page.goto(BASE);
await page.evaluate((apiBase) => {
  localStorage.clear();
  localStorage.setItem("novel-system-api-base", apiBase);
  localStorage.setItem("novel-system-api-base-default", "http://127.0.0.1:8000");
  localStorage.setItem("ws_active_work_v1", "work-a");
}, API);
await page.reload();
await page.waitForSelector(".ws-app");
await page.evaluate(() => { location.hash = "#settings"; });
await page.waitForSelector(".settings-nav");
await page.click('.settings-nav-btn:has-text("AI 模型")');
await page.waitForTimeout(1800);

await check("AI 模型页渲染:接入状态 + 模型服务 + 分工", async () => {
  for (const title of ["接入状态", "模型服务", "模型分工"]) {
    const visible = await page.locator(`.set-section-title:has-text("${title}")`).count();
    if (!visible) throw new Error(`section missing: ${title}`);
  }
});

await check("预设目录:添加流程展示分组与主流厂商", async () => {
  await page.click('button:has-text("添加模型服务")');
  await page.waitForSelector('text=选择厂商或接入方式');
  for (const label of ["国内厂商", "第三方中转", "DeepSeek", "Kimi / Moonshot", "OpenRouter", "自定义 OpenAI 兼容"]) {
    const count = await page.locator(`.set-section:has-text("模型服务") >> text=${label}`).count();
    if (!count) throw new Error(`preset missing: ${label}`);
  }
});

await check("添加服务:自定义兼容 + 免密钥 + 手填模型 → 保存出卡", async () => {
  // 记录添加前的默认服务：产品行为是「原本无默认→新增即默认；原本已有默认→保留不劫持」
  // （见 system_config.save_llm_provider: default_provider_id = 既有 or 新增）。
  // 不预设环境一定是空配置，否则在已配置默认 provider 的 dev/CI 库上会误报失败。
  const prevDefault = (await api("/api/v1/system-config/llm")).default_provider_id || null;
  await page.click('button:has-text("自定义 OpenAI 兼容")');
  await page.waitForSelector('text=服务标识');
  await page.fill('input[placeholder="如 my-deepseek"]', PROVIDER_ID);
  const urlInput = page.locator('.set-row:has-text("接口地址") input');
  await urlInput.fill("http://127.0.0.1:8123/v1");
  await page.click('.seg-btn:has-text("免密钥")');
  await page.fill("textarea", "test-model-a\ntest-model-b");
  await page.click('button:has-text("保存服务")');
  await page.waitForSelector(`.card-flat:has-text("${PROVIDER_ID}")`);
  const overview = await api("/api/v1/system-config/llm");
  const provider = overview.providers[PROVIDER_ID];
  if (!provider) throw new Error("provider not persisted");
  if (provider.credential_mode !== "none") throw new Error(`credential_mode: ${provider.credential_mode}`);
  if (!provider.models.includes("test-model-b")) throw new Error(`models: ${provider.models}`);
  const expectedDefault = prevDefault || PROVIDER_ID; // 已有默认则不变，无默认则新增即默认
  if (overview.default_provider_id !== expectedDefault) throw new Error(`default: ${overview.default_provider_id} (expected ${expectedDefault})`);
});

await check("分工槽位:写作主力 → 该服务/模型,应用后路由生效", async () => {
  const row = page.locator('.set-row:has-text("写作主力")');
  await row.locator("select").first().selectOption(PROVIDER_ID);
  await row.locator("select").nth(1).selectOption("test-model-a");
  await page.click('button:has-text("应用分工")');
  // 应用分工的确认是应用内确认框（ws-notify.jsx），不是浏览器 confirm
  await page.click('[data-testid="ws-confirm-ok"]');
  await page.waitForTimeout(2000);
  const overview = await api("/api/v1/system-config/llm");
  const route = overview.node_routes["neutral_draft"]; // scene_generation 组 ∈ drafting 槽
  if (route.provider_id !== PROVIDER_ID) throw new Error(`provider_id: ${route.provider_id}`);
  if (route.model !== "test-model-a") throw new Error(`model: ${route.model}`);
  const drafting = overview.role_slots.find(s => s.slot_id === "drafting");
  if (drafting.current?.provider_id !== PROVIDER_ID || drafting.current?.mixed) {
    throw new Error(`slot current: ${JSON.stringify(drafting.current)}`);
  }
});

await check("高级路由:展开矩阵 + 缺失路由补齐(或已齐)", async () => {
  await page.click('summary:has-text("高级路由")');
  await page.waitForTimeout(600);
  const before = await api("/api/v1/system-config/llm");
  if (before.missing_active_routes.length > 0) {
    // 系统配置快照跨 reseed 持久,首跑走补齐路径,复跑直接断言已齐
    await page.click('button:has-text("用默认服务补齐")');
    await page.waitForTimeout(2000);
  }
  const overview = await api("/api/v1/system-config/llm");
  if (overview.missing_active_routes.length !== 0) {
    throw new Error(`still missing: ${overview.missing_active_routes.slice(0, 5)}`);
  }
  // 此检查只验证路由矩阵已配置完整。全局 readiness 还会受历史路由所指服务是否
  // 存在/启用/可解密影响，不应与“缺失路由补齐”混为一个断言。
  if (overview.readiness.configured_route_count !== overview.readiness.active_route_count) {
    throw new Error(`configured routes: ${overview.readiness.configured_route_count}/${overview.readiness.active_route_count}`);
  }
});

await check("连接测试:不可达地址返回失败但不崩", async () => {
  const card = page.locator(`.card-flat:has-text("${PROVIDER_ID}")`);
  await card.locator('button:has-text("测试连接")').click();
  // 后端探活允许最多 30 秒。等待真实状态转换，避免用固定 sleep 把正常的慢失败误报成 UI 缺陷。
  await card.getByText(/^✕\s/).waitFor({ state: "visible", timeout: 40_000 });
});

await check("删除服务:卡片原位删除 → 后端配置移除(路由留作 orphan 待补齐)", async () => {
  const card = page.locator(`.card-flat:has-text("${PROVIDER_ID}")`);
  await card.locator('button:has-text("删除")').click();
  await page.click('[data-testid="ws-confirm-ok"]'); // 应用内确认框
  await page.waitForTimeout(1800);
  if (await page.locator(`.card-flat:has-text("${PROVIDER_ID}")`).count()) throw new Error("card still visible");
  const overview = await api("/api/v1/system-config/llm");
  if (overview.providers[PROVIDER_ID]) throw new Error("provider still in overview");
  // 复跑收敛:下一轮「添加服务」用同 id 重建,orphan 路由随之恢复 ready
});

await browser.close();
const uniq = [...new Set(errors)];
if (uniq.length) { console.log(`\n${uniq.length} page errors:`); uniq.slice(0, 10).forEach(e => console.log(" -", e.slice(0, 300))); }
process.exitCode = failed || uniq.length ? 1 : 0;
console.log(failed ? `\n${failed} checks failed` : "\nall checks passed");
