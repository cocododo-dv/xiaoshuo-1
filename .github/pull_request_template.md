## 改了什么

- 两到四条：改了什么、为什么；作者在界面上能看出来的变化单独说明（是否经作者批准）。

## 检查（完整清单见 docs/release-checklist.md）

- [ ] GitHub Actions 四项全绿：Backend Quality Gates、Backend Tests（4 片）、Frontend Tests、React Contract E2E
- [ ] 动到的领域测试与漂移守卫在本机跑过（守卫清单见 CLAUDE.md 的「Drift guards」）
- [ ] 有迁移：`CURRENT_SCHEMA_REVISION` 已改成新 head，迁移测试升到自己的版本，`docs/migrations.md` 加了一行
- [ ] 改了提示词：`version` 已升；部署说明里写了 `sync_prompt_templates --execute`
- [ ] 删了接口：已加进 `backend/tests/test_retired_surface.py`
- [ ] 新测试 / 文档只用中性的合成名字，没有作者真实作品的人名、地名与情节
- [ ] 部署要点（备份、迁移、压缩、一键补齐、刷新页面）已写在说明里

## 风险与后续

- 无。
