"""正史核对（成稿中心的「正史」）：终稿里抽出 / 作者手填的事实候选 → 作者采纳 → 确认本场 → 进运行时重放。

包的 ``__init__`` 不转出任何名字；``services/canon_continuity.CanonContinuityService`` 是门面，由这里的几个混入类组成：
``lifecycle``（归档、暂存抽取）、``decisions``（候选与核对）、``revisions``（版本更替时的退役）、``snapshots``（读模型与
连续性快照）、``entities``（候选指的是谁）、``prompt``（起草提示里最近的正史变化）、``common``（定位与校验）。
"""
