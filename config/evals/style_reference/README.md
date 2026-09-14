# 风格参考评测清单

本目录现在只保留 **RAG 内容克制 A/B 清单** `rag_content_independence_v1.json`：
`services/style_reference/rag_evaluation.py` 用它做 Strategy C（RAG）检索机制的确定性回归
（`backend/tests/test_style_reference_rag.py`、`test_style_reference_rag_chroma.py`）——
「异题材同风格 vs 同题材异风格」的合成 A/B，只证明检索按风格签名而不是题材词召回，
不是风格贴合度结论（`policy_evidence_eligible: false`）。

2026-09-14 减法：跨内容基准包（`services/style_reference/benchmark/`、
`style_benchmark_v1.public.json` / `.private.json`、`tools.style_reference_benchmark` CLI）已删除。
它自 2026-09 批次 1 起没有 CLI、没有路由、从未用真实模型跑过；「像不像」由作者阅读评测
（见 `docs/style-fidelity-fixes-2026-09-14.md` §9）。
