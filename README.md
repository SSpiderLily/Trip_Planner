# HelloAgents智能旅行助手 🌍✈️

基于HelloAgents框架构建的智能旅行规划助手,集成高德地图MCP服务,提供个性化的旅行计划生成。

## 学习文档

- [项目实现地图](docs/project-overview.html)：按分层、模块、四条执行流程和数据来源理解当前实现；点击函数入口可读取当前源码并定位行号。通过项目根目录的本地 HTTP 静态服务访问（例如 `python3 -m http.server 8765 --bind 127.0.0.1`），再打开 `http://127.0.0.1:8765/docs/project-overview.html`。说明核对日期与适用范围在页内记录，代码变化后需重新核对；交付状态仍见对应 Change。
- [规划改造设计参考](docs/planning-design.html)：**2026-10-12已确认的[简化流程与保留/删除清单](docs/planning-design.html#simplified-flow)为最新设计；分步实施、验证和剩余范围以[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#simplified-implementation)为准；旧章节仅供迁移核对。** 统一查看规划泳道、逻辑数据关系、预览状态与导出时序，支持节点说明、缩放及 SVG 下载，并附[上海三日数据结构示例](docs/planning-design.html#data-walkthrough)、[时间与交通算法推演](docs/planning-design.html#time-transport-algorithm)、[交通查询与缓存](docs/planning-design.html#route-query-cache)、参考窗口、缺项位置和[实现方式、算法与成本控制清单](docs/planning-design.html#implementation-choices)。单个 HTML 无外部依赖，可直接打开；沿用上方静态服务时访问 `http://127.0.0.1:8765/docs/planning-design.html`。页面区分已确认行为与待讨论方案，正式改造状态见 CHG-003。
- [单任务工程指标设计](docs/engineering-metrics-design.md)：观测页指标与 Token 采集的统计口径及验收依据。
- [当前变更与待办导航](docs/changes/CHG-003-planning-performance-observability.md)：性能、日志与结果提示的剩余需求，以及产品、工程与人工验收的逐项入口。
- [学习目标与阶段计划](docs/learning-plan.md)：全栈技术学习范围、掌握程度、阶段路线与第一轮项目完成标准。
- [问题解决记录](docs/problems/README.md)：一问题一份Markdown，记录描述、排查、尝试结果、解决边界和经验。
- [学习日志](docs/learning-log.md)：开发与设计时间线，链接独立问题记录。
- [旅行规划术语与规则](CONTEXT.md)：已确认的业务语言与规划规则。
- [旅行规划体验优化需求](docs/trip-planning-requirements.md)：本轮输入简化与每日路线展示的已确认需求、后续待办及验收方向（最小闭环已接入，完整目标分步落实）。

## ✨ 功能特点

- 🤖 **AI驱动的旅行规划**: 基于HelloAgents框架的SimpleAgent,智能生成详细的多日旅程
- 🗺️ **高德地图集成**: 通过MCP协议接入高德地图服务,支持景点搜索、路线规划、天气查询
- 🧠 **智能工具调用**: Agent自动调用高德地图MCP工具,获取实时POI、路线和天气信息
- 🎨 **现代化前端**: Vue3 + TypeScript + Vite,响应式设计,流畅的用户体验
- 📱 **完整功能**: 包含住宿、交通、餐饮和按时段安排的景点推荐

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

自定义 OpenAI 兼容接口配置在 `backend/.env`：`LLM_BASE_URL` 填接口根地址（通常以 `/v1` 结尾），`LLM_MODEL_ID` 填供应商提供的模型ID，`LLM_API_KEY` 填该接口的Key。当前使用 Chat Completions；规划调用还会传递 `response_format={"type":"json_object"}` 等参数，具体模型是否支持需真实调用确认。备用地址需手动替换 `LLM_BASE_URL`，当前没有自动切换。修改后重启后端；环境中显式设置的同名变量优先于 `.env`，启动时不要继续携带旧模型覆盖值。

模型输出长度：项目不主动传递输出 Token 上限（需求整理、初版/修订及旧版规划均适用），由供应商按模型能力和接口默认行为处理。供应商仍可能限制输出；本设置不代表无限输出。代码修改后须重启后端生效。

规划超时：当前默认 `PLANNER_ENFORCE_TIMEOUTS=false`，关闭新规划总流程、采集、子任务、模型HTTP请求及地图查询的主动超时。模型或地图服务返回的明确错误仍会结束对应调用，修复轮数和工具次数限制仍保留；要恢复原技术期限，可设置为 `true` 并重启后端。数据库锁等待、服务关闭等待和前端短请求超时不属于规划执行期限。

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

以下描述当前实现。2026-10-09已确认收敛核心需求，并调整预览、导出保存、主要消费计算及用户页面与后台工作台的边界；这些调整已接入v4用户流程，默认首页已切换到可编辑预览流程；目标规则见[CONTEXT](CONTEXT.md#本轮核心范围2026-10-09确认待实现)，当前讨论见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#核心范围与流程讨论2026-10-09)。

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

旧版v3入口使用 `backend/app/agents/route_planner.py` 的三个 HelloAgents 角色。模型负责理解完整备注、选择景点与提出安排，程序核实地点并计算路线、参考时间和查询费用。开放备注不使用关键词分类，模型价格不进入预算。

2026-10-10第一步已实现独立的 `backend/app/models/planning_v4/` 数据契约与 `backend/app/services/planning_v4_validator.py` 引用/版本校验，以及 `backend/app/services/amap_contract_adapter.py` 类型化地图适配。真实 MCP 样本核对了地点、天气、步行、骑行和公共交通字段。后续F01需求整理、F02日期时段规则和F11a路线缓存基础已作为独立模块验收并合并。这些模块已接入默认v4用户流程，其他后端模块也按小步计划完成接线；详见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#第一步实施与验收2026-10-10授权)与[学习日志](docs/learning-log.md#planning-v4-amap-live-contract-2026-10-10)。

当前工作区的v4使用确定的可用/跳过时段，模型与草稿只按时段安排地点和顺序，活动时间及停留字段已移除；新快照读写不依赖timing。住宿按已核实的连续日期范围复用。明确跨城或换住处时，在备注写清日期、城市和新住处；没有指定新住处时才推荐。仅必需时段缺真实地点时按日补查；已有候选直接修订，保留有效安排。初版不计、每日期最多3轮，技术恢复不扣业务轮；耗尽仍可编辑、导出。

交通只查住处到首个真实地点及当前顺序的相邻地点，比较公共交通、骑行和步行，默认推荐最快有效方案；同条件缓存继续复用，不查询全量矩阵或返程路线。公共交通使用当天当地09:00参考时刻，不代表活动实际出发时刻；缺起点保留未知。生成先完成地点初版、轻量校验和按日修订，再保存可读的地点快照并独立查询相邻交通。交通等待或失败时仍可查看、编辑和导出当前内容；交通旧结果不能覆盖后续编辑。增删和排序只查新相邻边，未变化的边保留已有方案；有效交通方式切换直接采用当前方案，不重新查询。刷新和重启只接续有效待查边，完整保存可还原为新草稿；损坏或不支持的数据明确提示，不自动重新规划。单次真实接线和浏览器功能已通过S09验收，推荐质量及人工接受仍未确认，证据边界见[CHG-003 S09](docs/changes/CHG-003-planning-performance-observability.md#s09-live-browser-acceptance)。

旧版v3代码曾恢复到性能方案1—3之前，保留各阶段完整 JSON 示例；模型由本地 `LLM_MODEL_ID` 指定。模型仍输出原 `Collection` / `Draft`，后端沿用原核实与计算流程，不再提供三项性能实验开关，不额外配置角色思考或思考预算。回滚包括实验期间的住宿来源与缺餐反馈修正；这些已确认的业务要求仍有效，当前实现缺口及逐项重做方案见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md)。

v3增加首尾到离约束及当天编辑。普通活动窗口默认为09:00—19:00，到达后和离开前各预留系统缓冲；晚到、早离不强行填满。路线或开放信息缺失时明确提示；初次生成没有已核实景点不能交付，但用户编辑后可保留空白日期。

创建任务仍独立于HTTP连接在线程池执行，SQLite保留任务、结果和Span。局部编辑基于服务端签发的快照和操作列表，不接受客户端改写全局条件、金额或参考时长。连续操作合并处理，旧响应不覆盖新列表，重排失败可重试且不会恢复过期路线。

地图查询保留数量限制；新规划的超时由 `PLANNER_ENFORCE_TIMEOUTS` 控制，默认关闭。免费数据不可用时保留缺失状态，不自动启用付费服务。天气只按查询覆盖日期展示；住宿区域不冒充具体酒店，地图顺序示意不冒充实际道路导航。营业预约、酒店房价等内容以供应商实际返回为限，不保证全面覆盖。费用只汇总可解析的明确数值；价格区间或无法解释的单位保留为缺失，不取均值估算。

高德 MCP 的独立 uvx 环境固定 `amap-mcp-server==0.1.11` 与 `pydantic==2.13.5`：该服务依赖的 MCP 1.8.1 与 Pydantic 2.14 的内部接口不兼容。后端 requirements.txt 不控制 uvx 子进程的依赖。高德 MCP 通过项目内 `backend/scripts/amap_stdio.py` 启动，并使用项目内保留许可的固定版本封装；`UV_CACHE_DIR` 可按部署环境显式指定。适配仅丢弃第三方工具模块的调试 `print`，避免原始公交响应污染 JSON-RPC 标准输出或进入日志；不屏蔽 MCP 日志、依赖警告或启动异常。

旧版v3参考时刻按活动与路段时长连续推算，尚未按午餐、晚餐窗口等待；餐次标签齐全不保证用餐时刻合理。相关缺口与后续验证维护于CHG-003。

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

“Token分析”按调用顺序展示输入/输出堆叠柱；“工具分析”按实际工具分组并支持排序与逐次定位；“异常调用”仅统计模型和工具自身的失败及中断，点击可回到执行轨迹。任务编号与视图保存在URL，支持刷新、前进和后退；旧 `view=metrics` 链接兼容进入轨迹。运行中每2秒更新状态和调用摘要，完整聚合待任务结束后读取，结束时保留当前视图。详情只按选中调用加载，需要更新时点击详情刷新。成功任务保留行程结果入口。入口兼容旧版行程与v4预览；v4按所选任务跳转到`/preview?task=<task_id>`，凭据仍仅保存在发起规划的浏览器中。新任务不会再清除旧任务凭据；如果本机浏览器缺少旧任务凭据，观测页会为已完成且未在更新的预览签发新凭据，再打开所选任务，不重新规划。恢复仅允许本机请求，旧凭据随轮换失效；正常预览及编辑接口仍校验凭据。

第一版使用单后端进程、一个活动规划任务，子 Agent 顺序执行；请勿用多个 worker 共享此数据库。关闭浏览器不会取消任务；后端正常退出等待正在执行的任务，强制退出后下次启动标记中断，不自动重跑。旧 `/api/trip/plan` 已移除。

配置写入 `backend/.env`，经 `app/config.py` 读取：

| 配置 | 默认值 |
| --- | --- |
| `TASK_DB_PATH` | `backend/data/tasks.sqlite3`（默认解析为绝对路径） |
| `OBSERVATION_RETENTION_DAYS` | 7 天 |
| `OBSERVATION_MAX_BYTES` | 209715200（200 MiB 治理目标，包含 WAL/SHM） |
| `OBSERVATION_CONTENT_LIMIT` | 65536（单步骤输入、输出各 64 KiB） |

启动及接收新任务前清理过期/超容量的已结束任务，连同行程和步骤一起删除；活动任务保留。无法腾出空间时停止保存观测详情并提示不完整。内容先脱敏后限长；截断有标记，未知温度与未知用量不填零。数据库及辅助文件不入 Git。

观测记录缺失时，后端控制台输出`[observation]`分类、异常类型及SQLite数值错误码，不输出异常原文。历史缺失记录没有原因日志时，不能倒推出确定根因。观测写入失败不改变业务判定；任务结果保存失败不能显示为成功。数据库无法写入时，当前进程仅保留有界错误摘要，重启后无法还原未落盘内容。输入校验拒绝仍返回 422，拒绝记录展示后置；第一版不提供自动重试、取消或重放。Token 只统计供应商实际返回的用量，不估算、不计价；历史用量与供应商未返回的字段保持未知，缺失用量与步骤记录不完整分别提示。工程指标不代表行程质量或所有价格信息都经过核验。

优化前基准及R1—R3的合成请求、源码归档、调用用量、失败和质量复核均保留在不入Git的 `backend/data/`。性能实验采集和运行脚本已随代码回滚撤回，历史快照可追溯；实验结论见[学习日志](docs/learning-log.md#不限思考预算实验r32026-10-09)，当前回滚与重新规划状态见CHG-003。历史实验数据库保留新增元数据列，恢复后的代码继续使用原有Token统计；不删除数据或改写旧报告。

验证：`cd backend && PYTHONPATH=. venv/bin/python -m unittest discover -s tests -v`；`cd frontend && npm run build`。替代依赖测试与真实调用验收结论见[学习日志](docs/learning-log.md)。

F04c天气查询的独立手动核验（只调用高德MCP，不调用模型或写数据库）：

```bash
cd backend
PYTHONPATH=. venv/bin/python -m scripts.run_weather_query --live
```

必须显式传入`--live`。命令保留固定上海三日的2026-10-10至12日，按实际预报匹配日期；未覆盖日期显示unknown，工具错误与日期缺失分开。输出包含日级状态、供应商发布时间和查询来源摘要。后续规划只使用`WeatherQueryExecution.fact_store`中的已核验投影；`diagnostic_tool_result`仅供诊断。正式规划入口尚未接入，验收与交付状态见[CHG-003 F04c](docs/changes/CHG-003-planning-performance-observability.md#f04c-weather-query)。

F01需求整理的独立手动核验（会调用配置的LLM，不属于离线测试）：

```bash
cd backend
PYTHONPATH=. venv/bin/python scripts/run_requirement_interpreter.py --timeout-seconds 120
```

命令使用设计页中的固定上海三日请求，最多进行一次格式修复；每个HTTP请求超时上限为120秒，SDK自动重试关闭。成功时只输出结构化需求摘要、供应商usage、模型调用次数和总耗时；不调用高德、不运行完整规划、不写正式任务数据库。超时、认证及结构化校验失败输出安全错误码，不显示原始模型响应或错误文本。

F03单个景点子任务的独立手动核验（会调用配置的LLM和高德MCP，必须明确传入`--live`）：

```bash
cd backend
PYTHONPATH=. venv/bin/python scripts/run_attraction_subtask.py --live
```

命令复用完整的上海三日原始请求构造`PlanningInput`，任务仅覆盖10月10日下午的“必须去外滩”景点子任务，首次规划的`base_revision`为空。模型只返回搜索关键词或已查到的POI选择；后端固定城市、查询参数、事实来源与任务版本。模型输入包含已有候选的坐标、城市、分类、费用原文/数值、营业时间原文及来源引用。营业时间只作原文参考，不推断旅行日期是否开放。模型预期执行一次关键词搜索后结束，但若它继续搜索、需要补真实景点详情或遇到可重试的地图错误，实际地图调用次数会增加。命令不会重跑F01，不生成整趟行程，也不写正式任务数据库。成功输出TaskResult、事实来源、地图调用摘要、Token和耗时；不输出原始模型响应、第三方异常文本或密钥。已完成离线回归及一次上海外滩真实子任务核验；这不代表完整三日行程已经生成，F03进度与验收状态见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#f03-单个景点子任务2026-10-11授权)。

F05a主智能体决定为独立模块：`MasterDecider.run(MasterDecisionContext(...))`接收固定的PlanningInput、事实、候选及历史任务，返回后端定稿的delegate/plan决定。可执行角色和直接查询由当前依赖收窄；最多一次JSON格式修复，业务引用不合法直接拒绝。它不执行MCP或生成行程；F05b负责调度续派，F06负责每日安排。调用会使用配置的真实LLM。范围及验收见[CHG-003 F05a](docs/changes/CHG-003-planning-performance-observability.md#f05a-master-decision)。

F05b可通过 `await PlanningDispatchService(...).run_async(PlanningDispatchContext(...))` 执行决定、就绪任务并行及结果续派。默认最多3个信息任务并行，工具仍共用4个名额；600秒为单次搜集技术期限，不是业务调用总额。返回已核实事实、候选、区域、各任务结果及停止原因；`technical_error`必须先分类；身份或事实损坏终止，其他错误经保留快照校验后可进入带提示的草稿。主模型只接收候选关联事实与工具查询摘要，后端保留完整事实。正式首页已接入，真实服务验收限制见[CHG-003 F05b](docs/changes/CHG-003-planning-performance-observability.md#f05b-dispatch)。


F06每日规划为独立模块：`DailyPlanGenerator.generate(planning_input, candidates, facts, area_hints, task_results, source_run_id=...)`调用配置模型，返回提案、已绑定事实的草稿和调用用量。后端计算缺项清单；空白或占位时段仍是未完成安排。120秒技术期限内，截断补全、格式修复与活动局部修复共用最多一次额外模型调用；模型截断不接受为完整结果。可定位到合法日期、时段及活动身份的错误会收到具体约束和可选候选；后端只合并目标活动的修复，保留有效安排。未修复的活动被排除并附位置提示，空缺时段进入既有按日补查；日期身份、事实来源及最终草稿仍须通过校验。`UsageAwareLLM.invoke_with_metadata`提供结束原因，原`invoke_with_usage`两值接口保持兼容。显式手动核验可用`python -m scripts.run_daily_plan_generator --help`查看输入格式；会调用模型，不调用MCP或写正式数据库。范围见[CHG-003 F06](docs/changes/CHG-003-planning-performance-observability.md#f06-daily-plan)，本次恢复策略验收见[局部修复](docs/changes/CHG-003-planning-performance-observability.md#v4-local-plan-repair)。

初版模型输入按候选关联地点摘要，并提供每时段可选ID；未入选的搜索结果不进入可选地点列表。输出Schema与后端保持一致：景点/用餐必须提供`candidate_ref`，自由活动不引用候选。景点搜索覆盖不再限制排程时段，餐馆的餐次区域范围继续保留；局部修复保留原时段与顺序，不要求或填充活动时长。观测`validation.daily_plan_local_repair`按活动记录可用候选数量及处理结果，`diagnose_task`报告会显示无候选、未返回有效修复、错误替换、主动留空、调用失败或修复额度耗尽。验收与历史证据边界见[初版候选恢复](docs/changes/CHG-003-planning-performance-observability.md#v4-initial-candidate-recovery)。

当前相邻路线为异步服务：`query_current_routes(planning_input, draft, facts, ...)`按当前顺序查询住处起点及相邻地点，使用共享四并发MCP与查询条件缓存。跨日并行，公共交通每段都使用当天当地09:00参考，不推进活动时间。若提供F04d execution，仍须同时提供当前context_revision并匹配请求、run、版本和端点原文；核实端点不追加返程查询。现行范围见[CHG-003 S06](docs/changes/CHG-003-planning-performance-observability.md#s06-adjacent-routes-cache)。显式真实核验仅查传入草稿的一段：

```bash
cd backend
PYTHONPATH=. venv/bin/python scripts/query_current_routes.py --live --input-file /path/to/route-input.json --day-id day-2026-10-10 --from-ref lodging:day-2026-10-10 --to-ref activity-id
```

输入文件须包含PlanningInput、PlanDraft与来源闭合的FactStore；命令不会代造POI或运行完整规划。

`CurrentRouteService.create_route_context`建立同一行程的查询缓存，由任务服务保留，初版相邻交通与编辑继续复用；停止持有行程时调用`aclose()`。备用矩阵服务与启动入口已移除，新预览的`alternative_backfill`为`not_required`；有效期与查询条件的专项证据见[CHG-003 S06](docs/changes/CHG-003-planning-performance-observability.md#s06-adjacent-routes-cache)。

已知人均消费纯函数为`aggregate_reference_costs(planning_input, draft, facts)`：返回`ReferenceCostSummary`和按日明细，景点同日去重、每次用餐分别计、酒店单列。调用方按覆盖状态显示未知/部分已知，不能把全未知的小计0显示为“免费”或“总旅费0”；只使用cost已有值，不推算人数或全程预算是否足够。范围见[CHG-003消费汇总](docs/changes/CHG-003-planning-performance-observability.md#reference-cost)。

独立预览存储模块为`PreviewRepository`：初始化专用SQLite路径后，使用`create/get_preview/replace_preview`维护完整草稿快照，`save_final`按当前存储版本保存最终结果，`read_final`读取最近成功保存。完整PreviewSnapshot校验来源、引用图与版本；单独ItineraryPreview只验证模型结构和存储身份/版本。新快照不读写timing；保存失败不覆盖上次成功保存，待查交通仍可保存和还原。损坏或不支持的数据明确拒绝，不转换历史数据。v4任务API和浏览器已单独接入，默认首页已使用新需求表单；范围见[CHG-003预览存储](docs/changes/CHG-003-planning-performance-observability.md#preview-storage)。

每日检查使用`validate_daily_plan(planning_input, draft, facts, candidates)`，保留地点覆盖、需求与来源/引用检查；`build_day_summaries(planning_input, draft, facts)`仅汇总已查到的交通耗时及未知路段。旧F07活动时间服务已删除，不再检查营业或推算返程可行性。只因必需时段缺真实地点启动按日补查，技术恢复不占业务轮；规则与专项证据见[CHG-003 S05](docs/changes/CHG-003-planning-performance-observability.md#s05-light-validation-repair)。地点修订只做轻量校验；独立交通交付及任务状态规则见[CHG-003 S07](docs/changes/CHG-003-planning-performance-observability.md#s07-place-first-pipeline)。

F04b住宿子任务的独立手动核验（会调用配置的LLM和高德MCP）：

```bash
cd backend
PYTHONPATH=. venv/bin/python scripts/run_lodging_subtask.py --live --anchor-file /path/to/dining-anchor.json
```

沿用上海三日空住宿请求，锚点文件包含已核实`fact_store`和`area_hints`。子任务推荐一家酒店及住宿区域；先查5公里，没有有效酒店时才扩大至10公里，缺cost不扩搜。服务也支持用户指定住处原文核实，允许住宅，拒绝同名区域冒充具体住处；歧义保留未知且不替换住处。候选以`area_hint_id`保留来源区域关联。完整范围及验收见[CHG-003 F04b](docs/changes/CHG-003-planning-performance-observability.md#f04b-lodging-subtask)。

F04d到离地点核实的独立手动核验（只调用高德MCP，不调用模型）：

```bash
cd backend
PYTHONPATH=. venv/bin/python -m scripts.verify_endpoints --live
PYTHONPATH=. venv/bin/python -m scripts.verify_endpoints --live --departure-query '上海虹桥站'
```

第一条保留原上海三日请求的空端点，因此零工具调用；第二条是明确补充地点的专项场景。只有唯一同城、全名匹配且带来源坐标的结果才标记verified；其他情况保留待确认、无结果或错误原因，不默认选择车站。返回请求与版本标识供后续调用方校验。实现范围及验收见[CHG-003 F04d](docs/changes/CHG-003-planning-performance-observability.md#f04d-endpoint-query)。

F04a餐饮子任务的独立手动核验（会调用配置的LLM和高德MCP，必须显式传入`--live`及来源化区域锚点文件）：

```bash
cd backend
PYTHONPATH=. venv/bin/python scripts/run_dining_subtask.py --live --anchor-file /path/to/dining-anchor.json
```

锚点文件包含`fact_store`和`area_hints`，使用F03已核实的地点事实与区域引用。命令保留原始上海三日请求，仅验证首日晚餐在外滩区域附近的餐馆子任务；这不是新增用户硬性条件。每个餐次绑定自己的区域锚点，先查1公里，没有有效候选时扩大至3公里。模型选择关键词、餐次或已有POI；后端校验类别、距离、来源与版本。人均`cost`缺失不扩大搜索，营业时间只保留原文。支持按原始要求核实明确餐馆名，同名歧义保留未知；饮食属性不能仅凭地图名称认定。结果包含餐次覆盖与缺项，正式调度和整趟行程尚未接入。范围及验收状态见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#f04a-dining-subtask)。

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

按日修订已接入正式v4流程，只补必需时段的真实地点缺项。已有有效候选时直接修订，不追加查询；保留其他日期和有效活动。每日期最多3个业务轮，初版不计，有限技术恢复不占业务轮；耗尽后交付可编辑草稿。未知价格、天气、营业或交通不单独触发修订，边界见[CHG-003 S05](docs/changes/CHG-003-planning-performance-observability.md#s05-light-validation-repair)。

### v4预览任务接口基础

`POST /api/trip/v4/tasks`接收`planning_v4.TripRequest`，立即返回task_id、plan_id和一次性返回的access_token；后端负责需求解释和规则，不接受客户端提供模型整理结果。`GET /api/trip/v4/tasks/{task_id}`及其`/preview`使用`Authorization: Bearer <access_token>`；访问标识不放URL。状态、草稿和消费摘要使用同一预览版本。

预览库通过`PREVIEW_DB_PATH`配置，默认`backend/data/previews.sqlite3`，必须与`TASK_DB_PATH`分开。观测清理不处理预览库；草稿生成不自动更新最终结果。进程退出后未完成任务标中断，已ready草稿可恢复。旧`/tasks/{id}/result`对v4任务返回明确的预览接口提示。

默认应用已注册`PlanningV4Pipeline`，v4接口运行需求整理→地点采集→地点初版→轻量校验与按日修订→可读预览→独立相邻交通；首页已接入新需求表单。地点先交付，交通等待或失败不阻塞查看、编辑和导出。独立测试应用未注册pipeline时返回503。实施与故障验收见[CHG-003 S07](docs/changes/CHG-003-planning-performance-observability.md#s07-place-first-pipeline)。


### v4用户预览页面

前端`/preview`通过本机当前访问标识读取v4预览；`savePreviewAccess(taskId, accessToken, planId)`保存当前指针，新规划替换旧指针，不提供历史列表。Bearer标识不放URL。页面展示三种交通、每日住宿/天气、来源化消费及缺项提示；地图不可用不影响查看。页面支持全部日期导出及还原上次保存；新需求入口为`/plan`，`/`与`/plan`使用同一新表单；预览已支持按日编辑，进度见[CHG-003 F10b](docs/changes/CHG-003-planning-performance-observability.md#f10b-preview-ui)。

预览与PNG导出按时段显示有序地点，不展示活动精确时刻、停留时长、总安排时长或返程可行性结论；保留已知交通耗时、三种交通方案和查询参考时刻。指定住处只展示酒店，不显示推荐住宿区域及区域地图标记；未指定住处才推荐区域与一家酒店。后端已取消活动时间契约、返程路线及备用矩阵，验证边界见[S03](docs/changes/CHG-003-planning-performance-observability.md#s03-slot-place-contract)和[S06](docs/changes/CHG-003-planning-performance-observability.md#s06-adjacent-routes-cache)。

生成中会显示转圈动画和后端实际阶段，完成后自动打开预览。规划失败会说明安全原因并引导“重新规划”；网络或服务暂不可读时可“重新读取”，不会重新提交模型任务。刷新仍读取当前任务。阶段与错误分类验证：`cd frontend && node tests/v4PreviewProgress.test.mjs`。

映射边界验证：`cd frontend && node tests/v4PreviewMapper.test.mjs`；同目录执行`npm run build`检查正式页面类型与生产构建。测试内的合成样本只验证展示，不代表完整旅行规划质量。


### v4按日编辑核心

`PlanningV4EditService.apply(preview, command)`是无外部调用的纯服务，返回新预览、变化活动/路段及待查路段。一个命令限一个日期，支持新增、按活动ID删除、完整顺序重排、已查到的交通方式选择；用户重复安排同一景点时，每次活动有独立ID。v4编辑API负责原子存储和路线重算；正式预览页面已接入按日编辑，进度见[编辑核心](docs/changes/CHG-003-planning-performance-observability.md#v4-edit-core)。


### v4最终结果保存与还原

`GET /api/trip/v4/tasks/{task_id}/final`返回上次成功保存的元数据；`POST .../final/save`携带`save_id`与`source_store_version`，只复制同版本服务端草稿。`POST .../final/restore`携带`restore_id`、`expected_store_version`、`expected_plan_revision`及`expected_save_id`，原子还原成新草稿版本。均使用该任务Bearer标识，不接受客户端快照，不提供历史列表。

保存过期返回`stale`；还原版本冲突或最后保存变化返回409，存储不可用返回503。只支持简化契约的新快照；损坏或不支持的旧记录明确拒绝，不转换或删除历史数据。失败不覆盖上次成功保存，图片下载与保存结果分开处理，边界见[S08](docs/changes/CHG-003-planning-performance-observability.md#s08-edit-persistence)。


### v4预览导出与还原操作

`/preview`的“导出全部日期”固定当前所见快照，生成逐日PNG，长日自动分页。文件准备完成后发起下载，再尝试保存最终结果；保存失败会单独提示，仍可使用逐张下载链接。后台已更新服务端版本时，本次图片可下载，但不会把其他版本当作本次结果保存。

“还原上次保存”仅在存在可还原快照时启用。成功后显示新预览版本，失败时保留当前内容。验证命令：`cd frontend && node tests/v4PreviewExportController.test.mjs`；浏览器及存储故障证据见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#v4-export-ui)。


### v4规划流程与技术截止

应用启动注册`PlanningV4Pipeline`并使用已确认的六个参考窗口；`TripRequest.explicit_return_buffer_s`可显式覆盖默认返程1小时，未填写时沿用默认。`PLANNER_ENFORCE_TIMEOUTS`默认关闭，模型与地点采集不按项目主动期限停止；只有开启该开关时才应用对应期限配置。相邻交通worker仍使用`PLANNER_EDIT_ROUTE_DEADLINE_SECONDS`默认120秒及工具有限查询。每日期最多3个业务修订轮，耗尽后保留带缺项草稿；交通故障保留已验证活动。

完整接线与独立离线验收见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#v4-planning-pipeline)。最新模型、在线验收与失败记录以[CHG-003当前状态](docs/changes/CHG-003-planning-performance-observability.md#当前状态)为准；不要把离线回放当作真实旅行方案质量证据。


### v4按日编辑API

`POST /api/trip/v4/tasks/{task_id}/edits`携带`expected_store_version`和`EditCommand`，接受后返回202及同版本envelope、变化活动/路段和可选job。`GET .../edit-jobs/{calculation_id}`读取重算状态及当前预览，均使用该任务Bearer。一个命令只编辑一天，可在operations中原子删除多个活动；新增引用服务端已核实候选；搜索新地点经独立选择接口加入。

网络结果不明时原样重放同一command_id；改参数必须新ID。409表示版本或幂等冲突，客户端先重新读取当前预览，不自动覆盖。后台路线失败保留已接受编辑；`PLANNER_EDIT_ROUTE_DEADLINE_SECONDS`默认120秒。重启恢复待查任务与具体故障验收见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#v4-edit-api)。


### v4预览编辑

`/preview`左侧拖动调整当天顺序，也可删除某一次活动、切换已查询到的交通方式；地图下方按地点批量删除关联活动，或把已有候选加入可用时段。一次请求未确认时先处理待同步编辑；路线计算期间仍可继续修改。网络响应不明时刷新会重放同一命令，版本冲突需用户明确重试。停止重试会重新读取服务器内容，已接受的编辑仍保留。

验证：`node tests/v4PreviewEditController.test.mjs`、`node tests/v4PreviewAccessStorage.test.mjs`（在frontend运行）；浏览器故障回放见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#v4-edit-ui)。地图下已支持搜索新景点和餐馆；新需求入口为`/plan`。


### v4目的地搜索

`POST /api/trip/v4/tasks/{task_id}/destinations/search`接收`keywords`、`category`（sightseeing或meal）及`page`，城市来自当前预览。结果包含名称、地址、参考cost、来源及是否可选；搜索本身不改草稿。

`POST .../destinations/select`接收服务端candidate_token、day_id、slot_id、两项期望版本、command_id和client_ref，成功返回同编辑API的202响应。请求结果不明时必须原样重放；过期新候选返回`DESTINATION_CANDIDATE_UNAVAILABLE`，重新搜索后再选择。两个接口均使用任务Bearer。

`DESTINATION_CANDIDATE_DB_PATH`默认`backend/data/destination_candidates.sqlite3`，必须与预览/观测库分开；`DESTINATION_CANDIDATE_TTL_SECONDS`默认1800秒。校验及验收边界见[CHG-003](docs/changes/CHG-003-planning-performance-observability.md#v4-destinations)。


每日安排初版与单日修订的结构校验会在观测中保留安全字段路径、错误类型及固定修复指引；超出8条时保留总数和截断标记，不保存完整模型回答。初版局部修复与格式修复共用一次额度；单日修订协议及按日最多三轮业务补查保持原规则。验收见[安全校验诊断](docs/changes/CHG-003-planning-performance-observability.md#v4-plan-validation-diagnostics)及[局部修复](docs/changes/CHG-003-planning-performance-observability.md#v4-local-plan-repair)。

单日修订的观测还记录`repair.*`阶段及每轮的`repair.round_result`。按`round_id`关联采集、合并修订、交通重算、重算后校验和是否采用；错误包含安全字段位置与条件，时间不一致时记录两个时刻及是否属于同一查询分钟。记录不包含完整模型回答，也不能补回旧任务未保存的错误；当前精度问题及实现/部署边界见[修订拒绝诊断](docs/changes/CHG-003-planning-performance-observability.md#v4-repair-rejection-diagnostics)。

主派发遇到区域或偏好引用错误时，会把具体条件、可用引用和原草案交给模型纠正；后端只合并受影响的引用字段，保留其他任务、目标、日期、事实和餐次锚点。该纠正与JSON格式修复共享一次额外调用；开启超时策略时也共享原调用期限。若纠正仍失败，但经重新校验的部分结果已为本次缺项提供可用候选，F09会在同一轮继续修订，不单独为这次引用错误多占一轮；没有可用候选时保持原上限并交付缺项。其他技术错误、来源损坏或身份不一致不套用此恢复策略。观测中的`master.decision.reference_repair`记录纠正条件，`repair.collection_recovery`记录继续/跳过修订及候选覆盖数量；实现与验证见[派发引用恢复](docs/changes/CHG-003-planning-performance-observability.md#v4-dispatch-reference-recovery)。

排查一份结果时，先只读导出该任务的诊断，不重新提交规划。以下命令从项目根目录运行；`--db`可指定观测库，默认`backend/data/tasks.sqlite3`，`--format json`可导出机器可读报告。

```bash
backend/venv/bin/python backend/scripts/diagnose_task.py <task_id>
```

新增诊断沿用SQLite观测：公共字段记录任务/父子调用标识、开始和结束时间、执行状态；输出记录`schema_version=1`、`stage`、`outcome`。阶段返回、校验通过、每轮采用及最终交付质量分别记录；`succeeded`只说明调用正常结束。`planning.v4.run`汇总初版每天实际景点、用餐、自由活动、缺失时段和补查停止原因，不代表后续编辑或数据库保存结果。错误只保留固定条件、安全字段路径及该条件所需的值，例如两个出发时刻。主派发目前细分区域/偏好引用的四种条件，其他未细分错误仍显示错误码并提示证据缺口。

报告按父子调用关联日期、轮次与版本，列出首条已记录异常、阶段经过、逐轮决策、交付质量及缺失/截断提示。它不读取完整请求或模型回答，不恢复、清理或修改任务；旧记录缺失时显示未知，不能用报告替代完整响应回放。修复流程为：固定一个任务的证据 → 最小离线复现 → 单项修复 → 同一用例回归；当前执行与遗留项见上述诊断记录。

v4中已查到有效天气但模型漏写出行建议时，会保留预报并显示建议尚未生成，不因此让整个规划失败；无效地点引用仍不进入草稿，可安全定位的活动错误按上述局部修复规则处理。真实失败与修复验证见[天气建议缺失](docs/changes/CHG-003-planning-performance-observability.md#v4-weather-advice-fallback)。


### 新规划入口

访问`/plan`填写需求，提交后进入`/preview`查看进度和可编辑结果；只保留当前预览访问入口，无历史列表。默认首页`/`与`/plan`使用同一新表单。日期按中国标准时间输入，预算指全程旅费；返程预留留空采用1小时，另算实际返程交通。

如果浏览器暂时无法保存访问信息，当前页面仍可继续查看预览，但刷新/关闭后可能无法恢复。页面提供重试保存，重试不会重新创建规划任务。验证：`cd frontend && node tests/v4TripRequest.test.mjs`；边界与证据见[新需求填写入口](docs/changes/CHG-003-planning-performance-observability.md#v4-home)。


`/preview`地图下的“搜索其他地点”可按景点或餐馆搜索当前城市，查看来源与参考消费，选定时段后加入当天。缺少可靠位置的结果不可加入；候选失效时重新搜索。网络结果不明时保留待同步选择，刷新会安全核对同一命令。验证和浏览器故障回放见[搜索页面](docs/changes/CHG-003-planning-performance-observability.md#v4-destination-ui)。


初始采集提前结束时，v4会重新校验已收集信息，并在信息有效时继续生成带提示的草稿；身份或事实引用损坏仍终止。规则与验收边界见[初始采集降级](docs/changes/CHG-003-planning-performance-observability.md#v4-initial-dispatch-recovery)。


### 默认用户流程

打开`/`或`/plan`填写需求，提交后进入`/preview`。预览支持编辑、搜索添加地点、导出和还原上次成功保存；工作台仍单独访问`/observability`，用户页面不提供入口。旧`/result`与v3 API继续保留兼容。

上海三日真实阶段续验及页面导出通过，但本例仍缺住宿候选、消费数据和部分交通/天气信息，活动时刻未全部核实。验收不代表每次模型规划均完整，也不是新的全程耗时基准。证据与剩余边界见[默认入口交付](docs/changes/CHG-003-planning-performance-observability.md#v4-default-entry)。

诊断报告补充：backend/scripts/diagnose_task.py 会按直接模型失败、异常结束和校验拒绝统计分布，并沿父链排除重复传播；新任务同时展示模型接口安全摘要、结束原因及具体拒绝条件。历史缺失字段保持未知，不回填猜测值。实现与离线验收见[日志补齐](docs/changes/CHG-003-planning-performance-observability.md#v4-complete-error-logging)。


### 人均预算与指定酒店

正式规划表单的预算为“人均预算（元/全程）”，新请求显式传入 `budget.amount_basis=per_person`；旧请求缺少该字段时保留 `whole_party` 原总预算语义，不重解释已保存行程。已知cost小计仍不代表完整旅费。

已订酒店位于目的地城市下方；先填写城市才能输入酒店名称，输入停顿350毫秒会自动查询，下拉最多展示5个酒店（含地址），支持鼠标或键盘选择；修改城市或酒店文本后需重新选择。`GET /api/poi/hotels?city=...&keywords=...` 通过现有高德MCP查询同城住宿。新表单提交 `lodging_poi_id` 和名称，后端按ID详情核实位置并生成有来源的住宿候选，浏览器坐标不作为可信事实。查不到可换关键词或清空酒店，由系统推荐；旧版纯文本请求暂保留兼容。查询成功不承诺营业或有房。

局部验证与部署状态见[预算及酒店修复](docs/changes/CHG-003-planning-performance-observability.md#v4-budget-hotel-selection)。
