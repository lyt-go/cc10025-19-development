<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="isColdStorage" class="verify-panel" data-testid="verify-panel">
      <header class="verify-head">
        <div>
          <h3>分层执行核验</h3>
          <p class="page-desc">
            同一资产多次切换分层策略时，按策略生效编号定位真实执行分区；
            来源不明的任务不会出现在可核验对象里。
          </p>
        </div>
        <button class="btn" data-testid="verify-query" type="button" @click="reloadCandidates">
          刷新可核验对象
        </button>
      </header>

      <form class="filter-bar" @submit.prevent="reloadCandidates">
        <label class="filter-item">
          <span>资产编号</span>
          <input v-model="candidateFilters.asset_id" data-testid="verify-filter-asset" placeholder="按资产编号检索" />
        </label>
        <label class="filter-item">
          <span>策略生效编号</span>
          <input v-model="candidateFilters.activation_id" data-testid="verify-filter-activation" placeholder="按策略生效编号检索" />
        </label>
        <label class="filter-item">
          <span>来源策略编号</span>
          <input v-model="candidateFilters.policy_number" data-testid="verify-filter-policy" placeholder="按策略编号检索" />
        </label>
        <button class="btn primary" data-testid="verify-candidates-query" type="submit">筛选</button>
        <button class="btn ghost" type="button" @click="resetCandidateFilters">重置条件</button>
      </form>

      <table class="data-table" data-testid="verify-table">
        <thead>
          <tr>
            <th>任务编号</th>
            <th>资产编号</th>
            <th>策略生效编号</th>
            <th>来源策略编号</th>
            <th>执行分区</th>
            <th>核验状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in candidateRows" :key="`verify-${row.id}`" :data-testid="`verify-row-${row.id}`">
            <td>{{ row['任务编号'] ?? '—' }}</td>
            <td>{{ row['源资产'] ?? '—' }}</td>
            <td>{{ row['策略生效编号'] || '—' }}</td>
            <td>{{ row['来源策略编号'] || '—' }}</td>
            <td>{{ row['执行分区'] || '—' }}</td>
            <td>{{ row['核验状态'] ?? '未核验' }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`verify-submit-${row.id}`"
                @click="submitVerification(row)"
              >
                提交核验
              </button>
            </td>
          </tr>
          <tr v-if="!candidateRows.length">
            <td colspan="7" class="empty-state">
              本轮没有可提交核验的冷存任务：来源不明或已提交核验的任务不会进入候选列表
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in createFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listColdStorageCandidates,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isColdStorage = computed(() => meta.key === 'cold_storage')
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const createFields = meta.createFields ?? meta.fields
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const candidateRows = ref<EntryRow[]>([])
const candidateFilters = ref({ asset_id: '', activation_id: '', policy_number: '' })
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(createFields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
    if (isColdStorage.value) await reloadCandidates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function submitVerification(row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), '提交核验')
    if (!result.ok) throw new Error(result.message)
    await reload()
    await reloadCandidates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交核验失败'
  }
}

function resetCandidateFilters() {
  candidateFilters.value = { asset_id: '', activation_id: '', policy_number: '' }
  void reloadCandidates()
}

async function reloadCandidates() {
  if (!isColdStorage.value) return
  try {
    const payload = await listColdStorageCandidates(candidateFilters.value)
    candidateRows.value = payload.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可核验对象读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (isColdStorage.value) await reloadCandidates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
