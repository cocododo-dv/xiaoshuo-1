/* ==========================================================
   设置 · AI 模型的纯函数（无 React、无 store）：错误文案、服务健康、路由没就绪的原因键。
   服务卡片（ws-settings-ai-providers.jsx）和接入状态 / 分工 / 高级路由（ws-settings-ai-routes.jsx）都用；
   单独成叶子模块，免得两边互相 import 成环。
   ========================================================== */

/* 后端的 readiness_reason 形如 "secret_decrypt_failed:openai"，冒号前是原因键 */
export const reasonKey = (reason) => {
  const text = String(reason || "");
  const at = text.indexOf(":");
  return at >= 0 ? text.slice(0, at) : text;
};

export function errText(error, fallback) {
  if (!error) return fallback;
  if (error.code === "ADMIN_TOKEN_REQUIRED") return "管理令牌缺失或不正确。在上方「接入状态」里填入后端的管理令牌后再试。";
  if (error.code === "CONFIG_SECRET_REQUIRED") return "后端没有本机配置密钥，存不了 API 密钥。请用启动脚本重新启动后端（它会自动生成并保存这把钥匙）后再试。";
  return error.message || fallback;
}

/* 服务健康：一张卡片上最多给出几枚状态标签 */
export function providerHealth(provider) {
  const tags = [];
  const needsKey = provider.credential_mode !== "none";
  const secret = provider.secret || {};
  if (provider.enabled === false) tags.push({ tone: "neutral", text: "已停用" });
  if (needsKey && secret.configured && secret.decryptable === false) tags.push({ tone: "danger", text: "密钥不可用" });
  else if (needsKey && !secret.configured) tags.push({ tone: "warn", text: "未填密钥" });
  if (provider.enabled !== false && (!Array.isArray(provider.models) || provider.models.length === 0)) tags.push({ tone: "warn", text: "没有模型" });
  return tags;
}
export function providerUsable(provider) {
  if (!provider || provider.enabled === false) return false;
  if (provider.credential_mode === "none") return true;
  const secret = provider.secret || {};
  return !!secret.configured && secret.decryptable !== false;
}
