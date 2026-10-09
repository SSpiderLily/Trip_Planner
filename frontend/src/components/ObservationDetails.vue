<template>
  <section class="details-panel" aria-label="调用详情" aria-live="polite">
    <div class="heading"><h3>调用详情</h3><button v-if="spanId" @click="$emit('retry')">刷新</button></div>
    <p v-if="loading" class="placeholder">正在读取调用详情…</p>
    <div v-else-if="error" class="error">详情查询失败：{{ error }}<button @click="$emit('retry')">重试</button></div>
    <p v-else-if="!detail" class="placeholder">选择一次调用，查看真实输入与输出。</p>
    <template v-else>
      <div class="identity"><span class="kind">{{ detail.operation_type === 'llm' ? '模型调用' : detail.operation_type === 'tool' ? '工具调用' : '执行步骤' }}</span><h4>{{ stepName(detail.name) }}</h4><p>{{ detail.name }}</p><span class="state" :class="detail.status">{{ spanState(detail, active) }}</span><b>{{ spanTime(detail, active, now) }}</b></div>
      <dl class="facts"><dt>调用编号</dt><dd>{{ detail.span_id }}</dd><dt>开始时间</dt><dd>{{ date(detail.started_at) }}</dd><dt>结束时间</dt><dd>{{ date(detail.finished_at) }}</dd><template v-if="detail.operation_type === 'llm'"><dt>模型</dt><dd>{{ model }}</dd><dt>Token 总量</dt><dd>{{ tokenText(tokenTotal(detail)) }}</dd><dt>输入 Token</dt><dd>{{ tokenText(detail.input_tokens) }}</dd><dt>输出 Token</dt><dd>{{ tokenText(detail.output_tokens) }}</dd></template></dl>
      <p v-if="timingIssue(detail)" class="warning">{{ timingIssue(detail) }}，不参与耗时比较。</p>
      <p v-if="detail.input_truncated || detail.output_truncated" class="warning">内容超出大小限制，已截断：{{ [detail.input_truncated ? '输入' : '', detail.output_truncated ? '输出' : ''].filter(Boolean).join('、') }}。</p>
      <section class="content"><h4>{{ detail.operation_type === 'tool' ? '调用参数' : '输入' }}</h4>
        <template v-if="messages.length"><div v-for="(message, index) in messages" :key="index" class="message"><span>{{ message.role }}</span><pre>{{ pretty(message.content) }}</pre></div></template><pre v-else>{{ pretty(detail.input_data) }}</pre>
      </section>
      <section class="content"><h4>{{ detail.operation_type === 'tool' ? '工具结果' : '输出' }}</h4><pre>{{ pretty(detail.output_data) }}</pre></section>
      <section v-if="detail.error" class="content error"><h4>错误记录</h4><pre>{{ pretty(detail.error) }}</pre></section>
      <details><summary>原始记录 JSON</summary><pre>{{ pretty(detail) }}</pre></details>
      <p class="footnote">来自已保存并脱敏的记录。未采集或截断的内容无法还原。</p>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { SpanDetail } from '@/types'
import { stepName } from '@/services/api'
import { spanState, spanTime, timingIssue, tokenText, tokenTotal } from '@/services/observationView'
const props = defineProps<{ detail: SpanDetail | null; spanId: string; loading: boolean; error: string; active: boolean; now: number }>()
defineEmits<{ retry: [] }>()
const pretty = (value: unknown) => value == null ? '无可用内容' : typeof value === 'string' ? value : JSON.stringify(value, null, 2)
const date = (value: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : '未知'
const input = computed(() => props.detail?.input_data && typeof props.detail.input_data === 'object' ? props.detail.input_data as Record<string, unknown> : null)
const model = computed(() => typeof input.value?.model === 'string' ? input.value.model : '未记录')
const messages = computed(() => {
  const raw = input.value?.messages
  return Array.isArray(raw) && raw.every(m => m && typeof m === 'object' && typeof m.role === 'string' && 'content' in m) ? raw as { role: string; content: unknown }[] : []
})
</script>
<style scoped>
.details-panel{min-width:0;overflow:hidden}.heading{display:flex;justify-content:space-between;align-items:center;padding:17px 18px;border-bottom:1px solid #e8edf1}.heading h3{font-size:13px;margin:0}.heading button,.error button{border:1px solid #dce4e9;background:white;border-radius:5px;padding:4px 9px;color:#54716b;cursor:pointer;font-size:11px}.identity{padding:20px 18px 16px;border-bottom:1px solid #edf0f3}.kind{font-size:10px;color:#279d7c;text-transform:uppercase;letter-spacing:1px}.identity h4{font-size:16px;margin:8px 0}.identity p{font-size:10px;color:#8691a1;overflow-wrap:anywhere}.identity b{font-size:12px;font-weight:500;margin-left:12px}.state{font-size:11px;padding:3px 7px;border-radius:4px;background:#f0f3f6;color:#66758b}.state.succeeded{background:#e9f5ef;color:#228365}.state.failed,.state.interrupted{background:#fff0ed;color:#bc655e}.facts{display:grid;grid-template-columns:85px minmax(0,1fr);gap:9px 7px;padding:16px 18px;margin:0;font-size:11px}.facts dt{color:#8691a1}.facts dd{margin:0;overflow-wrap:anywhere;color:#425169}.content{padding:0 18px 10px}.content h4{font-size:12px;margin:13px 0 8px}.content pre,details pre{font-family:ui-monospace,SFMono-Regular,monospace;font-size:11px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;border:1px solid #edf0f3;border-radius:6px;padding:11px;max-height:320px;overflow:auto;margin:0}.message{margin-bottom:10px}.message>span{font-size:10px;color:#7c8ba0;display:block;margin-bottom:5px}.warning,.error{color:#aa6841;background:#fff7ec;padding:10px 18px;font-size:12px}.error{overflow-wrap:anywhere}.error button{display:block;margin-top:10px}.placeholder{font-size:12px;padding:40px 20px;text-align:center;color:#8b97a5}.warning{margin:0 18px 12px;border-radius:5px}details{padding:10px 18px;font-size:11px;color:#7d899a}summary{cursor:pointer;margin-bottom:10px}.footnote{padding:5px 18px 15px;font-size:10px;color:#97a1ae}button:focus-visible,summary:focus-visible{outline:2px solid #21866b;outline-offset:2px}
</style>
