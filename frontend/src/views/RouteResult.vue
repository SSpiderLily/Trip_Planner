<template>
  <main class="result-shell">
    <nav><router-link to="/">← 调整需求，重新规划</router-link><router-link to="/observability">查看规划过程</router-link></nav>
    <template v-if="plan">
      <header class="hero">
        <div><span class="eyebrow">每日旅行路线</span><h1>{{ plan.planning_conditions.city }} · {{ plan.days.length }} 天</h1><p>{{ plan.planning_conditions.start_date }} — {{ plan.planning_conditions.end_date }}</p></div>
        <div class="budget"><small>{{ plan.cost_summary.complete ? '预计全程费用 / 人' : '已知费用小计 / 人' }}</small><strong>¥{{ plan.cost_summary.known_total }}</strong><small>不含住宿及往返目的地交通</small></div>
      </header>
      <section class="base">
        <div><strong>{{ plan.lodging_base.source === 'user' ? '已定住处' : '推荐住宿区域' }}</strong><h3>{{ plan.lodging_base.user_input || plan.lodging_base.area_name }}</h3><p>{{ plan.lodging_base.recommendation_reason }}</p></div>
        <div><p>路线基点：{{ plan.lodging_base.place?.name || '无法定位，往返交通未知' }}</p><small v-if="plan.lodging_base.place?.is_area_reference">使用区域参考点，实际住宿位置可能增加交通时间。</small><small>当前交通：{{ modeName(plan.planning_conditions.transportation) }}，短途可结合步行。</small></div>
      </section>
      <details v-if="plan.issues.length" class="issues" open>
        <summary>{{ adjustments.length ? `需调整 ${adjustments.length} 项` : '生成完成' }} · 待确认 {{ plan.issues.length - adjustments.length }} 项</summary>
        <div class="issue-list"><button v-for="issue in plan.issues" :key="issue.issue_id" :class="{ conflict: issue.category === 'needs_adjustment' }" @click="locate(issue)">{{ issue.date ? issue.date + ' · ' : '' }}{{ issue.message }} <span v-if="issue.date">↗</span></button></div>
      </details>
      <div class="day-tabs" role="tablist" aria-label="旅行日期"><button v-for="(day,index) in plan.days" :key="day.date" role="tab" :aria-selected="index === active" :class="{ active: index === active }" @click="active=index">第 {{ index+1 }} 天 <small>{{ day.date }}</small></button></div>
      <div v-if="day" class="day-layout">
        <section class="timeline">
          <h2>{{ day.date }} 的安排</h2><p class="muted">{{ day.description }}</p>
          <div class="daily-summary"><span>{{ day.time_summary.complete ? '预计安排' : '已知安排' }} {{ day.time_summary.known_total }} 分钟</span><span>含 {{ day.time_summary.buffer_minutes }} 分钟机动</span><span>{{ day.cost_summary.complete ? '预计' : '已知费用' }} ¥{{ day.cost_summary.known_total }} / 人</span></div>
          <p v-if="day.time_summary.status === 'incomplete'" class="notice">存在未知耗时，尚不能确认是否在每日 10 小时上限内。</p>
          <p v-else-if="day.time_summary.status === 'exceeded'" class="notice">当天安排超过每日 10 小时上限，需要调整。</p>
          <p v-if="day.weather" class="weather">天气：{{ day.weather.dayweather }} · {{ day.weather.nighttemp }}–{{ day.weather.daytemp }}℃</p><p v-else class="muted">天气未知，原因见上方提示。</p>
          <div class="endpoint">出发 · {{ plan.lodging_base.place?.name || plan.lodging_base.user_input || '住宿区域' }}</div>
          <template v-for="activity in day.activities" :key="activity.activity_id">
            <div v-for="leg in arriving(activity.activity_id)" :id="leg.leg_id" :key="leg.leg_id" class="leg"><span>{{ modeName(leg.mode) }} · {{ minutes(leg.duration_minutes) }}</span><small>{{ leg.data_basis === 'route_query' ? '地图路线查询' : leg.data_basis === 'same_place' ? '同一地点' : '交通信息未知' }}{{ leg.note ? ' · ' + leg.note : '' }}</small></div>
            <article :id="activity.activity_id" class="activity" @click="focusPlace(activity.place)">
              <div class="activity-top"><span class="period">{{ periodName(activity.period) }}</span><span>{{ minutes(activity.duration_minutes) }}</span></div>
              <h3>{{ activity.title }}</h3><p>{{ activity.description }}</p><p v-if="activity.place" class="address">{{ activity.place.name }} · {{ activity.place.address }}</p><p v-else class="muted">附近自由安排；未指定具体地点。</p>
              <footer><span>{{ activity.estimated_cost.amount === null ? '费用待确认' : `约 ¥${activity.estimated_cost.amount} / 人` }}</span><small>{{ activity.estimated_cost.basis }}</small></footer>
            </article>
          </template>
          <div v-for="leg in returning" :id="leg.leg_id" :key="leg.leg_id" class="leg"><span>返回住处 · {{ modeName(leg.mode) }} · {{ minutes(leg.duration_minutes) }}</span><small>{{ leg.note }}</small></div>
          <div class="endpoint">返回 · {{ plan.lodging_base.place?.name || plan.lodging_base.user_input || '住宿区域' }}</div>
        </section>
        <aside class="map-panel"><h2>当天地点与顺序</h2><div id="route-map" class="live-map" v-show="mapReady"></div>
          <svg v-if="!mapReady" class="route-sketch" viewBox="0 0 480 400" role="img" aria-label="当天地点位置与游览顺序示意">
            <path d="M0 80H480 M0 160H480 M0 240H480 M0 320H480 M80 0V400 M160 0V400 M240 0V400 M320 0V400 M400 0V400" stroke="#dce8e6" fill="none" />
            <polyline :points="points.map(p => `${p.x},${p.y}`).join(' ')" fill="none" stroke="#5b8f86" stroke-width="2" stroke-dasharray="5 5" />
            <g v-for="(point,index) in points" :key="index"><circle :cx="point.x" :cy="point.y" r="13" :fill="index === 0 ? '#223d38' : '#147d6b'"/><text :x="point.x" :y="point.y+4" text-anchor="middle" fill="white" font-size="11">{{ index === 0 ? '住' : index }}</text><text :x="point.x" :y="point.y+30" text-anchor="middle" font-size="11" fill="#233f39">{{ point.name.slice(0,12) }}</text></g>
          </svg>
          <p class="muted">{{ mapNote }} 虚线仅表示地点顺序，不是道路导航线路；路段耗时以左侧查询结果为准。</p>
          <ol><li v-for="(place,index) in mapPlaces" :key="index"><button @click="focusPlace(place)">{{ index === 0 && plan.lodging_base.place ? '住宿基点' : index }} · {{ place.name }}</button></li></ol>
        </aside>
      </div>
      <details class="raw"><summary>查看原始结果</summary><pre>{{ JSON.stringify(plan,null,2) }}</pre></details>
    </template>
    <section v-else-if="raw" class="legacy"><h1>历史行程 · 原始数据</h1><p>这份结果使用旧格式，未经过新版路线检查。可以返回首页重新生成。</p><pre>{{ JSON.stringify(raw,null,2) }}</pre></section>
    <a-empty v-else description="暂无行程，请返回首页生成或从观测页打开历史结果。" />
  </main>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import type { RouteItinerary, Place, Issue } from '@/types/itinerary'
const raw = ref<any>(null)
try { raw.value = JSON.parse(sessionStorage.getItem('tripPlan') || 'null') } catch { /* 损坏的本地缓存不阻断导航 */ }
const plan = computed<RouteItinerary | null>(() => raw.value?.schema_version === 2 && Array.isArray(raw.value.days) ? raw.value : null)
const active = ref(0), mapReady = ref(false), mapNote = ref('位置示意图。')
const day = computed(() => plan.value?.days[active.value])
const adjustments = computed(() => plan.value?.issues.filter(p => p.category === 'needs_adjustment') || [])
const mapPlaces = computed(() => [plan.value?.lodging_base.place, ...(day.value?.activities.map(a => a.place) || [])].filter((p): p is Place => !!p))
const returning = computed(() => day.value?.legs.filter(l => l.to_activity_id === 'lodging') || [])
const arriving = (id: string) => day.value?.legs.filter(l => l.to_activity_id === id) || []
const minutes = (n: number | null) => n === null ? '耗时未知' : `约 ${n} 分钟`
const modeName = (s: string) => ({ transit: '公共交通', walking: '步行', driving: '驾车' } as Record<string,string>)[s] || s
const periodName = (s: string) => ({ morning: '上午', lunch: '午餐', afternoon: '下午', dinner: '晚餐', evening: '晚间' } as Record<string,string>)[s] || s
const points = computed(() => {
  const ps = mapPlaces.value
  if (!ps.length) return []
  const xs = ps.map(p => p.longitude), ys = ps.map(p => p.latitude)
  const cx = (Math.max(...xs)+Math.min(...xs))/2, cy = (Math.max(...ys)+Math.min(...ys))/2
  const cos = Math.cos(cy*Math.PI/180), width = Math.max((Math.max(...xs)-Math.min(...xs))*cos, Math.max(...ys)-Math.min(...ys),0.005)
  return ps.map(p => ({ name:p.name, x:240+(p.longitude-cx)*cos/width*300, y:185-(p.latitude-cy)/width*270 }))
})
let map: any = null, amap: any = null, disposed = false
function drawMap() {
  if (!map || !amap) return
  map.clearMap()
  const ps = mapPlaces.value
  for (const [index,p] of ps.entries()) {
    const label = document.createElement('span'); label.className = 'map-label'; label.textContent = `${index === 0 && plan.value?.lodging_base.place ? '住' : index}. ${p.name}`
    map.add(new amap.Marker({ position:[p.longitude,p.latitude], title:p.name, label:{content:label.outerHTML, direction:'top'} }))
  }
  if (ps.length > 1) map.add(new amap.Polyline({ path:[...ps, ...(plan.value?.lodging_base.place ? [plan.value.lodging_base.place] : [])].map(p => [p.longitude,p.latitude]), strokeColor:'#147d6b', strokeStyle:'dashed' }))
  if (ps.length) map.setFitView()
}
function focusPlace(place: Place | null) { if (map && place) map.setZoomAndCenter(15,[place.longitude,place.latitude]) }
async function locate(issue: Issue) {
  if (issue.date && plan.value) { const index=plan.value.days.findIndex(d => d.date === issue.date); if(index>=0) active.value=index }
  await nextTick()
  const element=document.getElementById(issue.leg_id || issue.activity_id || '')
  if(element) element.scrollIntoView({ behavior:'smooth', block:'center' })
}
watch(active, () => nextTick(drawMap))
onMounted(async () => {
  if (!plan.value) return
  const key = import.meta.env.VITE_AMAP_WEB_JS_KEY?.trim()
  const security = import.meta.env.VITE_AMAP_WEB_JS_SECURITY_CODE?.trim()
  if (!key || !security || key.startsWith('your_')) return
  window._AMapSecurityConfig = { securityJsCode:security }
  try {
    amap = await AMapLoader.load({ key, version:'2.0', plugins:[] })
    if(disposed) return
    mapReady.value=true; await nextTick()
    map=new amap.Map('route-map',{zoom:12,viewMode:'2D'})
    mapNote.value='地图展示已核实的位置。'; drawMap()
  } catch { mapNote.value='地图暂不可用，显示基于已核实坐标的位置示意。' }
})
onUnmounted(() => { disposed=true; map?.destroy() })
</script>

<style scoped>
.result-shell{max-width:1320px;margin:auto;padding:32px;color:#203a35;font-family:system-ui,sans-serif}nav{display:flex;justify-content:space-between;margin-bottom:32px}a{color:#147d6b}.hero{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}.eyebrow{font-size:13px;letter-spacing:3px;color:#147d6b}h1{font-size:38px;margin:8px 0}h2{font-size:21px}h3{font-size:18px;margin:10px 0}.budget{display:grid;text-align:right;gap:6px}.budget strong{font-size:32px}.base{display:flex;justify-content:space-between;gap:24px;padding:20px 24px;border-radius:16px;background:#eaf3ef;margin-bottom:20px}.base small{display:block;color:#526b65}.base p{margin:8px 0}.issues{border:1px solid #e3d4b1;border-radius:12px;padding:14px 18px;background:#fffdf5}summary{cursor:pointer;font-weight:600}.issue-list{display:grid;grid-template-columns:1fr 1fr;max-height:180px;overflow:auto;margin-top:10px;gap:4px}.issue-list button{border:0;background:transparent;text-align:left;color:#75633c;padding:7px;cursor:pointer}.issue-list .conflict{color:#b64835}.day-tabs{display:flex;gap:8px;overflow:auto;margin:24px 0}.day-tabs button{border:1px solid #d9e3df;background:white;border-radius:12px;padding:12px 22px;cursor:pointer;white-space:nowrap;color:#36544b}.day-tabs .active{background:#24584b;color:white;border-color:#24584b}.day-tabs small{display:block;margin-top:4px}.day-layout{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:28px}.timeline{min-width:0}.daily-summary{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;background:#f0f5f2;padding:12px;border-radius:10px}.notice{color:#9b582c;font-size:13px}.muted{color:#667c75;font-size:13px;line-height:1.8}.weather{font-size:13px}.endpoint{padding:14px;border-left:3px solid #476b5b;margin:14px 0;font-weight:600;background:#f5f7f4;border-radius:0 8px 8px 0}.activity{border:1px solid #e0e7e2;background:white;border-radius:14px;padding:18px 22px;cursor:pointer}.activity-top{display:flex;justify-content:space-between;font-size:12px;color:#60756d}.period{color:#147d6b;font-weight:700}.activity p{line-height:1.8;font-size:14px}.address{color:#667c75;font-size:12px!important}.activity footer{border-top:1px solid #edf0ed;padding-top:12px;display:flex;justify-content:space-between;gap:12px;font-size:12px}.activity footer small{color:#7d8984}.leg{display:grid;gap:5px;border-left:2px dashed #b1c5bd;margin-left:24px;padding:16px 18px;color:#477b6e;font-size:13px}.leg small{color:#7a8b83}.map-panel{align-self:start;position:sticky;top:16px;background:#f5f8f5;border-radius:16px;padding:20px}.live-map{height:410px;border-radius:12px}.route-sketch{width:100%;background:#eef4f1;border-radius:12px}.map-panel ol{padding-left:20px}.map-panel button{background:none;border:0;color:#365c4b;cursor:pointer;padding:5px;text-align:left}.raw{margin-top:28px}.raw pre,.legacy pre{max-height:550px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f5f2;padding:20px;border-radius:12px;font-size:12px}article:target{outline:2px solid #147d6b}@media(max-width:800px){.result-shell{padding:18px}.hero,.base{display:block}.budget{text-align:left;margin-top:20px}.day-layout{grid-template-columns:1fr}.map-panel{position:static;grid-row:1}.issue-list{grid-template-columns:1fr}h1{font-size:30px}}
</style>
