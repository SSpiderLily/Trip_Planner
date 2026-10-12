# ISSUE-010：规划失败被备用行程掩盖与 Agent 历史串用

- 整理日期：2026-10-11
- 记录状态：历史修复已验证
- 范围：问题复盘；下方测试结果均为原记录中的历史证据，本次未重跑。
- 当前实现、验收及交付状态：[README](../../README.md)与[CHG-003](../changes/CHG-003-planning-performance-observability.md)；历史v3范围另见[CHG-002](../changes/CHG-002-trip-plan-enhancements.md)。

## 1. 问题描述

工具异常被转成文本，备用行程掩盖失败，共享历史可能影响下一次需求。

## 2. 排查过程与原因判断

以下迁移原学习日志“观测前置修复：失败传播与历史隔离”的完整问题记录。带日期的实现、调用、测试和Git状态是历史事实；后续变更以当前状态入口为准。原记录未列出的复现步骤或尝试不补造。

<details>
<summary>排查、实现与验证证据</summary>

- 原因：规划器返回占位备用行程，框架把工具异常转成文本继续执行；共享 Agent 历史可能串入下一次需求。
- 修改：项目内 PlanningAgent 保留框架消息循环但显式传播工具错误，独立维护实际调用证据，每次执行清空对话；去掉备用行程，校验日期完整性，天气只取真实预报且未知温度为空。
- 验证：`cd backend && PYTHONPATH=. venv/bin/python -m unittest discover -s tests -v`，14 项通过，覆盖未调用、错误、空结果、两次历史隔离和日期冲突。测试替代外部依赖，尚不作为真实 API 验收。
- 代码：`backend/app/agents/execution.py`、`trip_planner_agent.py`、`models/schemas.py`；测试：`backend/tests/test_planning_contract.py`。

</details>

## 3. 尝试方案与方案结果

传播工具错误、去除备用结果并清空每次历史；14项替身回归通过。

## 4. 解决结果与验证边界

历史故障传播和隔离通过；真实验收见原时间线后续记录。

## 5. 经验总结

成功状态必须有真实调用证据，Agent上下文需要明确生命周期。

## 6. 证据来源

- [学习日志时间线](../learning-log.md)：保留原章节入口，问题正文已迁至本文。
- 原文列出的代码、测试、提交和专项验收见上方证据。临时路径仅定位历史材料，不保证文件仍存在。
