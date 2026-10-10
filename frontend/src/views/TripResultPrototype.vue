<!-- THROWAWAY PROTOTYPE: 每日主题 + 左侧拖动排序 + 地图下方增删目的地；高德真实底图、地点查询及公交/地铁/骑行/步行路径；住宿与天气参考；费用和保存仍为演示。 -->
<template>
  <main class="result-shell">
    <div class="prototype-note"><b>交互原型</b><span>沿用当前页面 · 高德真实地图与路线 · 费用、行程安排与保存为演示</span></div>
    <nav class="result-nav">
      <button class="text-button" @click="notify('城市、日期、预算和住宿需回到首页重新生成；此原型只演示结果页。')">← 调整需求，重新规划</button>
      <div class="nav-actions"><button :disabled="!saved" @click="askRestore">↶ 还原上次保存</button><button class="primary" :disabled="exporting" @click="exportTrip">{{ exporting ? '正在生成图片…' : '↓ 导出行程长图' }}</button></div>
    </nav>
    <header class="hero"><div><span class="eyebrow">YOUR JOURNEY, YOUR WAY</span><h1>上海 · 两日漫游</h1><p>2026.10.10 — 2026.10.11 <span class="preview-tag">可编辑预览</span></p></div>
      <div class="budget"><small>示例主要消费小计 / 人</small><strong>¥{{ money(totalCost) }}</strong><small>全程旅费预算 ¥1,500 / 人</small><small>不含未查到的价格，不代表完整旅费</small></div>
    </header>
    <div class="preview-status"><span><i :class="{ dirty: changed }"></i>{{ saved ? (changed ? '已调整 · 与上次保存不同' : '与上次保存一致') : '初版预览 · 尚未保存最终结果' }}</span><span>当前编辑会在本机保留，导出时尝试保存</span></div>
    <section class="base"><div><strong>推荐住宿区域</strong><h3>人民广场周边</h3><p>作为两天出游的参考基点，每天同时提供酒店参考，你也可以在这一带重新选择酒店。</p><small>住宿参考单独展示，不计入主要消费小计。</small></div><div class="global-conditions"><p>到达：10月10日 10:00</p><p>离开：10月11日 19:00</p><small>按大致时段安排。交通与停留时长仅供参考。</small></div></section>
    <div class="day-tabs" role="tablist" aria-label="行程日期"><button v-for="(item,index) in plan.days" :key="item.date" role="tab" :aria-selected="active === index" :class="{ active: active === index }" @click="selectDay(index)">第 {{ index + 1 }} 天 <small>{{ item.date.slice(5).replace('-', '月') }}日</small><span>{{ item.activities.filter(a => a.kind === 'sight').length }}个景点 · {{ item.activities.filter(a => a.kind === 'meal').length }}餐</span></button></div>
    <div class="day-layout">
      <section class="timeline">
        <div class="day-heading"><div><span class="eyebrow">DAY {{ active + 1 }} / {{ day.date }}</span><h2>{{ day.title }}</h2><p class="muted">{{ day.description }}</p></div></div>
        <p class="order-hint">按住卡片右上方的 ⠿ 拖动排序。目的地增删在地图下方完成。</p>
        <div class="daily-summary"><span>{{ day.activities.length }}项活动</span><span>参考安排 {{ routeState === 'ready' ? formatDuration(totalMinutes) : '交通信息待补全' }}</span><span>已知费用 ¥{{ money(dayCost) }} / 人</span></div>
        <section class="weather-card" aria-label="当天天气与出行建议"><div><span class="eyebrow">WEATHER & TRAVEL</span><strong>{{ weatherFor(day.date).summary }}</strong><small>{{ weatherFor(day.date).source }}</small></div><p>{{ weatherFor(day.date).advice }}</p></section>
        <section ref="hotelRoot" class="stay-card" aria-label="当天住宿参考"><span class="eyebrow">STAY IN THE CITY</span><h3>第 {{ active+1 }} 天 · 住宿参考</h3><div class="stay-columns"><div><small>推荐酒店</small><b>{{ hotel?.name || (hotelStatus === 'loading' ? '正在查询酒店…' : '具体酒店未查询到') }}</b><span>{{ hotel?.address || '查到具体酒店后展示地址，不用区域冒充酒店。' }}</span><small>{{ hotelPriceText() }}</small><small>{{ active === 0 ? '当晚住宿参考' : '离开日住宿参考，按需要选择是否续住' }} · 不计入费用小计</small></div><div><small>推荐寻找区域</small><b>人民广场—南京东路周边</b><span>作为两天出游的参考基点。可以在这片区域按自己的预算重新找酒店，不必选择推荐酒店。</span></div></div></section>
        <div v-if="routeState !== 'ready'" class="notice"><b>{{ routeState === 'updating' ? '路线和参考时间更新中' : '路线暂未更新成功' }}</b><span>已保留你的最新安排，仍可调整顺序和导出。</span></div>
        <p v-else-if="totalMinutes > dayLimit" class="notice">按当前参考时长，可能超时约 {{ formatDuration(totalMinutes - dayLimit) }}。你可以在目的地列表中减少活动。</p>
        <p v-else class="day-note">{{ day.activities.some(a => a.price === null) ? '部分价格未查询到；不影响查看、调整和导出。' : '已知价格仅供参考；交通费用和住宿未包含在小计中。' }}</p>
        <p v-if="!day.activities.length" class="empty-day">这一天留给自己。你可以在地图下方添加目的地，或保持自由安排。</p>
        <template v-for="(activity,index) in day.activities" :key="activity.id">
          <section v-if="index > 0" class="leg-card"><div class="leg-heading"><span>{{ day.activities[index-1].name }} → {{ activity.name }}</span><strong>{{ routeScenario === 'auto' ? legText(day.activities[index-1], activity) : '交通耗时待更新' }}</strong></div><div class="transport-options" :aria-label="'出行方式：'+day.activities[index-1].name+'到'+activity.name"><button v-for="mode in modes" :key="mode.key" :disabled="routeScenario !== 'auto' || !optionFor(day.activities[index-1],activity,mode.key)" :aria-pressed="selectedLeg(day.activities[index-1],activity)?.mode === mode.key" :class="{chosen: selectedLeg(day.activities[index-1],activity)?.mode === mode.key}" :aria-label="'选择'+mode.label+'：'+day.activities[index-1].name+'到'+activity.name" @click="chooseMode(day.activities[index-1],activity,mode.key)"><b>{{ mode.label }}</b><span>{{ optionText(day.activities[index-1],activity,mode.key) }}</span><small v-if="optionFor(day.activities[index-1],activity,mode.key)?.note">{{ optionFor(day.activities[index-1],activity,mode.key)?.note }}</small></button></div><p class="transport-note">公交、地铁包含接驳步行与换乘；骑行不含找车、取还车时间。选择方式后更新地图与当天总时长。</p></section>
          <article :id="activity.id" :data-activity-id="activity.id" class="activity" :class="{ meal: activity.kind === 'meal', focused: selectedId === activity.id, dragging: draggingId === activity.id, 'drop-target': dropId === activity.id && draggingId !== activity.id }">
            <div class="activity-top"><span class="period">{{ activity.kind === 'meal' ? '用餐安排' : '景点游览' }}</span><div class="activity-reference"><span class="duration">{{ activity.kind === 'meal' ? '用餐' : '停留' }}约 {{ formatDuration(activity.duration) }}</span><span class="drag-handle" role="button" tabindex="0" :aria-label="'拖动排序：'+activity.name" title="拖动排序；也可用键盘上下方向键" @pointerdown="startDrag($event, activity.id)" @pointermove="trackDrag" @pointerup="finishDrag" @pointercancel="cancelDrag" @keydown.up.prevent="keyboardMove(index,-1)" @keydown.down.prevent="keyboardMove(index,1)">⠿</span></div></div>
            <div class="activity-title"><h3 class="activity-name"><span class="number">{{ index+1 }}</span>{{ activity.name }}</h3><span v-if="activity.required" class="required">必去</span></div>
            <p>{{ activity.description }}</p><p class="address">⌖ {{ activity.address }}</p>
            <footer><span>{{ activity.price === null ? '价格未查询到' : `示例参考${activity.kind === 'meal' ? '人均消费' : '门票'} ¥${money(activity.price)}` }}</span></footer>
          </article>
        </template>
        <div class="end-of-day"><span>✳</span><p>留一点空白，让旅途有自己的节奏。</p></div>
      </section>
      <aside class="map-panel"><div class="map-title"><span class="eyebrow">EXPLORE THE DAY</span><h2>当天路线</h2></div>
        <div class="map-wrap"><div ref="mapRoot" class="real-map" aria-label="高德真实地图与当天路线"></div><div v-if="mapStatus !== 'ready'" class="map-overlay"><b>{{ mapStatus === 'loading' ? '正在加载高德地图…' : '高德地图暂不可用' }}</b><span>{{ mapMessage }}</span></div></div>
        <div class="map-legend"><span><i class="bus"></i>公交</span><span><i></i>地铁</span><span><i class="cycling"></i>骑行</span><span><i class="walking"></i>步行</span><span class="hotel-legend">⌂ 推荐酒店</span></div>
        <p class="muted map-caption">{{ mapMessage }}<template v-if="mapStatus === 'ready'"> 已定位 {{ locatedCount }}/{{ day.activities.length }} 个地点，已查询 {{ availableLegs }}/{{ Math.max(0, day.activities.length-1) }} 段路线。</template></p>
        <button v-if="routeState === 'failed' && mapStatus === 'ready'" class="retry-route" @click="updateRoute(true)">重试缺失路线</button>
        <div class="destination-block"><div class="list-heading"><h3>第 {{ active+1 }} 天的目的地</h3><small>{{ day.activities.length }} 个地点</small></div><p class="muted management-note">在这里增删景点和餐馆，行程与地图同步更新。</p>
          <ul class="destination-list"><li v-for="(activity,index) in day.activities" :key="activity.id" :class="{ selected: selectedId === activity.id }"><button class="destination-focus" @click="focusActivity(activity.id)"><span class="destination-number">{{ index+1 }}</span><span><b>{{ activity.name }}</b><small>{{ activity.kind === 'meal' ? '餐馆' : '景点' }} · {{ activity.area }}{{ activity.required ? ' · 必去' : '' }}</small></span></button><button class="remove-place" :aria-label="'移除目的地：'+activity.name" @click="askDelete(activity)">移除</button></li></ul>
          <p v-if="!day.activities.length" class="muted">当天还没有目的地，可在下面搜索添加。</p>
        </div>
        <div class="search-block"><label for="place-search">添加目的地</label><div class="search-input"><input id="place-search" v-model="search" placeholder="搜索景点或餐馆（示例库）"/><span>⌕</span></div><div class="recommendation-heading"><h4>{{ search.trim() ? '搜索结果' : '为这一天推荐' }}</h4><small v-if="!search.trim()">{{ recommendedPlaces.filter(p=>p.kind==='sight').length }}个景点 · {{ recommendedPlaces.filter(p=>p.kind==='meal').length }}家餐馆</small></div><div class="search-results" :aria-label="search.trim() ? '目的地搜索结果' : '当天推荐目的地'"><div v-for="place in filteredPlaces" :key="place.key" class="candidate-row"><span><b>{{ place.name }}</b><small>{{ place.kind === 'meal' ? '餐馆' : '景点' }} · {{ place.area }} · {{ place.price === null ? '价格未知' : '示例参考 ¥'+money(place.price) }}</small><span class="candidate-description">{{ place.description }}</span></span><button :disabled="day.activities.some(a => a.key === place.key)" :aria-label="'添加目的地：'+place.name" @click="addPlace(place)">{{ day.activities.some(a => a.key === place.key) ? '已添加' : '＋ 添加' }}</button></div><p v-if="!filteredPlaces.length" class="muted">{{ search.trim() ? '示例库没有匹配地点，可以换个关键词。' : '可推荐地点已加入当天行程，可以搜索其他地点。' }}</p><p v-else-if="!search.trim() && recommendedPlaces.length < 3" class="muted">可推荐地点不足三个；已加入的地点不会重复推荐。</p></div></div>
      </aside>
    </div>
    <div class="prototype-tools"><details><summary>原型演示设置 <span>保存、恢复与异常场景</span></summary><div class="tool-grid"><label>路线场景<select v-model="routeScenario" @change="applyScenario"><option value="auto">正常更新</option><option value="updating">保持更新中</option><option value="failed">模拟更新失败</option></select></label><label class="check"><input type="checkbox" v-model="saveFailure"/>模拟数据库保存失败</label><button @click="resetDemo">重置全部示例</button></div><p>原型只使用本机专用浏览器缓存模拟预览与上次保存，不连接真实数据库或模型。地图、地点位置和路线来自高德；费用与停留时长仍为示例。每段查询公交、地铁、骑行、步行方案并支持选择；预报按旅行日期匹配。酒店已查询名称、位置及详情中的最低房价字段，不保证返回价格，不把POI人均消费当作房价；尚未实现日期房型报价、预订或整天自动排序。添加目的地默认从固定示例库选取未加入的两个景点和一个餐馆，未接入模型或实时推荐算法。</p><p>当前：第{{ active+1 }}天 · {{ day.activities.length }}项活动 · {{ routeState }} · {{ saved ? '已有上次保存' : '尚无上次保存' }} · {{ changed ? '预览有未保存调整' : '预览与保存一致' }}</p><details><summary>查看模拟状态</summary><pre>{{ JSON.stringify({ preview: plan, lastSaved: saved, map: mapStatus, located: locatedCount, routes: availableLegs, hotelDetail: hotelDetailStatus, hotelPriceKnown: !!hotel?.referenceRoomPrice, routeQueries, routeQueryLimit: ROUTE_LIMIT }, null, 2) }}</pre></details></details></div>
    <div v-if="toast" class="toast" role="status" aria-live="polite">{{ toast }}</div>
    <dialog ref="dialog" @close="panel = ''"><form @submit.prevent="submitDialog">
      <div class="dialog-header"><div><span class="eyebrow">MAKE IT YOURS</span><h2>{{ dialogTitle }}</h2></div><button type="button" aria-label="关闭" @click="closeDialog">×</button></div>
      <template v-if="panel === 'delete'"><p>从当天删除“{{ editingActivity?.name }}”？</p><p class="muted">{{ editingActivity?.required ? '这个地点原本是必去地点。确认删除后，不会自动补回。' : '其他活动会保留，系统只更新受影响的路线与费用。' }}</p><div class="dialog-actions"><button type="button" @click="closeDialog">取消</button><button class="primary" type="submit">确认删除</button></div></template>
      <template v-if="panel === 'restore'"><p>还原上次成功保存的两天行程？</p><p class="muted">当前未保存的调整将被替换。已导出的图片不会改变。</p><div class="dialog-actions"><button type="button" @click="closeDialog">保留当前编辑</button><button class="primary" type="submit">确认还原</button></div></template>
    </form></dialog>
    <div class="export-stage" aria-hidden="true"><article v-if="exportSnapshot" ref="exportRoot" class="export-sheet"><span class="eyebrow">旅程规划参考 · 原型示例</span><h1>上海 · 两日漫游</h1><p>2026.10.10 — 2026.10.11 · 全程旅费预算 ¥1,500 / 人</p><p>已知主要消费 ¥{{ money(exportSnapshot.days.flatMap(d=>d.activities).reduce((sum,a)=>sum+(a.price ?? 0),0)) }} / 人；不代表完整旅费。</p><p>人民广场周边为住宿参考区域。地点与交通信息来自高德查询；价格和停留时长为原型演示数据。</p><section v-for="(item,index) in exportSnapshot.days" :key="item.date"><h2>第{{ index+1 }}天 · {{ item.date }} · {{ item.title }}</h2><p>{{ item.description }}</p><p>天气：{{ exportWeather[item.date]?.summary }}。{{ exportWeather[item.date]?.advice }}</p><p>住宿酒店参考：{{ exportHotel?.name || '具体酒店未查询到' }} · {{ exportHotel?.address || '地址未知' }}。找酒店区域：人民广场—南京东路周边。{{ index === 0 ? '当晚住宿参考' : '离开日住宿参考，按需要选择是否续住' }}；{{ hotelPriceText(exportHotel) }}</p><p v-if="exportState !== 'ready'">{{ exportState === 'failed' ? '路线更新失败' : '路线更新中' }} · 当前内容仍可作为安排参考。</p><p v-if="!item.activities.length">当天自由安排。</p><div v-for="(activity,i) in item.activities" :key="activity.id" class="export-activity"><small>{{ activity.kind === 'meal' ? '用餐' : '游览' }} · 参考{{ formatDuration(activity.duration) }}</small><h3>{{ i+1 }}. {{ activity.name }}</h3><p>{{ activity.description }}</p><p>{{ activity.address }}</p><p>{{ activity.price === null ? '价格未查询到' : '示例参考费用 ¥'+money(activity.price) }}</p><p v-if="i>0">前段交通：{{ exportLegText(item.activities[i-1], activity,item) }}</p></div></section></article></div>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import html2canvas from 'html2canvas'
import AMapLoader from '@amap/amap-jsapi-loader'
type Mode = 'bus' | 'subway' | 'cycling' | 'walking'
type Coord = [number, number]
interface Located { name:string; address:string; coord:Coord; id?:string; referenceRoomPrice?:number }
interface RoutePart { mode:Mode; path:Coord[] }
interface Leg { mode:Mode; minutes:number; distance:number; parts:RoutePart[]; note?:string }
type Options = Record<Mode,Leg|null>
interface Forecast { date:string; dayWeather:string; nightWeather:string; dayTemp:number; nightTemp:number; dayWindPower:string }
interface Place { key:string; kind:'sight'|'meal'; name:string; area:string; address:string; description:string; price:number|null; duration:number }
interface Activity extends Place { id:string; period:string; required?:boolean }
interface Plan { days:{ date:string; title:string; description:string; activities:Activity[]; travelModes?:Record<string,Mode> }[] }
const CACHE = 'PROTOTYPE_trip_result_preview_v3', SAVED = 'PROTOTYPE_trip_result_saved_v3'
const places=ref<Place[]>([
  {key:'bund',kind:'sight',name:'外滩',area:'黄浦',address:'中山东一路 · 位置待查询',description:'沿江漫步，看看上海的城市天际线。',price:0,duration:90},
  {key:'yuyuan',kind:'sight',name:'上海豫园',area:'黄浦',address:'安仁街 · 位置待查询',description:'在园林与老城街巷之间，感受城市的另一面。',price:40,duration:120},
  {key:'museum',kind:'sight',name:'上海博物馆(人民广场馆)',area:'人民广场',address:'人民大道 · 位置待查询',description:'把一个上午留给展览与城市历史。',price:null,duration:120},
  {key:'xintiandi',kind:'sight',name:'新天地',area:'黄浦',address:'太仓路 · 位置待查询',description:'走走石库门街区，找一处喜欢的角落。',price:0,duration:90},
  {key:'park',kind:'sight',name:'复兴公园',area:'黄浦',address:'复兴中路 · 位置待查询',description:'在树荫下休息，放慢今天的节奏。',price:0,duration:60},
  {key:'tianzifang',kind:'sight',name:'上海田子坊',area:'黄浦',address:'泰康路 · 位置待查询',description:'探索小巷里的店铺与街头风景。',price:null,duration:90},
  {key:'noodles',kind:'meal',name:'松鹤楼(豫园商城店)',area:'豫园周边',address:'老城街区 · 位置待查询',description:'顺路吃一碗面，留出一小时慢慢午餐。',price:45,duration:60},
  {key:'shanghai',kind:'meal',name:'浦江老饭店(外滩总店)',area:'外滩周边',address:'外滩街区 · 位置待查询',description:'晚餐尝尝本帮风味，再去江边散步。',price:95,duration:75},
  {key:'dumpling',kind:'meal',name:'莱莱小笼',area:'人民广场',address:'广场周边 · 位置待查询',description:'午餐选择小笼与点心，方便接着游览。',price:55,duration:60},
  {key:'bistro',kind:'meal',name:'鼎泰丰(新天地时尚一期店)',area:'新天地',address:'街区周边 · 位置待查询',description:'在街角坐下来，为这次旅行留一顿晚餐。',price:null,duration:75},
])
const modes:{key:Mode;label:string}[]=[{key:'bus',label:'公交'},{key:'subway',label:'地铁'},{key:'cycling',label:'骑行'},{key:'walking',label:'步行'}]
const copy = <T,>(value:T):T => JSON.parse(JSON.stringify(value))
const make = (key:string,id:string,period:string,required=false):Activity => ({...copy(places.value.find(p=>p.key===key)!),id,period,required})
function initial():Plan {return {days:[{date:'2026-10-10',title:'从老城走向江边',description:'初版以老城和江边为主题，你可以调整下方地点与顺序。',activities:[make('yuyuan','a1','上午'),make('noodles','a2','午餐'),make('bund','a3','下午',true),make('shanghai','a4','晚餐')]},{date:'2026-10-11',title:'博物馆与街区漫步',description:'初版以看展和街区漫步为主题，你可以按自己的节奏调整。',activities:[make('museum','a5','上午'),make('dumpling','a6','午餐'),make('xintiandi','a7','下午'),make('bistro','a8','晚餐')]}]}}
function read(key:string):Plan|null {try {const value=JSON.parse(localStorage.getItem(key)||'null');return value?.days?.length===2 ? value : null} catch {return null}}
const restored = read(CACHE), plan=ref<Plan>(restored||initial()), saved=ref<Plan|null>(read(SAVED)), active=ref(0)
const search=ref(''), selectedId=ref(''), toast=ref(''), dialog=ref<HTMLDialogElement|null>(null), panel=ref(''), editingId=ref('')
const draggingId=ref(''), dropId=ref('')
let dragOrigin: { id:string; pointerId:number; x:number; y:number } | null = null
const routeState=ref<'ready'|'updating'|'failed'>('updating'), routeScenario=ref('auto'), saveFailure=ref(false), exporting=ref(false), exportRoot=ref<HTMLElement|null>(null), exportSnapshot=ref<Plan|null>(null), exportState=ref('ready')
let toastTimer:ReturnType<typeof setTimeout>
const day=computed(()=>plan.value.days[active.value]), dayCost=computed(()=>day.value.activities.reduce((sum,a)=>sum+(a.price??0),0)), totalCost=computed(()=>plan.value.days.flatMap(d=>d.activities).reduce((sum,a)=>sum+(a.price??0),0))
const changed=computed(()=>!saved.value||JSON.stringify(plan.value)!==JSON.stringify(saved.value)), editingActivity=computed(()=>day.value.activities.find(a=>a.id===editingId.value))
const totalMinutes=computed(()=>day.value.activities.reduce((sum,a,i)=>sum+a.duration+(i?(travelMinutes(day.value.activities[i-1],a)??0):0),0)), dayLimit=computed(()=>active.value===0?510:570)
// 地图和耗时使用同一条高德返回路线；未知路段不生成直线或估算耗时。
const mapRoot=ref<HTMLElement|null>(null), mapStatus=ref<'loading'|'ready'|'failed'>('loading'), mapMessage=ref('正在查询地点位置与路线。')
const located=ref<Record<string,Located>>({}), routeCache=ref<Record<string,Options>>({}), routeQueries=ref(0)
const ROUTE_LIMIT=60
let placeQueries=0
let amap:any, map:any, disposed=false, routeGeneration=0
let serviceQueue=Promise.resolve(), lastQueryAt=0
const pendingLegs=new Map<string,Promise<Options|null>>()
const locatedCount=computed(()=>day.value.activities.filter(a=>located.value[a.key]).length)
const availableLegs=computed(()=>day.value.activities.slice(1).filter((a,i)=>selectedLeg(day.value.activities[i],a)).length)
const legKey=(from:Place,to:Place)=>from.key+'>'+to.key
const exportRoutes=ref<Record<string,Options>>({}), exportHotel=ref<Located|null>(null), exportWeather=ref<Record<string,{summary:string;source:string;advice:string}>>({})
const hotel=ref<Located|null>(null), hotelStatus=ref<'loading'|'ready'|'failed'>('loading'), hotelRoot=ref<HTMLElement|null>(null), hotelDetailStatus=ref<'waiting'|'ready'|'failed'>('waiting')
const forecasts=ref<Forecast[]>([]), weatherStatus=ref<'loading'|'ready'|'failed'>('loading'), weatherReport=ref('')
function formatDuration(minutes:number){const value=Math.max(0,Math.round(minutes)), hours=Math.floor(value/60), rest=value%60;return hours?`${hours}小时${rest?rest+'分钟':''}`:`${rest}分钟`}
function weatherFor(date:string){
  const forecast=forecasts.value.find(f=>f.date===date)
  if(!forecast)return {summary:weatherStatus.value==='loading'?'天气预报查询中':weatherStatus.value==='failed'?'天气查询失败':'天气未知 · 预报未覆盖这一天',source:'高德天气 · '+(weatherReport.value?`发布于 ${weatherReport.value}`:'按旅行日期匹配'),advice:'出发前再确认当天天气，准备可增减的衣物；暂不根据未知天气改变行程。'}
  const conditions=[forecast.dayWeather,forecast.nightWeather].join(' / '), advice:string[]=[]
  if(/雨|雪/.test(conditions))advice.push('带雨具和防滑鞋，户外游览留出机动时间；降雨时优先公交或地铁，减少骑行。')
  if(/雷|暴雨|暴雪/.test(conditions))advice.push('留意临近天气预警，恶劣天气时优先室内活动。')
  if(Number(forecast.dayTemp)>=30)advice.push('注意防晒和补水，避开午后长时间步行。')
  if(Number(forecast.nightTemp)<=15)advice.push('早晚加一件外套。')
  if(/雾|霾/.test(conditions))advice.push('关注空气质量和能见度，缩短户外停留。')
  if(!advice.length)advice.push('适合按当前安排游览；带饮水和轻便外套，出发前再确认预报。')
  return {summary:`${forecast.dayWeather} / 夜间${forecast.nightWeather} · ${forecast.nightTemp}—${forecast.dayTemp}℃`,source:`高德预报 · ${date} · 发布于 ${weatherReport.value}`,advice:advice.join('')}
}
const recommendedPlaces=computed(()=>{
  const candidates=places.value.filter(p=>!day.value.activities.some(a=>a.key===p.key))
  return [...candidates.filter(p=>p.kind==='sight').slice(0,2),...candidates.filter(p=>p.kind==='meal').slice(0,1)]
})
const filteredPlaces=computed(()=>search.value.trim()?places.value.filter(p=>[p.name,p.area,p.kind==='meal'?'餐馆':'景点'].join('').includes(search.value.trim())).sort((a,b)=>Number(day.value.activities.some(x=>x.key===a.key))-Number(day.value.activities.some(x=>x.key===b.key))).slice(0,10):recommendedPlaces.value)
const dialogTitle=computed(()=>panel.value==='restore'?'还原上次保存':'移除目的地')
const money=(value:number)=>Number.isInteger(value)?String(value):value.toFixed(2)
function persist(){try{localStorage.setItem(CACHE,JSON.stringify(plan.value))}catch{notify('浏览器未允许本机缓存，刷新恢复暂不可用。')}}
watch(plan,persist,{deep:true});persist()
function notify(value:string){toast.value=value;clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.value='',5000)}
if(restored)notify('已恢复上次编辑的预览。')
// SDK 不输出原始异常，避免带凭据的请求地址进入页面或日志。
async function sdkSearch(service:any,args:any[],method='search'):Promise<any|null>{
  const previous=serviceQueue
  let release!:()=>void
  serviceQueue=new Promise<void>(resolve=>release=resolve)
  await previous
  try{
    // 共用串行队列，所有地点/路线请求最多每 1.1 秒发起一次。
    await new Promise(resolve=>setTimeout(resolve,Math.max(0,1100-(Date.now()-lastQueryAt))))
    if(disposed)return null
    lastQueryAt=Date.now()
    return await new Promise(resolve=>{
      let finished=false
      const finish=(value:any)=>{if(finished)return;finished=true;clearTimeout(timer);resolve(disposed?null:value)}
      const timer=setTimeout(()=>finish(null),15000)
      try{service[method](...args,(status:any,result:any)=>finish((method==='getForecast'?!status:status==='complete')?result:null))}catch{finish(null)}
    })
  }finally{release()}
}
function coords(path:any[]):Coord[]{return (path||[]).map(p=>[Number(p.lng??p[0]),Number(p.lat??p[1])] as Coord).filter(p=>p.every(Number.isFinite))}
async function findPlace(name:string):Promise<Located|null>{
  if(placeQueries>=20||disposed)return null
  placeQueries++
  const result=await sdkSearch(new amap.PlaceSearch({city:'上海',citylimit:true,pageSize:10}),[name])
  const candidates=(result?.poiList?.pois||[]).filter((p:any)=>p.location)
  const normal=(value:string)=>value.replace(/[（）()\s]/g,'').replace(/^上海/,'')
  const poi=candidates.find((p:any)=>normal(p.name)===normal(name))||candidates.find((p:any)=>normal(p.name).startsWith(normal(name)))
  return poi?{name:poi.name,address:poi.address||'地址未查到',coord:[poi.location.lng,poi.location.lat],id:poi.id}:null
}
async function resolvePlace(place:Place){
  const result=await findPlace(place.name)
  if(!result||disposed)return
  located.value[place.key]=result;place.name=result.name;place.address=result.address
  for(const item of plan.value.days)for(const a of item.activities)if(a.key===place.key){a.name=place.name;a.address=place.address}
}
async function loadTravelExtras(){
  const weather=await sdkSearch(new amap.Weather(),['上海'],'getForecast')
  if(disposed)return
  forecasts.value=weather?.forecasts||[];weatherReport.value=weather?.reportTime||'';weatherStatus.value=weather?'ready':'failed'
  hotel.value=await findPlace('上海雅居乐万豪侯爵酒店')
  if(hotel.value?.id && placeQueries<20 && !disposed){
    placeQueries++
    const detail=await sdkSearch(new amap.PlaceSearch({extensions:'all'}),[hotel.value.id],'getDetails')
    if(disposed)return
    const poi=detail?.poiList?.pois?.find((p:any)=>p.id===hotel.value?.id)
    hotelDetailStatus.value=poi?'ready':'failed'
    // 旧版酒店最低房价字段可能已不返回；cost 是人均消费，不能代替每晚房价。
    const raw=poi?.hotel?.lowest_price
    if(typeof raw==='string' && /^\d+(\.\d+)?$/.test(raw.trim()) && Number.isFinite(Number(raw)) && Number(raw)>0)hotel.value.referenceRoomPrice=Number(raw)
  }
  if(!disposed)hotelStatus.value=hotel.value?'ready':'failed'
  drawMap()
}
function hotelPriceText(venue=hotel.value){return venue?.referenceRoomPrice?`高德最低房价参考 ¥${money(venue.referenceRoomPrice)}（日期、房型未确认）`:hotelStatus.value==='loading'?'正在查询房价参考…':'房价参考未查到，请在预订平台确认旅行日期报价。'}
function focusHotel(){if(!hotel.value)return;hotelRoot.value?.scrollIntoView({behavior:'smooth',block:'center'});notify(`推荐酒店：${hotel.value.name} · ${hotel.value.address}`)}
async function queryMode(from:Located,to:Located,mode:Mode):Promise<Leg|null>{
  if(routeQueries.value>=ROUTE_LIMIT||disposed)return null
  routeQueries.value++
  const independent=mode==='walking'||mode==='cycling'
  const service=mode==='walking'?new amap.Walking():mode==='cycling'?new amap.Riding({policy:2}):new amap.Transfer({city:'上海',cityd:'上海',policy:mode==='bus'?amap.TransferPolicy.NO_SUBWAY:amap.TransferPolicy.LEAST_TIME})
  const result=await sdkSearch(service,[new amap.LngLat(...from.coord),new amap.LngLat(...to.coord)])
  let route:any
  if(independent)route=result?.routes?.[0]
  else{
    const candidates=(result?.plans||[]).filter((p:any)=>{
      const types=(p.segments||[]).map((s:any)=>s.transit_mode)
      return types.length && types.every((type:string)=>['WALK','SUBWAY','BUS'].includes(type)) && (mode==='bus'?types.includes('BUS')&&!types.includes('SUBWAY'):types.includes('SUBWAY'))
    })
    // 地铁优先采用不接驳公交的方案；只有混合方案时明确展示接驳公交。
    const pure=mode==='subway'?candidates.filter((p:any)=>!p.segments.some((s:any)=>s.transit_mode==='BUS')):candidates
    route=(pure.length?pure:candidates).sort((a:any,b:any)=>Number(a.time)-Number(b.time))[0]
  }
  if(!route||!Number.isFinite(Number(route.time)))return null
  const parts:RoutePart[]=independent
    ? [{mode,path:coords(((mode==='walking'?route.steps:route.rides)||[]).flatMap((step:any)=>step.path||[]))}]
    : (route.segments||[]).map((segment:any)=>({mode:segment.transit_mode==='WALK'?'walking':segment.transit_mode==='BUS'?'bus':'subway',path:coords(segment.transit?.path||[])}))
  if(!parts.length||parts.some(part=>part.path.length<2))return null
  const note=mode==='subway'&&parts.some(p=>p.mode==='bus')?'含接驳公交':undefined
  return {mode,minutes:Math.max(1,Math.ceil(Number(route.time)/60)),distance:Number(route.distance)||0,parts,note}
}
async function loadLeg(from:Place,to:Place,retry=false):Promise<Options|null>{
  const key=legKey(from,to), cached=routeCache.value[key]
  if(cached&&!retry)return cached
  if(pendingLegs.has(key))return pendingLegs.get(key)!
  const p=located.value[from.key],q=located.value[to.key]
  if(!p||!q)return null
  const request=(async()=>{
    const options:Options=cached?copy(cached):{bus:null,subway:null,cycling:null,walking:null}
    await Promise.all(modes.filter(mode=>!options[mode.key]).map(async mode=>{options[mode.key]=await queryMode(p,q,mode.key)}))
    if(!disposed)routeCache.value[key]=options
    return options
  })()
  pendingLegs.set(key,request)
  try{return await request}finally{pendingLegs.delete(key)}
}
function optionFor(from:Place,to:Place,mode:Mode){return routeCache.value[legKey(from,to)]?.[mode]??null}
function selectedFrom(options:Options|undefined,preferred?:Mode):Leg|null{
  if(!options)return null
  if(preferred)return options[preferred]
  return Object.values(options).filter((leg):leg is Leg=>!!leg).sort((a,b)=>a.minutes-b.minutes)[0]??null
}
function selectedLeg(from:Place,to:Place,item=day.value){return selectedFrom(routeCache.value[legKey(from,to)],item.travelModes?.[legKey(from,to)])}
function optionText(from:Place,to:Place,mode:Mode){
  if(routeScenario.value!=='auto')return '待更新'
  const option=optionFor(from,to,mode)
  return option?'约'+formatDuration(option.minutes):routeState.value==='updating'&&!routeCache.value[legKey(from,to)]?'查询中':'未查到'
}
function chooseMode(from:Place,to:Place,mode:Mode){
  if(!optionFor(from,to,mode))return
  day.value.travelModes??={};day.value.travelModes[legKey(from,to)]=mode
  drawMap();notify(`已采用${modeLabel(mode)}，地图路径与当天总时长已更新。`)
}
function drawMap(fit=true){
  if(!map||disposed)return
  map.clearMap()
  const overlays:any[]=[]
  if(hotel.value){
    const content=document.createElement('button');content.className='amap-hotel-marker';content.textContent='⌂ 酒店';content.setAttribute('aria-label',`地图推荐酒店：${hotel.value.name}`);content.title=hotel.value.name
    const marker=new amap.Marker({position:hotel.value.coord,content,anchor:'center',zIndex:180});marker.on('click',focusHotel);overlays.push(marker)
  }
  day.value.activities.forEach((a,index)=>{
    const place=located.value[a.key];if(!place)return
    const content=document.createElement('button');content.className='amap-order-marker'+(a.kind==='meal'?' meal-marker':'')+(a.id===selectedId.value?' selected-marker':'');content.textContent=String(index+1);content.setAttribute('aria-label',`地图地点 ${index+1}：${a.name}`);content.title=a.name
    const marker=new amap.Marker({position:place.coord,content,anchor:'center',zIndex:200});marker.on('click',()=>focusActivity(a.id));overlays.push(marker)
  })
  if(routeScenario.value==='auto')day.value.activities.slice(1).forEach((a,index)=>{
    const leg=selectedLeg(day.value.activities[index],a);if(!leg)return
    for(const part of leg.parts)overlays.push(new amap.Polyline({path:part.path,strokeColor:({walking:'#b77755',cycling:'#477d9c',bus:'#92764f',subway:'#355c46'})[part.mode],strokeWeight:5,strokeStyle:part.mode==='walking'?'dashed':'solid',isOutline:true,outlineColor:'#fffdf8',borderWeight:2,lineJoin:'round',showDir:part.mode!=='walking'}))
  })
  map.add(overlays)
  if(fit&&overlays.length)map.setFitView(overlays,false,[45,35,45,35],16)
}
async function updateRoute(retryMissing=false){
  const generation=++routeGeneration
  routeState.value=routeScenario.value==='failed'?'failed':'updating'
  drawMap()
  if(mapStatus.value!=='ready'||routeScenario.value!=='auto')return
  mapMessage.value='正在按当前顺序更新路线，已查询路段继续展示。'
  const list=copy(day.value.activities)
  if(retryMissing)await Promise.all(list.filter(a=>!located.value[a.key]).map(a=>resolvePlace(places.value.find(p=>p.key===a.key)!)))
  if(disposed||generation!==routeGeneration)return
  const legs=await Promise.all(list.slice(1).map((a,i)=>loadLeg(list[i],a,retryMissing)))
  if(disposed||generation!==routeGeneration)return
  routeState.value=legs.every((options,i)=>!!selectedFrom(options??undefined,day.value.travelModes?.[legKey(list[i],list[i+1])]))&&list.every(a=>located.value[a.key])?'ready':'failed'
  mapMessage.value=routeState.value==='ready'?'路径与耗时来自高德查询，出发时请重新确认。':'部分地点或路线未查到；已查询路段继续展示，缺失段不画连接线。'
  drawMap()
}
function applyScenario(){updateRoute()}
function selectDay(index:number){cancelDrag();active.value=index;selectedId.value='';search.value='';updateRoute()}
const modeLabel=(mode:Mode)=>modes.find(m=>m.key===mode)?.label||'出行'
function travelMinutes(from:Place,to:Place){return selectedLeg(from,to)?.minutes??null}
function legText(from:Place,to:Place){const leg=selectedLeg(from,to);return leg?`${modeLabel(leg.mode)} · 约${formatDuration(leg.minutes)}${leg.note?' · '+leg.note:''}`:'交通信息未查到'}
function exportLegText(from:Place,to:Place,item:Plan['days'][number]){const leg=selectedFrom(exportRoutes.value[legKey(from,to)],item.travelModes?.[legKey(from,to)]);return leg?`${modeLabel(leg.mode)} · 约${formatDuration(leg.minutes)}（高德查询）`:'交通信息未查到或待更新'}
watch(selectedId,()=>drawMap(false))
onMounted(async()=>{
  const key=import.meta.env.VITE_AMAP_WEB_JS_KEY?.trim(),security=import.meta.env.VITE_AMAP_WEB_JS_SECURITY_CODE?.trim()
  if(!key||!security||key.startsWith('your_')){mapStatus.value='failed';weatherStatus.value='failed';hotelStatus.value='failed';routeState.value='failed';mapMessage.value='需配置高德 JS API Key 与安全密钥。';return}
  window._AMapSecurityConfig={securityJsCode:security}
  try{
    amap=await Promise.race([AMapLoader.load({key,version:'2.0',plugins:['AMap.PlaceSearch','AMap.Walking','AMap.Transfer','AMap.Riding','AMap.Weather','AMap.Scale','AMap.ToolBar','AMap.GeometryUtil']}),new Promise((_,reject)=>setTimeout(()=>reject(new Error('timeout')),20000))])
    if(disposed)return
    map=new amap.Map(mapRoot.value!,{center:[121.475,31.23],zoom:13,viewMode:'2D',resizeEnable:true,scrollWheel:false})
    map.addControl(new amap.Scale());map.addControl(new amap.ToolBar({position:'RB'}))
    mapStatus.value='ready'
    await loadTravelExtras()
    await Promise.all(places.value.map(async place=>{await resolvePlace(place);drawMap()}))
    if(disposed)return
    await updateRoute()
  }catch{if(!disposed){mapStatus.value='failed';weatherStatus.value='failed';hotelStatus.value='failed';routeState.value='failed';mapMessage.value='地图加载失败，请检查网络和高德配置后刷新。地点编辑与导出仍可使用。'}}
})
function reorder(from:number,to:number){if(from===to||from<0||to<0||to>=day.value.activities.length)return;const [a]=day.value.activities.splice(from,1);day.value.activities.splice(to,0,a);updateRoute();notify('顺序已按你的调整保留，正在更新路段。')}
function keyboardMove(index:number,direction:number){const id=day.value.activities[index].id;reorder(index,index+direction);nextTick(()=>document.querySelector<HTMLElement>(`[data-activity-id="${id}"] .drag-handle`)?.focus())}
function startDrag(event:PointerEvent,id:string){if(event.button!==0)return;dragOrigin={id,pointerId:event.pointerId,x:event.clientX,y:event.clientY};(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)}
function trackDrag(event:PointerEvent){if(!dragOrigin||event.pointerId!==dragOrigin.pointerId)return;if(Math.hypot(event.clientX-dragOrigin.x,event.clientY-dragOrigin.y)<6&&!draggingId.value)return;draggingId.value=dragOrigin.id;const target=document.elementFromPoint(event.clientX,event.clientY)?.closest<HTMLElement>('[data-activity-id]');if(target)dropId.value=target.dataset.activityId||'';if(event.clientY<70)window.scrollBy(0,-12);else if(event.clientY>window.innerHeight-70)window.scrollBy(0,12)}
function finishDrag(){if(draggingId.value&&dropId.value)reorder(day.value.activities.findIndex(a=>a.id===draggingId.value),day.value.activities.findIndex(a=>a.id===dropId.value));cancelDrag()}
function cancelDrag(){dragOrigin=null;draggingId.value='';dropId.value=''}
function focusActivity(id:string){selectedId.value=id;document.getElementById(id)?.scrollIntoView({behavior:'smooth',block:'center'})}
function addPlace(place:Place){if(day.value.activities.some(a=>a.key===place.key))return;day.value.activities.push(make(place.key,'demo-'+Date.now(),place.kind==='meal'?'用餐':'自由安排'));updateRoute();notify('已加入当天末尾，其他地点顺序保留；可在左侧拖动调整。')}
function showPanel(name:string,a?:Activity){panel.value=name;editingId.value=a?.id||'';nextTick(()=>dialog.value?.showModal())}
function closeDialog(){dialog.value?.close();panel.value=''}
function askDelete(a:Activity){showPanel('delete',a)}
function askRestore(){if(saved.value)showPanel('restore')}
function submitDialog(){const activity=editingActivity.value;
  if(panel.value==='delete'&&activity){day.value.activities=day.value.activities.filter(a=>a.id!==activity.id);if(selectedId.value===activity.id)selectedId.value='';updateRoute();notify('已移除目的地，其他地点相对顺序保留。')}
  if(panel.value==='restore'&&saved.value){plan.value=copy(saved.value);active.value=0;selectedId.value='';updateRoute();notify('已还原上次成功保存的结果。')}
  closeDialog()
}
function resetDemo(){cancelDrag();plan.value=initial();saved.value=null;localStorage.removeItem(SAVED);active.value=0;selectedId.value='';search.value='';routeScenario.value='auto';updateRoute();saveFailure.value=false;persist();notify('已重置示例。')}
async function exportTrip(){if(exporting.value)return;exporting.value=true;const frozen=copy(plan.value);exportSnapshot.value=frozen;exportState.value=routeState.value;exportHotel.value=copy(hotel.value);exportWeather.value=Object.fromEntries(frozen.days.map(item=>[item.date,copy(weatherFor(item.date))]));exportRoutes.value=routeScenario.value==='auto'?copy(routeCache.value):{};let saveOK=!saveFailure.value;
  if(saveOK){try{localStorage.setItem(SAVED,JSON.stringify(frozen));saved.value=copy(frozen)}catch{saveOK=false}}
  try{await nextTick();const canvas=await html2canvas(exportRoot.value!,{backgroundColor:'#fffdf8',scale:1.5,logging:false});const blob=await new Promise<Blob|null>(resolve=>canvas.toBlob(resolve,'image/png'));if(!blob)throw new Error('图片未生成');const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download='旅程规划原型_上海两日.png';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notify(saveOK?'图片已导出；已模拟保存，之后可继续编辑或还原。':'图片已导出，但保存失败；上次保存结果保持不变。')}
  catch{notify('图片生成失败，请重新导出；你的当前编辑仍保留。')}
  finally{exporting.value=false;exportSnapshot.value=null}
}
onUnmounted(()=>{disposed=true;routeGeneration++;clearTimeout(toastTimer);map?.destroy()})
</script>

<style scoped>
*{box-sizing:border-box} .result-shell{max-width:1460px;margin:auto;padding:28px clamp(20px,5vw,72px) 90px;color:#1b322c;font-family:'PingFang SC','Microsoft YaHei',sans-serif}button,input,select{font:inherit}button{cursor:pointer;border:1px solid #cdd8c9;background:#fffdfa;color:#355b44;padding:9px 14px;border-radius:2px;font-size:12px;transition:background .15s}button:hover{background:#eaf0df}button:disabled{opacity:.4;cursor:default}button.primary{background:#355c46;color:white;border-color:#355c46}.primary:hover{background:#284a36}button.text-button{border:0;background:transparent;padding:0}input,select{border:1px solid #cdd8c9;background:#fffdfa;padding:12px;color:#254634;border-radius:2px;max-width:100%}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #a76943;outline-offset:3px}p{line-height:1.8}.muted,small{color:#718173}.prototype-note{display:flex;gap:12px;align-items:center;font-size:11px;color:#788572;padding:10px 0 24px}.prototype-note b{background:#e6ecd9;color:#426141;padding:4px 9px}.result-nav{display:flex;align-items:center;justify-content:space-between;gap:18px;padding-bottom:21px;border-bottom:1px solid #d7ded2}.nav-actions{display:flex;gap:12px}.hero{display:flex;align-items:end;justify-content:space-between;gap:30px;padding:37px 0 27px;border-bottom:1px solid #cbd7c6}.eyebrow{color:#a65c40;font-size:10px;letter-spacing:.16em;font-weight:700}.hero h1{font-family:'Songti SC','Noto Serif SC',serif;font-size:clamp(42px,5vw,66px);line-height:1.15;letter-spacing:-.05em;font-weight:600;margin:16px 0 14px}.hero p{color:#708172;font-size:13px;margin:0}.preview-tag{display:inline-block;margin-left:15px;border:1px solid #d2dbc7;padding:3px 8px;font-size:10px}.budget{min-width:250px;padding:20px 25px;background:#e4edcd;display:flex;flex-direction:column;gap:7px}.budget strong{font:38px/1.2 'Songti SC',serif}.budget small{font-size:11px;color:#62735f}.preview-status{display:flex;justify-content:space-between;gap:14px;font-size:11px;padding:17px 0;color:#708172}.preview-status i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#688564;margin-right:9px}.preview-status i.dirty{background:#bc7d4a}.base{display:flex;justify-content:space-between;gap:25px;padding:25px 30px;background:#eff3e8;margin:4px 0 20px}.base strong{font-size:10px;letter-spacing:.15em;color:#a65c40}.base h3{font:23px 'Songti SC',serif;margin:10px 0}.base p{font-size:12px;color:#61746a;margin:7px 0}.base small{font-size:11px}.global-conditions{min-width:250px}.day-tabs{display:flex;border-bottom:1px solid #d9e1d4;margin:30px 0 34px}.day-tabs button{display:flex;align-items:center;gap:12px;flex-wrap:wrap;border:0;border-bottom:2px solid transparent;background:transparent;padding:17px 22px;color:#718274}.day-tabs button.active{background:#e5edcf;border-color:#bb704f;color:#1b392e}.day-tabs button small{font-size:11px}.day-tabs button span{width:100%;text-align:left;font-size:10px}.day-layout{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,1fr);gap:45px;align-items:start}.day-heading{display:flex;align-items:center;justify-content:space-between;gap:18px}.day-heading h2,.map-panel h2{font:27px 'Songti SC',serif;letter-spacing:-.035em;margin:10px 0}.day-heading p{font-size:12px;margin:8px 0 19px}.daily-summary{display:flex;gap:20px;flex-wrap:wrap;background:#edf2e4;padding:13px 16px;font-size:11px;color:#45624f}.day-note{font-size:11px;color:#788675;margin:13px 0 23px}.notice{background:#fcf4e7;border:1px solid #e6d7b9;padding:12px 16px;font-size:12px;margin:16px 0;display:flex;gap:12px;flex-wrap:wrap;align-items:center}.activity{border:1px solid #d9dfd3;background:#fffdfa;padding:22px 24px;box-shadow:6px 6px 0 #f0f2e9;margin:0 0 15px}.activity.focused{border-color:#a8754e;box-shadow:6px 6px 0 #e9e4d8}.activity.meal{background:#fcfaf5}.activity-top{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:10px}.period{color:#a75b3f}.duration{padding:5px 8px;font-size:10px;border-color:#e2e6dc;background:transparent}.duration span{margin-left:8px;color:#a65c40}.activity-title{display:flex;align-items:center;gap:9px;margin:14px 0 10px}.activity-name{padding:0;border:0;background:transparent;text-align:left;font:23px 'Songti SC',serif;color:#254130;display:flex;align-items:center;gap:10px}.number{font:12px sans-serif;color:#92a08a}.required{font-size:10px;padding:3px 6px;background:#edf1e1;color:#698156}.activity p{font-size:12px;color:#61746a;margin:8px 0}.activity p.address{font-size:10px;color:#81907d}.activity footer{margin-top:17px;border-top:1px solid #e9eddf;padding-top:14px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;font-size:11px;color:#6a7f60}.activity-actions{display:flex;gap:5px;flex-wrap:wrap}.activity-actions button{padding:5px 8px;font-size:10px}.danger{color:#a35c46}.leg-card{margin:24px 0 20px 13px;padding:0 0 0 17px;border-left:2px solid #b2c7a6}.leg-heading{display:flex;justify-content:space-between;gap:15px;font-size:10px;color:#64785e;margin-bottom:10px}.leg-heading small{font-size:9px}.mode-options{display:flex;gap:8px}.mode-options button{display:flex;gap:4px;flex-direction:column;align-items:flex-start;min-width:90px;padding:8px 12px;font-size:10px;border-color:#e0e7d5;background:transparent}.mode-options button b{font-size:12px;font-weight:500}.mode-options button small{font-size:9px}.mode-options button.selected{border-color:#789272;background:#eaf1df}.end-of-day{display:flex;align-items:center;gap:15px;color:#98a188;padding:22px 0}.end-of-day span{font-size:22px}.end-of-day p{font-size:11px}.map-panel{position:sticky;top:22px;border:1px solid #d8e0d2;background:#f4f6ee;padding:23px;box-shadow:8px 8px 0 #e8eee1}.map-panel h2{font-size:23px;margin:10px 0 22px}.route-sketch{display:block;width:100%;border:1px solid #dce5d3}.map-point{cursor:pointer}.map-caption{font-size:10px}.place-detail{border:1px solid #dce3d3;padding:16px;margin:18px 0;background:#fffdfa}.place-detail h3{font:20px 'Songti SC',serif;margin:8px 0}.place-detail p{font-size:11px}.place-detail button{width:100%}.search-block{margin-top:22px;border-top:1px solid #dce3d3;padding-top:20px}.search-block label{font-size:12px;color:#456348}.search-input{display:flex;align-items:center;background:#fffdfa;border:1px solid #cdd8c9;margin:12px 0}.search-input input{border:0;width:100%;font-size:11px}.search-input span{padding:0 13px;font-size:24px;color:#68825f}.search-results button{width:100%;display:flex;justify-content:space-between;text-align:left;border:0;border-bottom:1px solid #e0e6d6;background:transparent;padding:13px 4px}.search-results button span:first-child{display:flex;flex-direction:column;gap:6px}.search-results b{font-size:12px;font-weight:500}.search-results small{font-size:10px}.prototype-tools{border:1px dashed #c9d3c2;padding:16px 20px;margin-top:35px;color:#687761;font-size:11px}.prototype-tools summary{cursor:pointer}.prototype-tools summary span{color:#94a08a;margin-left:15px}.tool-grid{display:flex;gap:22px;align-items:center;flex-wrap:wrap;margin-top:18px}.tool-grid label{display:flex;align-items:center;gap:8px}.tool-grid select{font-size:11px;padding:6px}.tool-grid input{padding:0}.prototype-tools pre{overflow:auto;max-height:250px;font-size:10px;background:#edf1e5;padding:12px}.toast{position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:#294a36;color:white;padding:14px 22px;box-shadow:0 6px 28px #1b322c22;font-size:12px;z-index:20;max-width:calc(100vw - 35px);width:max-content;line-height:1.7}.empty-day{padding:30px;border:1px dashed #b7c9a8;font-size:12px;color:#748467}dialog{border:1px solid #ced9c7;background:#fffdf8;color:#254532;padding:28px;width:520px;max-width:calc(100vw - 32px);box-shadow:14px 14px 0 #31453222}dialog::backdrop{background:#1d322755;backdrop-filter:blur(3px)}.dialog-header{display:flex;justify-content:space-between;align-items:start;margin-bottom:18px}.dialog-header h2{font:26px 'Songti SC',serif;margin:10px 0}.dialog-header>button{border:0;font-size:23px;padding:0 5px;background:transparent}dialog p{font-size:12px}.picker-option{display:flex;align-items:center;justify-content:space-between;text-align:left;width:100%;gap:18px;padding:16px;margin:10px 0}.picker-option span:first-child{display:flex;flex-direction:column;gap:8px}.picker-option small{font-size:10px;line-height:1.7}.picker-option b{font:18px 'Songti SC',serif}.field-label{display:flex;flex-direction:column;gap:10px;font-size:12px;margin:20px 0}.quick-options{display:flex;gap:10px;margin-bottom:25px}.quick-options button.selected{background:#e5edcf;border-color:#718e62}.full{width:100%}.dialog-actions{display:flex;justify-content:end;gap:10px;margin-top:25px}.export-stage{position:fixed;left:-20000px;top:0;pointer-events:none}.export-sheet{width:880px;padding:52px;background:#fffdf8;color:#254532}.export-sheet h1{font:46px 'Songti SC',serif}.export-sheet h2{font:28px 'Songti SC',serif;border-bottom:1px solid #cdd8c9;padding-bottom:15px;margin-top:36px}.export-sheet p{font-size:14px}.export-activity{border:1px solid #d5ddcd;padding:20px;margin:18px 0}.export-activity h3{font:22px 'Songti SC',serif;margin:12px 0}
@media(max-width:1000px){.day-layout{gap:26px;grid-template-columns:minmax(0,1.5fr) minmax(270px,1fr)}.activity{padding:20px}.activity-actions button{padding:5px 6px}}@media(max-width:800px){.day-layout{grid-template-columns:1fr}.map-panel{position:static}.hero{align-items:start}.budget{min-width:220px}.base{padding:22px}.global-conditions{min-width:190px}.preview-status{flex-wrap:wrap}.activity footer{align-items:start}.mode-options button{min-width:84px}}@media(max-width:600px){.result-shell{padding:20px 18px 60px}.prototype-note{align-items:start;font-size:10px}.result-nav{flex-wrap:wrap}.nav-actions{width:100%;justify-content:space-between}.hero{display:block;padding:28px 0}.hero h1{font-size:43px}.budget{margin-top:23px}.base{display:block}.global-conditions{border-top:1px solid #dce4d0;margin-top:16px;padding-top:10px}.day-tabs button{padding:13px 14px;gap:8px}.day-heading h2{font-size:25px}.day-heading{align-items:start}.day-heading>button{white-space:nowrap;margin-top:18px;padding:7px 9px}.daily-summary{gap:12px;font-size:10px}.activity-title .activity-name{font-size:22px}.activity-actions{gap:5px}.leg-heading{flex-wrap:wrap;gap:6px}.preview-tag{margin-left:8px}.prototype-tools{padding:14px}.quick-options{gap:7px}.quick-options button{padding:8px}.toast{bottom:18px}}

.order-hint{font-size:11px;color:#75866f;margin:0 0 16px;line-height:1.8}
.activity-reference{display:flex;align-items:center;gap:12px}.activity-name{margin:0;font-weight:500}
.activity-reference .duration{padding:0;border:0}.drag-handle{font-size:24px;color:#8d9b82;cursor:grab;touch-action:none;user-select:none;line-height:1;padding:5px 8px;border-radius:3px}
.drag-handle:hover,.drag-handle:focus-visible{background:#e5edcf;color:#355c46}.drag-handle:focus-visible{outline:2px solid #a76943;outline-offset:2px}
.activity.dragging{opacity:.45}.activity.drop-target{border:2px dashed #678560;background:#f1f5e8}.leg-card{margin:17px 0 19px 13px}.leg-heading{align-items:center;margin:0;flex-wrap:wrap}.leg-heading strong{font-size:11px;font-weight:500;color:#47654e}
.map-panel{max-height:calc(100vh - 44px);overflow-y:auto;scrollbar-width:thin}.map-panel h2{margin-bottom:15px}.map-legend{display:flex;gap:16px;margin:13px 0 5px;font-size:10px;color:#75836d}.map-legend i{display:inline-block;width:16px;border-top:3px solid #4f7d64;margin-right:7px;vertical-align:middle}.map-legend i.walking{border-top:3px dashed #b77755}
.destination-block{border-top:1px solid #dce3d3;margin-top:20px;padding-top:18px}.list-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.list-heading h3{font:20px 'Songti SC',serif;margin:0}.list-heading small{font-size:10px}.management-note{font-size:10px;margin:9px 0 10px}.destination-list{list-style:none;margin:0;padding:0}.destination-list li{display:flex;align-items:center;gap:8px;border-bottom:1px solid #dce3d3;padding:7px 0}.destination-list li.selected{background:#eaf0df}
button.destination-focus{display:flex;align-items:center;gap:12px;border:0;background:transparent;text-align:left;flex:1;min-width:0;padding:7px 0}.destination-focus>span:last-child{display:flex;flex-direction:column;gap:6px}.destination-focus b{font-size:12px;font-weight:500}.destination-focus small{font-size:10px}.destination-number{font-size:11px;color:#8b9983;min-width:12px}.remove-place{font-size:10px;padding:5px 8px;background:transparent;color:#a16c51;border:0;flex-shrink:0}.candidate-row{display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid #e0e6d6;padding:12px 0}.candidate-row>span{display:flex;flex-direction:column;gap:6px}.candidate-row b{font-size:12px;font-weight:500}.candidate-row small{font-size:10px}.candidate-row>button{font-size:10px;white-space:nowrap;padding:6px 8px}.search-block{margin-top:20px;padding-top:18px}.search-results p{font-size:11px}
@media(max-width:800px){.map-panel{max-height:none;overflow:visible}}@media(max-width:600px){.activity{padding:17px}.activity-reference{gap:5px}.drag-handle{padding:5px}.activity-top{gap:5px}.leg-heading{font-size:10px}.leg-heading strong{font-size:10px}}


.map-wrap{position:relative}.real-map{height:340px;width:100%;border:1px solid #dce5d3;background:#edf1e6}.map-overlay{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:12px;padding:25px;background:#eff3e8e8;text-align:center;font-size:12px}.map-overlay span{font-size:11px;line-height:1.8}.retry-route{font-size:10px;padding:5px 9px}.real-map :deep(.amap-order-marker){width:29px;height:29px;border:3px solid #fffdf8;border-radius:50%;padding:0;background:#355c46;color:white;box-shadow:0 2px 7px #25453266;font-size:12px;font-weight:600}.real-map :deep(.meal-marker){background:#b77755}.real-map :deep(.selected-marker){outline:3px solid #d6bd79;transform:scale(1.12)}
.weather-card,.stay-card{border:1px solid #d9dfd3;padding:18px 22px;background:#f7f8f1;margin:18px 0}.weather-card>div{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.weather-card strong{font-size:13px;font-weight:500}.weather-card small{font-size:10px}.weather-card p{font-size:12px;color:#61746a;margin:10px 0 0}.stay-card h3{font:21px 'Songti SC',serif;margin:10px 0 17px}.stay-columns{display:grid;grid-template-columns:1.15fr 1fr;gap:24px}.stay-columns>div{display:flex;flex-direction:column;gap:8px}.stay-columns b{font-size:13px;font-weight:500}.stay-columns span,.stay-columns small{font-size:11px;line-height:1.8;color:#718173}.transport-options{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:12px}.transport-options button{display:flex;flex-direction:column;align-items:flex-start;gap:6px;font-size:10px;padding:9px 11px;background:transparent;min-width:0}.transport-options b{font-size:11px;font-weight:500}.transport-options button.chosen{border-color:#789272;background:#eaf1df}.transport-options small{font-size:9px}.transport-options button:disabled{opacity:.65}.transport-note{font-size:10px;color:#788675;line-height:1.7;margin:9px 0 0}.map-legend{gap:12px;flex-wrap:wrap}.map-legend i.bus{border-color:#92764f}.map-legend i.cycling{border-color:#477d9c}@media(max-width:600px){.stay-columns{grid-template-columns:1fr;gap:16px}.weather-card,.stay-card{padding:16px}.transport-options{grid-template-columns:repeat(2,minmax(0,1fr))}}
.real-map :deep(.amap-hotel-marker){width:auto;min-width:63px;height:28px;border:2px solid #fffdf8;border-radius:5px;padding:3px 8px;background:#586c8a;color:white;white-space:nowrap;font-size:11px;box-shadow:0 2px 7px #25453266}.hotel-legend{color:#586c8a}.recommendation-heading{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:15px 0 0}.recommendation-heading h4{font-size:12px;font-weight:500;margin:0}.recommendation-heading small{font-size:10px}.candidate-row>span{min-width:0}.candidate-row .candidate-description{font-size:10px;color:#718173;line-height:1.7}.search-results .candidate-row>button{width:auto;border:1px solid #cdd8c9;flex-shrink:0;padding:6px 8px}
</style>
