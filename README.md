# HelloAgents智能旅行助手 🌍✈️

基于HelloAgents框架构建的智能旅行规划助手,集成高德地图MCP服务,提供个性化的旅行计划生成。

## 学习文档

- [学习目标与阶段计划](docs/learning-plan.md)：以后端和 Agent 开发为重点，包含实践任务、验收标准与当前进度。
- [学习日志](docs/learning-log.md)：记录实践中的问题、原因、修改和验证结果。
- [旅行规划术语与规则](CONTEXT.md)：已确认的业务语言与规划规则。

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
# 创建.env文件, 填入高德地图Web API Key 和 Web端JS API Key
cp .env.example .env
```

4. 启动开发服务器

```bash
npm run dev
```

5. 打开浏览器访问 `http://localhost:5173`

## 📝 使用指南

1. 在首页填写旅行信息:
   - 目的地城市
   - 旅行日期和天数
   - 交通方式偏好
   - 住宿偏好
   - 旅行风格标签

2. 点击"生成旅行计划"按钮

3. 系统将:
   - 调用HelloAgents Agent生成初步计划
   - Agent自动调用高德地图MCP工具搜索景点
   - Agent获取天气信息和路线规划
   - 整合所有信息生成完整行程

4. 查看结果:
   - 每日详细行程
   - 景点信息与地图标记
   - 交通路线规划
   - 天气预报
   - 餐饮推荐

## 🔧 核心实现

### 当前 Agent 编排

`backend/app/agents/trip_planner_agent.py` 中的 `MultiAgentTripPlanner` 顺序运行景点、天气、酒店和行程规划四个 Agent。前三个注册通过 `get_expanded_tools()` 发现的高德工具，最终规划 Agent 整合文本结果，不注册工具。注册时使用 `agent.add_tool(tool, auto_expand=False)`。

任务服务独立于 HTTP 连接运行，初始化及规划均在线程池执行。必要工具未调用、返回错误或空结果均使规划失败，不返回占位备用行程；不同规划的 Agent 对话历史隔离。天气只使用实际查询中日期匹配的预报，其余日期显示天气未知。

### HelloAgents 基础集成示意

下面是单 Agent 示例，不代表完整的四 Agent 运行流程：

```python
from hello_agents import SimpleAgent, HelloAgentsLLM
from hello_agents.tools import MCPTool

# 创建高德地图MCP工具
amap_tool = MCPTool(
    name="amap",
    server_command=["uvx", "amap-mcp-server"],
    env={"AMAP_MAPS_API_KEY": "your_api_key"},
    auto_expand=True
)

# 创建旅行规划Agent
agent = SimpleAgent(
    name="旅行规划助手",
    llm=HelloAgentsLLM(),
    system_prompt="你是一个专业的旅行规划助手..."
)

# 显式注册发现的子工具，与当前项目注册方式一致
for tool in amap_tool.get_expanded_tools():
    agent.add_tool(tool, auto_expand=False)
```

### MCP工具调用

Agent可以自动调用以下高德地图MCP工具:

- `maps_text_search`: 搜索景点POI
- `maps_weather`: 查询天气
- `maps_direction_walking_by_address`: 步行路线规划
- `maps_direction_driving_by_address`: 驾车路线规划
- `maps_direction_transit_integrated_by_address`: 公共交通路线规划

## 📄 API文档

启动后端服务后,访问 `http://localhost:8000/docs` 查看完整的API文档。

主要端点:

- `POST /api/trip/tasks` - 接收任务（202）；忙时返回 409 / TASK_BUSY，不排队
- `GET /api/trip/tasks` - 任务列表；支持 status、limit、offset
- `GET /api/trip/tasks/{task_id}` - 轻量状态与当前步骤；任务失败时查询仍返回 200
- `GET /api/trip/tasks/{task_id}/result` - 成功行程；运行中/失败/中断分别返回 409 及对应错误码
- `GET /api/trip/tasks/{task_id}/spans` - 调用树摘要
- `GET /api/trip/tasks/{task_id}/spans/{span_id}` - 单步骤输入输出与错误详情
- `GET /api/map/poi` - 搜索POI
- `GET /api/map/weather` - 查询天气
- `POST /api/map/route` - 规划路线

## 本地任务观测

启动 `python3 dev.py` 后，从首页的“查看任务与调用记录”进入 `/observability`。提交需求后，首页每 2 秒读取真实步骤；刷新会恢复当前任务查询。观测页可筛选历史、展开 Agent → 模型/工具调用树、点击查看脱敏输入输出，并打开成功行程。终态停止自动轮询；需要查看新提交的任务时点击刷新。

第一版使用单后端进程、一个活动规划任务，子 Agent 顺序执行；请勿用多个 worker 共享此数据库。关闭浏览器不会取消任务；后端正常退出等待正在执行的任务，强制退出后下次启动标记中断，不自动重跑。旧 `/api/trip/plan` 已移除。

配置写入 `backend/.env`，经 `app/config.py` 读取：

| 配置 | 默认值 |
| --- | --- |
| `TASK_DB_PATH` | `backend/data/tasks.sqlite3`（默认解析为绝对路径） |
| `OBSERVATION_RETENTION_DAYS` | 7 天 |
| `OBSERVATION_MAX_BYTES` | 209715200（200 MiB 治理目标，包含 WAL/SHM） |
| `OBSERVATION_CONTENT_LIMIT` | 65536（单步骤输入、输出各 64 KiB） |

启动及接收新任务前清理过期/超容量的已结束任务，连同行程和步骤一起删除；活动任务保留。无法腾出空间时停止保存观测详情并提示不完整。内容先脱敏后限长；截断有标记，未知温度与未知用量不填零。数据库及辅助文件不入 Git。

观测写入失败不改变业务判定；任务结果保存失败不能显示为成功。数据库无法写入时，当前进程仅保留有界错误摘要，重启后无法还原未落盘内容。输入校验拒绝仍返回 422，拒绝记录展示后置；第一版不提供自动重试、取消、重放和 Token 用量统计。观测成功不等于行程质量或所有价格信息都经过核验。

验证：`cd backend && PYTHONPATH=. venv/bin/python -m unittest discover -s tests -v`；`cd frontend && npm run build`。替代依赖测试与真实调用验收结论见[学习日志](docs/learning-log.md)。

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
