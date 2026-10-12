# ISSUE-002：健康检查读取不存在的 Agent 属性

- 整理日期：2026-10-11
- 记录状态：历史修复已验证
- 范围：问题复盘；下方测试结果均为原记录中的历史证据，本次未重跑。
- 当前实现、验收及交付状态：[README](../../README.md)与[CHG-003](../changes/CHG-003-planning-performance-observability.md)；历史v3范围另见[CHG-002](../changes/CHG-002-trip-plan-enhancements.md)。

## 1. 问题描述

健康路由沿用单 Agent 结构，实际规划器没有 agent 属性，初始化成功仍返回503。

## 2. 排查过程与原因判断

以下迁移原学习日志“问题 002：旅行规划健康检查读取不存在的 Agent 属性”的完整问题记录。带日期的实现、调用、测试和Git状态是历史事实；后续变更以当前状态入口为准。原记录未列出的复现步骤或尝试不补造。

<details>
<summary>排查、实现与验证证据</summary>

#### 1. 目标

让 `/api/trip/health` 按当前四 Agent 规划器结构返回健康信息，并避免首次初始化阻塞 FastAPI 事件循环。

#### 2. 错误现象

健康检查把 `get_trip_planner_agent()` 的返回值当作单 Agent 包装对象，读取 `agent.agent.name` 和 `agent.agent.list_tools()`。实际返回的是 `MultiAgentTripPlanner`，它没有 `agent` 属性，因此成功初始化的规划器也会让健康检查进入异常分支并返回 503。

#### 3. 原因分析

当前规划器分别持有景点、天气、酒店和行程规划四个 `SimpleAgent`，并把 `get_expanded_tools()` 得到的高德工具列表保存在 `amap_tools`。健康路由仍沿用旧的单 Agent 对象结构。

此外，首次调用 `get_trip_planner_agent()` 会同步创建 LLM 与 MCP 工具；如果直接在异步路由中调用，会占用事件循环线程。

#### 4. 尝试过的方案

没有给规划器添加兼容用的 `agent` 属性，因为这会掩盖实际的多 Agent 结构，也无法准确代表工具数。

#### 5. 最终解决方案

健康路由在线程池中调用 `get_trip_planner_agent()`，保留原有初始化异常到 HTTP 503 的映射。健康响应保留 `status`、`service`、`agent_name` 和 `tools_count`：`agent_name` 明确标为“多智能体旅行规划系统”，`tools_count` 使用 `len(planner.amap_tools)`；新增 `agents` 列表返回四个 `SimpleAgent.name`。

#### 6. 验证方法

在 `backend` 目录执行：

```bash
venv/bin/python -m unittest tests/test_trip_health.py tests/test_trip_planner_agent.py
```

实际结果：3 个测试通过。使用不含 `agent` 属性的替代规划器验证健康响应结构和工具数，并模拟初始化异常验证 503 响应；同时现有的 MCP 工具注册测试通过。

#### 7. 注意事项

该端点报告规划器初始化和已发现工具的数量，不代表每个 LLM 或高德 MCP 请求都能成功完成。

#### 8. 学习总结

健康检查应读取当前服务对象的真实结构；多 Agent 系统应分别报告系统名称、成员 Agent 和共享/发现工具数量。异步路由调用可能阻塞的同步初始化函数时，应把初始化放入线程池。

---

</details>

## 3. 尝试方案与方案结果

读取多 Agent 成员及共享工具列表，将同步初始化放入线程池；3项历史测试通过。

## 4. 解决结果与验证边界

修复对象结构不匹配；健康检查不证明每次外部请求成功。

## 5. 经验总结

接口应检查真实服务结构，阻塞初始化应离开事件循环。

## 6. 证据来源

- [学习日志时间线](../learning-log.md)：保留原章节入口，问题正文已迁至本文。
- 原文列出的代码、测试、提交和专项验收见上方证据。临时路径仅定位历史材料，不保证文件仍存在。
