# HelloAgents智能旅行助手 🌍✈️

基于HelloAgents框架构建的智能旅行规划助手,集成高德地图MCP服务,提供个性化的旅行计划生成。

## 学习文档

- [项目实现地图](docs/project-overview.html)：按分层、模块、四条执行流程和数据来源理解当前实现；点击函数入口可读取当前源码并定位行号。通过项目根目录的本地 HTTP 静态服务访问（例如 `python3 -m http.server 8765 --bind 127.0.0.1`），再打开 `http://127.0.0.1:8765/docs/project-overview.html`。说明核对日期与适用范围在页内记录，代码变化后需重新核对；交付状态仍见对应 Change。
- [规划改造设计参考](docs/planning-design.html)：统一查看规划泳道、逻辑数据关系、预览状态与导出时序，支持节点说明、缩放及 SVG 下载，并附[上海三日数据结构示例](docs/planning-design.html#data-walkthrough)、[时间与交通算法推演](docs/planning-design.html#time-transport-algorithm)、[交通查询与缓存](docs/planning-design.html#route-query-cache)、参考窗口、缺项位置和[实现方式、算法与成本控制清单](docs/planning-design.html#implementation-choices)。单个 HTML 无外部依赖，可直接打开；沿用上方静态服务时访问 `http://127.0.0.1:8765/docs/planning-design.html`。页面区分已确认行为与待讨论方案，正式改造状态见 CHG-003。
- [单任务工程指标设计](docs/engineering-metrics-design.md)：观测页指标与 Token 采集的统计口径及验收依据。
- [当前变更与待办导航](docs/changes/CHG-003-planning-performance-observability.md)：性能、日志与结果提示的剩余需求，以及产品、工程与人工验收的逐项入口。
- [学习目标与阶段计划](docs/learning-plan.md)：全栈技术学习范围、掌握程度、阶段路线与第一轮项目完成标准。
- [学习日志](docs/learning-log.md)：记录实践中的问题、原因、修改和验证结果。
- [旅行规划术语与规则](CONTEXT.md)：已确认的业务语言与规划规则。
- [旅行规划体验优化需求](docs/trip-planning-requirements.md)：本轮输入简化与每日路线展示的已确认需求、后续待办及验收方向（最小闭环已接入，完整目标分步落实）。

## ✨ 功能特点

- 🤖 **AI驱动的旅行规划**: 基于HelloAgents框架的SimpleAgent,智能生成详细的多日旅程
- 🗺️ **高德地图集成**: 通过MCP协议接入高德地图服务,支持景点搜索、路线规划、天气查询
- 🧠 **智能工具调用**: Agent自动调用高德地图MCP工具,获取实时POI、路线和天气信息
- 🎨 **现代化前端**: Vue3 + TypeScript + Vite,响应式设计,流畅的用户体验
- 📱 **完整功能**: 包含住宿、交通、餐饮和景点游览时间推荐

## 🏗️ 技术栈

### 后端

- **框架**: HelloAgents (基于SimpleAgent)
- **API**: FastAPI
- **MCP工具**: amap-mcp-server (高德地图)
- **LLM**: 支持多种LLM提供商(OpenAI, DeepSeek等)

### 前端

- **框架**: Vue 3 + TypeScript
- **构建工具**: Vite
- **UI组件库**: Ant Design Vue
- **地图服务**: 高德地图 JavaScript API
- **HTTP客户端**: Axios

## 📁 项目结构

```
helloagents-trip-planner/
├── backend/                    # 后端服务
│   ├── app/
│   │   ├── agents/            # Agent实现
│   │   │   └── trip_planner_agent.py
│   │   ├── api/               # FastAPI路由
│   │   │   ├── main.py
│   │   │   └── routes/
│   │   │       ├── trip.py
│   │   │       └── map.py
│   │   ├── services/          # 服务层
│   │   │   ├── amap_service.py
│   │   │   └── llm_service.py
│   │   ├── models/            # 数据模型
│   │   │   └── schemas.py
│   │   └── config.py          # 配置管理
│   ├── requirements.txt
│   ├── .env.example
│   └── .gitignore
├── frontend/                   # 前端应用
│   ├── src/
│   │   ├── components/        # Vue组件
│   │   ├── services/          # API服务
│   │   ├── types/             # TypeScript类型
│   │   └── views/             # 页面视图
│   ├── package.json
│   └── vite.config.ts
└── README.md
```

## 🚀 快速开始

### 本地一键启动（macOS / Linux）

本机日常开发与运行统一使用 VS Code 打开的 `production/helloagents-trip-planner` 主目录。独立 worktree 的文件改动不会自动同步；修复需先整合回主目录并在主目录验证，再重启服务。

首次使用先按下文安装后端和前端依赖，并分别配置 `backend/.env` 与 `frontend/.env`。之后在项目根目录的 VS Code 终端执行：

```bash
python3 dev.py
```

脚本直接使用 `backend/venv/bin/python` 和前端已安装的 Vite，无需激活虚拟环境或打开两个终端。浏览器访问 `http://127.0.0.1:5173`；按 `Ctrl+C` 同时停止两个服务。后端仅监听本机 `127.0.0.1:8000`。若端口已被占用，脚本会停止另一服务并报告启动失败。前端支持热更新；修改后端代码后请按 `Ctrl+C` 停止并重新运行脚本。

若 VS Code 终端不在项目根目录，请先 `cd` 到包含 `dev.py` 的目录，或直接运行脚本的绝对路径；虚拟环境仍位于 `backend/venv`，无需另建根目录虚拟环境。

### 前提条件

- Python 3.10+
- Node.js 16+
- 高德地图API密钥 (Web服务API和Web端(JS API))
- LLM API密钥 (OpenAI/DeepSeek等)

### 后端安装

1. 进入后端目录

```bash
cd backend
```

2. 创建虚拟环境

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

3. 安装依赖

```bash
pip install -r requirements.txt
```

4. 配置环境变量

```bash
cp .env.example .env
# 编辑.env文件,填入你的API密钥
```

5. 启动后端服务

```bash
uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000
```

### 前端安装

1. 进入前端目录

```bash
cd frontend
```

2. 安装依赖

```bash
npm install
```

3. 配置环境变量

```bash
# 创建.env文件，按示例填写浏览器使用的高德 JS Key 与安全码占位项
cp .env.example .env
```

`frontend/.env.example` 仅保留占位值。`VITE_*` 变量会进入浏览器构建产物，不要在其中填写后端 `AMAP_API_KEY` 或其他服务端凭据；实际前端配置写入不提交的 `frontend/.env`。

4. 启动开发服务器

```bash
npm run dev
```

5. 打开浏览器访问 `http://localhost:5173`

## 📝 使用指南

以下描述当前实现。2026-10-09已确认收敛核心需求，并调整预览、导出保存、主要消费计算及用户页面与后台工作台的边界；这些调整尚未实现，目标规则见[CONTEXT](CONTEXT.md#本轮核心范围2026-10-09确认待实现)，当前讨论见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#核心范围与流程讨论2026-10-09)。

1. 填写目的地、到达与离开日期时间，可选人均参考预算、到离地点、住处和完整备注。首页保持默认公共交通结合步行，出行偏好通过备注表达。
2. 提交后查看真实执行步骤；结果按天展示活动、用餐区域、交通参考和地图位置。到离地点未填的一端不生成接驳。
3. 在地图旁搜索目的地城市内的地点，先查看详情，再加入当天。系统选择插入位置；可确认删除、上移或下移景点，不能跨天自动移动。参考游玩时长只读；新增地点暂用90分钟系统参考值。
4. 各路段比较步行、骑行、公共交通，自驾行程增加驾车。初次采用方式参考首页偏好；编辑产生的新路段默认最快，未变路段保留选择。手动切换只更新当天参考时间。
5. “可能需要调整”和“信息未查到”分别提示可能超时、开放冲突或缺失信息，不自动删改已选景点。交通只作路线参考，不承担具体导航、停车取车或班次保障。
6. 预算按一人独住的住宿、餐饮、门票计算，不含交通。只统计查询价格；缺失显示“未查询到真实数据”，不使用模型估价。已知小计不等于完整支出。
7. “导出长图”独立导出当前所见行程及提示，含全部日期。超长行程分图，空白日、更新中或更新失败不阻止导出。

结果页只提供当天局部调整；改变城市、日期时间、预算、住处或首页偏好需重新生成。编辑没有新增服务端持久化，原生成任务结果保留；不要依赖编辑跨设备或长期恢复。历史v1/v2结果只读，保留原费用口径。

## 🔧 当前规划流程

### 结果页交互原型

在 `frontend` 运行 `npm run dev`，访问 [结果页原型](http://127.0.0.1:5173/prototype/result)。原型沿用当前结果页并保留每日主题；左侧卡片通过右上角手柄拖动排序（也可聚焦手柄后用键盘上下方向键），地图下方管理当天目的地增删，默认推荐未加入的两个景点和一家餐馆，仍可搜索景点和餐馆。推荐目前来自固定示例库，添加后补位，候选不足时显示实际数量。两侧顺序、地图编号和实际道路路线同步更新。每天同时展示具体酒店参考、寻找酒店的区域及对应日期的天气建议；地图用独立酒店标记展示推荐住处，点击可查看住宿卡片。参考时长显示小时和分钟。路段提供公交、地铁、骑行、步行的查询耗时，可选择采用方式，地图和当天总时长随之更新。仅开发模式注册该路由，生产构建不包含原型页面。

原型已接入高德真实底图、地点查询、公交、地铁、骑行和步行的路径。需在 `frontend/.env` 配置 `VITE_AMAP_WEB_JS_KEY` 和 `VITE_AMAP_WEB_JS_SECURITY_CODE`；沿用项目已有配置，不把凭据写入原型。天气按旅行日期匹配预报，未覆盖时显示未知；酒店核实名称、位置并尝试查询详情中的最低房价参考，可能不返回，不代表旅行日期及房型报价，POI人均消费不充当房价。费用、停留时长及行程安排仍是示例；预览恢复和上次保存用专用v3本机缓存模拟，不连接业务API、模型或数据库。新增地点先加到当天末尾，用户可拖动调整；整天自动排序尚未实现。长图导出会实际下载PNG。页面底部“原型演示设置”可以模拟重算中、重算失败、保存失败，并重置示例。2026-10-10用户已初步接受展示与操作方向，原型仍未接入正式功能。数据契约模型、引用校验及AMap字段适配已作为独立模块实现，正式规划流程、页面和计算算法仍待后续接入，状态见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#第一步实施与验收2026-10-10授权)。

### 正式规划流程

默认入口继续使用 `backend/app/agents/route_planner.py` 的三个 HelloAgents 角色。模型负责理解完整备注、选择景点与提出安排，程序核实地点并计算路线、参考时间和查询费用。开放备注不使用关键词分类，模型价格不进入预算。

2026-10-10第一步已实现独立的 `backend/app/models/planning_v4/` 数据契约与 `backend/app/services/planning_v4_validator.py` 引用/版本校验，以及 `backend/app/services/amap_contract_adapter.py` 类型化地图适配。真实 MCP 样本核对了地点、天气、步行、骑行和公共交通字段。该模块尚未接入默认 v3 规划入口，也未实现主从调度、`ActivityTiming`、返程算法、路线缓存、后台补齐或正式前端预览；详见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#第一步实施与验收2026-10-10授权)与[学习日志](docs/learning-log.md#planning-v4-amap-live-contract-2026-10-10)。

当前代码已恢复到性能方案1—3之前，保留各阶段完整 JSON 示例及 `qwen3.7-flash` 配置。模型仍输出原 `Collection` / `Draft`，后端沿用原核实与计算流程，不再提供三项性能实验开关，不额外配置角色思考或思考预算。回滚包括实验期间的住宿来源与缺餐反馈修正；这些已确认的业务要求仍有效，当前实现缺口及逐项重做方案见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md)。

v3增加首尾到离约束及当天编辑。普通活动窗口默认为09:00—19:00，到达后和离开前各预留系统缓冲；晚到、早离不强行填满。路线或开放信息缺失时明确提示；初次生成没有已核实景点不能交付，但用户编辑后可保留空白日期。

创建任务仍独立于HTTP连接在线程池执行，SQLite保留任务、结果和Span。局部编辑基于服务端签发的快照和操作列表，不接受客户端改写全局条件、金额或参考时长。连续操作合并处理，旧响应不覆盖新列表，重排失败可重试且不会恢复过期路线。

地图查询有数量和超时限制，免费数据不可用时保留缺失状态，不自动启用付费服务。天气只按查询覆盖日期展示；住宿区域不冒充具体酒店，地图顺序示意不冒充实际道路导航。营业预约、酒店房价等内容以供应商实际返回为限，不保证全面覆盖。费用只汇总可解析的明确数值；价格区间或无法解释的单位保留为缺失，不取均值估算。

高德 MCP 的独立 uvx 环境固定 `amap-mcp-server==0.1.11` 与 `pydantic==2.13.5`：该服务依赖的 MCP 1.8.1 与 Pydantic 2.14 的内部接口不兼容。后端 requirements.txt 不控制 uvx 子进程的依赖。高德 MCP 通过项目内 `backend/scripts/amap_stdio.py` 启动，并使用项目内保留许可的固定版本封装；`UV_CACHE_DIR` 可按部署环境显式指定。适配仅丢弃第三方工具模块的调试 `print`，避免原始公交响应污染 JSON-RPC 标准输出或进入日志；不屏蔽 MCP 日志、依赖警告或启动异常。

当前参考时刻按活动与路段时长连续推算，尚未按午餐、晚餐窗口等待；餐次标签齐全不保证用餐时刻合理。相关缺口与后续验证维护于CHG-003。

`PLANNER_PLACE_QUERY_LIMIT` 默认36，`PLANNER_ROUTE_QUERY_LIMIT` 默认80，失败调用也计入额度；`PLANNER_TOOL_TIMEOUT_SECONDS` 默认25秒。当天编辑共享查询缓存并设总查询期限，额度耗尽后显示未知。`uvx` 需在 PATH 或 `~/.local/bin/uvx`，首次运行需要可访问 MCP 包与缓存；初始化没有发现工具时明确报错。

编辑请求携带 `task_id`、`date`、`edit_token`、`client_revision`、`request_id` 和最多20项 `operations`。操作支持 `add_poi`、确认后的 `delete_activity`、`move_activity` 和 `select_leg_mode`；服务端签名绑定任务、日期、当天内容及已撤销的必去要求。签名密钥仅在当前服务进程内有效，重启后需要重新载入原行程。签名用于校验快照，不提供编辑历史存储或多设备同步。

## 📄 API文档

启动后端服务后,访问 `http://localhost:8000/docs` 查看完整的API文档。

主要端点:

- `POST /api/trip/tasks` - 接收任务（202）；忙时返回 409 / TASK_BUSY，不排队
- `GET /api/trip/tasks` - 任务列表；支持 status、limit、offset
- `GET /api/trip/tasks/{task_id}` - 轻量状态与当前步骤；任务失败时查询仍返回 200
- `GET /api/trip/tasks/{task_id}/result` - 成功行程；运行中/失败/中断分别返回 409 及对应错误码
- `GET /api/trip/tasks/{task_id}/spans` - 调用树摘要
- `GET /api/trip/tasks/{task_id}/spans/{span_id}` - 单步骤输入输出与错误详情
- `GET /api/trip/tasks/{task_id}/metrics` - 已结束任务的单任务工程指标；运行中返回 409
- `GET /api/map/poi` - 搜索POI
- `GET /api/map/poi/{poi_id}` - 查询地点详情
- `POST /api/trip/recalculate-day` - 执行当天局部编辑并重新计算
- `GET /api/map/weather` - 查询天气
- `POST /api/map/route` - 规划路线

## 本地任务观测

启动 `python3 dev.py` 后，从首页的“查看任务、工程指标与调用记录”进入 `/observability`。左侧任务列表支持状态筛选、每页50条及刷新；默认打开“执行轨迹”，调用树和共用时间轴对齐展示真实步骤。点击调用，在右侧查看已脱敏的输入、输出、错误和截断提示；手机上详情位于轨迹下方。初始优先选中最慢的已计时模型调用，也可按模型、异常、≥10秒慢调用或名称筛选。

“Token分析”按调用顺序展示输入/输出堆叠柱；“工具分析”按实际工具分组并支持排序与逐次定位；“异常调用”仅统计模型和工具自身的失败及中断，点击可回到执行轨迹。任务编号与视图保存在URL，支持刷新、前进和后退；旧 `view=metrics` 链接兼容进入轨迹。运行中每2秒更新状态和调用摘要，完整聚合待任务结束后读取，结束时保留当前视图。详情只按选中调用加载，需要更新时点击详情刷新。成功任务保留行程结果入口。

第一版使用单后端进程、一个活动规划任务，子 Agent 顺序执行；请勿用多个 worker 共享此数据库。关闭浏览器不会取消任务；后端正常退出等待正在执行的任务，强制退出后下次启动标记中断，不自动重跑。旧 `/api/trip/plan` 已移除。

配置写入 `backend/.env`，经 `app/config.py` 读取：

| 配置 | 默认值 |
| --- | --- |
| `TASK_DB_PATH` | `backend/data/tasks.sqlite3`（默认解析为绝对路径） |
| `OBSERVATION_RETENTION_DAYS` | 7 天 |
| `OBSERVATION_MAX_BYTES` | 209715200（200 MiB 治理目标，包含 WAL/SHM） |
| `OBSERVATION_CONTENT_LIMIT` | 65536（单步骤输入、输出各 64 KiB） |

启动及接收新任务前清理过期/超容量的已结束任务，连同行程和步骤一起删除；活动任务保留。无法腾出空间时停止保存观测详情并提示不完整。内容先脱敏后限长；截断有标记，未知温度与未知用量不填零。数据库及辅助文件不入 Git。

观测写入失败不改变业务判定；任务结果保存失败不能显示为成功。数据库无法写入时，当前进程仅保留有界错误摘要，重启后无法还原未落盘内容。输入校验拒绝仍返回 422，拒绝记录展示后置；第一版不提供自动重试、取消或重放。Token 只统计供应商实际返回的用量，不估算、不计价；历史用量与供应商未返回的字段保持未知，缺失用量与步骤记录不完整分别提示。工程指标不代表行程质量或所有价格信息都经过核验。

优化前基准及R1—R3的合成请求、源码归档、调用用量、失败和质量复核均保留在不入Git的 `backend/data/`。性能实验采集和运行脚本已随代码回滚撤回，历史快照可追溯；实验结论见[学习日志](docs/learning-log.md#不限思考预算实验r32026-10-09)，当前回滚与重新规划状态见CHG-003。历史实验数据库保留新增元数据列，恢复后的代码继续使用原有Token统计；不删除数据或改写旧报告。

验证：`cd backend && PYTHONPATH=. venv/bin/python -m unittest discover -s tests -v`；`cd frontend && npm run build`。替代依赖测试与真实调用验收结论见[学习日志](docs/learning-log.md)。

任务总耗时不累加父子步骤。共用时间轴用于查看先后与重叠，累计调用耗时不等于任务实际总耗时；工具平均仅基于有可靠起止时间的调用。异常总数下列出“调用失败”和“中断未完成”两个子项；仍在执行或状态未确定的记录不计为失败。未知耗时不参与慢调用比较，未采集Token与真实零值分别显示，供应商总量不反推缺失的输入/输出。角色汇总保留在Token分析的折叠明细中。

前端边界验证：`cd frontend && node tests/observationView.test.mjs`。正式页展示已于2026-10-09获用户接受，验证证据见[学习日志](docs/learning-log.md#agent观测正式页改版2026-10-09)；剩余需求见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md)。

## 🤝 贡献指南

欢迎提交Pull Request或Issue!

## 📜 开源协议

CC BY-NC-SA 4.0

## 🙏 致谢

- [HelloAgents](https://github.com/datawhalechina/Hello-Agents) - 智能体教程
- [HelloAgents框架](https://github.com/jjyaoao/HelloAgents) - 智能体框架
- [高德地图开放平台](https://lbs.amap.com/) - 地图服务
- [amap-mcp-server](https://github.com/sugarforever/amap-mcp-server) - 高德地图MCP服务器

---

**HelloAgents智能旅行助手** - 让旅行计划变得简单而智能 🌈

## Deployment

Production deployment test.
