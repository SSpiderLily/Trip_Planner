/* 学习导航只维护职责、流程与阅读入口；源码在点击时从当前项目按需读取。 */
'use strict';
const sources = {};
function source(id, file, marker, label) { sources[id] = {file, marker, label}; }
const f = 'frontend/src/', b = 'backend/app/';
source('router', f+'main.ts', 'const router =', 'main.ts · router');
source('vite', 'frontend/vite.config.ts', 'export default defineConfig', 'vite.config.ts · server / alias');
source('home', f+'views/Home.vue', 'const handleSubmit =', 'Home.vue · handleSubmit');
source('poll', f+'views/Home.vue', 'const poll =', 'Home.vue · poll');
source('api', f+'services/api.ts', 'const apiClient =', 'api.ts · apiClient');
source('submit', f+'services/api.ts', 'export async function submitTask', 'api.ts · submitTask');
source('resultapi', f+'services/api.ts', 'export async function getTaskResult', 'api.ts · getTaskResult');
source('startup', b+'api/main.py', 'async def startup_event', 'main.py · startup_event');
source('mount', b+'api/main.py', 'app.include_router(trip.router', 'main.py · include_router');
source('create', b+'api/routes/trip.py', 'async def create_task', 'trip.py · create_task');
source('result', b+'api/routes/trip.py', 'async def task_result', 'trip.py · task_result');
source('editapi', b+'api/routes/trip.py', 'async def recalculate_day', 'trip.py · recalculate_day');
source('mapapi', b+'api/routes/map.py', 'async def search_poi', 'map.py · search_poi');
source('request', b+'models/schemas.py', 'class TripRequest(', 'schemas.py · TripRequest');
source('dates', b+'models/schemas.py', 'def validate_dates(self)', 'schemas.py · validate_dates');
source('editrequest', b+'models/schemas.py', 'class RecalculateDayRequest(', 'schemas.py · RecalculateDayRequest');
source('conditions', b+'models/itinerary.py', 'class Conditions(', 'itinerary.py · Conditions');
source('collection', b+'models/itinerary.py', 'class Collection(', 'itinerary.py · Collection');
source('draftmodel', b+'models/itinerary.py', 'class Draft(', 'itinerary.py · Draft');
source('itinerary', b+'models/itinerary.py', 'class Itinerary(', 'itinerary.py · Itinerary');
source('place', b+'models/itinerary.py', 'class Place(', 'itinerary.py · Place');
source('types', f+'types/itinerary.ts', 'export interface RouteItinerary {', 'types/itinerary.ts · RouteItinerary');
source('daytype', f+'types/itinerary.ts', 'export interface RouteDay {', 'types/itinerary.ts · RouteDay');
source('legtype', f+'types/itinerary.ts', 'export interface Leg {', 'types/itinerary.ts · Leg');
source('factory', b+'agents/trip_planner_agent.py', 'def get_trip_planner_agent():', 'trip_planner_agent.py · get_trip_planner_agent');
source('accept', b+'services/task_service.py', 'async def _accept(', 'TaskService · _accept');
source('execute', b+'services/task_service.py', 'async def _execute(', 'TaskService · _execute');
source('taskplan', b+'services/task_service.py', 'def _plan(', 'TaskService · _plan');
source('taskstatus', b+'services/task_service.py', 'async def status(', 'TaskService · status');
source('failure', b+'services/task_service.py', 'async def _fail(', 'TaskService · _fail');
source('taskmetrics', b+'services/task_service.py', 'async def metrics(', 'TaskService · metrics');
source('planner', b+'agents/route_planner.py', 'def plan_trip(', 'RouteTripPlanner · plan_trip');
source('roles', b+'agents/route_planner.py', 'def __init__(self, llm=None, mcp=None)', 'RouteTripPlanner · 三角色初始化');
source('collect', b+'agents/route_planner.py', 'def collect(', 'RouteTripPlanner · collect');
source('model', b+'agents/route_planner.py', 'def model(', 'RouteTripPlanner · model');
source('tool', b+'agents/route_planner.py', 'def tool(', 'RouteTripPlanner · tool');
source('draft', b+'agents/route_planner.py', 'def draft(', 'RouteTripPlanner · draft');
source('context', b+'agents/route_planner.py', 'def context(', 'RouteTripPlanner · context');
source('ground', b+'agents/route_planner.py', 'def ground(', 'RouteTripPlanner · ground');
source('evaluate', b+'agents/route_planner.py', 'def _evaluate(', 'RouteTripPlanner · _evaluate');
source('upgrade', b+'agents/route_planner.py', 'def _upgrade_v3(', 'RouteTripPlanner · _upgrade_v3');
source('route', b+'agents/route_planner.py', 'def route(', 'RouteTripPlanner · route');
source('metadata', b+'agents/route_planner.py', 'def _reference_metadata(', 'RouteTripPlanner · _reference_metadata');
source('prompt', b+'agents/route_planner.py', 'COLLECT_PROMPT =', 'route_planner.py · COLLECT_PROMPT');
source('planprompt', b+'agents/route_planner.py', 'PLAN_PROMPT =', 'route_planner.py · PLAN_PROMPT');
source('agent', b+'agents/execution.py', 'class PlanningAgent(', 'execution.py · PlanningAgent');
source('decode', b+'agents/execution.py', 'def decode_result(', 'execution.py · decode_result');
source('forecasts', b+'agents/execution.py', 'def forecast_by_date(', 'execution.py · forecast_by_date');
source('maps', b+'services/planning_maps.py', 'async def call_with_timeout(', 'planning_maps.py · call_with_timeout');
source('mcp', b+'services/amap_service.py', 'def get_amap_mcp_tool(', 'amap_service.py · get_amap_mcp_tool');
source('amap', b+'services/amap_service.py', 'def search_poi(', 'AmapService · search_poi');
source('stdio', 'backend/scripts/amap_stdio.py', 'import ', 'amap_stdio.py · MCP 启动适配');
source('llm', b+'services/llm_service.py', 'def invoke_with_usage(', 'UsageAwareLLM · invoke_with_usage');
source('routeoptions', b+'services/route_options_service.py', 'def options(', 'RouteOptionsService · options');
source('routequery', b+'services/route_options_service.py', 'def _query(', 'RouteOptionsService · _query');
source('choose', b+'services/route_options_service.py', 'def choose_mode(', 'route_options_service.py · choose_mode');
source('schedule', b+'services/schedule_service.py', 'def schedule_day(', 'schedule_service.py · schedule_day');
source('opening', b+'services/schedule_service.py', 'def opening_conflict(', 'schedule_service.py · opening_conflict');
source('price', b+'services/cost_service.py', 'def queried_reference_cost(', 'cost_service.py · queried_reference_cost');
source('daycost', b+'services/cost_service.py', 'def summarize_day_costs(', 'cost_service.py · summarize_day_costs');
source('tripcost', b+'services/cost_service.py', 'def summarize_trip(', 'cost_service.py · summarize_trip');
source('editor', b+'services/day_edit_service.py', 'def recalculate(', 'DayEditService · recalculate');
source('tokens', b+'services/day_edit_service.py', 'def attach_tokens(', 'DayEditService · attach_tokens');
source('sign', b+'services/day_edit_service.py', 'def _sign_snapshot(', 'DayEditService · _sign_snapshot');
source('verify', b+'services/day_edit_service.py', 'def _decode(', 'DayEditService · _decode');
source('operation', b+'services/day_edit_service.py', 'def _apply_operation(', 'DayEditService · _apply_operation');
source('resolve', b+'services/day_edit_service.py', 'def _resolve_poi(', 'DayEditService · _resolve_poi');
source('rebuild', b+'services/day_edit_service.py', 'def _rebuild_day(', 'DayEditService · _rebuild_day');
source('editmode', b+'services/day_edit_service.py', 'def _build_leg(', 'DayEditService · _build_leg');
source('insertion', b+'services/day_edit_service.py', 'def _best_insertion_index(', 'DayEditService · _best_insertion_index');
source('edits', f+'views/RouteResultEditable.vue', 'function appendEdit(', 'RouteResultEditable.vue · appendEdit');
source('flush', f+'views/RouteResultEditable.vue', 'async function flushEdits(', 'RouteResultEditable.vue · flushEdits');
source('persist', f+'views/RouteResultEditable.vue', 'function persistPlan(', 'RouteResultEditable.vue · persistPlan');
source('current', f+'views/RouteResultEditable.vue', 'const currentPlan =', 'RouteResultEditable.vue · currentPlan');
source('draw', f+'views/RouteResultEditable.vue', 'function drawMap(', 'RouteResultEditable.vue · drawMap');
source('view', f+'views/RouteResult.vue', 'const raw =', 'RouteResult.vue · schema_version 分发');
source('optimistic', f+'services/dayEditing.ts', 'export function applyOptimisticEdit(', 'dayEditing.ts · applyOptimisticEdit');
source('compact', f+'services/pendingDayEdits.ts', 'export function compactCancelledPendingAdds(', 'pendingDayEdits.ts · compactCancelledPendingAdds');
source('schema', b+'services/task_repository.py', 'SCHEMA =', 'task_repository.py · SCHEMA');
source('save', b+'services/task_repository.py', 'def succeed(', 'TaskRepository · succeed');
source('recover', b+'services/task_repository.py', 'def recover(', 'TaskRepository · recover');
source('cleanup', b+'services/task_repository.py', 'def cleanup(', 'TaskRepository · cleanup');
source('repo', b+'services/task_repository.py', 'class TaskRepository:', 'task_repository.py · TaskRepository');
source('sanitize', b+'services/observation_service.py', 'def sanitize(', 'observation_service.py · sanitize');
source('span', b+'services/observation_service.py', 'def span(', 'observation_service.py · span');
source('observe', b+'services/observation_service.py', 'class ObservedLLM:', 'observation_service.py · ObservedLLM');
source('metrics', b+'services/metrics_service.py', 'def build_metrics(', 'metrics_service.py · build_metrics');
source('tokenusage', b+'services/observation_service.py', 'def token_usage(', 'observation_service.py · token_usage');
source('loadtask', f+'views/Observability.vue', 'async function loadTask(', 'Observability.vue · loadTask');
source('loaddetail', f+'views/Observability.vue', 'async function loadDetail(', 'Observability.vue · loadDetail');
source('showresult', f+'views/Observability.vue', 'async function showResult(', 'Observability.vue · showResult');
source('forest', f+'services/observationView.ts', 'export function buildForest(', 'observationView.ts · buildForest');
source('export', f+'views/TripImageExport.vue', 'async function exportImages(', 'TripImageExport.vue · exportImages');
source('render', f+'views/TripImageExport.vue', 'async function renderElement(', 'TripImageExport.vue · renderElement');
source('split', f+'views/TripImageExport.vue', 'function splitDayByMeasuredBlocks(', 'TripImageExport.vue · splitDayByMeasuredBlocks');
source('config', b+'config.py', 'class Settings(', 'config.py · Settings');
source('dev', 'dev.py', 'def main()', 'dev.py · main');
source('oldagent', b+'agents/trip_planner_agent.py', 'class MultiAgentTripPlanner:', '历史实现 · MultiAgentTripPlanner');
source('oldresult', f+'views/Result.vue', '<script', '历史文件 · Result.vue');
source('timeline', f+'components/ObservationTimeline.vue', 'const forest =', 'ObservationTimeline.vue · forest / rows');
source('analysis', f+'components/ObservationAnalysis.vue', 'const props =', 'ObservationAnalysis.vue · props / 派生图表');
source('details', f+'components/ObservationDetails.vue', 'const props =', 'ObservationDetails.vue · props / 详情展示');

// 模块只列主要直接依赖；数据契约、配置等横向依赖在详情说明。
const layers = [
 ['浏览器 / Vue 3','页面、状态与交互',['home','result','editqueue','observability','observeui','export','browserapi']],
 ['HTTP / FastAPI','校验输入，分发请求',['http','maphttp','contracts']],
 ['任务与规划编排','接收与执行分开；角色顺序运行',['tasks','planner','agent','editor']],
 ['程序核实与计算','基于查询事实计算，不由模型补价',['routes','schedule','cost']],
 ['模型与地图接入','后端查询 / 浏览器绘图区分',['llm','maps','amap','mapjs']],
 ['数据与横向支撑','存储、调用记录、指标、配置',['repository','observation','metrics','config']]
];
const modules = {};
function module(id,name,short,role,input,output,calls,refs,note) { modules[id]={name,short,role,input,output,calls,refs,note}; }
module('home','需求首页','输入 → 提交 → 轮询','Home.vue 收集城市、到离时间、预算、住处与备注，发起一次规划。','表单状态 formData；到离地点通过高德搜索选取 POI ID。','TripFormData 请求；activeTripTask 任务编号；成功后缓存 tripPlan 并跳转 /result。',['browserapi'],['home','poll','api'],'日期时间以 +08:00 序列化。页面卸载只停止轮询，不取消已接收任务。');
module('result','行程结果与编辑','v3 编辑 / 历史只读','RouteResult.vue 按 schema_version 分发；v3 由 RouteResultEditable.vue 展示活动、路段、提示和地图。','sessionStorage 中的 tripPlan、tripTaskId；服务端返回的每日 edit_token。','当前可见行程 currentPlan；局部编辑操作；导出数据 exportPlan。',['editqueue','browserapi','mapjs','export'],['view','current','edits','persist'],'当前路由 /result 指向 RouteResult.vue，不能把旧 Result.vue 当作主入口。');
module('observability','观测页面','任务 → 调用 → 图表','Observability.vue 查询任务、调用摘要和详情，用时间轴与分析视图解释生成过程。','URL 中 task / view；任务摘要、Span 列表、按需获取的详情和已结束任务指标。','执行轨迹、Token 分析、工具分析、异常调用；成功任务可进入结果页。',['browserapi','observeui'],['loadtask','loaddetail','forest','showresult'],'运行时每 2 秒更新状态和摘要，详情按选择加载；调用树通过 parent_span_id 组装。');
module('export','长图导出','冻结画面 → PNG','TripImageExport.vue 复制当前可见行程与各日编辑状态，渲染离屏排版后生成图片。','exportPlan 和 statusMap（包含更新中、失败等展示状态）。','一张或多张 PNG；导出过程中清理临时切片。',[],['export','render','split'],'只在浏览器运行。最大排版高度 12000 CSS 像素、渲染 scale=2；超长按日或按块切分，不发规划请求。');
module('http','旅行任务 API','任务 / 结果 / 编辑 / 调用','trip.py 在 /api/trip 下接收任务、状态、结果、编辑、Span 和指标请求。','FastAPI 校验后的 TripRequest / RecalculateDayRequest，以及 URL 中的 task_id。','202 接收响应；状态、结果、调用记录、指标；或带错误码的 HTTP 响应。',['tasks','editor','repository'],['mount','create','result','editapi'],'GET result 返回前附加编辑签名；POST 编辑在线程池中调用服务，不启动新的生成任务。');
module('maphttp','地图查询 API','搜索 / 详情 / 天气 / 路线','map.py 为首页到离地点搜索、结果页地点搜索和详情提供接口；还保留独立天气与路线接口。','关键词和城市，或 POI ID、路线起终点。','经 AmapService 归一化的地点及查询响应。',['amap'],['mapapi','amap'],'生成规划在后端直接调用地图适配，不经浏览器 GET /api/map/poi 再绕回后端。');
module('contracts','请求与数据契约','Pydantic / TypeScript','schemas.py 校验 HTTP 请求；itinerary.py 校验模型输出与最终行程；前端 types 描述消费的数据结构。','未校验请求 JSON；模型 JSON 文本；程序组装结果。','TripRequest、Collection、Conditions、Draft、Itinerary；前端 RouteItinerary / RouteDay。',[],['request','dates','collection','draftmodel','itinerary','types'],'Draft 目前仍包含住宿 source、user_input；程序随后校验是否符合表单。未填住处时不让模型选择用户住宿的独立修复已随性能实验回滚，当前待重新实施；状态见 CHG-003。');
module('tasks','任务服务','登记 → 后台 → 保存','TaskService 用锁限制一个活动生成任务，持有后台 asyncio 任务，再将同步规划放到线程池。','已校验的 TripRequest；planner_factory；任务仓库与观测服务。','task_id 与 accepted；后台最终写 succeeded / failed；查询状态、结果与工程指标。',['repository','planner','observation','metrics'],['accept','execute','taskplan','taskstatus'],'HTTP 返回 202 后执行继续。当前是单进程、单活动规划，不是持久消息队列，也没有取消或自动重放。');
module('planner','三角色规划编排','搜集 → 安排 → 修改','RouteTripPlanner.plan_trip 控制整个生成顺序、候选池、查询额度、检查与有界修改。','TripRequest；每次任务独立的 state、候选池与路线查询缓存。','校验后的 schema_version=3 行程；活动、路段、天气、时间、费用与 issues。',['agent','maps','routes','schedule','cost'],['factory','planner','collect','draft','evaluate','upgrade'],'候选搜集、行程安排、行程修改为三个角色。角色次数依条件变化；不会固定为每个角色调用一次。');
module('agent','角色与结构化输出','提示词 → JSON → 校验','PlanningAgent 继承 SimpleAgent；model() 把 schema 与上下文交给模型，解析 JSON 并用 Pydantic 校验。','system_prompt；request、conditions、candidates、weather、draft、problems 等阶段上下文。','Collection（规划条件与搜索词）或 Draft（日程建议）。',['llm'],['roles','model','context','agent','prompt','planprompt','draftmodel'],'各阶段提供具体 JSON 示例。当前三个角色 enable_tool_calling=False，程序调用 MCP；每次 run 前后清空角色历史。');
module('editor','当天编辑服务','签名快照 + 操作 → 重算','DayEditService 从签名快照恢复当天，校验操作，核实新增地点并重建当天路线、时间、费用和问题。','已成功任务的持久结果与请求；edit_token；最多 20 项 operations。','request_id、client_revision、date、day（含新签名）和服务端 cost_summary。',['repository','amap','maps','routes','schedule','cost'],['tokens','sign','verify','editor','operation','rebuild'],'不调用规划角色，不写回 task_results。签名密钥随服务进程生成；重启后旧 token 失效，需重载结果。');
module('routes','路线候选与方式选择','多种方式 → 选中方式','RouteOptionsService 查询每条路段的步行、骑行、公交；自驾偏好增加驾车，归一化秒数为分钟。','已核实起终点、城市、偏好；query_tool 调用器。','options 各方式的耗时/距离/状态；selected_mode、fastest_mode；每次实例的查询缓存。',['maps'],['routeoptions','routequery','choose','route','editmode'],'初次生成参考偏好；短途步行有单独规则。编辑中新路段选已知最快，未变路段保留选择。失败也计额度。');
module('schedule','每日时间计算','活动 + 路段 → 参考时刻','schedule_day 按顺序累加活动与当前采用路段耗时，结合普通窗口、到离时刻和系统缓冲。','day.activities、day.legs 与 planning_conditions。','活动 start_at / end_at；time_summary；opening_conflict 用可解析开放时段判断可能冲突。',[],['schedule','opening'],'普通窗口 09:00—19:00，到达后和离开前各 30 分钟缓冲。未知耗时会中断后续时刻推算，不按零计。');
module('cost','查询费用汇总','价格事实 → 已知小计','cost_service 只接受高德查询出的可解析非负数，按门票、餐饮、住宿汇总，再比较人均预算。','候选 POI 的 reference_cost 与 cost_basis；每日活动、住宿晚数、预算。','reference_cost；每日和全程 known_total、unknown_count、complete 与预算状态。',[],['metadata','price','daycost','tripcost'],'交通不计入；住宿必须能确认 room_night 才乘晚数，住宿全程费用放入第一日汇总。模型 estimated_cost 不进入 v3 预算。');
module('editqueue','前端编辑队列','乐观列表 → 串行批次','前端先更新可见列表，再合并待发操作；同一天保持一批在途，成功后对新基准继续叠加待发操作。','add / delete / move / select_mode 操作；accepted、pending、inFlight 和 revision。','更新中的 viewDay；每批至多 20 操作；成功后新的 accepted 和浏览器缓存。',['browserapi'],['optimistic','compact','edits','flush','persist'],'400ms 合并连续操作。更新中隐藏过期时间与费用；失败保留当前列表和待发操作，用户可重试。');
module('llm','模型接入与用量','HelloAgents → 模型 API','UsageAwareLLM 继承 HelloAgentsLLM，用 chat.completions.create 获取文本与供应商 usage。','实际 messages、model、temperature、max_tokens；配置由框架环境约定读取。','模型文本以及供应商返回的 usage；观测代理从 usage 中采集用量。',[],['llm','observe','tokenusage'],'不固定当前供应商或模型名称。缺失 Token 保持未知；网页不显示本地凭据或真实模型响应。');
module('maps','规划地图超时适配','MCP 会话 + timeout','PlanningMaps 通过 MCPClient 创建会话，在 asyncio.timeout 内执行 call_tool 并退出会话。','MCPTool 的服务命令、环境与参数；工具名、arguments、超时秒数。','第三方原始 MCP 结果，交给执行解析器和路线服务处理。',['amap'],['maps','tool','decode'],'默认单次工具超时 25 秒。生成由程序显式调用，编辑路线查询还受剩余总期限约束。');
module('amap','高德工具与归一化','uvx → MCP → 地图服务','get_amap_mcp_tool 初始化 MCPTool 并发现工具；AmapService 归一化独立地图 API 所用的 POI、天气与路线。','服务器配置、工具调用参数、地点关键词或 ID。','展开工具元信息；高德响应；POI 的 ID、坐标、图片、价格、开放时间等字段。',[],['mcp','amap','stdio'],'uvx 独立环境固定 amap-mcp-server==0.1.11 和 pydantic==2.13.5。stdio 适配避免第三方调试输出污染协议。');
module('mapjs','浏览器地图','坐标 → 标记与顺序线','RouteResultEditable.vue 加载高德 JS SDK，按当前日定位地点、绘制标记与顺序示意。','当前活动、住宿和到离地点的经纬度；浏览器构建时的 JS Key 配置。','可交互的地点地图；SDK 不可用时页面仍可显示位置示意。',[],['draw'],'前端 JS Key 与后端地图 Web 服务 Key 分开。当前顺序连线不是道路轨迹，也不是导航路线。');
module('repository','SQLite 任务仓库','tasks / task_results / spans','TaskRepository 每次调用独立连接，业务状态与结果在事务中保存；启动恢复中断并执行清理。','已脱敏请求/结果、Span 数据、task_id 与查询筛选条件。','可跨后端重启读取的任务与生成结果；调用摘要/详情；期限与容量清理。',[],['schema','repo','save','recover','cleanup'],'活动任务不清理；历史清理会级联删除结果和调用。局部编辑没有写入此仓库。');
module('observation','调用观测与脱敏','ContextVar → Span → SQLite','观测上下文关联 task_id；嵌套 span 记录 parent_span_id；ObservedLLM 在模型调用周围记录消息、结果和用量。','planning / agent / llm / tool / validation 的输入、输出、时间、错误和 usage。','脱敏且限长的 spans；失败摘要；observation_incomplete 标记。',['repository'],['taskplan','span','sanitize','observe'],'先脱敏再限长，单步骤输入/输出各默认 64 KiB。采集失败不会替换业务结果；调用记录与 print 运行日志不同。');
module('metrics','工程指标计算','Span 元数据 → 汇总','build_metrics 按 operation_type 区分模型与工具，追溯父 Agent，统计次数、耗时与用量覆盖。','已结束任务摘要与 metric_spans 元数据。','任务总耗时、模型/工具汇总、按角色用量与按工具明细。',[],['taskmetrics','metrics'],'任务总耗时用任务起止时间，不累加父子 Span。指标反映执行过程，不能证明行程质量。');
module('browserapi','前端 API 与路由','axios 请求 / 路由分发','main.ts 注册页面路由；api.ts 统一封装 HTTP 请求、错误文本与步骤名称。','页面传入的需求、任务编号、地点查询或编辑参数。','后端响应的 TypeScript 对象；异常继续交给页面展示。',['http','maphttp'],['router','api','submit','resultapi','vite'],'Vue 与 TS 在浏览器运行；Vite 用 Node 构建前端。axios baseURL 默认直连后端，Vite 的代理配置不会自动改变 axios 地址。');
module('observeui','观测展示组件','时间轴 / 分析 / 详情','观测页把数据通过 props 分给 ObservationTimeline、ObservationAnalysis、ObservationDetails；子组件发出选择或刷新事件。','任务、Span 摘要、指标、选中调用与详情；当前时间。','调用树与共用时间轴；Token、工具、异常图表；脱敏输入输出详情。',[],['timeline','analysis','details','forest'],'子组件处理展示与选择事件，网络读取由 Observability.vue 负责；缺失时间与用量保留未知。');
module('config','配置与启动','环境 → Settings → 服务实例','config.py 读取环境和默认值；FastAPI startup_event 初始化仓库、观测、任务与编辑服务。','后端环境变量；前端 VITE_* 构建变量；本地依赖与端口。','Settings；app.state 服务实例；dev.py 同时启动前后端。',[],['config','startup','dev','vite'],'前端 axios 默认直连 localhost:8000，实际不走 Vite /api 代理；后端 CORS 负责开发时跨域。');

function step(title,actor,input,actions,output,next,failure,refs,mods=[]) { return {title,actor,input,actions,output,next,failure,refs,mods}; }
const flows = {
 generate:{name:'生成行程',intro:'一次生成 = 一份独立任务。HTTP 接收、后台执行和前端轮询是三条相互协作的路径；规划角色在后台顺序执行。',steps:[
 step('提交旅行需求','浏览器 · Home.vue','城市、到离时刻、预算、住处、偏好与完整备注。',['handleSubmit 检查到离时刻并以 +08:00 序列化。','submitTask 用 axios POST /api/trip/tasks；超时为 120000ms。'],'请求 JSON（TripFormData）。','交给 FastAPI 校验；页面开始显示提交状态。','本地表单检查未通过则不提交；HTTP 请求失败展示错误。',['home','submit','api'],['home','http']),
 step('校验请求、登记任务','后端 · FastAPI → TaskService','请求 JSON。',['TripRequest 推导日期、统一时区，校验日期范围与 1—30 天。','create_task 要求到离时间；_accept 用锁检查是否已有活动任务。','cleanup 后生成 task_id，把脱敏请求写入 tasks，再持有后台 _execute。'],'HTTP 202：{ task_id, status: accepted }。','前端保存 localStorage.activeTripTask，每 2 秒 GET /api/trip/tasks/{task_id} 轮询；后端继续执行。','校验失败 422；忙时 409 / TASK_BUSY，不排队；登记失败 503，不启动规划。',['dates','create','accept'],['contracts','tasks']),
 step('在后台线程执行规划','后端 · TaskService','已校验请求对象和 task_id。',['_execute 把任务设为 running。','run_in_threadpool 执行 _plan，不占用 FastAPI 事件循环等待模型。','_plan 建立观测上下文，工厂获取 RouteTripPlanner，再调用 plan_trip。'],'每次任务独立的 state：候选池、条件、反馈、天气与路线服务缓存。','进入景点搜集阶段；前端轮询独立进行。','浏览器断开不取消执行；规划器初始化失败进入任务失败路径。',['execute','taskplan','factory','planner'],['tasks','planner','observation']),
 step('理解需求并核实候选','后端 · 候选搜集角色 + 程序','request；剩余查询额度；已有反馈。',['模型输出 Collection：第一次包含 Conditions 和 searches；程序复制显式表单条件。','程序逐项执行 maps_text_search，必要时 maps_search_detail。','只将带有效 ID 和坐标的地点放入 state.pool，以 source_id 关联；每次搜索最多处理前三项。','搜集阶段最多两轮；失败也占额度并把反馈交回模型。'],'统一 Conditions；已核实的景点候选池；查询反馈。','候选可用后查询天气，再安排游玩分布。','完全没有可定位景点则失败；单次搜索失败可在剩余额度内补查。',['collect','model','tool','conditions'],['planner','agent','maps']),
 step('查天气、形成分布初稿','后端 · 地图查询 + 行程安排角色','候选池、规划条件、城市。',['maps_weather 查询预报，forecast_by_date 按日期建立映射。','draft(layout) 将 schema、request、conditions、candidates、weather 给安排角色。','模型返回完整 Draft：住宿基点和每日有序活动，不负责生成最终真实路段。'],'天气日期映射；分布初稿 layout。','初稿供住宿与餐饮候选搜集使用。','天气失败保留提示；格式不合法时整次规划至多一次 draft 结构修复，不重新搜索。',['planner','forecasts','draft','draftmodel'],['planner','agent','maps']),
 step('补充住宿餐饮、生成终稿','后端 · 候选搜集 → 行程安排','layout 与现有条件、候选、天气。',['collect(support, layout) 在剩余额度内搜索顺路住宿区域和用餐区域。','用户已填住处时优先搜索原文，不能换成其他酒店。','draft(final, layout) 将新增候选交给安排角色，生成完整 Draft。'],'Draft：lodging_base + days[].activities[]；活动引用候选 source_id。','进入程序核实和计算，不直接交给页面。','当前住宿来源仍在 Draft 模型输出中；错误的 source=user、user_input为空可能通过结构校验，随后被业务校验拒绝。格式修复次数与上一阶段共享。',['collect','draft','context','planprompt'],['planner','agent']),
 step('核实地点、计算行程','后端 · 程序规则','Draft + state.pool + Conditions。',['_evaluate 检查每日日期、活动 ID、需求 ID 与住宿条件；ground 用候选事实替换地点字段。','按活动顺序建立路段，首尾按到离点或住宿基点连接；查询多种交通方式。','_upgrade_v3 去掉模型费用，用查询元数据生成 reference_cost。','schedule_day 推算时刻；费用服务汇总；组装天气、缺失信息和可能冲突 issues。'],'schema_version=3 行程；含活动、路段、时间摘要、查询费用和问题。','若有 needs_adjustment 则进入修改；否则完成最终校验。','日期不符或引用未知地点为失败；未知路线、价格等可以带提示交付，不伪造事实。',['evaluate','ground','route','upgrade','schedule','daycost'],['planner','routes','schedule','cost']),
 step('有条件地修改并终检','后端 · 行程修改角色 + 程序','当前 Draft、已计算行程与 issues。',['只有存在 needs_adjustment 才调用修改角色，最多两次。','模型返回完整 Draft，每份修改提案都重新 evaluate；如果引入新的已知冲突则拒绝。','修改未完成时保留上一份通过基本校验的结果并提示；最后用 Itinerary 校验结构。'],'最终 Itinerary；稳定 issue_id。','返回 TaskService 准备保存。','初次生成没有已核实景点不能交付；最终未通过校验则任务失败。',['planner','evaluate','itinerary'],['planner','agent']),
 step('保存任务结果','后端 · TaskService → TaskRepository','Itinerary.model_dump()。',['对结果脱敏。','repository.succeed 在同一事务内更新 succeeded 并写入 task_results.result_data。','生成过程的 Span 已分别增量写入；解除单活动任务占用。'],'持久化任务摘要与生成结果。','前端下一次轮询检测到 succeeded，然后请求 result。','结果保存失败记为 RESULT_SAVE_FAILED；观测保存失败只提示 observation_incomplete。',['execute','save','failure'],['tasks','repository']),
 step('取结果并展示','浏览器 + 后端结果 API','轮询状态 succeeded；task_id。',['Home.poll 调用 GET /api/trip/tasks/{id}/result。','结果 API 读取已保存结果，attach_tokens 在响应副本的每一天附签名。','前端把 tripPlan / tripTaskId 写入 sessionStorage，跳转 /result。','RouteResult 按版本分发；v3 显示活动、路段、费用、问题与地图。'],'可查看并局部编辑的行程页面。','用户可进入编辑或导出；成功任务也可从观测页重载。','状态查询网络错误不等于规划失败；历史 v1/v2 只读，结果未就绪返回 409。',['poll','result','tokens','view','draw'],['home','result','editor'])]},
 edit:{name:'当天编辑',intro:'编辑 = 签名当天快照 + 有序操作列表。服务端使用生成时的全局条件；前端维护当前可见行程；不会调用三角色重新生成。',steps:[
 step('选择编辑操作','浏览器 · 结果页','某一天的活动或路段；可搜索并查看新增 POI 详情。',['支持 add_poi、确认后的 delete_activity、move_activity、select_leg_mode。','applyOptimisticEdit 先改可见列表/交通选择，不自行计算新耗时和价格。'],'待发 operations；更新中的 viewDay。','appendEdit 合并连续操作。','新增地点重复或缺少编辑凭据时阻止操作；删除需要用户确认。',['edits','optimistic','editrequest'],['result','editqueue']),
 step('合并操作、串行发出','浏览器 · 编辑队列','accepted 已确认日快照、pending、inFlight、revision。',['等待 400ms 合并；尚未发出的新增后删除可抵消。','同一天只有一批在途，每批最多 20 项。','发送 task_id、date、edit_token、client_revision、request_id、operations。'],'POST /api/trip/recalculate-day 请求。','FastAPI 校验 RecalculateDayRequest 后在线程池调用编辑服务。','更新中隐藏过期派生数据；已有失败时等待重试，不继续自动发送。',['compact','flush','editapi','editrequest'],['editqueue','http']),
 step('恢复可信当天快照','后端 · DayEditService','操作列表 + 签名 token；仓库中的生成结果与请求。',['确认原任务 succeeded、v3、目标日期存在。','用 HMAC 比较签名，检查 task_id 与 date，解出 day 和 revoked_requirement_ids。','全局条件来自持久结果，不能由客户端任意改写。'],'通过校验的 day；已撤销必去要求；原全局条件。','按列表顺序应用操作。','签名无效或服务重启返回 EDIT_TOKEN_INVALID；历史版本或已清理任务拒绝编辑。',['editor','verify','sign'],['editor','repository']),
 step('执行操作并重算当天','后端 · 编辑 / 地图 / 计算服务','可信 day 与 operations。',['新增 POI 查详情并确认城市，游玩参考时长固定 90 分钟；按插入成本选择位置。','删除必去活动同时撤销该地点要求；上移下移仅在当天进行。','重建路段，保留未变路段方式；新路段选已知最快。','schedule_day、费用和开放检查重算当天，生成新的 issues。'],'重算后的 day：activities、legs、time_summary、cost_summary、issues。','重新签名并返回。','路线不可用保留未知；不自动恢复删除景点；删除全部景点允许空白日期。',['operation','resolve','insertion','rebuild','editmode','schedule'],['editor','routes','schedule','cost']),
 step('签发新快照、返回响应','后端 · DayEditService','重算 day 与已撤销要求。',['签名包含新的当天内容和撤销集合。','原样回传 request_id、client_revision、date，前端据此匹配批次。','响应还含按原其他日期汇总的 cost_summary；服务端不存编辑历史。'],'day（新 edit_token）+ 响应关联字段。','前端确认此响应属于当前在途批次。','签名用于完整性校验，不是服务端编辑版本锁；旧签名不自动撤销，不提供跨设备同步。',['editor','sign'],['editor']),
 step('接受响应并保留新操作','浏览器 · 编辑队列','响应 day；响应期间用户追加的 pending 操作。',['核对 request_id、client_revision、date。','更新 accepted 和 plan 对应日期；再将 pending 叠加到新基准。','currentPlan 在浏览器汇总各日当前费用；保存 sessionStorage；必要时发下一批。'],'新的当前可见行程；下一批操作或 idle 状态。','用户继续操作或导出。','请求失败把本批操作放回 pending，保留列表；用户重试。重新从服务器加载会回到持久的生成结果。',['flush','current','persist'],['editqueue','result'])]},
 observe:{name:'任务观测',intro:'观测分为“生成时采集”和“页面读取”。Span 是一次调用记录，parent_span_id 说明它处于哪个父步骤内。',steps:[
 step('关联任务与父步骤','后端 · ObservationService','task_id 与执行中的函数调用。',['_plan 用 observation.task 建立 ContextVar 上下文。','planning → agent → llm / validation、planning → tool 等嵌套 span 形成父子关系。'],'带 task_id、span_id、parent_span_id 的步骤身份。','开始调用时写入 running。','未处于生成观测上下文时 span 不写库；不能声称所有编辑和地图搜索都出现在生成调用树。',['taskplan','span','observe'],['observation']),
 step('采集、脱敏、保存','后端 · Span → 仓库','调用输入、输出、异常、供应商 usage。',['开始时 begin_span 写输入与时间；结束时 end_span 写输出、状态、用量和时间。','内容先 sanitize 再 bounded；异常保存有界摘要。','UsageAwareLLM 返回真实 usage；token_usage 只接受非负整数。'],'spans 增量记录；截断标记；用量缺失或记录不完整标记。','供状态、调用摘要、详情与指标接口读取。','观测失败不改变业务判定；未知 Token 不填零，不估算模型计费。',['span','sanitize','tokenusage','llm'],['observation','repository']),
 step('页面读取状态和摘要','浏览器 · Observability.vue','URL 的 task / view；用户选择的任务。',['GET /api/trip/tasks 查询任务列表，按状态筛选、每页 50 条；loadTask 并行查询 /tasks/{id} 状态和 /tasks/{id}/spans 摘要（均在 /api/trip 下）。','活动任务每 2 秒更新；buildForest 用父 ID 建树，处理缺失关系。','初始默认选择最慢的已计时模型调用。'],'调用树、时间轴、状态与步骤摘要。','点击节点按需读详情；任务结束后读指标。','旧异步响应通过版本与选中任务检查丢弃；接口失败展示独立错误。',['loadtask','forest'],['observability','http']),
 step('查看详情与聚合指标','后端指标 + 浏览器分析','选中 span_id；已结束任务摘要与调用元数据。',['GET /api/trip/tasks/{id}/spans/{span_id} 按需读取单次输入输出；GET /api/trip/tasks/{id}/metrics 读取已结束任务指标。','build_metrics 区分 llm 与 tool，用父关系找到角色。','前端按真实调用画 Token、工具耗时和异常分析；成功任务可重载结果。'],'单调用详情 + 工程指标视图。','用当前调用链解释慢在哪里或失败在哪里。','运行中指标返回 409；累计调用耗时不等于任务实际耗时；统计不能证明旅行安排合理。',['loaddetail','metrics','showresult'],['metrics','observability'])]},
 export:{name:'图片导出',intro:'导出只消费当前浏览器状态，不查询模型或重排行程。生成 PNG 时冻结快照，避免导出中继续编辑改变图片。',steps:[
 step('取得当前可见行程','浏览器 · RouteResultEditable','各日 viewDay；各日编辑状态；当前问题。',['currentPlan 使用当前日列表与费用汇总。','exportPlan 带全部日期和当前 issues；statusMap 带更新中/失败提示。'],'传给 TripImageExport 的 plan + statuses。','开始导出时深复制冻结。','更新时间/费用未知时保留状态说明，不阻止导出。',['current','export'],['result','export']),
 step('渲染冻结快照并切片','浏览器 · TripImageExport','冻结的 plan / statuses。',['等待 Vue nextTick、字体，渲染宽度 1080 CSS 像素的离屏排版。','高度 ≤12000 CSS 像素则整页输出；更长按日切，超长单日按实际内容块继续切。','图片加载最多等待约 3 秒；保留提示和空白日期。'],'一个或多个 DOM 图片片段。','交给 html2canvas。','不使用旧生成结果覆盖当前编辑；过长内容不会无限增加一张画布。',['export','split','render'],['export']),
 step('画布转 PNG 并下载','浏览器 · html2canvas','离屏 DOM 片段。',['html2canvas 使用 scale=2、useCORS=true。','canvas.toBlob 生成 image/png；创建下载链接逐张保存。','finally 清理切片和临时快照，解除导出状态。'],'旅行计划_城市_序号.png 文件。','图片在用户设备保存，不写数据库。','图片加载受跨域约束；生成 Blob 或画布失败时显示导出错误，可重试。',['render','export'],['export'])]}
};

const dataObjects = [
 {id:'request',name:'TripRequest',caption:'用户请求',owner:'用户输入 → Pydantic 校验',role:'前端将日期控件转成 ISO 时间；后端推导起止日期并计算含首尾的天数。输入原文不被关键词规则重新解释。',relations:'arrival_at / departure_at → start_date / end_date → travel_days；arrival_place_id / departure_place_id 引用高德地点。',next:'作为完整请求对象供 plan_trip 使用；脱敏副本保存在 tasks.request_data。',refs:['home','request','dates'],sample:{city:'北京',arrival_at:'2026-10-12T10:00:00+08:00',departure_at:'2026-10-13T18:00:00+08:00',preferences:['历史文化'],free_text_input:'想去故宫',travel_days:2}},
 {id:'conditions',name:'Conditions',caption:'统一规划条件',owner:'模型理解备注 + 程序固定显式输入',role:'候选搜集角色首次返回条件；程序复制城市、日期、到离地点等表单事实，并保留完整备注。后续各角色使用同一份条件。',relations:'must_visit_requests[].requirement_id → activity.requirement_ids；解释冲突进入 interpretation_notes。',next:'进入候选搜集、安排、修改与程序校验；最终输出 planning_conditions。',refs:['conditions','collect','context'],sample:{city:'北京',transportation:'transit',must_visit_requests:[{requirement_id:'req-1',text:'故宫'}],budget_per_person:null,remarks:'想去故宫'}},
 {id:'pool',name:'state.pool',caption:'已核实候选',owner:'高德查询 → 程序归一化',role:'模型提出搜索词；程序查地点与详情，只收录有效坐标，按 source_id 去重。pool 是生成过程的内存字典，不是一张数据库表。',relations:'高德 id → source_id；模型 Draft 通过 source_id 引用候选。程序 ground 再取回候选名称与坐标。',next:'候选交给模型选择排序；价格、开放时间、图片供程序计算与最终展示。',refs:['collect','ground','metadata','place'],sample:{'demo-poi-1':{source_id:'demo-poi-1',name:'教学地点 A',longitude:116.4,latitude:39.9,categories:['sightseeing'],reference_cost:null,opening_hours:null}}},
 {id:'draft',name:'Draft',caption:'模型日程建议',owner:'行程安排 / 修改角色',role:'模型输出住宿基点与按日排序的活动，还不是可直接交付的最终行程。程序使用地图事实核实并组装 v3。',relations:'day.date 必须与请求日期序列相同；activity_id 标识一次活动；place.source_id 必须存在于 pool；住宿来源与用户表单的一致性由 evaluate 检查。',next:'evaluate 核实地点、建立路段、计算时间费用、检查问题。',refs:['draftmodel','draft','evaluate'],sample:{days:[{date:'2026-10-12',activities:[{activity_id:'a1',type:'sightseeing',title:'参观地点 A',duration_minutes:120,place:{source_id:'demo-poi-1'},requirement_ids:['req-1']}]}]}},
 {id:'leg',name:'Leg / options',caption:'真实路线候选',owner:'高德路线查询 → RouteOptionsService',role:'程序把相邻已定位活动连成路段。每种交通方式独立查询、记录状态，将秒数向上取整为分钟；当前方式参与排时。',relations:'from_activity_id / to_activity_id → 活动或 lodging / arrival / departure 锚点；options[selected_mode] → 本段耗时。',next:'schedule_day 使用选中耗时；页面展示可切换方式。没有已知耗时就保留 null。',refs:['route','routeoptions','routequery','legtype'],sample:{from_activity_id:'a1',to_activity_id:'a2',options:{walking:{duration_minutes:18,status:'available',source:'amap'},transit:{duration_minutes:null,status:'no_route'}},selected_mode:'walking'}},
 {id:'itinerary',name:'Itinerary v3',caption:'程序组装结果',owner:'程序计算 + 最终 Pydantic 校验',role:'_upgrade_v3 用同一组事实构造最终契约，移除旧模型费用。HTTP 返回结果前才附加每一天的编辑签名。',relations:'planning_conditions + lodging_base + days；每日 activities 与 legs 分开；issues 通过 date / activity_id / leg_id 关联范围。',next:'脱敏后整体 JSON 保存到 task_results；前端按 schema_version=3 消费。',refs:['upgrade','itinerary','daytype','types'],sample:{schema_version:3,days:[{date:'2026-10-12',time_summary:{known_minutes:120,unknown_leg_count:1,status:'incomplete'},cost_summary:{known_total:0,unknown_count:1,complete:false}}],issues:[{code:'COST_UNKNOWN',category:'needs_confirmation',date:'2026-10-12'}]}},
 {id:'editing',name:'Edit request / day',caption:'编辑操作与快照',owner:'用户操作 + 后端签名与重算',role:'编辑请求不提交任意改写的整份行程；token 包含服务端签过的当天内容。后端应用操作，返回重算 day 与新 token。',relations:'task_id / date 绑定快照；request_id / client_revision 匹配前端在途批次，服务端原样回传，不构成持久版本锁。',next:'前端更新 accepted 与 plan，叠加未发送操作，写 sessionStorage。数据库生成结果保持原样。',refs:['editrequest','sign','editor','flush'],sample:{task_id:'trip_demo',date:'2026-10-12',edit_token:'<服务端签发的快照，示例不展开>',client_revision:2,request_id:'edit-demo',operations:[{type:'move_activity',activity_id:'a1',direction:'down'}]}},
 {id:'span',name:'Span / metrics',caption:'调用记录与指标',owner:'观测服务采集 → 指标服务聚合',role:'生成过程各步骤记录自身起止时间与父步骤。记录文本脱敏限长；模型用量仅来自供应商返回的 usage。',relations:'task_id → tasks；parent_span_id → 同任务的父 Span；operation_type 区分 planning / agent / llm / tool / validation。',next:'数据库读出摘要和按需详情；build_metrics 聚合；前端绘制调用树和分析图。',refs:['schema','span','tokenusage','metrics'],sample:{task_id:'trip_demo',span_id:'span-model',parent_span_id:'span-agent',name:'llm.invoke',operation_type:'llm',status:'succeeded',input_tokens:null,output_tokens:null}}
];
const provenance = [
 ['城市、到离时间、住处','用户表单','程序优先固定显式条件；备注不能任意覆盖已选城市和日期。住宿来源仍有模型可选字段，随后执行一致性检查；相关修复待独立实施。',['dates','collect','evaluate']],
 ['必去地点、开放备注理解','模型','程序保留原文并校验 requirement_id；不使用关键词分类意图。',['prompt','conditions','collect']],
 ['候选地点名、地址、坐标','高德 POI','核实后进入 pool；ground 用查询事实替换模型地点字段。',['collect','ground']],
 ['活动顺序、推荐理由、游玩时长','模型 / 编辑服务','生成时由模型提出并受结构校验；新增景点参考时长由编辑服务固定为 90 分钟。',['draftmodel','operation']],
 ['路段耗时、距离','高德路线','分方式查询；秒→分钟向上取整；缺失保持未知，不使用直线距离冒充交通时间。',['routequery','routeoptions']],
 ['活动开始结束时刻、超时判断','程序','按选中路线与活动时长推算；遇未知时长后续时刻保留未知。',['schedule']],
 ['门票、餐饮、住宿费用','高德查询 + 程序','不接受模型估价；住宿确认每房每晚再乘晚数；已知小计与未知项目分开。',['metadata','price','daycost']],
 ['天气、开放时间、图片','高德查询','天气按日期匹配；复杂开放时段不能断言冲突；图片缺失可无图展示。',['forecasts','metadata','opening']],
 ['Token 用量','模型供应商','采集实际 usage 的非负整数；缺失不是零，不能从总量反推缺失输入输出。',['llm','tokenusage']],
 ['行程问题与稳定问题编号','程序','规则检查后写入 issues；页面按影响范围定位并分别展示冲突与缺失。',['upgrade','current']]
];
const stores = [
 {title:'浏览器存储',tag:'localStorage / sessionStorage',text:'保存当前页面恢复所需的数据，不是服务端行程数据库。',items:['localStorage.activeTripTask：当前生成任务编号，刷新首页可继续轮询。','sessionStorage.tripPlan / tripTaskId：当前行程与编号；v3 编辑成功后写回。','观测页 task / view 保存在 URL；关闭浏览器不取消后端任务。'],refs:['poll','persist','showresult']},
 {title:'后端与前端内存',tag:'每次生成 / 每个编辑会话',text:'承担执行中状态，进程或页面销毁后不保证恢复。',items:['后端：active_id、候选 pool、查询缓存、观测上下文；角色 run 每次清历史。','签名密钥在后端进程内；编辑以签名快照承接下一步，不存服务器编辑历史。','前端：accepted、viewDay、pending、inFlight 与 revision；待发操作没有长期保存。'],refs:['planner','agent','sign','flush']},
 {title:'SQLite 持久数据',tag:'backend/data/tasks.sqlite3（默认）',text:'保留生成任务、生成结果与生成调用记录，数据库路径可配置。',items:['tasks：脱敏请求、状态、时间与错误。','task_results：成功行程完整 JSON；当天编辑不写回。','spans：调用树、脱敏详情、用量与时间；重启识别未完成任务。'],refs:['schema','save','recover','config']}
];
const reading = [
 {title:'从点击生成读到后台执行',question:'为什么请求先返回任务编号，而不是直接返回行程？',stops:[['home','看表单如何序列化，调用 submitTask。'],['create','看已校验 body 如何进入任务服务。'],['accept','看锁、登记、后台任务与 202 响应。'],['execute','看线程池调用与成功/失败保存。'],['poll','回到前端看状态轮询与成功后的结果请求。']]},
 {title:'从角色调用读到行程结果',question:'模型提出什么，程序又补充和校验什么？',stops:[['planner','先读总流程，确认两阶段搜集、两次安排、最多两次修改。'],['model','跟进 schema、agent.run、JSON 解析与模型校验。'],['collect','看 searches 如何变成地图调用和 pool。'],['evaluate','看日期、地点、住宿、必去与路段检查。'],['upgrade','看 v3 时间、价格和问题如何组装。']]},
 {title:'从活动列表读到时间与费用',question:'活动、路段、价格和提示之间如何关联？',stops:[['draftmodel','先了解模型建议的范围。'],['route','看路段端点、方式候选与初次选择。'],['schedule','看时刻累加、到离缓冲与未知传播。'],['price','看价格事实的来源门槛。'],['daycost','看住宿晚数只在第一日计入，再跟 summarize_trip。']]},
 {title:'从编辑点击读到安全重算',question:'怎么防止改价格，以及旧响应覆盖新操作？',stops:[['edits','看乐观列表更新与 400ms 合并。'],['flush','看单批在途、请求标识匹配与待发操作重放。'],['verify','看签名如何约束任务、日期和日快照。'],['operation','看四种操作；新增地点还需核实。'],['rebuild','看当天重建路线、排时，再回前端 persistPlan。']]},
 {title:'从模型调用读到观测图表',question:'调用树、Token 和总耗时来自哪里？',stops:[['observe','看 invoke 包装与 usage 采集。'],['span','看 ContextVar 父关系、开始与结束落盘。'],['schema','看三张表、外键与 parent_span_id。'],['metrics','看 llm/tool 分类和任务起止时间。'],['loadtask','看页面怎样读摘要、详情与指标。']]},
 {title:'从当前结果读到导出图片',question:'导出中继续编辑，为什么不会改变正在生成的图？',stops:[['current','看当前各日视图与 exportPlan。'],['export','看冻结快照、nextTick、分日和分块。'],['split','看超长单日如何拆分而保留内容。'],['render','看图片等待、scale=2、PNG Blob。']]}
];
const boundaries = [
 ['三个 Agent 会自己调用高德工具吗？','当前三个角色禁用框架自动工具调用。搜集角色返回 searches，RouteTripPlanner.collect 逐项调用 tool，PlanningMaps 执行 MCP。角色是模型职责分工，不是三个并行后台服务。',['roles','collect','tool','maps']],
 ['HTTP 202、HTTP 200 和生成完成有什么区别？','202 表示任务已接收；状态查询 200 表示查到状态，任务本身可能 failed；succeeded 需要行程满足最低条件且结果已保存，不表示费用和路线信息全都齐全。',['create','taskstatus','save']],
 ['模型写了坐标或门票价格，程序就采用吗？','程序通过 source_id 取 pool 中的查询地点事实；未知引用会失败。v3 价格来自查询元数据，模型 estimated_cost 被丢弃，不能用模型金额填补查询缺失。',['ground','upgrade','price']],
 ['路线、天气、价格没查到，会不会整份失败？','单个缺失可以保留 null 与问题提示；天气查询失败和预报未覆盖分开表达。未知路段使后续参考时刻无法完整推算。初次没有已核实景点、日期错或结果保存失败，不能显示生成完成。',['planner','evaluate','schedule','failure']],
 ['修复 JSON、自动修改行程、用户编辑是一回事吗？','draft 的结构修复整次最多一次；自动修改处理 needs_adjustment，最多两次并拒绝引入新已知冲突；用户编辑以签名快照和操作重算当天，不调用规划角色。',['draft','planner','editor']],
 ['关页面和重启后端分别会发生什么？','关页面只停止浏览器轮询，后端持有任务继续执行；正常关闭后端等待后台任务，强制退出的未完成任务在下次启动时标为 interrupted。后端重启还会更换编辑签名密钥，旧编辑凭据不能继续使用。',['execute','recover','sign','verify']],
 ['编辑结果会更新数据库并同步其他设备吗？','不会。生成结果持久化在 task_results；编辑成功更新前端 plan 和 sessionStorage。重新从观测页取结果会载入生成时的结果。request_id 和 client_revision 用于前端匹配响应，不是服务端存储的编辑版本或幂等锁。',['editor','flush','persist','showresult']],
 ['编辑费用为什么在前端再汇总一次？','编辑服务返回的全程 summary 基于原生成结果的其他日期。前端 currentPlan 使用各日当前 viewDay 独立汇总，避免其他日期已编辑时被原生成结果覆盖；更新中的日期明确标记费用待更新。',['editor','current']],
 ['地图上的线就是实际道路路线吗？','当前浏览器地图使用地点坐标画顺序示意。路段耗时由后端路线查询提供，两者是不同数据路径；前端连线不能作为道路导航。',['draw','routequery']],
 ['所有操作都会出现在任务调用树吗？','生成任务的 _plan 建立观测上下文，规划与嵌套调用在其中写 Span。当天编辑和独立地图 API 没有建立这份生成上下文，不能把调用树当作整个系统全部请求的追踪记录。',['taskplan','span','editapi']],
 ['调用耗时能直接相加得到任务耗时吗？','不能。父步骤包含子步骤，用于展示嵌套；任务耗时来自任务起止时间，模型和工具累计耗时来自各自调用。未知时间、未知用量与真实零值分别展示。',['metrics','tokenusage']],
 ['查询和存储有没有限制？','地点搜集默认 36 次、路线查询默认 80 次、单次工具默认 25 秒；失败也计入对应额度。编辑重算设 90 秒查询期限。观测内容每步骤输入输出各默认 64 KiB，已结束任务默认保留 7 天，容量治理目标 200 MiB。地图独立搜索与生成候选搜集的限制路径不同。',['config','collect','routeoptions','editor','cleanup']]
];

// Rendering: 用户交互不请求业务 API；源码查看仅允许 sources 中列出的路径。
const $ = id => document.getElementById(id);
function esc(value) { return String(value).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function ref(id) { return `<button type="button" class="source-ref" data-source="${id}"><span>${esc(sources[id].label)}</span><small>源码 ↗</small></button>`; }
function refs(ids) { return `<div class="refs">${ids.map(ref).join('')}</div>`; }
function chips(ids) { return `<div class="chips">${ids.map(id=>`<button type="button" class="chip" data-module="${id}">${esc(modules[id].name)} →</button>`).join('')}</div>`; }
function field(name,text) { return `<div class="detail-field"><h4>${name}</h4><p>${esc(text)}</p></div>`; }
let selectedModule='planner', selectedFlow='generate', selectedStep=0, selectedData='request';
function renderMap() {
  $('layer-map').innerHTML=layers.map(([title,caption,ids],i)=>`<div class="layer"><div class="layer-head"><strong>${String(i+1).padStart(2,'0')} ${title}</strong><small>${caption}</small></div><div class="nodes">${ids.map(id=>`<button type="button" class="node" data-module="${id}" aria-pressed="${id===selectedModule}"><strong>${esc(modules[id].name)}</strong><small>${esc(modules[id].short)}</small></button>`).join('')}</div></div>`).join('');
  renderModule();
}
function renderModule() {
  const m=modules[selectedModule], parents=Object.keys(modules).filter(id=>modules[id].calls.includes(selectedModule));
  document.querySelectorAll('.node').forEach(node=>{ const id=node.dataset.module; node.dataset.relation=id===selectedModule?'selected':parents.includes(id)?'upstream':m.calls.includes(id)?'downstream':''; node.setAttribute('aria-pressed',String(id===selectedModule)); });
  $('module-detail').innerHTML=`<p class="eyebrow">MODULE / ${esc(selectedModule)}</p><h3>${esc(m.name)}</h3><p>${esc(m.role)}</p>${field('输入',m.input)}${field('输出',m.output)}<div class="detail-field"><h4>谁调用它 / 使用它</h4>${parents.length?chips(parents):'<p>用户操作或启动入口；可从下方流程追踪。</p>'}</div><div class="detail-field"><h4>它调用谁 / 使用谁</h4>${m.calls.length?chips(m.calls):'<p>此处是主要依赖路径的末端；外部库或横向配置见说明。</p>'}</div>${field('阅读时注意',m.note)}<div class="detail-field"><h4>实现入口</h4>${refs(m.refs)}</div>`;
  $('module-detail').scrollTop=0;
}
function renderFlow() {
  const flow=flows[selectedFlow];
  $('flow-tabs').innerHTML=Object.entries(flows).map(([key,v])=>`<button type="button" role="tab" id="tab-${key}" aria-controls="step-detail" aria-selected="${key===selectedFlow}" tabindex="${key===selectedFlow?0:-1}" data-flow="${key}">${v.name}</button>`).join('');
  $('step-detail').setAttribute('aria-labelledby',`tab-${selectedFlow}`);
  $('flow-intro').innerHTML=`<strong>${flow.steps.length} 个步骤</strong> · ${esc(flow.intro)}`;
  $('flow-steps').innerHTML=flow.steps.map((v,i)=>`<li><button type="button" class="step" data-step="${i}" ${i===selectedStep?'aria-current="step"':''}><span class="step-number">${String(i+1).padStart(2,'0')}</span><span><span class="step-title">${esc(v.title)}</span><span class="step-caption">${esc(v.actor)}</span></span></button></li>`).join('');
  renderStep();
}
function renderStep() {
  const flow=flows[selectedFlow], s=flow.steps[selectedStep];
  document.querySelectorAll('[data-step]').forEach(el=>{if(Number(el.dataset.step)===selectedStep)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});
  $('step-detail').innerHTML=`<p class="eyebrow">${flow.name} / ${selectedStep+1} OF ${flow.steps.length}</p><h3>${esc(s.title)}</h3><p class="muted">${esc(s.actor)}</p><div class="io"><div><strong>INPUT / 输入</strong>${esc(s.input)}</div></div><ol class="step-detail-list">${s.actions.map(action=>`<li>${esc(action)}</li>`).join('')}</ol><div class="io"><div><strong>OUTPUT / 输出</strong>${esc(s.output)}</div></div>${field('交给下一步',s.next)}${field('异常与限制',s.failure)}<div class="detail-field"><h4>本步代码</h4>${refs(s.refs)}</div><div class="detail-field"><h4>回到模块</h4>${chips(s.mods)}</div>`;
  $('step-detail').scrollTop=0;
}
function renderData() {
  $('data-chain').innerHTML=dataObjects.map((obj,i)=>`<button type="button" class="data-node" data-object="${obj.id}" aria-pressed="${obj.id===selectedData}">${i+1}. ${esc(obj.name)}<small>${obj.caption}</small></button>`).join('');
  const d=dataObjects.find(obj=>obj.id===selectedData);
  $('data-detail').innerHTML=`<div><p class="eyebrow">${esc(d.owner)}</p><h3>${esc(d.name)}</h3><p>${esc(d.role)}</p>${field('关联方式',d.relations)}${field('下一步用途 / 保存位置',d.next)}${refs(d.refs)}</div><div><div class="example-label">教学示例 · 字段节选 · 非完整请求 / 非真实查询</div><pre class="sample">${esc(JSON.stringify(d.sample,null,2))}</pre></div>`;
}
function renderStatic() {
  $('provenance').innerHTML=provenance.map(([name,owner,action,ids])=>`<tr><td>${name}</td><td>${owner}</td><td>${action}</td><td>${ids.map(ref).join('')}</td></tr>`).join('');
  $('stores').innerHTML=stores.map(s=>`<article class="store"><p class="eyebrow">${esc(s.tag)}</p><h3>${s.title}</h3><p>${s.text}</p><ul>${s.items.map(item=>`<li>${esc(item)}</li>`).join('')}</ul>${refs(s.refs)}</article>`).join('');
  $('schema-link').innerHTML=refs(['schema']); $('state-link').innerHTML=refs(['execute','recover']);
  $('reading-routes').innerHTML=reading.map(r=>`<article class="reading-card"><h3>${r.title}</h3><p>${r.question}</p>${r.stops.map(([id,why],i)=>`<div class="reading-stop"><p class="eyebrow">READ ${i+1}</p>${ref(id)}<small>${esc(why)}</small></div>`).join('')}</article>`).join('');
  $('legacy-note').innerHTML=`<p><strong>trip_planner_agent.py：</strong>当前工厂 get_trip_planner_agent 返回 RouteTripPlanner。同文件里的 MultiAgentTripPlanner 是历史四角色实现，不能据此解释当前生成过程。</p>${refs(['factory','oldagent'])}<p><strong>Result.vue：</strong>当前 main.ts 将 /result 指向 RouteResult.vue，再按版本分发。旧 Result.vue 的景点与图片处理不能代表当前 v3 入口。</p>${refs(['router','view','oldresult'])}<p><strong>Unsplash / 旧 POI 路由：</strong>后端仍有历史照片接口与服务；当前 v3 生成图片元数据来自高德。判断是否进入主链路，先从调用者追踪，不能仅看文件存在。</p>`;
  $('boundary-list').innerHTML=boundaries.map(([question,answer,ids])=>`<details class="historical"><summary>${question}</summary><p>${esc(answer)}</p>${refs(ids)}</details>`).join('');
}
function setHash(value) { if(location.hash!==value) history.pushState(null,'',value); }
function applyHash() {
  let hash=location.hash.slice(1);try{hash=decodeURIComponent(hash);}catch{/* 无效编码保留原锚点 */}const parts=hash.split('/');
  if(parts[0]==='module' && modules[parts[1]]){selectedModule=parts[1];renderModule();$('architecture').scrollIntoView();}
  else if(parts[0]==='flow' && flows[parts[1]]){selectedFlow=parts[1];selectedStep=Math.min(Math.max(Math.trunc(Number(parts[2]))||0,0),flows[selectedFlow].steps.length-1);renderFlow();$('flows').scrollIntoView();}
  else if(parts[0]==='data' && dataObjects.some(d=>d.id===parts[1])){selectedData=parts[1];renderData();$('data').scrollIntoView();}
  else if(parts[0]==='code' && sources[parts[1]]){void showSource(parts[1],false);}
  else if(hash==='progress' || hash==='features' || hash==='roadmap' || hash==='evidence'){history.replaceState(null,'','#architecture');$('architecture').scrollIntoView();}
  else if($(hash)) $(hash).scrollIntoView();
  if(parts[0]!=='code' && $('source-dialog').open) $('source-dialog').close();
}
let sourceVersion=0, currentSource=null, sourceReturnHash='#reading';
async function showSource(id,updateHash=true) {
  const s=sources[id]; if(!s)return;
  const version=++sourceVersion; currentSource=s;
  if(updateHash){sourceReturnHash=location.hash.startsWith('#code/')?'#reading':location.hash||'#reading';setHash('#code/'+id);}
  const dialog=$('source-dialog'); if(!dialog.open)dialog.showModal();
  $('source-title').textContent=s.label; $('source-path').textContent=s.file+' · 查找 '+s.marker;
  $('raw-source').href='../'+s.file; $('source-status').textContent='正在读取当前源码…'; $('source-code').textContent='';
  try {
    const response=await fetch('../'+s.file,{cache:'no-store'});
    if(!response.ok)throw new Error('HTTP '+response.status);
    const text=await response.text();if(version!==sourceVersion)return;
    const lines=text.split('\n'), index=lines.findIndex(line=>line.includes(s.marker));
    if(index<0)throw new Error('当前文件中未找到符号，请打开完整文件搜索');
    $('source-status').textContent=`定位第 ${index+1} 行 · 下方是当前文件完整源码，可继续滚动阅读；说明与源码不一致时以源码为准。`;
    $('source-path').textContent=s.file+':'+(index+1)+' · '+s.marker;
    $('source-code').innerHTML=lines.map((line,i)=>`<span class="code-line${i===index?' target':''}"><span class="line-number">${i+1}</span>${esc(line)}</span>`).join('');
    const target=$('source-code').querySelector('.target');$('source-code').scrollTop=Math.max(0,target.offsetTop-$('source-code').offsetTop-65);
  } catch(error) {if(version===sourceVersion)$('source-status').textContent='源码未载入：'+error.message+'。请通过项目根目录的本地 HTTP 服务访问，或复制路径与符号到编辑器搜索。';}
}
document.addEventListener('click',event=>{
  const button=event.target.closest('button'); if(!button)return;
  if(button.dataset.module){selectedModule=button.dataset.module;renderModule();setHash('#module/'+selectedModule);if(!button.classList.contains('node'))$('architecture').scrollIntoView();else if(matchMedia('(max-width:800px)').matches)$('module-detail').scrollIntoView({block:'start'});}
  if(button.dataset.flow){selectedFlow=button.dataset.flow;selectedStep=0;renderFlow();setHash('#flow/'+selectedFlow+'/0');$('flow-tabs').querySelector(`[data-flow="${selectedFlow}"]`).focus({preventScroll:true});}
  if(button.dataset.step){selectedStep=Number(button.dataset.step);renderStep();setHash('#flow/'+selectedFlow+'/'+selectedStep);if(matchMedia('(max-width:800px)').matches)$('step-detail').scrollIntoView({block:'start'});}
  if(button.dataset.object){selectedData=button.dataset.object;renderData();setHash('#data/'+selectedData);$('data-chain').querySelector(`[data-object="${selectedData}"]`).focus({preventScroll:true});}
  if(button.dataset.source)void showSource(button.dataset.source);
});
$('close-source').addEventListener('click',()=>{sourceVersion++;$('source-dialog').close();history.replaceState(null,'',sourceReturnHash);});
$('source-dialog').addEventListener('cancel',()=>{sourceVersion++;history.replaceState(null,'',sourceReturnHash);});
$('copy-source').addEventListener('click',async()=>{if(!currentSource)return;try{await navigator.clipboard.writeText($('source-path').textContent);$('source-status').textContent='已复制相对路径、行号与符号；可在编辑器中按路径打开。';}catch{$('source-status').textContent='浏览器未允许复制；可选中上方路径手动复制。';}});
$('flow-tabs').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const keys=Object.keys(flows);let index=keys.indexOf(selectedFlow);index=event.key==='Home'?0:event.key==='End'?keys.length-1:(index+(event.key==='ArrowRight'?1:-1)+keys.length)%keys.length;selectedFlow=keys[index];selectedStep=0;renderFlow();setHash('#flow/'+selectedFlow+'/0');$('tab-'+selectedFlow).focus();});
window.addEventListener('hashchange',applyHash);
renderMap();renderFlow();renderData();renderStatic();applyHash();
const sectionObserver=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){document.querySelectorAll('.sidebar nav a').forEach(a=>a.classList.toggle('active',a.getAttribute('href')==='#'+entry.target.id));}},{rootMargin:'-10% 0px -65% 0px'});
document.querySelectorAll('main>section:not(.intro)').forEach(section=>sectionObserver.observe(section));
