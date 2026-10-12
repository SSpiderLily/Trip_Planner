# ISSUE-011：长 HTTP 请求不能可靠承载规划任务生命周期

- 整理日期：2026-10-11
- 记录状态：历史实现已验证
- 范围：问题复盘；下方测试结果均为原记录中的历史证据，本次未重跑。
- 当前实现、验收及交付状态：[README](../../README.md)与[CHG-003](../changes/CHG-003-planning-performance-observability.md)；历史v3范围另见[CHG-002](../changes/CHG-002-trip-plan-enhancements.md)。

## 1. 问题描述

请求断开或进程中断后，仅结束时保存无法说明任务进展。

## 2. 排查过程与原因判断

以下迁移原学习日志“第一版后端：独立任务与增量观测”的完整问题记录。带日期的实现、调用、测试和Git状态是历史事实；后续变更以当前状态入口为准。原记录未列出的复现步骤或尝试不补造。

<details>
<summary>排查、实现与验证证据</summary>

- 原因：HTTP 长连接不能承担任务生命周期，只在结束保存无法定位中断。
- 修改：应用持有单活动任务，SQLite 保存任务/结果/Span；线程池执行规划，ContextVar 关联父子步骤；实际 invoke 和工具执行入口采集，脱敏限长。业务结果事务与观测写入错误隔离，重启标记中断。
- 验证：25 项 unittest 通过；实际 SimpleAgent 循环配合替代 LLM/MCP 验证 7 次模型、3 次工具及消息反馈；覆盖互斥、存储失败、容量和级联清理。真实外部 API 与浏览器验收仍待最终检查。
- 位置：`backend/app/services/{task_service,task_repository,observation_service}.py`；`backend/tests/test_task_runtime.py`。

</details>

## 3. 尝试方案与方案结果

线程池任务与SQLite增量Span记录，重启标记中断；25项历史测试通过。

## 4. 解决结果与验证边界

独立任务基础已验证；当前v4还使用独立预览存储。

## 5. 经验总结

请求生命周期和业务任务生命周期需要分开。

## 6. 证据来源

- [学习日志时间线](../learning-log.md)：保留原章节入口，问题正文已迁至本文。
- 原文列出的代码、测试、提交和专项验收见上方证据。临时路径仅定位历史材料，不保证文件仍存在。
