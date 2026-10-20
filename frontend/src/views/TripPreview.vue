<template>
  <main class="preview-shell">
    <nav v-if="observationTarget && pageState !== 'loading'" class="planning-observation-entry" aria-label="规划过程"><router-link :to="observationTarget">查看当前任务的规划过程 ↗</router-link></nav>
    <section v-if="pageState === 'loading'" class="state-page loading-state">
      <span class="eyebrow">TRIP PREVIEW</span>
      <div class="generation-spinner" aria-hidden="true"><span></span></div>
      <h1>正在生成你的行程</h1>
      <PlanningProgress :stage="generationStage" :status="generationStatus" :task-id="activeAccess?.taskId ?? null" />
      <p v-if="accessPersistencePending" class="storage-warning">本机暂未保存恢复信息。当前页可继续查看；刷新或关闭后无法恢复。</p>
      <button v-if="accessPersistencePending" class="quiet-button" :disabled="accessStorageRetrying" @click="retryAccessPersistence">{{ accessStorageRetrying ? '正在保存恢复信息…' : '重试保存恢复信息' }}</button>
    </section>

    <section v-else-if="pageState === 'missing_access'" class="state-page">
      <span class="eyebrow">TRIP PREVIEW</span>
      <h1>这里还没有行程预览</h1>
      <p>{{ route.query.task ? '本浏览器没有此任务的预览访问凭证。请使用发起该任务的浏览器；旧凭证已清除时无法恢复。' : '从首页填写旅行计划后，预览会保存在这台设备上，方便你稍后继续查看。' }}</p>
      <router-link class="quiet-link" to="/">返回首页</router-link>
    </section>

    <section v-else-if="pageState === 'failed'" class="state-page">
      <span class="eyebrow">TRIP PREVIEW</span>
      <h1>{{ failureAction === 'replan' ? '这次没有生成可用行程' : '暂时无法读取这份行程' }}</h1>
      <p>{{ errorMessage }}</p>
      <div v-if="failureAction === 'replan'" class="state-actions">
        <router-link class="quiet-link" to="/">重新规划</router-link>
      </div>
      <div v-else class="state-actions">
        <button class="quiet-button" @click="loadLatest">重新读取</button>
        <router-link class="quiet-link" to="/">返回首页</router-link>
      </div>
    </section>

    <template v-else-if="view && activeDay">
      <header class="page-topline">
        <span class="eyebrow">YOUR TRIP</span>
        <div class="preview-actions">
          <span class="readonly-label">行程预览</span>
          <button class="preview-action" type="button" :disabled="exporting" @click="exportCurrent">
            {{ exporting ? (exportPhase === 'saving' ? '图片已导出，正在保存…' : '正在生成图片…') : '导出全部日期' }}
          </button>
          <button class="preview-action restore-action" type="button" :disabled="!canRestore" @click="restoreLastSaved">
            {{ restoring ? '正在还原…' : '还原上次保存' }}
          </button>
          <small v-if="finalState?.restorable && finalState.final">上次保存于 {{ formatSavedAt(finalState.final.saved_at) }}</small>
          <small v-else-if="finalState?.unavailable_reason === 'snapshot_incomplete'">上次保存内容不完整，暂不可还原</small>
          <small v-else-if="finalStateChecked && finalStateLoadFailed">暂时无法读取上次保存状态</small>
          <small v-else-if="finalStateChecked && !finalState?.final">还没有可还原的已保存版本</small>
          <TripPreviewExport v-if="exportSnapshot" ref="exportRenderer" :visible="exportSnapshot.visible" />
        </div>
      </header>
      <section v-if="exportFiles.length" class="export-download-fallback" aria-live="polite">
        <span>如果浏览器没有自动开始下载，可逐张保存：</span>
        <a v-for="file in exportFiles" :key="file.filename" :href="file.url" :download="file.filename">{{ file.label }}</a>
      </section>
      <section v-if="accessPersistencePending" class="pending-edit-banner" aria-live="polite">
        <div class="pending-edit-copy"><b>当前行程可继续查看</b><p>本机暂未保存恢复信息。刷新或关闭后无法恢复这份预览。</p></div>
        <button class="preview-action" type="button" :disabled="accessStorageRetrying" @click="retryAccessPersistence">{{ accessStorageRetrying ? '正在保存…' : '重试保存恢复信息' }}</button>
      </section>
      <section v-if="pendingEdit" class="pending-edit-banner" :class="{ conflict: pendingEdit.status === 'conflict' }" aria-live="polite">
        <div class="pending-edit-copy">
          <b>{{ pendingEdit.status === 'conflict' ? '这项操作与最新行程有冲突' : pendingEdit.status === 'sending' ? (pendingEdit.kind === 'destination_selection' ? '正在加入所选地点' : '正在同步这项编辑') : (pendingEdit.kind === 'destination_selection' ? '地点选择尚未同步' : '这项编辑尚未同步') }}</b>
          <p>{{ pendingEdit.summary }}</p>
          <ul v-if="pendingEdit.details.length"><li v-for="detail in pendingEdit.details" :key="detail">{{ detail }}</li></ul>
          <small v-if="pendingEdit.conflictMessage">{{ pendingEdit.conflictMessage }}</small>
          <small v-else-if="pendingEdit.status !== 'sending'">页面已保留这项具体操作。你可以重试，或读取服务端结果并停止重试。</small>
        </div>
        <div class="pending-edit-actions">
          <button class="preview-action" type="button" :disabled="editRequestInFlight || (pendingEdit.status === 'conflict' && !canRetryConflict)" @click="retryPendingEdit">
            {{ pendingEdit.status === 'conflict' && !canRetryConflict ? '请先刷新最新行程' : pendingEdit.status === 'conflict' ? '按最新行程重试' : '重试同步' }}
          </button>
          <button class="preview-action discard-edit-action" type="button" :disabled="editRequestInFlight || discardRefreshing" @click="discardPendingEdit">{{ discardRefreshing ? '正在确认服务端结果…' : pendingEdit.status === 'conflict' ? '放弃这项编辑' : '停止重试并刷新' }}</button>
        </div>
      </section>
      <section v-else-if="pendingEditCacheBroken" class="pending-edit-banner conflict" aria-live="polite">
        <div class="pending-edit-copy"><b>本机有一项编辑缓存无法读取</b><p>当前行程仍保持服务端内容。请清除损坏的缓存后继续编辑。</p></div>
        <button class="preview-action discard-edit-action" type="button" @click="clearBrokenPendingEdit">清除无法读取的缓存</button>
      </section>
      <section v-if="routeJobState === 'pending' || routeJobState === 'running'" class="route-job-banner" aria-live="polite">
        {{ routeJobState === 'running' ? '正在核实交通。你可以继续编辑或导出当前内容。' : '交通信息正在等待核实。你可以继续编辑或导出当前内容。' }}
      </section>
      <section v-else-if="routeJobState === 'failed'" class="route-job-banner failed" aria-live="polite">
        {{ routeJobMessage || '部分交通信息仍未查到。当前行程可继续编辑和导出。' }}
      </section>
      <section class="hero">
        <div>
          <p class="hero-kicker">A JOURNEY MADE FOR YOU</p>
          <h1>{{ view.city }} · 行程预览</h1>
          <p class="date-range">{{ formatDate(view.arrivalAt, view.timezone) }} — {{ formatDate(view.departureAt, view.timezone) }}</p>
        </div>
        <div class="cost-summary" aria-label="已知人均消费参考">
          <span>已查询到的人均消费小计</span>
          <strong v-if="view.cost.knownSubtotal !== null">¥{{ view.cost.knownSubtotal }}</strong>
          <strong v-else>未查询到可供参考的人均消费</strong>
          <small v-if="view.tripBudget !== null">全程旅费预算 ¥{{ view.tripBudget }}</small>
          <small v-if="view.cost.unknownCount || view.cost.conflictCount">
            {{ costGapText(view.cost.unknownCount, view.cost.conflictCount) }}
          </small>
          <small>仅汇总已查到的景点、餐饮及住宿消费；住宿按每晚计入，离开日不计住宿。不代表完整旅费。</small>
        </div>
      </section>

      <section v-if="view.planIssues.length" class="global-issues" aria-label="行程提示">
        <div v-for="issue in view.planIssues" :key="issue.issue_id" class="issue-row" :class="issueTone(issue)">
          <b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span>
        </div>
      </section>

      <nav class="day-tabs" aria-label="行程日期">
        <button
          v-for="(day, index) in view.days"
          :key="day.id"
          type="button"
          :class="{ active: activeIndex === index }"
          :aria-current="activeIndex === index ? 'date' : undefined"
          @click="activeIndex = index"
        >
          <span>第 {{ index + 1 }} 天</span>
          <small>{{ formatShortDate(day.date) }}</small>
          <i>{{ day.activities.filter((activity) => activity.kind === 'sightseeing').length }} 个景点 · {{ day.activities.filter((activity) => activity.kind === 'meal').length }} 餐</i>
        </button>
      </nav>

      <section class="day-overview">
        <div class="overview-copy">
          <span class="eyebrow">DAY {{ activeIndex + 1 }} / {{ activeDay.date }}</span>
          <h2>{{ activeDay.theme }}</h2>
          <p>{{ activityCountText(activeDay.activities.length) }}</p>
        </div>
        <div class="daily-facts">
          <span>已知交通耗时 {{ formatReferenceDuration(activeDay.transport.knownTravelSeconds) }}</span>
          <span v-if="activeDay.transport.unknownLegCount > 0">{{ activeDay.transport.unknownLegCount }} 段交通耗时待确认</span>
          <span v-if="activeDay.cost.knownSubtotal !== null">当日已知消费 ¥{{ activeDay.cost.knownSubtotal }} / 人</span>
          <span v-if="activeDay.cost.unknownCount || activeDay.cost.conflictCount">{{ costGapText(activeDay.cost.unknownCount, activeDay.cost.conflictCount) }}</span>
        </div>
      </section>

      <div class="day-layout">
        <section class="timeline-column" aria-label="当天有序行程">
          <section class="info-card weather-card">
            <div class="card-topline"><span class="eyebrow">WEATHER</span><span :class="activeDay.weather.known ? 'fact-known' : 'fact-unknown'">{{ activeDay.weather.known ? '旅行日预报' : '天气未知' }}</span></div>
            <h3>{{ activeDay.weather.summary }}</h3>
            <p>{{ activeDay.weather.advice }}</p>
          </section>

          <section class="info-card lodging-card">
            <div class="card-topline"><span class="eyebrow">STAY</span><span class="fact-known">住宿参考</span></div>
            <div class="lodging-grid" :class="{ 'hotel-only': !activeDay.area }">
              <div>
                <small>{{ activeDay.hotel.label }}</small>
                <h3>{{ activeDay.hotel.name }}</h3>
                <span class="hotel-cost">{{ activeDay.hotel.cost.text }}</span>
                <small v-if="activeDay.hotel.cost.evidenceLabel">{{ activeDay.hotel.cost.evidenceLabel }}</small>
                <p v-if="activeDay.hotel.address">{{ activeDay.hotel.address }}</p>
                <p v-else class="muted">具体酒店未核实时，会保留未知状态。</p>
              </div>
              <div v-if="activeDay.area" class="area-reference">
                <small>推荐寻找区域</small>
                <h3>{{ activeDay.area.label }}</h3>
                <p>住宿区域与具体酒店分别展示。区域参考点不代表已选酒店。</p>
              </div>
            </div>
          </section>

          <section v-if="activeDay.returnLeg" class="leg-card" aria-label="返程交通参考">
            <div class="card-topline"><span class="eyebrow">TRANSPORT</span><span>返程交通参考</span></div>
            <div>
              <div class="leg-heading"><span>{{ activeDay.returnLeg.fromLabel }} → {{ activeDay.returnLeg.toLabel }}</span><b>{{ activeDay.returnLeg.selectedModeLabel }} · {{ activeDay.returnLeg.selectedDurationLabel }}</b></div>
              <div v-if="activeDay.returnLeg.modes.length" class="mode-list" aria-label="返程交通方式耗时参考">
                <button v-for="mode in activeDay.returnLeg.modes" :key="mode.mode" type="button" class="mode-chip" :class="{ chosen: activeDay.returnLeg.selectedMode === mode.mode }" :disabled="!canChooseMode(activeDay.returnLeg, mode)" @click="chooseLegMode(activeDay.id, activeDay.returnLeg, mode)"><span>{{ mode.label }}</span><b>{{ mode.durationLabel }}</b><small>{{ activeDay.returnLeg.selectedMode === mode.mode ? '当前采用' : mode.status === '路线已查询' ? '选择该方式' : mode.status }}</small></button>
              </div>
              <p class="route-note">{{ activeDay.returnLeg.geometryMessage }}</p>
              <p v-if="activeDay.returnLeg.timeBasisLabel" class="route-note">{{ activeDay.returnLeg.timeBasisLabel }}</p>
              <div v-for="issue in activeDay.returnLeg.issues" :key="issue.issue_id" class="issue-row scoped-issue" :class="issueTone(issue)"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
            </div>
          </section>

          <div v-if="dayLevelIssues.length" class="day-issues" aria-label="当天提示">
            <div v-for="issue in dayLevelIssues" :key="issue.issue_id" class="issue-row" :class="issueTone(issue)">
              <b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span>
            </div>
          </div>
          <div v-if="activeDay.unboundIssues.length" class="day-issues" aria-label="其他当天提示">
            <div v-for="issue in activeDay.unboundIssues" :key="issue.issue_id" class="issue-row" :class="issueTone(issue)">
              <b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span>
            </div>
          </div>

          <section v-if="!activeDay.timeline.length" class="empty-day">
            <h3>这一天还没有具体活动</h3>
            <p>当前预览保留了这一天。安排缺项会在对应时段显示。</p>
          </section>

          <template v-for="item in activeDay.timeline" :key="item.type === 'activity' ? item.activity.id : item.type === 'missing_slot' ? item.slot.id : item.issue.issue_id">
            <template v-if="item.type === 'missing_slot'">
              <section class="missing-slot" :aria-label="`${item.slot.periodLabel}安排缺项`">
                <span class="missing-icon" :class="{ conditional: item.slot.kind === 'conditional' }">{{ item.slot.kind === 'conditional' ? '?' : '!' }}</span>
                <div><b>{{ item.slot.periodLabel }} · {{ item.slot.kind === 'conditional' ? '待确认' : '尚未安排' }}</b><p>{{ item.slot.prompt }}</p>
                  <p v-for="issue in item.slot.issues" :key="issue.issue_id" class="issue-detail">{{ issue.message }}</p>
                </div>
              </section>
            </template>
            <section v-else-if="item.type === 'slot_issue'" class="issue-row" :class="issueTone(item.issue)">
              <b>{{ item.periodLabel }} · {{ issueTitle(item.issue) }}</b><span>{{ item.issue.message }}</span>
            </section>
            <template v-else>
              <section v-if="item.incomingLeg" class="leg-card">
                <div class="leg-heading">
                  <span>{{ item.incomingLeg.fromLabel }} → {{ item.incomingLeg.toLabel }}</span>
                  <b>{{ item.incomingLeg.selectedModeLabel }} · {{ item.incomingLeg.selectedDurationLabel }}</b>
                </div>
                <div v-if="item.incomingLeg.modes.length" class="mode-list" aria-label="交通方式耗时参考">
                  <button v-for="mode in item.incomingLeg.modes" :key="mode.mode" type="button" class="mode-chip" :class="{ chosen: item.incomingLeg.selectedMode === mode.mode }" :disabled="!canChooseMode(item.incomingLeg, mode)" @click="chooseLegMode(activeDay.id, item.incomingLeg, mode)">
                    <span>{{ mode.label }}</span><b>{{ mode.durationLabel }}</b><small>{{ item.incomingLeg.selectedMode === mode.mode ? '当前采用' : mode.status === '路线已查询' ? '选择该方式' : mode.status }}</small>
                  </button>
                </div>
                <p class="route-note">{{ item.incomingLeg.geometryMessage }}</p>
                <p v-if="item.incomingLeg.timeBasisLabel" class="route-note">{{ item.incomingLeg.timeBasisLabel }}</p>
                <div v-for="issue in item.incomingLeg.issues" :key="issue.issue_id" class="issue-row scoped-issue" :class="issueTone(issue)"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
              </section>
              <article :data-activity-id="item.activity.id" class="activity-card" :class="{ meal: item.activity.kind === 'meal', free: item.activity.kind === 'free_time', dragging: draggingId === item.activity.id, 'drop-target': dropId === item.activity.id && draggingId !== item.activity.id }">
                <div class="activity-meta"><span>{{ item.activity.periodLabel }} · {{ activityKind(item.activity.kind) }}</span><div class="activity-actions"><button class="drag-handle" type="button" :disabled="editingBlocked" :aria-label="`拖动排序：${item.activity.title}`" title="拖动排序；也可聚焦后用键盘方向键" @pointerdown="startDrag($event, item.activity.id)" @pointermove="trackDrag" @pointerup="finishDrag" @pointercancel="cancelDrag" @keydown.up.prevent="keyboardMove(item.activity.id, -1)" @keydown.down.prevent="keyboardMove(item.activity.id, 1)">⠿</button><button class="activity-remove" type="button" :disabled="editingBlocked" :aria-label="`删除活动：${item.activity.title}`" @click="removeActivity(item.activity)">删除</button></div></div>
                <div class="activity-title"><span class="activity-number">{{ item.position }}</span><div><h3>{{ item.activity.title }}</h3></div></div>
                <p v-if="item.activity.description" class="activity-description">{{ item.activity.description }}</p>
                <p v-if="item.activity.address" class="activity-address">⌖ {{ item.activity.address }}</p>
                <div v-for="issue in item.activity.issues" :key="issue.issue_id" class="issue-row scoped-issue" :class="issueTone(issue)"><b>{{ issueTitle(issue) }}</b><span>{{ issue.message }}</span></div>
                <footer>
                  <span v-if="item.activity.kind !== 'free_time'">{{ item.activity.cost.text }}</span>
                  <span v-if="item.activity.kind === 'free_time'">不指定地点</span>
                </footer>
                <small v-if="item.activity.kind !== 'free_time' && item.activity.cost.evidenceLabel" class="cost-evidence">{{ item.activity.cost.evidenceLabel }}</small>
              </article>
            </template>
          </template>
        </section>

        <aside class="map-column">
          <PreviewMap :locations="activeDay.locations" :routes="activeDay.legs" />
          <section class="destination-card">
            <div class="section-heading"><div><span class="eyebrow">PLACES</span><h3>当天目的地</h3></div><small>{{ destinationGroups.length }} 个地点</small></div>
            <p class="muted management-note">相同地点合并显示；删除地点会移除当天它的全部安排。</p>
            <ul v-if="destinationGroups.length" class="destination-list">
              <li v-for="(destination, index) in destinationGroups" :key="destination.key">
                <input v-model="selectedPoiIds" class="destination-check" type="checkbox" :value="destination.key" :disabled="editingBlocked" :aria-label="`选择删除：${destination.name}`">
                <span class="destination-number">{{ index + 1 }}</span>
                <span class="destination-copy"><b>{{ destination.name }}</b><small>{{ destination.kind === 'meal' ? '餐馆' : '景点' }} · {{ destination.area || destination.address || '区域信息未提供' }}</small><small>安排在：{{ destination.periodLabels.join('、') }}</small></span>
                <div class="destination-actions">
                  <small v-if="!destination.coordinate" class="coord-missing">位置未核实</small>
                  <template v-if="destination.candidateRef && availableSlotsFor(destination.kind).length">
                    <select :value="slotChoice(`poi:${destination.key}`, destination.kind)" :disabled="editingBlocked" :aria-label="`再次安排${destination.name}的时段`" @change="setSlotChoice(`poi:${destination.key}`, $event)">
                      <option v-for="slot in availableSlotsFor(destination.kind)" :key="slot.id" :value="slot.id">再放入{{ slot.label }}</option>
                    </select>
                    <button type="button" class="small-action" :disabled="editingBlocked" @click="addCandidate(destination.candidateRef, destination.kind, slotChoice(`poi:${destination.key}`, destination.kind), destination.name, destination.poiId || undefined)">再次安排</button>
                  </template>
                </div>
              </li>
            </ul>
            <p v-else class="muted">当天还没有已安排的具体目的地。</p>
            <div v-if="selectedPoiIds.length" class="bulk-remove-row">
              <span>已选择 {{ selectedActivityCount }} 项安排</span>
              <button type="button" class="activity-remove" :disabled="editingBlocked || selectedActivityCount > 20" @click="removeSelectedDestinations">删除所选目的地</button>
            </div>
            <small v-if="selectedActivityCount > 20" class="limit-note">一次最多处理20项安排，请减少勾选范围。</small>
          </section>

          <section class="destination-card recommendations">
            <div class="section-heading"><div><span class="eyebrow">MORE PLACES</span><h3>可选候选</h3></div><small>景点 {{ activeDay.recommendations.filter((item) => item.kind === 'sightseeing').length }} · 餐馆 {{ activeDay.recommendations.filter((item) => item.kind === 'meal').length }}</small></div>
            <p class="muted">已排除当天已安排地点；最多展示两个景点和一家餐馆。添加时可选择对应时段。</p>
            <ul v-if="activeDay.recommendations.length" class="candidate-list">
              <li v-for="candidate in activeDay.recommendations" :key="candidate.id">
                <div><span class="candidate-type">{{ candidate.kind === 'meal' ? '餐馆' : '景点' }}</span><b>{{ candidate.name }}</b></div>
                <small>{{ candidate.area || candidate.address || '区域信息未提供' }}</small>
                <small>{{ candidate.cost.text }}</small>
                <small v-if="candidate.cost.evidenceLabel" class="cost-evidence">{{ candidate.cost.evidenceLabel }}</small>
                <p v-if="candidate.reason">{{ candidate.reason }}</p>
                <div v-if="availableSlotsFor(candidate.kind).length" class="candidate-add-row">
                  <select :value="slotChoice(candidate.id, candidate.kind)" :disabled="editingBlocked" :aria-label="`选择${candidate.name}的安排时段`" @change="setSlotChoice(candidate.id, $event)">
                    <option v-for="slot in availableSlotsFor(candidate.kind)" :key="slot.id" :value="slot.id">{{ slot.label }}</option>
                  </select>
                  <button type="button" class="small-action" :disabled="editingBlocked" @click="addCandidate(candidate.id, candidate.kind, slotChoice(candidate.id, candidate.kind), candidate.name)">加入当天</button>
                </div>
                <small v-else class="muted">当前没有可添加的对应时段。</small>
              </li>
            </ul>
            <p v-else class="muted">当前没有可展示的候选地点。</p>
          </section>

          <section class="destination-card destination-search">
            <div class="section-heading"><div><span class="eyebrow">SEARCH PLACES</span><h3>搜索其他地点</h3></div></div>
            <p class="muted">搜索当前城市的景点或餐馆。搜索不会修改行程；选择后再加入当天。</p>
            <form class="destination-search-form" @submit.prevent="searchDestinations(1)">
              <select v-model="destinationSearchCategory" aria-label="搜索类别">
                <option value="sightseeing">景点</option>
                <option value="meal">餐馆</option>
              </select>
              <input v-model="destinationSearchKeywords" type="search" maxlength="120" placeholder="输入地点名称或关键词" aria-label="地点搜索关键词">
              <button class="small-action" type="submit" :disabled="destinationSearchBusy || !destinationSearchKeywords.trim()">
                {{ destinationSearchBusy ? '正在搜索…' : '搜索' }}
              </button>
            </form>
            <p v-if="destinationSearchError" class="search-error" role="alert">{{ destinationSearchError }}</p>
            <p v-else-if="destinationSearchBusy" class="muted search-state" aria-live="polite">正在查找当前城市的地点。</p>
            <template v-else-if="destinationSearchResponse">
              <p v-if="destinationSearchResponse.status === 'error'" class="search-error" role="alert">
                地点搜索暂时失败，请稍后重试。
              </p>
              <p v-else-if="destinationSearchResponse.candidates.length === 0" class="muted search-state">没有找到可展示的地点，可以更换关键词再试。</p>
              <ul v-if="destinationSearchResponse.status !== 'error' && destinationSearchResponse.candidates.length" class="destination-search-results">
                <li v-for="(candidateView, index) in destinationSearchCandidateViews" :key="candidateView.key">
                  <div class="search-result-heading">
                    <span class="candidate-type">{{ destinationSearchResponse.category === 'meal' ? '餐馆' : '景点' }}</span>
                    <b>{{ candidateView.name }}</b>
                  </div>
                  <small>{{ candidateView.city ? `${candidateView.city} · ` : '' }}{{ candidateView.address }}<template v-if="candidateView.typeName"> · {{ candidateView.typeName }}</template></small>
                  <small>{{ candidateView.costLabel }}</small>
                  <small v-if="candidateView.missingFieldsLabel" class="muted">{{ candidateView.missingFieldsLabel }}</small>
                  <small class="search-source">来源：{{ candidateView.sourceLabel }} · 查询于 {{ candidateView.queriedAtLabel }}</small>
                  <div v-if="candidateView.canSelect && destinationSearchResponse.category" class="candidate-add-row">
                    <select :value="searchSlotChoice(index, destinationSearchResponse.category)" :disabled="editingBlocked" :aria-label="`选择${candidateView.name}的安排时段`" @change="setSearchSlotChoice(index, destinationSearchResponse.category, $event)">
                      <option v-for="slot in availableSlotsFor(destinationSearchResponse.category)" :key="slot.id" :value="slot.id">{{ slot.label }}</option>
                    </select>
                    <button class="small-action" type="button" :disabled="editingBlocked || !searchSlotChoice(index, destinationSearchResponse.category)" @click="selectDestinationSearchResult(index)">加入当天</button>
                  </div>
                  <small v-else class="search-blocked">{{ candidateView.blockedReasonLabel || '当前不能加入，请重新搜索后再试。' }}</small>
                </li>
              </ul>
              <p v-if="destinationSearchResponse.omitted_count > 0" class="muted omitted-results">另有 {{ destinationSearchResponse.omitted_count }} 条候选未展示。</p>
              <div v-if="showDestinationSearchPagination" class="search-pagination">
                <button class="small-action" type="button" :disabled="destinationSearchBusy || destinationSearchResponse.page <= 1" @click="searchDestinations(destinationSearchResponse.page - 1)">返回上一页</button>
                <small>第 {{ destinationSearchResponse.page }} 页<template v-if="destinationSearchResponse.provider_count !== null"> · 地图来源报告 {{ destinationSearchResponse.provider_count }} 条</template></small>
                <button class="small-action" type="button" :disabled="destinationSearchBusy || !canLoadNextDestinationPage" @click="searchDestinations(destinationSearchResponse.page + 1)">查询下一页</button>
              </div>
            </template>
          </section>
        </aside>
      </div>
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { message } from 'ant-design-vue'
import PreviewMap from '@/components/preview/PreviewMap.vue'
import PlanningProgress from '@/components/preview/PlanningProgress.vue'
import TripPreviewExport from '@/views/TripPreviewExport.vue'
import {
  getLatestPreviewAccess,
  getPreviewAccessForTask,
  getPendingPreviewAccess,
  getV4FinalState,
  getV4EditJob,
  getV4Preview,
  getV4TaskStatus,
  isV4EditConflict,
  restoreV4Final,
  retrySavePreviewAccess,
  saveV4Final,
  searchV4Destinations,
  selectV4Destination,
  submitV4Edit,
  v4FinalErrorMessage,
  v4EditErrorMessage,
  v4DestinationSearchErrorMessage,
  v4DestinationSelectErrorMessage,
  v4PreviewErrorMessage,
} from '@/services/v4Preview'
import type { V4DestinationCategory, V4DestinationSearchResponse, V4EditOperation, V4FinalSaveResponse, V4FinalState, V4PreviewAccess, V4PreviewEnvelope } from '@/types/v4Preview'
import { formatReferenceDuration, mapV4Preview, type PreviewActivityView, type PreviewLegView, type PreviewModeView, type TripPreviewView } from '@/utils/v4PreviewMapper'
import { groupV4DestinationActivities } from '@/utils/v4PreviewEditMapper'
import { mapV4DestinationSearchCandidate, type V4DestinationSearchCandidateView } from '@/utils/v4DestinationSearchMapper'
import { canQueryNextDestinationPage, shouldShowDestinationSearchPagination } from '@/utils/v4DestinationSearchPagination'
import { canRetryV4PreviewRead } from '@/utils/v4PreviewProgress'
import { planningObservationTarget, waitForV4PlanningPreview, V4PlanningReadError as PreviewPageError } from '@/utils/v4PlanningSession'
import {
  canApplyV4Restore,
  captureV4PreviewExport,
  exportV4Snapshot,
  type V4ExportFile,
  type V4PreviewExportSnapshot,
  type V4ExportVisibleState,
} from '@/utils/v4PreviewExportController'
import {
  clearV4PendingEdit,
  createV4PendingEdit,
  createV4PendingDestinationSelection,
  hasStoredV4PendingEdit,
  isV4EnvelopeAtLeastAsNew,
  markV4PendingEdit,
  nextV4RouteJobAfterSuperseded,
  pendingV4RouteJobId,
  readV4PendingEdit,
  rebaseV4PendingEdit,
  persistV4PendingEdit,
  refreshBeforeDiscardV4PendingEdit,
  type V4PendingEdit,
} from '@/utils/v4PreviewEditController'

const pageState = ref<'loading' | 'missing_access' | 'failed' | 'ready'>('loading')
const errorMessage = ref('')
const failureAction = ref<'read' | 'replan'>('read')
const generationStage = ref('accepted')
const generationStatus = ref('accepted')
const view = ref<TripPreviewView | null>(null)
const envelope = ref<V4PreviewEnvelope | null>(null)
const activeAccess = ref<V4PreviewAccess | null>(null)
const finalState = ref<V4FinalState | null>(null)
const finalStateChecked = ref(false)
const finalStateLoadFailed = ref(false)
const activeIndex = ref(0)
const exporting = ref(false)
const exportPhase = ref<'rendering' | 'saving'>('rendering')
const exportFiles = ref<Array<{ filename: string; label: string; url: string }>>([])
const restoring = ref(false)
const exportSnapshot = ref<V4PreviewExportSnapshot | null>(null)
const exportRenderer = ref<{ renderAll: () => Promise<readonly V4ExportFile<Blob>[]> } | null>(null)
const pendingEdit = ref<V4PendingEdit | null>(null)
const pendingEditCacheBroken = ref(false)
const editRequestInFlight = ref(false)
const discardRefreshing = ref(false)
const routeJobState = ref<'idle' | 'pending' | 'running' | 'failed'>('idle')
const routeJobMessage = ref('')
const accessPersistencePending = ref(false)
const accessStorageRetrying = ref(false)
const selectedPoiIds = ref<string[]>([])
const slotChoices = ref<Record<string, string>>({})
const destinationSearchCategory = ref<V4DestinationCategory>('sightseeing')
const destinationSearchKeywords = ref('')
const destinationSearchResponse = ref<V4DestinationSearchResponse | null>(null)
const destinationSearchBusy = ref(false)
const destinationSearchError = ref('')
const destinationSearchCandidateViews = computed<V4DestinationSearchCandidateView[]>(() => (
  destinationSearchResponse.value?.candidates.map(mapV4DestinationSearchCandidate) ?? []
))
const canLoadNextDestinationPage = computed(() => {
  const response = destinationSearchResponse.value
  return Boolean(response && canQueryNextDestinationPage(response))
})
const showDestinationSearchPagination = computed(() => Boolean(
  destinationSearchResponse.value && shouldShowDestinationSearchPagination(destinationSearchResponse.value),
))
const draggingId = ref('')
const dropId = ref('')
const localDirty = computed(() => Boolean(pendingEdit.value || pendingEditCacheBroken.value))
const localSaveInFlight = computed(() => editRequestInFlight.value)
let disposed = false
let pendingTimer: number | null = null
let requestGeneration = 0
let finalStateGeneration = 0
let routeJobGeneration = 0
let editRequestSequence = 0
let destinationSearchGeneration = 0
let destinationSearchAbort: AbortController | null = null
let dragOrigin: { id: string; pointerId: number } | null = null


const observationTarget = computed(() => planningObservationTarget(activeAccess.value?.taskId))
const activeDay = computed(() => view.value?.days[activeIndex.value] ?? null)
const activeConstraints = computed(() => {
  const dayId = activeDay.value?.id
  return envelope.value?.preview.planning_input.days.find((day) => day.day_id === dayId)?.slots ?? []
})
const destinationGroups = computed(() => groupV4DestinationActivities(activeDay.value?.activities ?? []))
const selectedActivityCount = computed(() => destinationGroups.value
  .filter((destination) => selectedPoiIds.value.includes(destination.key))
  .reduce((count, destination) => count + destination.activityIds.length, 0))
const editingBlocked = computed(() => pageState.value !== 'ready'
  || editRequestInFlight.value
  || discardRefreshing.value
  || Boolean(pendingEdit.value)
  || pendingEditCacheBroken.value
  || restoring.value)
const canRetryConflict = computed(() => Boolean(
  pendingEdit.value?.status !== 'conflict' || (envelope.value && pendingEdit.value && (
    envelope.value.store_version > pendingExpectedStoreVersion(pendingEdit.value)
    || envelope.value.plan_revision > pendingExpectedPlanRevision(pendingEdit.value)
  )),
))
const dayLevelIssues = computed(() => activeDay.value?.issues.filter((issue) => issue.scope === 'day') ?? [])
const canRestore = computed(() => Boolean(
  pageState.value === 'ready'
  && envelope.value
  && finalState.value?.restorable
  && finalState.value.final
  && !localDirty.value
  && !localSaveInFlight.value
  && !exporting.value
  && !restoring.value,
))

watch(activeIndex, () => {
  selectedPoiIds.value = []
  slotChoices.value = {}
  cancelDrag()
})

watch([destinationSearchKeywords, destinationSearchCategory, activeIndex], () => {
  invalidateDestinationSearch()
})

function invalidateDestinationSearch(): void {
  destinationSearchGeneration++
  destinationSearchAbort?.abort()
  destinationSearchAbort = null
  destinationSearchBusy.value = false
  destinationSearchError.value = ''
  destinationSearchResponse.value = null
}

async function searchDestinations(page: number): Promise<void> {
  const access = activeAccess.value
  const dayId = activeDay.value?.id
  const keywords = destinationSearchKeywords.value.trim()
  if (!access || !dayId || !keywords || !Number.isInteger(page) || page < 1 || destinationSearchBusy.value) return
  destinationSearchAbort?.abort()
  const controller = new AbortController()
  destinationSearchAbort = controller
  const generation = ++destinationSearchGeneration
  const pageGeneration = requestGeneration
  const category = destinationSearchCategory.value
  destinationSearchBusy.value = true
  destinationSearchError.value = ''
  destinationSearchResponse.value = null
  try {
    const response = await searchV4Destinations(access, { keywords, category, page }, controller.signal)
    if (disposed || generation !== destinationSearchGeneration || pageGeneration !== requestGeneration
      || activeDay.value?.id !== dayId || activeAccess.value?.taskId !== access.taskId) return
    const normalizedResponseKeywords = response.keywords.trim().replace(/\s+/g, ' ')
    const normalizedRequestKeywords = keywords.replace(/\s+/g, ' ')
    if (response.category !== category || response.page !== page
      || normalizedResponseKeywords !== normalizedRequestKeywords) {
      throw new Error('搜索结果与当前关键词不匹配。')
    }
    destinationSearchResponse.value = response
  } catch (error) {
    if (disposed || generation !== destinationSearchGeneration || controller.signal.aborted) return
    destinationSearchError.value = v4DestinationSearchErrorMessage(error)
  } finally {
    if (generation === destinationSearchGeneration) {
      destinationSearchBusy.value = false
      if (destinationSearchAbort === controller) destinationSearchAbort = null
    }
  }
}

function searchSlotChoice(index: number, category: V4DestinationCategory): string {
  const searchId = destinationSearchResponse.value?.search_id ?? 'current'
  const key = `search:${searchId}:${index}`
  return slotChoice(key, category)
}

function setSearchSlotChoice(index: number, category: V4DestinationCategory, event: Event): void {
  setSlotChoice(`search:${destinationSearchResponse.value?.search_id ?? 'current'}:${index}`, event)
  // A category change invalidates the result set; this guard also prevents stale DOM events
  // from keeping a meal slot selected for a sightseeing candidate (or the reverse).
  const selected = (event.target as HTMLSelectElement).value
  if (!availableSlotsFor(category).some((slot) => slot.id === selected)) {
    const key = `search:${destinationSearchResponse.value?.search_id ?? 'current'}:${index}`
    const next = { ...slotChoices.value }
    delete next[`${activeDay.value?.id ?? ''}:${key}`]
    slotChoices.value = next
  }
}

async function selectDestinationSearchResult(index: number): Promise<void> {
  const response = destinationSearchResponse.value
  const candidate = response?.candidates[index]
  const access = activeAccess.value
  const current = envelope.value
  const dayId = activeDay.value?.id
  const category = response?.category
  if (!candidate || !access || !current || !dayId || !category || editingBlocked.value) return
  const candidateView = destinationSearchCandidateViews.value[index]
  const slotId = searchSlotChoice(index, category)
  const slotLabel = availableSlotsFor(category).find((slot) => slot.id === slotId)?.label
  if (!candidateView?.canSelect || !candidate.candidate_token || !slotId || !slotLabel) {
    message.info('请选择一个可安排的时段后再加入。')
    return
  }
  let pending: V4PendingEdit
  try {
    pending = createV4PendingDestinationSelection({
      envelope: current,
      candidateToken: candidate.candidate_token,
      category,
      dayId,
      slotId,
      commandId: newRequestId('destination'),
      clientRef: newRequestId('client'),
      name: candidateView.name,
      slotLabel,
    })
    // Persist the complete opaque-token request before sending. A lost response can then
    // replay the identical command even if the search token later expires.
    persistV4PendingEdit(pending)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '无法保留这项地点选择，尚未发送。')
    return
  }
  routeJobGeneration++
  routeJobState.value = 'idle'
  routeJobMessage.value = ''
  pendingEdit.value = pending
  await sendPendingEdit(pending, access, requestGeneration)
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => {
    pendingTimer = window.setTimeout(() => {
      pendingTimer = null
      resolve()
    }, milliseconds)
  })
}

const route = useRoute()

async function loadLatest(): Promise<void> {
  const generation = ++requestGeneration
  activeAccess.value = null
  invalidateDestinationSearch()
  routeJobGeneration++
  routeJobState.value = 'idle'
  routeJobMessage.value = ''
  pendingEdit.value = null
  pendingEditCacheBroken.value = false
  editRequestInFlight.value = false
  selectedPoiIds.value = []
  const requestedTask = route.query.task === undefined ? null : typeof route.query.task === 'string' ? route.query.task : ''
  const access = requestedTask !== null ? getPreviewAccessForTask(requestedTask) : getLatestPreviewAccess()
  accessPersistencePending.value = Boolean(
    access && getPendingPreviewAccess()?.taskId === access.taskId,
  )
  if (!access) {
    pageState.value = 'missing_access'
    return
  }
  pageState.value = 'loading'
  errorMessage.value = ''
  failureAction.value = 'read'
  generationStage.value = 'accepted'
  generationStatus.value = 'accepted'
  view.value = null
  envelope.value = null
  activeAccess.value = access
  finalState.value = null
  finalStateChecked.value = false
  finalStateLoadFailed.value = false
  finalStateGeneration++
  try {
    await loadUntilReady(access, generation)
  } catch (error) {
    if (disposed || generation !== requestGeneration) return
    if (error instanceof PreviewPageError) {
      errorMessage.value = error.message
      failureAction.value = error.action
    } else {
      errorMessage.value = v4PreviewErrorMessage(error)
      const response = typeof error === 'object' && error !== null && 'response' in error
        ? (error as { response?: { status?: number } }).response
        : undefined
      failureAction.value = canRetryV4PreviewRead(Boolean(response), response?.status ?? null) ? 'read' : 'replan'
    }
    pageState.value = 'failed'
  }
}

async function loadUntilReady(access: V4PreviewAccess, generation: number): Promise<void> {
  const loadedEnvelope = await waitForV4PlanningPreview(access, {
    readStatus: getV4TaskStatus,
    readPreview: getV4Preview,
    isCurrent: () => !disposed && generation === requestGeneration,
    onProgress: (status) => {
      generationStage.value = status.stage
      generationStatus.value = status.status
    },
    pause: wait,
  })
  if (!loadedEnvelope || disposed || generation !== requestGeneration) return
  view.value = mapV4Preview(loadedEnvelope)
  envelope.value = loadedEnvelope
  activeAccess.value = access
  activeIndex.value = 0
  pageState.value = 'ready'
  void loadFinalState(access, generation)
  const hasCachedEdit = hasStoredV4PendingEdit(access.taskId)
  const cachedEdit = readV4PendingEdit(access.taskId)
  if (hasCachedEdit && (!cachedEdit || cachedEdit.planId !== access.planId)) {
    pendingEditCacheBroken.value = true
  } else if (cachedEdit) {
    pendingEdit.value = cachedEdit
    if (cachedEdit.status === 'conflict') {
      message.info('已恢复一项与最新行程冲突的编辑，请选择重试，或读取服务端结果。')
      void startRouteJobForEnvelope(access, loadedEnvelope, generation)
    } else {
      // A prior request may have reached the server before its response was lost.
      // Replay this exact persisted body and command_id before offering a new edit.
      void sendPendingEdit(cachedEdit, access, generation)
    }
  } else {
    void startRouteJobForEnvelope(access, loadedEnvelope, generation)
  }
}

async function loadFinalState(access: V4PreviewAccess, pageGeneration: number): Promise<void> {
  const request = ++finalStateGeneration
  try {
    const state = await getV4FinalState(access)
    if (disposed || pageGeneration !== requestGeneration || request !== finalStateGeneration) return
    if (state.task_id !== access.taskId || state.plan_id !== access.planId) return
    finalState.value = state
    finalStateChecked.value = true
    finalStateLoadFailed.value = false
  } catch {
    // Final-state lookup is optional. It never blocks the preview or image export.
    if (!disposed && pageGeneration === requestGeneration && request === finalStateGeneration) {
      finalStateChecked.value = true
      finalStateLoadFailed.value = true
    }
  }
}

async function startRouteJobForEnvelope(
  access: V4PreviewAccess,
  source: V4PreviewEnvelope,
  pageGeneration: number,
): Promise<void> {
  if (disposed || pageGeneration !== requestGeneration) return
  const generation = ++routeJobGeneration
  routeJobState.value = 'idle'
  routeJobMessage.value = ''
  let calculationId = pendingV4RouteJobId(source)
  if (!calculationId) return
  routeJobState.value = 'pending'
  while (!disposed && generation === routeJobGeneration && pageGeneration === requestGeneration) {
    try {
      const result = await getV4EditJob(access, calculationId)
      if (disposed || generation !== routeJobGeneration || pageGeneration !== requestGeneration) return
      const current = envelope.value
      if (current?.preview.calculation.calculation_id !== calculationId) return
      if (result.envelope.task_id !== access.taskId || result.envelope.plan_id !== access.planId) return
      const responseIsCurrent = isV4EnvelopeAtLeastAsNew(result.envelope, current)
      if (responseIsCurrent) {
        envelope.value = result.envelope
        view.value = mapV4Preview(result.envelope)
      }
      if (result.job.status === 'completed') {
        routeJobState.value = 'idle'
        routeJobMessage.value = ''
        message.success('交通信息已更新。')
        return
      }
      if (result.job.status === 'failed') {
        routeJobState.value = 'failed'
        routeJobMessage.value = '部分交通信息未能更新；当前地点安排已保留。'
        return
      }
      if (result.job.status === 'superseded') {
        const nextCalculationId = nextV4RouteJobAfterSuperseded(calculationId, result.envelope, current)
        if (nextCalculationId) {
          calculationId = nextCalculationId
          routeJobState.value = 'pending'
          routeJobMessage.value = ''
          continue
        }
        routeJobState.value = 'idle'
        routeJobMessage.value = ''
        return
      }
      routeJobState.value = result.job.status === 'running' ? 'running' : 'pending'
      await new Promise((resolve) => window.setTimeout(resolve, 900))
    } catch {
      if (disposed || generation !== routeJobGeneration || pageGeneration !== requestGeneration) return
      routeJobState.value = 'failed'
      routeJobMessage.value = '暂时无法读取交通更新状态；当前行程仍可查看、编辑和导出。'
      return
    }
  }
}

async function submitEditIntent(
  operations: V4EditOperation[],
  summary: string,
  details: string[],
): Promise<void> {
  const current = envelope.value
  const access = activeAccess.value
  if (!current || !access || editingBlocked.value) return
  let pending: V4PendingEdit
  try {
    pending = createV4PendingEdit({
      envelope: current,
      commandId: newRequestId('edit'),
      operations,
      summary,
      details,
    })
    // Persist exact request bytes before network I/O. If the response is lost,
    // refresh can safely replay the same idempotency key and CAS versions.
    persistV4PendingEdit(pending)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '无法保留这项编辑，尚未发送。')
    return
  }
  routeJobGeneration++
  routeJobState.value = 'idle'
  pendingEdit.value = pending
  await sendPendingEdit(pending, access, requestGeneration)
}

async function sendPendingEdit(
  pending: V4PendingEdit,
  access: V4PreviewAccess,
  pageGeneration: number,
): Promise<void> {
  if (editRequestInFlight.value || disposed || pageGeneration !== requestGeneration) return
  if (pending.taskId !== access.taskId || pending.planId !== access.planId) {
    pendingEditCacheBroken.value = true
    return
  }
  const sending = markV4PendingEdit(pending, 'sending')
  try {
    persistV4PendingEdit(sending)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '无法保留这项编辑。')
    return
  }
  pendingEdit.value = sending
  const requestSequence = ++editRequestSequence
  editRequestInFlight.value = true
  try {
    const response = sending.kind === 'destination_selection'
      ? await selectV4Destination(access, sending.request)
      : await submitV4Edit(access, sending.request)
    if (disposed || pageGeneration !== requestGeneration) return
    if (response.envelope.task_id !== access.taskId || response.envelope.plan_id !== access.planId) {
      throw new Error('编辑响应与当前行程不匹配。')
    }
    if (isV4EnvelopeAtLeastAsNew(response.envelope, envelope.value)) {
      envelope.value = response.envelope
      view.value = mapV4Preview(response.envelope)
      selectedPoiIds.value = []
      slotChoices.value = {}
    }
    const cacheCleared = clearV4PendingEdit(access.taskId)
    if (cacheCleared) {
      pendingEdit.value = null
      pendingEditCacheBroken.value = false
      invalidateDestinationSearch()
      message.success(response.replayed
        ? (sending.kind === 'destination_selection' ? '已恢复并加入所选地点。' : '已恢复并同步这项编辑。')
        : (sending.kind === 'destination_selection' ? '已将所选地点加入当天。' : '编辑已保存到当前预览。'))
    } else {
      const retained = markV4PendingEdit(sending, 'retry', '服务端已保存这项编辑，但浏览器暂时无法清除本机缓存。重试会使用同一命令安全核对保存结果。')
      pendingEdit.value = retained
      try { persistV4PendingEdit(retained) } catch { /* Keep the prior exact request for idempotent recovery. */ }
      message.warning('操作已保存，但本机缓存暂未清除。可以重试以安全核对结果。')
    }
    const currentEnvelope = envelope.value
    if (currentEnvelope) void startRouteJobForEnvelope(access, currentEnvelope, pageGeneration)
  } catch (error) {
    if (disposed || pageGeneration !== requestGeneration) return
    if (isV4EditConflict(error)) {
      let refreshed = false
      try {
        const latest = await getV4Preview(access)
        if (disposed || pageGeneration !== requestGeneration) return
        if (latest.task_id === access.taskId && latest.plan_id === access.planId
          && isV4EnvelopeAtLeastAsNew(latest, envelope.value)) {
          envelope.value = latest
          view.value = mapV4Preview(latest)
          selectedPoiIds.value = []
          slotChoices.value = {}
          refreshed = true
        }
      } catch {
        // Keep the visible draft and the exact pending request if refresh is unavailable.
      }
      const conflict = markV4PendingEdit(
        sending,
        'conflict',
        refreshed
          ? '已读取最新行程。请确认这项编辑后，选择“按最新行程重试”，或停止重试并刷新。'
          : '暂时无法读取最新行程。操作仍保留；重试前请先刷新页面。',
      )
      pendingEdit.value = conflict
      try { persistV4PendingEdit(conflict) } catch { /* The sending request is already stored. */ }
      message.warning('行程已有变化。这项操作已保留，请确认后重试，或读取服务端结果。')
      if (envelope.value) void startRouteJobForEnvelope(access, envelope.value, pageGeneration)
    } else {
      const errorText = sending.kind === 'destination_selection'
        ? v4DestinationSelectErrorMessage(error)
        : v4EditErrorMessage(error)
      const retry = markV4PendingEdit(sending, 'retry', errorText)
      pendingEdit.value = retry
      try { persistV4PendingEdit(retry) } catch { /* The exact request was persisted before submission. */ }
      message.warning(errorText)
    }
  } finally {
    if (requestSequence === editRequestSequence) editRequestInFlight.value = false
  }
}

async function retryPendingEdit(): Promise<void> {
  const pending = pendingEdit.value
  const current = envelope.value
  const access = activeAccess.value
  if (!pending || !current || !access || editRequestInFlight.value) return
  if (pending.status !== 'conflict') {
    await sendPendingEdit(pending, access, requestGeneration)
    return
  }
  if (!canRetryConflict.value) {
    message.info('请刷新并读取最新行程后，再重试这项编辑。')
    return
  }
  try {
    const rebased = rebaseV4PendingEdit(pending, current, newRequestId('edit'))
    persistV4PendingEdit(rebased)
    pendingEdit.value = rebased
    await sendPendingEdit(rebased, access, requestGeneration)
  } catch (error) {
    message.info(error instanceof Error ? error.message : '这项编辑无法按最新行程重试。')
  }
}

async function discardPendingEdit(): Promise<void> {
  const access = activeAccess.value
  const pending = pendingEdit.value
  if (!access || !pending || editRequestInFlight.value || discardRefreshing.value) return
  const pageGeneration = requestGeneration
  discardRefreshing.value = true
  try {
    const outcome = await refreshBeforeDiscardV4PendingEdit({
      access,
      current: envelope.value,
      readLatest: () => getV4Preview(access),
      adopt: (latest) => {
        if (disposed || pageGeneration !== requestGeneration
          || !pendingEdit.value
          || pendingCommandId(pendingEdit.value) !== pendingCommandId(pending)) {
          throw new Error('页面状态已变化，未清除这项编辑。')
        }
        envelope.value = latest
        view.value = mapV4Preview(latest)
        selectedPoiIds.value = []
        slotChoices.value = {}
      },
      clear: () => clearV4PendingEdit(access.taskId),
    })
    if (disposed || pageGeneration !== requestGeneration) return
    if (outcome === 'stale') {
      message.error('无法确认服务端结果，编辑仍保留。请刷新行程后再试。')
      return
    }
    if (outcome === 'cache_failed') {
      message.error('已刷新服务端结果，但浏览器无法清除编辑缓存；操作仍保留，请稍后重试。')
      return
    }
    if (!pendingEdit.value || pendingCommandId(pendingEdit.value) !== pendingCommandId(pending)) return
    pendingEdit.value = null
    pendingEditCacheBroken.value = false
    invalidateDestinationSearch()
    message.info('已停止重试，并刷新为服务端结果。若这项编辑此前已提交，其结果仍会保留。')
  } catch {
    if (!disposed && pageGeneration === requestGeneration) {
      message.error('无法读取服务端最新结果，编辑仍保留。请稍后重试或刷新行程。')
    }
  } finally {
    discardRefreshing.value = false
  }
}

function clearBrokenPendingEdit(): void {
  const access = activeAccess.value
  if (!access) return
  if (!clearV4PendingEdit(access.taskId)) {
    message.error('浏览器无法清除损坏的缓存，请稍后重试。')
    return
  }
  pendingEditCacheBroken.value = false
  message.info('已清除无法读取的本机编辑缓存。')
}

function retryAccessPersistence(): void {
  if (!accessPersistencePending.value || accessStorageRetrying.value) return
  accessStorageRetrying.value = true
  try {
    if (!retrySavePreviewAccess()) {
      message.error('浏览器仍无法保存恢复信息。当前页可继续使用，请稍后重试。')
      return
    }
    accessPersistencePending.value = false
    message.success('恢复信息已保存在本机。')
  } finally {
    accessStorageRetrying.value = false
  }
}

function availableSlotsFor(kind: 'sightseeing' | 'meal') {
  const allowed = kind === 'meal'
    ? new Set(['breakfast', 'lunch', 'dinner'])
    : new Set(['morning', 'afternoon', 'evening'])
  return activeConstraints.value
    .filter((slot) => slot.eligibility !== 'skipped' && allowed.has(slot.period))
    .map((slot) => ({ id: slot.slot_id, label: slotPeriodLabel(slot.period) }))
}

function slotPeriodLabel(period: string): string {
  const labels: Record<string, string> = {
    breakfast: '早餐', morning: '上午', lunch: '午餐', afternoon: '下午', dinner: '晚餐', evening: '晚上',
  }
  return labels[period] ?? '所选时段'
}

function slotChoice(key: string, kind: 'sightseeing' | 'meal'): string {
  const scopedKey = `${activeDay.value?.id ?? ''}:${key}`
  return slotChoices.value[scopedKey] ?? availableSlotsFor(kind)[0]?.id ?? ''
}

function setSlotChoice(key: string, event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  const scopedKey = `${activeDay.value?.id ?? ''}:${key}`
  slotChoices.value = { ...slotChoices.value, [scopedKey]: value }
}

function addCandidate(
  candidateRef: string,
  kind: 'sightseeing' | 'meal',
  slotId: string,
  name: string,
  poiId?: string,
): void {
  if (!slotId) return
  const label = availableSlotsFor(kind).find((slot) => slot.id === slotId)?.label
  const clientRef = newRequestId('client')
  void submitEditIntent([{
    type: 'add_activity',
    client_ref: clientRef,
    candidate_ref: candidateRef,
    slot_id: slotId,
    day_id: activeDay.value?.id ?? '',
  }], `将“${name}”加入${label ?? '所选时段'}`, [
    `类别：${kind === 'meal' ? '餐馆' : '景点'}`,
    `时段：${label ?? '未知'}`,
    ...(poiId ? ['这是同一地点的另一项活动，保留各自的时段安排。'] : []),
  ])
}

function removeActivity(activity: PreviewActivityView): void {
  const dayId = activeDay.value?.id
  if (!dayId) return
  void submitEditIntent([{
    type: 'remove_activity', day_id: dayId, activity_id: activity.id,
  }], `从当天删除“${activity.title}”`, [`安排时段：${activity.periodLabel}`])
}

function removeSelectedDestinations(): void {
  const dayId = activeDay.value?.id
  if (!dayId) return
  const selected = destinationGroups.value.filter((destination) => selectedPoiIds.value.includes(destination.key))
  const operations: V4EditOperation[] = selected.flatMap((destination) => destination.activityIds.map((activityId) => ({
    type: 'remove_activity' as const, day_id: dayId, activity_id: activityId,
  })))
  if (!operations.length || operations.length > 20) return
  const details = selected.map((destination) => (
    `${destination.name}：${destination.periodLabels.join('、')}（${destination.activityIds.length} 项）`
  ))
  void submitEditIntent(operations, `删除当天所选 ${selected.length} 个目的地，共 ${operations.length} 项安排`, details)
}

function canChooseMode(leg: PreviewLegView, mode: PreviewModeView): boolean {
  return !editingBlocked.value
    && mode.status === '路线已查询'
    && Boolean(mode.optionId)
    && leg.selectedMode !== mode.mode
}

function chooseLegMode(dayId: string, leg: PreviewLegView, mode: PreviewModeView): void {
  if (!canChooseMode(leg, mode) || !mode.optionId) return
  void submitEditIntent([{
    type: 'select_leg_mode', day_id: dayId, leg_id: leg.id, mode: mode.mode, option_id: mode.optionId,
  }], `将“${leg.fromLabel} → ${leg.toLabel}”切换为${mode.label}`,
  [`参考耗时：${mode.durationLabel}`, leg.timeBasisLabel ?? '查询时刻依据未核实'])
}

function startDrag(event: PointerEvent, activityId: string): void {
  if (editingBlocked.value || event.button !== 0) return
  const target = event.currentTarget
  if (!(target instanceof HTMLElement)) return
  dragOrigin = { id: activityId, pointerId: event.pointerId }
  draggingId.value = activityId
  dropId.value = activityId
  target.setPointerCapture(event.pointerId)
}

function trackDrag(event: PointerEvent): void {
  if (!dragOrigin || dragOrigin.pointerId !== event.pointerId) return
  const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-activity-id]')
  dropId.value = target?.dataset.activityId ?? ''
}

function finishDrag(event: PointerEvent): void {
  const origin = dragOrigin
  if (!origin || origin.pointerId !== event.pointerId) return
  const targetElement = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-activity-id]')
  const targetId = targetElement?.dataset.activityId
  const activities = activeDay.value?.activities ?? []
  const ids = activities.map((activity) => activity.id)
  const from = ids.indexOf(origin.id)
  let to = targetId ? ids.indexOf(targetId) : -1
  if (from >= 0 && to >= 0 && targetElement && targetId !== origin.id) {
    const bounds = targetElement.getBoundingClientRect()
    if (event.clientY >= bounds.top + bounds.height / 2) to += 1
    const [moved] = ids.splice(from, 1)
    const insertionIndex = Math.max(0, Math.min(ids.length, to > from ? to - 1 : to))
    ids.splice(insertionIndex, 0, moved)
    if (ids.some((id, index) => id !== activities[index]?.id)) submitReorder(ids)
  }
  const captureTarget = event.currentTarget
  if (captureTarget instanceof HTMLElement && captureTarget.hasPointerCapture(event.pointerId)) {
    captureTarget.releasePointerCapture(event.pointerId)
  }
  cancelDrag()
}

function cancelDrag(): void {
  dragOrigin = null
  draggingId.value = ''
  dropId.value = ''
}

function keyboardMove(activityId: string, offset: -1 | 1): void {
  const activities = activeDay.value?.activities ?? []
  const ids = activities.map((activity) => activity.id)
  const index = ids.indexOf(activityId)
  const target = index + offset
  if (editingBlocked.value || target < 0 || target >= ids.length) return
  const [moved] = ids.splice(index, 1)
  ids.splice(target, 0, moved)
  submitReorder(ids)
}

function submitReorder(activityIds: string[]): void {
  const dayId = activeDay.value?.id
  if (!dayId || activityIds.length < 2) return
  const names = new Map((activeDay.value?.activities ?? []).map((activity) => [activity.id, activity.title]))
  void submitEditIntent([{
    type: 'reorder_activities', day_id: dayId, activity_ids: activityIds,
  }], '调整当天活动顺序', activityIds.map((id, index) => `${index + 1}. ${names.get(id) ?? '活动'}`))
}

async function exportCurrent(): Promise<void> {
  if (exporting.value || !envelope.value || !view.value || !activeAccess.value) return
  const access = activeAccess.value
  const snapshot = captureV4PreviewExport(envelope.value, view.value, {
    dirty: localDirty.value,
    saving: localSaveInFlight.value,
  })
  exportSnapshot.value = snapshot
  exporting.value = true
  exportPhase.value = 'rendering'
  try {
    const result = await exportV4Snapshot({
      snapshot,
      renderAll: async (_visible: V4ExportVisibleState) => {
        await nextTick()
        const renderer = exportRenderer.value
        if (!renderer) throw new Error('图片导出区域尚未准备好。')
        return renderer.renderAll()
      },
      startDownloads: (files: readonly V4ExportFile<Blob>[]) => {
        const links = files.map((file) => ({
          filename: file.filename,
          label: file.filename.replace(/^旅行计划_/, ''),
          url: URL.createObjectURL(file.blob),
        }))
        revokeExportLinks()
        exportFiles.value = links
        for (const link of links) startImageDownload(link.url, link.filename)
      },
      canPersist: (source) => Boolean(
        !localDirty.value
        && !localSaveInFlight.value
        && envelope.value?.task_id === source.taskId
        && envelope.value.plan_id === source.planId
        && envelope.value.store_version === source.storeVersion
        && envelope.value.plan_revision === source.planRevision,
      ),
      onDownloadsStarted: (willAttemptSave) => {
        if (willAttemptSave) exportPhase.value = 'saving'
      },
      saveFinal: (source) => saveV4Final(access, {
        save_id: newRequestId('save'),
        source_store_version: source.storeVersion,
      }),
      isSaved: (response: V4FinalSaveResponse) => {
        if (response.task_id !== access.taskId || response.plan_id !== access.planId) {
          throw new Error('保存结果与当前行程不匹配，图片已导出。')
        }
        return response.status !== 'stale'
      },
    })
    if (result.status === 'saved') {
      updateFinalState(result.response)
      message.success(result.response.restorable
        ? `已导出 ${result.fileCount} 张图片，并保存当前行程。`
        : `已导出 ${result.fileCount} 张图片；保存结果目前不完整，暂不可还原。`)
    } else if (result.status === 'stale') {
      updateFinalState(result.response)
      message.info(`已导出 ${result.fileCount} 张图片；行程已有变化，本次没有覆盖上次保存。`)
    } else if (result.status === 'skipped') {
      message.info(`已导出 ${result.fileCount} 张图片。当前内容有未同步修改，暂未保存服务器快照。`)
    } else {
      message.warning(v4FinalErrorMessage(result.error, 'save'))
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : '图片生成失败，请重试。')
  } finally {
    exportSnapshot.value = null
    exporting.value = false
    exportPhase.value = 'rendering'
  }
}

function updateFinalState(response: V4FinalSaveResponse): void {
  finalStateGeneration++
  finalStateChecked.value = true
  finalStateLoadFailed.value = false
  finalState.value = {
    task_id: response.task_id,
    plan_id: response.plan_id,
    restorable: response.restorable,
    final: response.final,
    unavailable_reason: response.unavailable_reason,
  }
}

async function restoreLastSaved(): Promise<void> {
  const current = envelope.value
  const access = activeAccess.value
  const final = finalState.value?.final
  if (!current || !access || !final || !canRestore.value) return
  const pageGeneration = requestGeneration
  const expected = {
    task_id: current.task_id,
    plan_id: current.plan_id,
    store_version: current.store_version,
    plan_revision: current.plan_revision,
  }
  routeJobGeneration++
  routeJobState.value = 'idle'
  routeJobMessage.value = ''
  restoring.value = true
  try {
    const response = await restoreV4Final(access, {
      restore_id: newRequestId('restore'),
      expected_store_version: expected.store_version,
      expected_plan_revision: expected.plan_revision,
      expected_save_id: final.save_id,
    })
    if (disposed || pageGeneration !== requestGeneration) return
    if (!canApplyV4Restore({
      dirty: localDirty.value,
      saving: localSaveInFlight.value,
      current: envelope.value ?? expected,
      expected,
    })) {
      message.info('页面内容在还原期间已有变化，已保留当前内容；请确认后重新操作。')
      if (envelope.value) void startRouteJobForEnvelope(access, envelope.value, pageGeneration)
      return
    }
    if (response.envelope.task_id !== access.taskId
      || response.envelope.plan_id !== access.planId
      || response.restored_from.save_id !== final.save_id) {
      throw new Error('还原结果与当前行程不匹配，当前页面内容保持不变。')
    }
    const restoredView = mapV4Preview(response.envelope)
    envelope.value = response.envelope
    view.value = restoredView
    activeIndex.value = Math.min(activeIndex.value, Math.max(0, restoredView.days.length - 1))
    selectedPoiIds.value = []
    slotChoices.value = {}
    message.success('已还原上次成功保存的行程。')
    void startRouteJobForEnvelope(access, response.envelope, requestGeneration)
  } catch (error) {
    if (disposed || pageGeneration !== requestGeneration) return
    message.error(error instanceof Error && error.message.includes('不匹配')
      ? error.message
      : v4FinalErrorMessage(error, 'restore'))
    if (envelope.value) void startRouteJobForEnvelope(access, envelope.value, pageGeneration)
  } finally {
    restoring.value = false
  }
}

function newRequestId(prefix: string): string {
  const randomId = globalThis.crypto?.randomUUID?.()
  return randomId ? `${prefix}_${randomId}` : `${prefix}_${Date.now()}_${Math.random().toString(36).slice(2)}`
}

function pendingExpectedStoreVersion(pending: V4PendingEdit): number {
  return pending.request.expected_store_version
}

function pendingExpectedPlanRevision(pending: V4PendingEdit): number {
  return pending.kind === 'destination_selection'
    ? pending.request.expected_plan_revision
    : pending.request.command.base_revision
}

function pendingCommandId(pending: V4PendingEdit): string {
  return pending.kind === 'destination_selection'
    ? pending.request.command_id
    : pending.request.command.command_id
}

function startImageDownload(url: string, filename: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
}

function revokeExportLinks(): void {
  for (const file of exportFiles.value) URL.revokeObjectURL(file.url)
  exportFiles.value = []
}

function formatSavedAt(value: string): string {
  const date = new Date(value)
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
    : value
}


function formatDate(value: string, timezone: string): string {
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return value.slice(0, 10)
  return new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', timeZone: timezone }).format(date)
}

function formatShortDate(value: string): string {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  return match ? `${Number(match[2])}月${Number(match[3])}日` : value
}

function activityCountText(count: number): string {
  return count === 0 ? '当天没有具体活动安排。' : `按顺序安排 ${count} 项活动。`
}

function activityKind(kind: string): string {
  if (kind === 'meal') return '用餐安排'
  if (kind === 'free_time') return '自由活动'
  return '景点游览'
}

function costGapText(unknownCount: number, conflictCount: number): string {
  const parts = []
  if (unknownCount) parts.push(`${unknownCount} 项未查到`)
  if (conflictCount) parts.push(`${conflictCount} 项存在冲突`)
  return `消费信息：${parts.join('，')}`
}

function issueTitle(issue: { category: string }): string {
  if (issue.category === 'known_conflict') return '可能需要调整'
  if (issue.category === 'missing_arrangement') return '安排缺项'
  if (issue.category === 'unknown_info' || issue.category === 'invalid_reference') return '信息未查到'
  return '行程提示'
}

function issueTone(issue: { category: string }): string {
  return issue.category === 'known_conflict' ? 'warning' : 'info'
}

watch(() => route.query.task, () => { void loadLatest() })
onMounted(() => { void loadLatest() })
onUnmounted(() => {
  disposed = true
  requestGeneration++
  invalidateDestinationSearch()
  if (pendingTimer !== null) window.clearTimeout(pendingTimer)
  revokeExportLinks()
})
</script>

<style scoped>
.planning-observation-entry { text-align:right; margin-bottom:16px; font-size:12px; }
.planning-observation-entry a { color:#355b44; text-underline-offset:4px; }
.preview-shell { max-width: 1440px; min-height: 100vh; margin: 0 auto; padding: 30px clamp(20px, 5vw, 72px) 84px; color: #1b322c; font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif; }
.page-topline { display: flex; justify-content: space-between; align-items: center; padding-bottom: 17px; border-bottom: 1px solid #d7ded2; }
.preview-actions { display: flex; align-items: center; flex-wrap: wrap; justify-content: end; gap: 8px; position: relative; }
.preview-actions > small { color: #718173; font-size: 10px; }
.preview-action { border: 1px solid #cdd8c9; background: #fffdfa; padding: 7px 11px; color: #355b44; font: inherit; font-size: 11px; cursor: pointer; }
.preview-action:disabled { color: #9aa497; background: #f2f3ee; cursor: not-allowed; }
.preview-action.restore-action { border-color: #b7c9a8; background: #eaf1df; }
.pending-edit-banner, .route-job-banner { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin: 14px 0; padding: 13px 16px; border: 1px solid #d7cba9; background: #fbf7eb; color: #6d6046; font-size: 11px; line-height: 1.7; }
.pending-edit-banner.conflict, .route-job-banner.failed { border-color: #dfbfa7; background: #fbf0e8; color: #80583f; }
.pending-edit-copy { min-width: 0; }
.pending-edit-copy p { margin: 3px 0; }
.pending-edit-copy ul { margin: 6px 0; padding-left: 18px; }
.pending-edit-copy small { color: #857451; }
.pending-edit-actions { display: flex; flex: 0 0 auto; flex-wrap: wrap; gap: 7px; }
.discard-edit-action, .activity-remove { border-color: #dbc9ba; color: #8a5c45; }
.export-download-fallback { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 11px 14px; border: 1px solid #d8e0d2; background: #f7f8f1; color: #64785e; font-size: 11px; }
.export-download-fallback a { color: #355b44; text-decoration: underline; }
.eyebrow { color: #a65c40; font-size: 10px; letter-spacing: .15em; font-weight: 700; }
.readonly-label { color: #718173; border: 1px solid #d2dbc7; padding: 5px 9px; font-size: 10px; }
.hero { display: flex; align-items: end; justify-content: space-between; gap: 30px; padding: 34px 0 27px; border-bottom: 1px solid #cbd7c6; }
.hero-kicker { color: #a65c40; font-size: 10px; letter-spacing: .17em; font-weight: 700; margin: 0; }
.hero h1 { margin: 13px 0 10px; font: 600 clamp(37px, 5vw, 58px)/1.16 'Songti SC', 'Noto Serif SC', serif; letter-spacing: -.04em; }
.date-range { margin: 0; color: #708172; font-size: 13px; }
.cost-summary { display: flex; flex-direction: column; gap: 7px; min-width: 275px; max-width: 330px; padding: 19px 23px; background: #e4edcd; color: #51664f; font-size: 11px; line-height: 1.6; }
.cost-summary strong { color: #244032; font: 34px/1.2 'Songti SC', 'Noto Serif SC', serif; }
.cost-summary small { color: #63745f; font-size: 10px; }
.global-issues { margin: 18px 0; }
.day-tabs { display: flex; gap: 2px; overflow-x: auto; margin: 24px 0 20px; border-bottom: 1px solid #d9e1d4; }
.day-tabs button { display: flex; flex-direction: column; align-items: flex-start; gap: 5px; min-width: 150px; border: 0; border-bottom: 2px solid transparent; background: transparent; padding: 14px 17px; color: #718274; text-align: left; cursor: pointer; }
.day-tabs button.active { background: #e5edcf; border-bottom-color: #bb704f; color: #1b392e; }
.day-tabs small { color: inherit; font-size: 11px; }
.day-tabs i { color: inherit; font-size: 10px; font-style: normal; }
.day-overview { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin: 22px 0 19px; }
.overview-copy h2 { margin: 8px 0 6px; color: #254130; font: 27px 'Songti SC', 'Noto Serif SC', serif; }
.overview-copy p { margin: 0; color: #718173; font-size: 11px; }
.daily-facts { display: flex; justify-content: end; flex-wrap: wrap; gap: 8px; max-width: 560px; }
.daily-facts span { background: #edf2e4; padding: 8px 10px; color: #45624f; font-size: 10px; }
.day-layout { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(340px, .9fr); align-items: start; gap: 38px; }
.timeline-column { min-width: 0; }
.info-card { margin: 0 0 15px; padding: 17px 20px; border: 1px solid #d9dfd3; background: #f7f8f1; }
.card-topline { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.fact-known, .fact-unknown { color: #64785e; font-size: 10px; }
.fact-unknown { color: #9a6445; }
.info-card h3 { margin: 9px 0 5px; color: #254130; font: 20px 'Songti SC', 'Noto Serif SC', serif; }
.info-card p { margin: 6px 0 0; color: #61746a; font-size: 11px; line-height: 1.8; }
.lodging-grid { display: grid; grid-template-columns: 1.15fr 1fr; gap: 24px; margin-top: 15px; }
.lodging-grid small { color: #718173; font-size: 10px; }
.lodging-grid h3 { font-size: 17px; }
.lodging-grid .hotel-cost { display: block; margin-top: 9px; }
.cost-evidence { display: block; margin-top: 5px; color: #8a9585 !important; font-size: 9px !important; line-height: 1.6; }
.lodging-grid.hotel-only { grid-template-columns: 1fr; }
.area-reference { border-left: 1px solid #dce3d3; padding-left: 20px; }
.muted { color: #718173 !important; }
.day-issues { margin: 14px 0; }
.issue-row { display: flex; align-items: flex-start; gap: 10px; margin: 7px 0; padding: 10px 13px; border: 1px solid #e1d6bd; background: #fbf6ea; color: #6e5e43; font-size: 11px; line-height: 1.7; }
.issue-row.info { border-color: #dce3d3; background: #f6f7f0; color: #63735d; }
.issue-row b { flex-shrink: 0; font-weight: 600; }
.missing-slot { display: flex; gap: 12px; margin: 13px 0; padding: 15px 17px; border: 1px dashed #c8b791; background: #fbf7eb; color: #64563c; }
.missing-icon { display: grid; flex: 0 0 22px; place-items: center; height: 22px; border-radius: 50%; background: #e5d9b8; font-size: 12px; }
.missing-icon.conditional { background: #d9e2cc; color: #526a54; }
.missing-slot b { font-size: 12px; font-weight: 600; }
.missing-slot p { margin: 4px 0 0; font-size: 11px; line-height: 1.7; }
.missing-slot .issue-detail { color: #837353; }
.leg-card { margin: 15px 0 15px 14px; padding: 0 0 0 16px; border-left: 2px solid #b2c7a6; }
.leg-heading { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; color: #64785e; font-size: 10px; }
.leg-heading b { color: #47654f; font-size: 11px; font-weight: 600; }
.mode-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 7px; margin-top: 9px; }
.mode-chip { display: flex; flex-direction: column; gap: 5px; min-width: 0; padding: 8px 9px; border: 1px solid #e0e7d5; background: #fffdfa; color: #718173; font: inherit; font-size: 9px; text-align: left; }
.mode-chip:enabled { cursor: pointer; }
.mode-chip:disabled { opacity: .68; cursor: not-allowed; }
.mode-chip b { color: #475d4c; font-size: 10px; font-weight: 500; }
.mode-chip small { color: #8a9585; font-size: 8px; line-height: 1.4; }
.mode-chip.chosen { border-color: #789272; background: #eaf1df; }
.route-note { margin: 7px 0 0; color: #788675; font-size: 9px; line-height: 1.6; }
.activity-card { margin: 0 0 14px; padding: 19px 21px; border: 1px solid #d9dfd3; background: #fffdfa; box-shadow: 5px 5px 0 #f0f2e9; }
.activity-card.meal { background: #fcfaf5; }
.activity-card.free { border-style: dashed; }
.activity-card.dragging { opacity: .55; }
.activity-card.drop-target { outline: 2px dashed #91a581; outline-offset: 3px; }
.activity-meta { display: flex; justify-content: space-between; gap: 12px; color: #a75b3f; font-size: 10px; }
.activity-meta span + span { color: #61746a; }
.activity-actions { display: flex; align-items: center; gap: 8px; }
.drag-handle, .activity-remove, .small-action { border: 1px solid #cdd8c9; background: #fffdfa; color: #526d54; font: inherit; font-size: 9px; cursor: pointer; }
.drag-handle { width: 27px; height: 25px; padding: 0; touch-action: none; cursor: grab; }
.drag-handle:active { cursor: grabbing; }
.drag-handle:disabled, .activity-remove:disabled, .small-action:disabled { color: #a2aa9d; cursor: not-allowed; }
.activity-remove, .small-action { padding: 5px 8px; }
.activity-title { display: flex; align-items: center; gap: 12px; margin: 14px 0 9px; }
.activity-number, .destination-number { display: grid; flex: 0 0 23px; place-items: center; width: 23px; height: 23px; border-radius: 50%; color: #688060; background: #edf2e4; font-size: 10px; }
.activity-title h3 { margin: 0 0 4px; color: #254130; font: 21px 'Songti SC', 'Noto Serif SC', serif; }
.activity-title small { color: #718173; font-size: 10px; }
.activity-description, .activity-address { margin: 7px 0; color: #61746a; font-size: 11px; line-height: 1.8; }
.activity-address { color: #81907d; font-size: 10px; }
.activity-card footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-top: 13px; padding-top: 11px; border-top: 1px solid #e9eddf; color: #6a7f60; font-size: 10px; }
.map-column { position: sticky; top: 20px; min-width: 0; }
.destination-card { margin-top: 18px; padding: 19px 21px; border: 1px solid #d8e0d2; background: #fffdfa; }
.section-heading { display: flex; align-items: end; justify-content: space-between; gap: 12px; }
.section-heading h3 { margin: 7px 0 0; color: #254130; font: 20px 'Songti SC', 'Noto Serif SC', serif; }
.section-heading > small { color: #718173; font-size: 10px; }
.destination-list, .candidate-list { list-style: none; padding: 0; margin: 13px 0 0; }
.destination-list li { display: grid; grid-template-columns: auto auto minmax(0, 1fr); align-items: center; gap: 8px; padding: 10px 0; border-bottom: 1px solid #edf0e9; }
.destination-check { accent-color: #718d67; }
.destination-copy { display: flex; min-width: 0; flex-direction: column; gap: 4px; }
.destination-list b, .candidate-list b { color: #355342; font-size: 11px; font-weight: 500; }
.destination-list small, .candidate-list small { color: #718173; font-size: 9px; line-height: 1.6; }
.destination-actions { grid-column: 3; display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 6px; min-width: 0; }
.destination-actions select, .candidate-add-row select { max-width: 100%; min-width: 0; border: 1px solid #d8e0d2; background: #fffdfa; padding: 5px 7px; color: #526d54; font: inherit; font-size: 9px; }
.bulk-remove-row { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 9px; margin-top: 12px; padding-top: 10px; border-top: 1px solid #edf0e9; color: #718173; font-size: 10px; }
.limit-note { display: block; margin-top: 6px; color: #9a6445; font-size: 9px; }
.coord-missing { flex: 0 0 auto; color: #a16c51 !important; }
.recommendations > p { margin: 8px 0; font-size: 10px; }
.candidate-list li { padding: 12px 0; border-bottom: 1px solid #edf0e9; }
.candidate-list li > div { display: flex; align-items: center; gap: 8px; }
.candidate-type { padding: 3px 6px; background: #edf2e4; color: #6b805e; font-size: 9px; }
.candidate-list li > small { display: block; margin: 5px 0; }
.candidate-list p { margin: 5px 0 0; color: #718173; font-size: 10px; line-height: 1.7; }
.candidate-add-row { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 7px; margin-top: 9px; }
.destination-search > p { margin: 8px 0; font-size: 10px; }
.destination-search-form { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 7px; margin-top: 12px; }
.destination-search-form select, .destination-search-form input { min-width: 0; border: 1px solid #d8e0d2; background: #fffdfa; padding: 7px 8px; color: #526d54; font: inherit; font-size: 10px; }
.destination-search-results { list-style: none; margin: 12px 0 0; padding: 0; }
.destination-search-results li { padding: 12px 0; border-bottom: 1px solid #edf0e9; }
.destination-search-results small { display: block; margin-top: 5px; color: #718173; font-size: 9px; line-height: 1.6; overflow-wrap: anywhere; }
.search-result-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
.search-result-heading b { color: #355342; font-size: 11px; font-weight: 500; overflow-wrap: anywhere; }
.destination-search-results .search-source { color: #77856e; }
.destination-search-results .search-blocked, .destination-search .search-error { color: #9a6445; }
.search-pagination { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 11px; }
.search-pagination small { color: #718173; font-size: 9px; text-align: center; }
.omitted-results { margin-top: 8px !important; }
.empty-day { padding: 24px; border: 1px dashed #b7c9a8; color: #748467; }
.empty-day h3 { font: 19px 'Songti SC', 'Noto Serif SC', serif; }
.empty-day p { font-size: 11px; }
.state-page { display: flex; min-height: 72vh; align-items: center; justify-content: center; flex-direction: column; text-align: center; }
.state-page h1 { margin: 13px 0 8px; color: #254130; font: 32px 'Songti SC', 'Noto Serif SC', serif; }
.state-page p { max-width: 470px; margin: 0; color: #718173; font-size: 12px; line-height: 1.8; }
.state-page .storage-warning { margin-top: 10px; color: #9a6445; }
.generation-spinner { width: 58px; height: 58px; display: grid; place-items: center; margin: 30px 0 7px; border: 4px solid #dce5d5; border-top-color: #50775d; border-right-color: #92a688; border-radius: 50%; animation: generation-spin .9s linear infinite; }
.generation-spinner span { width: 9px; height: 9px; border-radius: 50%; background: #d46b46; }
.loading-caption { margin-top: 5px; color: #879285; font-size: 10px; letter-spacing: .08em; }
.state-page p.loading-stage { margin-top: 6px; color: #355b44; font-size: 16px; font-weight: 600; }
.state-page p.loading-note { margin-top: 10px; }
.quiet-button { margin-top: 20px; border: 1px solid #cdd8c9; background: #fffdfa; padding: 9px 15px; color: #355b44; cursor: pointer; }
.quiet-link { display: inline-block; margin-top: 20px; border: 1px solid #cdd8c9; background: #fffdfa; padding: 9px 15px; color: #355b44; text-decoration: none; font-size: 12px; }
.state-actions { display: flex; align-items: center; gap: 10px; }
.state-actions .quiet-button, .state-actions .quiet-link { margin-top: 20px; }
@keyframes generation-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .generation-spinner { animation: none; } }
@media (max-width: 940px) { .day-layout { grid-template-columns: 1fr; }.map-column { position: static; order: -1; }.hero { align-items: start; }.daily-facts { max-width: 430px; } }
@media (max-width: 620px) { .preview-shell { padding: 20px 16px 55px; }.page-topline { align-items: flex-start; }.preview-actions { max-width: 74%; }.pending-edit-banner { flex-direction: column; }.pending-edit-actions { width: 100%; }.hero { display: block; padding: 25px 0; }.hero h1 { font-size: 39px; }.cost-summary { margin-top: 19px; min-width: 0; max-width: none; }.day-overview { display: block; }.daily-facts { justify-content: start; margin-top: 12px; }.lodging-grid { grid-template-columns: 1fr; gap: 13px; }.area-reference { border-left: 0; border-top: 1px solid #dce3d3; padding: 13px 0 0; }.mode-list { gap: 5px; }.mode-chip { padding: 7px; }.activity-card { padding: 16px; }.activity-title h3 { font-size: 19px; }.activity-meta { align-items: flex-start; }.activity-actions { flex-wrap: wrap; justify-content: flex-end; }.destination-list li { grid-template-columns: auto auto minmax(0, 1fr); gap: 6px; }.destination-actions { grid-column: 1 / -1; }.destination-search-form { grid-template-columns: minmax(0, 1fr) auto; }.destination-search-form select { grid-column: 1 / -1; }.map-column { order: 0; } }
</style>
