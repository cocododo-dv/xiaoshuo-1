"""接口错误码 → 作者看的中文说明（B12-04，批准 #27）。

界面（React）按错误码分支，从不按报错文字分支；没有专门处理的错误就把信封里的 ``message`` 直接显示给作者。以前 600
多处 ``DomainError`` 的说明是英文，作者看到的是「verified Q0/Q1 findings block adoption…」这样的句子。现在异常处理器
回信封时按这张表换成中文：

- 原来的说明已经是中文（有汉字）的原样保留——那是写的人专门写给作者的，往往比这里的通用说法具体；
- 表里的说明带 ``{message}`` 的，把原来的英文细节（节点名、模型名、字段名这类标识）放进括号里，别的整句替换；
- 被换掉的英文原文只在 ``NOVEL_SYSTEM_EXPOSE_ERROR_DETAIL`` 打开时放进 ``details.debug_message``。

守卫 ``tests/test_error_catalog.py``：源码里每个带英文说明的 ``DomainError`` 错误码都要在这张表里；表里的说明都是中文；
表里不留已经没人抛的错误码。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[一-鿿]")

# 外壳自己发的错误（校验 / 数据库 / 未处理异常 / 访问边界 / 请求体上限）与领域错误共用一张表。
ERROR_MESSAGES: dict[str, str] = {
    # ---- 外壳 ----
    "REQUEST_VALIDATION_FAILED": "请求内容不符合接口要求，没有执行。",
    "DATABASE_BUSY": "数据库正忙（有一个长操作还在进行），请稍后重试。",
    "DATABASE_OPERATION_FAILED": "数据库操作失败。如果刚更新过程序，请重启后端（启动脚本会自动升级数据库）。",
    "INTERNAL_ERROR": "后端出了意外错误，这次操作没有完成；请重试，仍然失败请查看后端日志。",
    "SERVICE_NOT_READY": "后端还没准备好：数据库不可用或结构需要升级，请重启后端。",
    "REMOTE_ACCESS_DISABLED": "后端只接受本机访问。",
    "REMOTE_ACCESS_TOKEN_REQUIRED": "需要有效的远程访问令牌。",
    "REQUEST_BODY_TOO_LARGE": "提交的内容太大，超过了后端的上限。",
    "INVALID_CONTENT_LENGTH": "请求的内容长度不合法。",
    "ADMIN_TOKEN_REQUIRED": "需要管理令牌：没有配置令牌时只接受本机访问。",
    "TEXT_ENCODING_INVALID": "输入的文字像是乱码或没有正确解码，请粘贴 UTF-8 / GB18030 文本。",
    # ---- 幂等 ----
    "IDEMPOTENCY_KEY_REQUIRED": "请求缺少幂等键（X-Idempotency-Key），没有执行。",
    "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD": "同一个请求编号带了不同的内容，已拒绝；请重新操作一次。",
    "IDEMPOTENCY_REQUEST_IN_PROGRESS": "同一个操作还在进行，请等它完成后再试。",
    "RUN_OWNER_LEASE_LOST": "这次操作的执行权已被另一个请求接走，结果以那一次为准；请刷新后查看。",
    # ---- 作品 / 大纲 / 章 / 场 ----
    "PROJECT_NOT_FOUND": "找不到这部作品。",
    "PROJECT_TRASHED": "这部作品在回收站里，先恢复它。",
    "PROJECT_NOT_TRASHED": "作品要先移入回收站才能彻底删除。",
    "PROJECT_OUTLINE_REQUIRED": "新建作品需要一段大纲文字。",
    "PROJECT_TITLE_REQUIRED": "作品标题不能为空。",
    "PROJECT_CHAPTER_NOT_FOUND": "这一章不属于这部作品。",
    "PROJECT_CHAPTER_NOT_CURRENT": "只有作品当前的这一章可以这样操作（{message}）。",
    "PROJECT_OWNERSHIP_CONFLICT": "这一场的归属与它所在的章对不上，请刷新后再试。",
    "PROJECT_OWNERSHIP_UNRESOLVED": "这一场不属于任何作品，无法继续。",
    "OUTLINE_PLAN_NOT_FOUND": "找不到这份大纲计划。",
    "OUTLINE_PLAN_NOT_REVIEWABLE": "这份大纲计划不在待确认状态。",
    "OUTLINE_PLAN_EMPTY": "这份大纲计划里没有章。",
    "OUTLINE_PLAN_INVALID": "大纲计划里有一章缺少章编号。",
    "CHAPTER_NOT_FOUND": "找不到这一章。",
    "CHAPTER_TRASHED": "这一章在回收站里，先恢复它。",
    "CHAPTER_ALREADY_OWNED": "这一章属于另一部作品。",
    "CHAPTER_IDENTITY_IMMUTABLE": "已有的章不能改到另一部作品或另一份大纲下。",
    "CHAPTER_DISPLAY_ORDER_CONFLICT": "另一章已经用了这个章序。",
    "CHAPTER_PROJECT_REQUIRED": "这一章不属于任何作品，不能发布正史。",
    "SCENE_NOT_FOUND": "找不到这一场。",
    "SCENE_TRASHED": "这一场在回收站里，先恢复它。",
    "SCENE_ALREADY_OWNED": "这一场属于另一部作品。",
    "SCENE_IDENTITY_IMMUTABLE": "已有的场景不能改到另一章、另一部作品或另一份大纲下。",
    "SCENE_SEQUENCE_CONFLICT": "这一章里已经有别的场用了这个场序。",
    "SCENE_STATE_NOT_FOUND": "这一场还没有运行记录。",
    "SCENE_AUTHOR_NOTES_CONFLICT": "这一场的作者笔记在别处改过了，请刷新后再保存。",
    "SCENE_DEEP_REVIEW_PREFERENCES_CONFLICT": "深改面板的设置在别处改过了，请刷新后再保存。",
    "WRITER_BRIEF_INVALID": "写作简报的格式不对（{message}）。",
    "AUTHOR_NOTE_INVALID": "改写指令必须是文字。",
    "AUTHOR_NOTE_TOO_LONG": "改写指令太长了，请精简后再提交。",
    # ---- 目录（章节编排）----
    "CATALOG_APPROVED_CHAPTER_ORDER_INCONSISTENT": "已定稿章的先后顺序对不上，先重开定稿再调整。",
    "CATALOG_APPROVED_CHAPTER_ORDER_LOCKED": "已定稿的章不能改变先后顺序或位置。",
    "CATALOG_APPROVED_CHAPTER_REOPEN_REQUIRED": "已定稿的章要先重开定稿，才能改它的状态。",
    "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW": "章的定稿只能走成稿中心的「确认定稿」。",
    "CATALOG_CHAPTER_ORDER_DUPLICATE": "章的顺序里有重复的章。",
    "CATALOG_CHAPTER_ORDER_INCOMPLETE": "章的顺序必须包含这部作品的每一章，且每章只出现一次。",
    "CATALOG_CHAPTER_ORDER_PROJECT_MISMATCH": "章的顺序里混进了不属于这部作品的章。",
    "CATALOG_IMPORT_APPROVAL_ORDER_INVALID": "导入的目录里，已定稿的章必须连续排在最前面。",
    "CATALOG_IMPORT_CURRENT_INVALID": "导入的目录最多只能有一个当前章，而且它不能已经定稿。",
    "CATALOG_IMPORT_EMPTY": "导入的目录里没有章。",
    "CATALOG_NOT_EMPTY": "只能向空目录导入。",
    "CATALOG_POV_CHARACTER_NOT_FOUND": "这部作品里没有这个视角人物。",
    "CATALOG_STATE_INVALID": "状态不在可选的范围里（{message}）。",
    "SCENE_ORDER_CHAPTER_MISMATCH": "排序里的场必须都属于同一章。",
    "SCENE_ORDER_DUPLICATE": "场的顺序里有重复的场。",
    "SCENE_ORDER_INCOMPLETE": "场的顺序必须包含这一章的每一场。",
    "SCENE_ORDER_INVALID": "场的顺序不能为空。",
    "SCENE_ORDER_LAST_SCENE_INVALID": "指定的章末一场不在排序里。",
    # ---- 资料库 ----
    "LIBRARY_CHARACTER_IN_USE": "这个人物还被构思或起草用着，不能删除。",
    "LIBRARY_CHARACTER_NOT_FOUND": "这部作品里没有这个人物。",
    "LIBRARY_ENTITY_KIND_INVALID": "条目类型不在可选的范围里（{message}）。",
    "LIBRARY_ENTITY_NAME_REQUIRED": "名称不能为空。",
    "LIBRARY_ENTITY_NOT_FOUND": "找不到这个资料条目。",
    "LIBRARY_ENTITY_STATUS_INVALID": "条目状态不在可选的范围里（{message}）。",
    "LIBRARY_RELATION_CHARACTER_NOT_FOUND": "关系的一端人物不在这部作品里。",
    "LIBRARY_RELATION_NOT_FOUND": "找不到这条关系。",
    "LIBRARY_RELATION_REF_INVALID": "关系两端的写法不对（要么是条目，要么是人物）。",
    "LIBRARY_RELATION_SELF_LOOP": "关系的两端不能是同一个。",
    "TIMELINE_EVENT_NOT_FOUND": "找不到这条时间线事件。",
    "TIMELINE_LABEL_REQUIRED": "时间线事件需要一个名字。",
    "TIMELINE_REALIZED_EVENT_IMMUTABLE": "这条时间线事件已经写进正史，不能删除。",
    # ---- 回收站 ----
    "TRASH_ENTRY_INVALID": "回收站条目编号不对。",
    "TRASH_OPERATION_BLOCKED": "回收站操作被拦下了（{message}）。",
    # ---- 待办 ----
    "REVIEW_NOT_FOUND": "找不到这张待办卡片。",
    "REVIEW_PROJECT_REQUIRED": "需要指明是哪部作品的待办。",
    "REVIEW_STATE_INVALID": "待办的筛选只能是「待处理」或「稍后提醒」。",
    "REVIEW_ACTION_INDEX_INVALID": "卡片上没有这个动作。",
    "REVIEW_CARD_KIND_INVALID": "待办卡片的类型不在可选的范围里（{message}）。",
    "REVIEW_CARD_TITLE_REQUIRED": "待办卡片需要标题。",
    "REVIEW_EFFECT_INVALID": "这个卡片动作缺少必要的信息（{message}）。",
    "REVIEW_EFFECT_PROJECT_REQUIRED": "这个卡片动作需要在一部作品里执行。",
    "REVIEW_EFFECT_UNKNOWN": "不认识这个卡片动作（{message}）。",
    # ---- 作者稿 / 权威正文 ----
    "AUTHOR_DRAFT_CONFLICT": "作者稿在别处改过了，请刷新后再操作。",
    "AUTHOR_DRAFT_EMPTY": "空的作者稿不能成为正文。",
    "AUTHOR_DRAFT_INVALID": "作者稿的正文必须是文字。",
    "AUTHOR_DRAFT_NOT_CURRENT": "这不是这一场当前的作者稿，请刷新后再操作。",
    "AUTHOR_DRAFT_NOT_FOUND": "找不到这份作者稿。",
    "AUTHOR_DRAFT_PROMOTION_INVALID": "晋升正文的请求缺少或带错了参数（{message}）。",
    "AUTHOR_DRAFT_PROMOTION_SCOPE_UNSUPPORTED": "目前只有场景的作者稿可以晋升为正文。",
    "AUTHOR_DRAFT_PROPOSAL_MODE_UNSUPPORTED": "只支持「AI 续写」这一种候选。",
    "AUTHOR_DRAFT_REVISION_NOT_FOUND": "找不到作者稿的这个历史版本。",
    "AUTHOR_DRAFT_SAVE_INCOMPLETE": "作者稿保存后没有得到版本号，采纳没有继续；请重试。",
    "AUTHOR_DRAFT_SCENE_MISMATCH": "这份作者稿不属于正在采纳的这一场。",
    "AUTHOR_DRAFT_TARGET_INVALID": "作者稿只能属于场景。",
    "AUTHOR_DRAFT_TOO_LARGE": "作者稿太长，超过了正文的上限。",
    "AUTHOR_PROPOSAL_OUTPUT_INVALID": "模型这次没有给出可用的续写，请再试一次。",
    "CANONICAL_BASE_CONFLICT": "这一场的正文在别处改过了，请刷新并对比后再晋升。",
    "CANONICAL_BASE_DETACHED": "这一场当前的终稿与作者稿对不上，请刷新后再操作。",
    "CANONICAL_NARRATIVE_EFFECT_INVALID": "晋升时的事实影响说明不对。",
    "FINAL_SCENE_NOT_FOUND": "这一场指向的终稿不见了。",
    "FINAL_SCENE_CONTENT_HASH_MISMATCH": "要归档的正文与存下的终稿不一致，归档没有进行；请刷新后重试。",
    "FINAL_TEXT_BUNDLE_INTEGRITY_FAILED": "这一稿的起草资料包与记录的指纹对不上，不能归档；请重新起草这一场。",
    "FINAL_TEXT_CONTINUITY_BLOCKED": "核实过的连续性问题挡住了归档：先改稿，或先确认相关的正史事实。",
    "FINAL_TEXT_EMPTY": "空的正文不能归档。",
    "FINAL_TEXT_GATE_HASH_MISMATCH": "成稿检查看的不是这一份正文，请刷新后重试。",
    "CONTENT_SAFETY_REVIEW_REQUIRED": "内容风险检查要求你先确认，再归档。",
    "SOURCE_SAFETY_BLOCKED": "这段文字和绑定的参考书原文重合太多，已拦下；草稿都还在，改过之后再试。",
    # ---- 成稿中心 / 定稿 ----
    "CHAPTER_CANONICAL_MANUSCRIPT_INCOMPLETE": "这一章还有场没有正文，不能审阅或定稿。",
    "CHAPTER_APPROVAL_NOTES_TOO_LONG": "定稿备注不能超过 2000 字。",
    "CHAPTER_FINAL_NOT_APPROVED": "只有已经定稿的章才能重开。",
    "CHAPTER_FINAL_READ_CONFIRM_REQUIRED": "请先通读并确认这一章当前的正文，再定稿。",
    "CHAPTER_FINAL_READ_CONFIRM_UNAVAILABLE": "这一章现在还没有可以通读的正文。",
    "CHAPTER_READ_CONFIRM_INVALID": "「已通读」需要带上读到的那一份正文的指纹，请刷新后再确认。",
    "CHAPTER_READ_CONFIRM_NOTE_TOO_LONG": "通读备注不能超过 1000 字。",
    "CHAPTER_REOPEN_REASON_INVALID": "重开定稿需要一句 1 到 1000 字的理由。",
    "CHAPTER_NEAR_FINAL_SOURCE_MISSING": "章级准终稿评审需要这一章已有终稿正文。",
    "LLM_DISABLED_FOR_CHAPTER_RUN": "还没有启用模型：先在系统设置里配好模型，再运行本章。",
    # ---- 起草（场景运行 / 任务 / 候选终选 / 预算）----
    "INVALID_RUN_POLICY": "运行方式只能是「可靠」或「严格」。",
    "RUN_CHECKPOINT_CONTROL_FORBIDDEN": "场景只能从服务器保存的检查点续跑，不能手动指定起点。",
    "RUN_BUDGET_RESUME_UNAVAILABLE": "没有可以追加预算后续跑的检查点。",
    "RUN_CHECKPOINT_CORRUPT": "这一场的续跑检查点已损坏，请重新起草这一场。",
    "RUN_CHECKPOINT_OUTPUT_MISSING": "检查点记录的产出不见了，请重新起草这一场。",
    "RUN_EXECUTION_CANCELLED": "这次起草已经取消。",
    "RUN_EXECUTION_IN_PROGRESS": "这一场正在另一次起草里运行，请等它结束。",
    "RUN_EXECUTION_SUPERSEDED": "这次起草已被更新的一次接替。",
    "RUN_INPUT_MISMATCH": "改写指令与这次起草开始时的不一致；换指令请重新起草。",
    "RUN_JOB_CANCELLED_BY_AUTHOR": "这次起草已按你的要求取消。",
    "RUN_JOB_CANCEL_CONFLICT": "这个起草任务已经结束，不能再取消。",
    "RUN_JOB_IN_PROGRESS": "这一场已经有一个起草任务在运行，请等它结束。",
    "RUN_JOB_NOT_CLAIMABLE": "这个起草任务已经不能再开始运行。",
    "RUN_JOB_NOT_FOUND": "找不到这个起草任务。",
    "RUN_SELECTION_RESUME_IN_PROGRESS": "终选后的续跑已经在进行。",
    "RUN_SELECTION_WAIT": "这一场在等你终选，选完会自动续跑。",
    "RESUME_EXECUTION_NOT_FOUND": "没有可以续跑的起草记录，请重新起草这一场。",
    "RESUME_NOT_AVAILABLE": "这一场没有停在终选门，不需要续跑。",
    "NEUTRAL_DRAFT_REPAIR_INVALID": "首稿修复一次后仍不合格，这次起草没有继续；请重新起草。",
    "SCENE_EXECUTION_CONTRACT_BLOCKED": "场景卡还缺起草需要的内容（{message}）。",
    "SCENE_BLUEPRINT_INVALID": "模型给出的场景蓝图不完整（{message}），请重新起草。",
    "CANDIDATE_NOT_FOUND": "这份候选稿不属于这一场（可能已被新一轮起草替换），请刷新。",
    "CANDIDATE_NOT_IN_GATE": "这份候选稿不在这次终选的候选里。",
    "SELECTION_LOCKED": "终选已经提交，不能改选；想换一稿，请重新起草这一场。",
    "SELECTION_REQUIRED": "关键场景要先读完候选、做出终选，才能继续。",
    "HARD_BLOCKED": "核实过的硬问题挡住了采纳：先解决或改稿，再归档。",
    "NO_VALID_DRAFT": "这一场还没有可采纳的正文：先起草，或在写作台写好。",
    "INVALID_BUDGET_TOPUP": "追加预算的数量不合法：都要是非负整数，至少一项大于 0。",
    # ---- 写作台深改 ----
    "PASSAGE_PATCH_INVALID": "局部改写的请求缺少必要的信息（{message}）。",
    "PASSAGE_PATCH_NOT_FOUND": "找不到这份局部改写。",
    "PASSAGE_PATCH_OPTION_NOT_FOUND": "找不到选中的这个改写方案。",
    "WRITER_PASSAGE_PATCH_OUTPUT_INVALID": "模型这次的改写没有可追溯的调用记录，已丢弃；请再试一次。",
    # ---- 文学质量 ----
    "LITERARY_QUALITY_CHAPTER_NOT_FOUND": "找不到这一章。",
    "LITERARY_QUALITY_CHAPTER_SET_REQUIRED": "请至少选一章。",
    "LITERARY_QUALITY_LAYER_INVALID": "不支持这种正文层。",
    "LITERARY_QUALITY_RISK_TYPE_INVALID": "不支持这种风险维度。",
    "LITERARY_QUALITY_SEVERITY_INVALID": "不支持这个严重程度。",
    "LITERARY_QUALITY_TEXT_REQUIRED": "请提供要分析的文字。",
    # ---- 正史 / 叙事位置 ----
    "NARRATIVE_CURSOR_CHAPTER_NOT_FOUND": "找不到这一场所在的章。",
    "NARRATIVE_CURSOR_PROJECT_MISMATCH": "这一场与作品对不上，请刷新后再试。",
    "NARRATIVE_CURSOR_SCENE_NOT_FOUND": "找不到这一场。",
    # ---- 雪花构思 ----
    "SNOWFLAKE_LLM_CALL_FAILED": "模型调用失败（{message}）。",
    "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA": "模型返回的内容格式不对，请再生成一次（{message}）。",
    "SNOWFLAKE_LLM_ROUTE_OR_PROMPT_MISSING": "这一步的模型分工或提示词没有配置（{message}）。",
    "SNOWFLAKE_SCENES_REQUIRED": "还没有场景，先在第 09 步列出场景。",
    # ---- 系统配置 ----
    "CONFIG_CATEGORY_UNSUPPORTED": "不支持这类配置（{message}）。",
    "CONFIG_PROVIDER_INVALID": "服务商配置有误（{message}）。",
    "CONFIG_PROVIDER_NOT_FOUND": "找不到这个服务商（{message}）。",
    "CONFIG_PROVIDER_UNSUPPORTED": "不支持这个服务商类型（{message}）。",
    "CONFIG_ROLE_ASSIGNMENTS_REQUIRED": "请至少给一个角色分配模型。",
    "CONFIG_ROLE_ASSIGNMENT_INVALID": "角色分工的写法不对（{message}）。",
    "CONFIG_ROLE_SLOT_UNKNOWN": "不认识这个角色（{message}）。",
    "CONFIG_ROUTE_INVALID": "模型分工配置有误（{message}）。",
    "CONFIG_ROUTE_MODEL_MISSING": "模型分工用了服务商没有列出的模型（{message}）。",
    "CONFIG_ROUTE_PROVIDER_MISSING": "模型分工引用了不存在的服务商（{message}）。",
    "CONFIG_ROUTE_PROVIDER_NOT_READY": "模型分工引用的服务商还没启用或没配好密钥（{message}）。",
    "CONFIG_SECRET_REQUIRED": "后端没有配置密钥加密口令（NOVEL_SYSTEM_CONFIG_SECRET），不能保存或读取服务商密钥；请用启动脚本启动后端。",
    "CONFIG_SNAPSHOT_NOT_FOUND": "找不到这一版配置。",
    "CONFIG_VALIDATION_FAILED": "配置没有通过校验（{message}）。",
    # ---- 风格参考 ----
    "STYLE_REFERENCE_BOOK_ENCODING_UNSUPPORTED": "参考书必须是 UTF-8 或 GB18030 编码的文本。",
    "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED": "只支持导入 TXT / MD 格式的参考书。",
    "STYLE_REFERENCE_BOOK_PATH_FORBIDDEN": "这个路径不在允许导入的目录里。",
    "STYLE_REFERENCE_BOOK_PATH_INVALID": "这个路径不是一个普通文件。",
    "STYLE_REFERENCE_BOOK_PATH_LINK_FORBIDDEN": "按路径导入不接受符号链接。",
    "STYLE_REFERENCE_BOOK_PATH_NOT_FOUND": "这个路径下没有文件。",
    "STYLE_REFERENCE_IMPORT_ROOT_INVALID": "没有可用的参考书导入目录。",
    "STYLE_REFERENCE_PARAGRAPH_RANGE_INVALID": "段落范围不对：起点不能大于终点，也不能小于 0。",
    "STYLE_REFERENCE_PATH_IMPORT_DISABLED": "服务器路径导入没有开启：请用上传，或先配置允许导入的目录。",
    "STYLE_REFERENCE_PROJECT_NOT_FOUND": "找不到这部作品。",
    "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID": "导入权属声明的格式不对。",
    "STYLE_REFERENCE_UPLOAD_TOO_LARGE": "参考书太大（{message}）。",
    "STYLE_REFERENCE_BOOK_NOT_FOUND": "找不到这本参考书。",
    "STYLE_REFERENCE_PROFILE_NOT_FOUND": "找不到这份文风画像。",
    "STYLE_REFERENCE_BINDING_NOT_FOUND": "找不到这条绑定。",
    "STYLE_REFERENCE_APPLY_PARAM_INVALID": "用于作品的参数不对（{message}）。",
    "STYLE_REFERENCE_CARD_LINE_STATE_INVALID": "文风卡一句的状态只能是「总带上」「不用这句」或清空。",
    "STYLE_REFERENCE_CARD_LINE_NOT_FOUND": "这份文风卡上没有这一句。",
    "STYLE_REFERENCE_BANNED_TERM_INVALID": "禁用词的写法不对（{message}）。",
    "STYLE_REFERENCE_BANNED_TERM_NOT_FOUND": "找不到这个禁用词。",
    "STYLE_REFERENCE_BANNED_TERM_PROTECTED": "预置的禁用词不能删除。",
    "STYLE_REFERENCE_CHECK_NOT_FOUND": "找不到这次对照检查。",
    "STYLE_REFERENCE_CHECK_JUDGE_FAILED": "对照检查的评审没有给出可用的结果（{message}）。",
    "STYLE_REFERENCE_JOB_NOT_FOUND": "找不到这个参考书作业。",
    # ---- 章定稿锁 / 抄袭门 / 输入预算 ----
    "CHAPTER_APPROVED_LOCKED": "这一章已经定稿：要改它或它的场，先在成稿中心重开定稿。",
    "SOURCE_SAFETY_UNAVAILABLE": "参考书抄袭检查这次没能完成，稿子先不归档；稍后再试。",
    "CONTINUITY_BUDGET_EXCEEDED": "这一场的上下文太长，精简之后仍超出模型的输入上限；请把场景拆小一些再起草。",
    # ---- 需要模型的各个节点（fail-closed：没配好模型或调用失败）----
    "AUTHOR_PROPOSAL_LLM_NOT_CONFIGURED": "AI 续写需要先配好模型（{message}）。",
    "AUTHOR_PROPOSAL_GENERATE_FAILED": "AI 续写失败了（{message}）。",
    "SCENE_BLUEPRINT_LLM_REQUIRED": "场景蓝图需要先配好模型（{message}）。",
    "SCENE_BLUEPRINT_FAILED": "场景蓝图生成失败（{message}）。",
    "CHAPTER_STORY_ARCHITECTURE_LLM_REQUIRED": "章节故事架构需要先配好模型（{message}）。",
    "CHAPTER_STORY_ARCHITECTURE_FAILED": "章节故事架构生成失败（{message}）。",
    "CHAPTER_STORY_ARCHITECTURE_OUTPUT_INVALID": "模型给出的章节故事架构不完整，请再试一次（{message}）。",
    "CHARACTER_PRESSURE_BLUEPRINT_LLM_REQUIRED": "人物压力蓝图需要先配好模型（{message}）。",
    "CHARACTER_PRESSURE_BLUEPRINT_FAILED": "人物压力蓝图生成失败（{message}）。",
    "CHARACTER_PRESSURE_BLUEPRINT_OUTPUT_INVALID": "模型给出的人物压力蓝图不完整，请再试一次（{message}）。",
    "WRITER_DEEP_REVIEW_LLM_REQUIRED": "AI 深评需要先配好模型（{message}）。",
    "WRITER_DEEP_REVIEW_LLM_FAILED": "AI 深评失败了（{message}）。",
    "WRITER_PASSAGE_PATCH_LLM_REQUIRED": "局部改写需要先配好模型（{message}）。",
    "WRITER_PASSAGE_PATCH_LLM_FAILED": "局部改写失败了（{message}）。",
    "CHAPTER_PLAN_LLM_CALL_FAILED": "章节编排的模型调用失败（{message}）。",
    "CHAPTER_PLAN_LLM_RESPONSE_INVALID_SCHEMA": "模型给出的章节编排格式不对，请再试一次（{message}）。",
    # ---- 模型调用层原样透出的错误码（记账拒绝、预算用尽、服务商出错）----
    "LLM_PROVIDER_DISABLED": "还没有启用模型：先在系统设置里配好模型。",
    "LLM_ROUTE_NOT_CONFIGURED": "这个节点还没有分配模型：在系统设置里补齐模型分工（{message}）。",
    "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED": "这一场的 token 预算已用完；已有正文都保留着，追加预算后可以接着起草。",
    "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED": "这一场的重试次数已用完；已有正文都保留着，追加预算后可以接着起草。",
    "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED": "这一场调用服务商的次数已用完；已有正文都保留着，追加预算后可以接着起草。",
    "LLM_SCENE_CALL_IN_FLIGHT": "这一场还有一次模型调用没有结束，请稍后再试。",
    "LLM_USAGE_EXCEEDS_RESERVATION": "模型实际用掉的 token 超过了这次预留的额度，结果没有采用（{message}）。",
    "LLM_REQUEST_TIMEOUT": "模型响应超时，请稍后重试。",
    "LLM_RATE_LIMITED": "服务商限流了，请稍后重试。",
    "LLM_HTTP_FAILURE": "调用服务商失败（{message}）。",
    "LLM_RESPONSE_TRUNCATED": "模型的输出被截断了，请再试一次（{message}）。",
}


def localized_message(code: str, message: str) -> tuple[str, bool]:
    """``(作者看的说明, 是否换过)``：原说明已经是中文、或错误码不在表里时原样返回。"""

    original = str(message or "")
    if _CJK.search(original):
        return original, False
    template = ERROR_MESSAGES.get(code)
    if template is None:
        return original, False
    if "{message}" in template:
        if not original:
            return template.replace("（{message}）", "").replace("{message}", ""), True
        return template.replace("{message}", original), True
    return template, True


__all__ = ["ERROR_MESSAGES", "localized_message"]
