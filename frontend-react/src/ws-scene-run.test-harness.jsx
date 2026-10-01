// 起草台单测的共用装配（2026-09-29 从 ws-scene-run.test.jsx 拆出）。文件名以 .test-harness.jsx 结尾：
// vitest 只收 *.test.js / *.test.jsx，不会把它当测试跑；名字里没有 ".test."，设计守卫与模块守卫
// 照样把它当源码扫（与 test-helpers.js 一样）。
// 调用方自己写 vi.mock("./lib/client.js") 与 vi.mock("./ws-scene-job-api.js")（vi.mock 按文件提升）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, vi } from "vitest";
import { installApiRouter, DEFAULT_CHAP, DEFAULT_PROJECT, settleActiveWork } from "./test-helpers.js";

export const T = { timeout: 5000, interval: 25 };

export const RUN_STATES_URL = /^\/api\/v1\/scene-run-states\?/;
export const NON_DEMO_PROJECT = { ...DEFAULT_PROJECT, project_id: "novel-1", title: "回归小说" };
export const TWO_SCENE_CHAP = {
  ...DEFAULT_CHAP,
  scenes: [
    ...DEFAULT_CHAP.scenes,
    {
      ...DEFAULT_CHAP.scenes[0],
      slug: "ch01s2",
      scene_id: "s2",
      title: "回潮",
      brief: { goal: "追上证人", conflict: "潮水封路", setback: "证人失踪" },
    },
  ],
};

export async function settleActive(projectId = "prj-main") {
  await settleActiveWork(projectId, T);
}

/* 在 installApiRouter 之上叠一层 scene-run-states 路由（贯通轮惯用法：包装现有实现） */
export function routeRunStates(client, responder) {
  const base = client.apiGet.getMockImplementation();
  client.apiGet.mockImplementation((url) => {
    if (RUN_STATES_URL.test(url)) return responder(url);
    return base(url);
  });
}

export async function loadSceneRun(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
  // 场景采用依赖真实 author-draft 元数据；默认模拟器也应返回 draft/revision，
  // 避免测试继续走已经删除的“没有服务端作者稿也照样归档”旧路径。
  const basePost = client.apiPost.getMockImplementation();
  client.apiPost.mockImplementation((url, body, options) => {
    if (/\/api\/v1\/author-drafts\/scene\/s1\/ensure$/.test(url)) {
      return Promise.resolve({
        draft: {
          draft_id: "author_draft_scene_s1",
          revision_no: 1,
          content: "",
          last_promoted_revision_no: null,
          last_promoted_final_scene_row_id: null,
          canonical_dirty: true,
        },
        runtime_final_ref: null,
      });
    }
    return basePost(url, body, options);
  });
  const mod = await import("./ws-scene-run.jsx");
  await settleActive((opts && opts.projects && opts.projects[0] && opts.projects[0].project_id) || "prj-main");
  // 任务控制条的两个请求（mock）并进 client，测试照旧写 client.getLatestSceneRunJob
  const { cancelRunJob, getLatestSceneRunJob } = await import("./ws-scene-job-api.js");
  return { mod, client: { ...client, cancelRunJob, getLatestSceneRunJob } };
}

export const mountedRoots = [];

export async function renderRunJobControl(Component, props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mountedRoots.push({ root, host });
  await act(async () => {
    root.render(<Component {...props} />);
  });
  return {
    host,
    rerender: async (nextProps) => {
      await act(async () => {
        root.render(<Component {...nextProps} />);
      });
    },
    unmount: async () => {
      const index = mountedRoots.findIndex((item) => item.root === root);
      if (index >= 0) mountedRoots.splice(index, 1);
      await act(async () => root.unmount());
      host.remove();
    },
  };
}

export async function click(element) {
  await act(async () => {
    element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
}

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

export async function queueSceneIntent(detail) {
  const { queueViewIntent } = await import("./ws-view-intents.js");
  queueViewIntent("scene", "ws:scene-enqueue", detail);
}
