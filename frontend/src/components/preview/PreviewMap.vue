<template>
  <section class="map-card" aria-label="当天地图" :style="{ '--transit-color': routeColors.transit, '--cycling-color': routeColors.bicycling, '--walking-color': routeColors.walking }">
    <div class="map-heading">
      <div>
        <span class="eyebrow">DAY MAP</span>
        <h2>当天地点与路线</h2>
      </div>
      <span class="map-count">已定位 {{ locatedCount }} / {{ locations.length }} 个地点</span>
    </div>
    <div class="map-frame">
      <div ref="mapElement" class="map-canvas" aria-label="高德地图"></div>
      <div v-if="mapState !== 'ready'" class="map-overlay" role="status">
        <b>{{ mapState === 'loading' ? '正在加载地图' : '地图暂不可用' }}</b>
        <span>{{ mapState === 'loading' ? '地点和路线信息会继续显示在页面中。' : '地点和路线数据仍可在页面中查看。' }}</span>
      </div>
    </div>
    <div class="map-legend" aria-label="地图图例">
      <span><i class="activity-dot"></i>行程地点</span>
      <span><i class="hotel-dot"></i>住宿地点</span>
      <span v-if="locations.some((location) => location.kind === 'area')"><i class="area-dot"></i>住宿区域参考点</span>
      <span><i class="transit-line"></i>公共交通</span>
      <span><i class="cycling-line"></i>骑行</span>
      <span><i class="walking-line"></i>步行（含换乘）</span>
    </div>
    <p class="map-note" role="status">{{ mapNote }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import AMapLoader from '@amap/amap-jsapi-loader'
import type { PreviewLocationView, PreviewLegView } from '@/utils/v4PreviewMapper'

const props = defineProps<{
  locations: PreviewLocationView[]
  routes: PreviewLegView[]
}>()

const mapElement = ref<HTMLElement | null>(null)
const mapState = ref<'loading' | 'ready' | 'failed'>('loading')
const mapNote = ref('地图底图加载后，将显示有真实坐标的地点和路线。')
const locatedCount = computed(() => props.locations.filter((location) => location.coordinate).length)
// 路径和图例共用配色；公交、地铁、铁路统一归入公共交通。
const routeColors = { transit: '#2563eb', bicycling: '#0d9488', walking: '#ea580c' }
let map: any = null
let AMap: any = null
let disposed = false

function drawMap(): void {
  if (!map || !AMap || disposed) return
  const overlays: any[] = []
  const located = props.locations.filter((location) => location.coordinate)
  for (const location of located) {
    const markerContent = document.createElement('div')
    markerContent.className = `preview-marker ${location.kind}-marker`
    markerContent.textContent = location.kind === 'hotel'
      ? '住'
      : location.kind === 'area' ? '区' : String(location.number ?? '')
    markerContent.setAttribute('aria-label', `${location.kind === 'hotel' ? '住宿地点' : location.kind === 'area' ? '住宿区域参考点' : '行程地点'}：${location.name}`)
    const marker = new AMap.Marker({
      position: location.coordinate,
      content: markerContent,
      anchor: 'center',
      zIndex: location.kind === 'hotel' ? 220 : location.kind === 'area' ? 100 : 180,
      title: location.name,
    })
    overlays.push(marker)
  }

  for (const route of props.routes) {
    for (const path of route.paths) {
      if (path.coordinates.length < 2) continue
      overlays.push(new AMap.Polyline({
        path: path.coordinates,
        strokeColor: pathColor(path.mode),
        strokeWeight: path.mode === 'walking' ? 5 : 7,
        strokeStyle: path.mode === 'walking' ? 'dashed' : 'solid',
        strokeDasharray: path.mode === 'walking' ? [6, 5] : undefined,
        strokeOpacity: 0.95,
        isOutline: true,
        outlineColor: '#fffdf8',
        borderWeight: 2,
        lineJoin: 'round',
        lineCap: 'round',
        zIndex: path.mode === 'walking' ? 120 : 110,
        showDir: path.mode !== 'walking',
      }))
    }
  }

  map.clearMap()
  if (overlays.length) {
    map.add(overlays)
    map.setFitView(overlays, false, [35, 28, 35, 28], 16)
  }
  const missingGeometry = props.routes.some((route) => route.geometryStatus === 'missing' || route.geometryStatus === 'partial')
  const noSelectedGeometry = props.routes.some((route) => route.geometryStatus === 'not_selected')
  if (located.length === 0) {
    mapNote.value = '当天地点没有可用坐标，地图不显示虚构位置或连接线。'
  } else if (missingGeometry) {
    mapNote.value = `已定位 ${located.length} 个地点。部分路线坐标未返回；地图只画出查询返回的真实路径。`
  } else if (noSelectedGeometry) {
    mapNote.value = `已定位 ${located.length} 个地点。尚未采用的路段不会显示连接线。`
  } else if (props.routes.length === 0) {
    mapNote.value = `已定位 ${located.length} 个地点；当前没有路段查询结果。`
  } else if (props.routes.every((route) => route.geometryStatus === 'not_required')) {
    mapNote.value = '活动位于同一地点，无需交通，因此不绘制路线。'
  } else {
    mapNote.value = `已定位 ${located.length} 个地点。路线只使用当前采用方案返回的路径坐标。`
  }
}

function pathColor(mode: string): string {
  if (mode === 'walking') return routeColors.walking
  if (mode === 'bicycling') return routeColors.bicycling
  return routeColors.transit
}

onMounted(async () => {
  const key = import.meta.env.VITE_AMAP_WEB_JS_KEY?.trim()
  const security = import.meta.env.VITE_AMAP_WEB_JS_SECURITY_CODE?.trim()
  if (!key || !security || key.startsWith('your_')) {
    mapState.value = 'failed'
    mapNote.value = '地图服务当前不可用。行程中仍会说明哪些地点或路线缺少查询数据。'
    return
  }
  window._AMapSecurityConfig = { securityJsCode: security }
  try {
    AMap = await AMapLoader.load({
      key,
      version: '2.0',
      plugins: ['AMap.Scale', 'AMap.ToolBar'],
    })
    if (disposed || !mapElement.value) return
    const firstPoint = props.locations.find((location) => location.coordinate)?.coordinate
    map = new AMap.Map(mapElement.value, {
      center: firstPoint ?? [0, 0],
      zoom: firstPoint ? 13 : 2,
      viewMode: '2D',
      resizeEnable: true,
      scrollWheel: false,
    })
    map.addControl(new AMap.Scale())
    map.addControl(new AMap.ToolBar({ position: 'RB' }))
    mapState.value = 'ready'
    drawMap()
  } catch {
    if (!disposed) {
      mapState.value = 'failed'
      mapNote.value = '地图服务当前不可用。行程中仍会说明哪些地点或路线缺少查询数据。'
    }
  }
})

watch(() => [props.locations, props.routes], drawMap, { deep: true })
onUnmounted(() => {
  disposed = true
  map?.destroy()
  map = null
})
</script>

<style scoped>
.map-card { background: #f4f6ee; border: 1px solid #d8e0d2; padding: 22px; box-shadow: 7px 7px 0 #e8eee1; }
.map-heading { display: flex; align-items: end; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.eyebrow { color: #a65c40; font-size: 10px; letter-spacing: .15em; font-weight: 700; }
h2 { margin: 7px 0 0; color: #254130; font: 24px 'Songti SC', 'Noto Serif SC', serif; }
.map-count { color: #718173; font-size: 10px; white-space: nowrap; }
.map-frame { position: relative; min-height: 330px; background: #e8eee2; border: 1px solid #dce5d3; }
.map-canvas { width: 100%; height: 390px; }
.map-overlay { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; flex-direction: column; gap: 10px; padding: 24px; background: #eff3e8e8; text-align: center; color: #355b44; font-size: 12px; }
.map-overlay span { color: #718173; font-size: 11px; line-height: 1.7; }
.map-legend { display: flex; flex-wrap: wrap; gap: 9px 17px; padding: 13px 0 4px; color: #657667; font-size: 10px; }
.map-legend span { display: inline-flex; align-items: center; gap: 6px; }
.map-legend i { display: inline-block; width: 24px; height: 10px; }
.activity-dot, .hotel-dot, .area-dot { width: 11px !important; height: 11px !important; border: 2px solid white; border-radius: 50%; background: #355c46; box-shadow: 0 1px 4px #2224; }
.hotel-dot { border-radius: 3px; background: #586c8a; }
.area-dot { background: transparent; border: 2px dashed #8a8060; }
.transit-line, .cycling-line, .walking-line { height: 0 !important; border-top: 4px solid var(--transit-color); }
.cycling-line { border-color: var(--cycling-color); }
.walking-line { border-color: var(--walking-color); border-top: 3px dashed var(--walking-color); }
.map-note { margin: 7px 0 0; color: #718173; font-size: 11px; line-height: 1.7; }
:deep(.preview-marker) { display: grid; place-items: center; width: 28px; height: 28px; border: 3px solid #fffdf8; border-radius: 50%; background: #355c46; color: white; box-shadow: 0 2px 7px #25453266; font: 12px sans-serif; }
:deep(.hotel-marker) { width: 31px; height: 31px; border-radius: 5px; background: #586c8a; }
:deep(.area-marker) { width: 27px; height: 27px; border: 2px dashed #8a8060; background: #fffdf8; color: #756c4f; }
@media (max-width: 600px) {
  .map-card { padding: 16px; }
  .map-frame { min-height: 280px; }
  .map-canvas { height: 300px; }
  .map-count { white-space: normal; text-align: right; }
}
</style>
