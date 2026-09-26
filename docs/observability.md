# 智能体观测：实现现状与目标结构

更新时间：2026-09-26。调用链依据应用源码静态核对。此前本地启动检查确认前后端可响应，用户随后确认已生成行程结果；尚未检查这次请求的后端调用记录，也未验证本地安装的 HelloAgents 内部实现。

## 当前状态

观测框架尚未实现。现有代码使用 `print()` 和异常堆栈输出，没有应用层统一 Trace ID、Span、观测导出器或调用树界面。OpenTelemetry、Langfuse、本地 JSONL 导出均为[实施计划](observability-plan.md)中的待办，不是已有能力。

## 当前真实调用链

```text
frontend/src/services/api.ts: generateTripPlan()
└─ POST /api/trip/plan
   └─ backend/app/api/routes/trip.py: plan_trip()
      ├─ get_trip_planner_agent()              在线程池外获取/懒初始化单例
      │  └─ MultiAgentTripPlanner.__init__()   仅首次创建时执行
      │     ├─ get_llm()                      获取共享 HelloAgentsLLM
      │     ├─ MCPTool(uvx amap-mcp-server)
      │     ├─ get_expanded_tools()
      │     └─ 创建四个 SimpleAgent
      └─ run_in_threadpool(agent.plan_trip, request)
         ├─ attraction_agent.run()            搜索景点
         ├─ weather_agent.run()               查询天气
         ├─ hotel_agent.run()                 搜索酒店
         ├─ planner_agent.run()               整合前三者的文本结果
         └─ _parse_response()
            ├─ 提取 JSON 文本
            ├─ json.loads()
            └─ TripPlan(**data)
```

四个 Agent 顺序执行，并未并行。前三个通过 `_register_amap_tools()` 注册发现的高德子工具，使用 `agent.add_tool(tool, auto_expand=False)`；最终规划 Agent 未注册工具。模型轮数、消息角色、工具调用顺序和结束原因需在安装版本中确认，不能从上述业务代码推定。

## 影响观测的代码事实

| 位置 | 当前行为 | 对观测设计的影响 |
| --- | --- | --- |
| `backend/app/agents/trip_planner_agent.py` | 规划器自行创建 MCPTool 并注册展开工具 | 只包装 `amap_service.py` 会漏掉规划 Agent 的主要工具调用 |
| 同上，`_build_attraction_query()` | 只取第一个偏好作为搜索关键词，并在输入中包含 `[TOOL_CALL:...]` 文本 | 输入包含调用文本不代表工具实际执行，必须在执行边界记录 |
| 同上，`_build_planner_query()` | 将景点、天气、酒店文本与需求一起传入最终规划 Agent | 需要展示输入来源；没有独立、固定执行的路线规划步骤 |
| 同上，`plan_trip()` 与 `_parse_response()` | 捕获异常并尝试调用 `_create_fallback_plan()` | 必须在捕获处保存错误，并将备用结果标记为降级 |
| 同上，`_create_fallback_plan()` | 生成占位景点、程序构造坐标、空天气列表 | 备用数据不能作为高德查询成功或真实行程质量的证据 |
| `backend/app/api/routes/trip.py` | 只要规划方法返回 TripPlan，就返回 `success=True`；未被处理的异常转为 HTTP 500 | HTTP 状态、操作状态和业务结果必须分别记录 |
| 同上 | 仅 `agent.plan_trip()` 进入线程池；初始化仍在线程池外 | 初始化与执行耗时分别记录，不能声称初始化已避免阻塞 |
| `backend/app/services/llm_service.py` | 缓存一个 HelloAgentsLLM，四个 Agent 共用 | 共享对象不保存当前请求的 Trace ID；具体调用入口待查框架源码 |
| `frontend/src/services/api.ts` | Axios 默认超时 120000ms，行程请求单独设为 `timeout: 0` | 不能把浏览器端所有请求都描述为两分钟超时 |
| `backend/app/api/main.py` | `/health` 返回基本服务状态 | 仅证明接口可响应，不证明 LLM/MCP 正常 |
| `backend/app/api/routes/trip.py` | `/api/trip/health` 已按规划器结构返回系统名、四个 Agent 名称及发现的高德工具数量；首次初始化进入线程池 | 隔离测试覆盖 200/503；尚未通过真实 LLM/MCP 初始化验收，也不能据此证明外部调用成功 |
| `backend/tests/` | 已有基于 unittest 的工具注册测试与 Unsplash 容错测试 | 复用现有测试方式，不按“项目没有测试”设计 |

共享 Agent 的对话历史和线程安全、MCP 内部错误表达、流式调用及 Token 可用性仍待核实。健康检查修复见[学习日志问题 002](learning-log.md#问题-002旅行规划健康检查读取不存在的-agent-属性)；其余规划行为没有在此次修复中更改。

## 目标记录模型（尚未实现）

- Trace：一次完整 HTTP 请求。
- Span：一次有起止时间的操作，如 Agent 执行、LLM 调用、工具调用和数据校验。
- Event：操作内的事件，如错误被捕获、触发降级、重试。

| 字段 | 约定 |
| --- | --- |
| `schema_version` | 本地导出格式版本 |
| `trace_id`、`span_id`、`parent_span_id` | 请求关联与父子关系，实际 ID 由 SDK 生成 |
| `name`、`operation_type` | 操作名称和业务分类；业务分类不等于 OTel SpanKind |
| `started_at`、`ended_at`、`duration_ms` | 可读时间与耗时；耗时使用单调时钟或 SDK 的时序机制 |
| `status` | 本地显示为 unset/ok/error，对应 SDK 操作状态 |
| `business.outcome` | 请求/编排完成后标记 success/degraded/failed，独立于 HTTP 和 Span 状态 |
| `input_summary`、`output_summary` | 脱敏、限长后的摘要；详情模式另行显式启用 |
| `error`、`events` | 错误类型、脱敏消息、失败阶段与关联操作 |
| `attributes` | Agent、模型、工具、版本、用量等元数据；未知用量不填零 |

降级不是 OTel 的自定义状态码。解析 Span 可以是 error，HTTP 请求仍返回 200，而业务结果为 degraded。Agent 正常返回文本也不意味着文本通过 TripPlan 校验或行程合理。

## 预期展示示例（虚构，非运行证据）

```text
旅行规划请求：杭州三日游       HTTP 200 · business.outcome=degraded
├─ 获取规划器                 已复用实例
└─ 执行旅行规划
   ├─ 景点搜索 Agent
   │  ├─ LLM 调用 第 1 轮
   │  ├─ amap_maps_text_search  参数：city=杭州，keywords=自然风光
   │  └─ LLM 调用 第 2 轮       使用工具结果
   ├─ 天气查询 Agent
   │  └─ …                     以真实调用为准
   ├─ 酒店推荐 Agent
   │  └─ …
   ├─ 行程规划 Agent
   │  └─ LLM 调用              汇总需求与前三个 Agent 输出
   ├─ 解析与校验               error：缺少 days[1].date
   └─ 创建备用行程             关联解析失败，标记备用数据来源
```

界面包含请求列表、调用树、单次调用详情。详情按实际消息顺序展示脱敏输入输出；记录实际选择和执行行为，不将其当作模型完整内部思考。工具 Span 首先覆盖应用到 MCP 的调用边界，MCP 服务内部 HTTP 耗时需额外观测才能拆分。

## 后续使用说明的补充规则

每阶段实现后，在这里补充实际安装版本、配置项、启动/查询命令和已验证限制。当前不提供尚未实现的观测启动命令。代码审查、替代依赖测试、真实 API 验收分开记录，验证证据写入[学习日志](learning-log.md)。
