<template>
  <section class="page" data-module="runtime">
    <header class="page-head">
      <div>
        <h2>运行实例管理</h2>
        <p class="page-desc">记录每次ETL作业的运行实例；结束后只落一条不可变终态，终态、结束时间与吞吐量一起生效。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="toggleCreate">登记运行记录</button>
        <button class="btn" type="button" @click="exportRows">导出运行实例清单</button>
      </div>
    </header>

    <form v-if="createVisible" class="filter-bar create-bar" @submit.prevent="submitCreate">
      <label v-for="field in createFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="createValues[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit">提交登记</button>
      <button class="btn ghost" type="button" @click="toggleCreate">取消</button>
    </form>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>记录编号 / 作业名称</span>
        <input v-model="keyword" placeholder="按记录编号或作业名称检索" />
      </label>
      <label class="filter-item">
        <span>执行状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ display(row[column]) }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">详情</button>
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!availableActions(row).length && row.status !== '运行中'" class="muted-text">已结束</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无运行实例数据，可先登记运行记录</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <strong>运行详情 · {{ detail.记录编号 }}</strong>
        <button class="link" type="button" @click="closeDetail">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd>{{ display(detail[field]) }}</dd>
        </template>
      </dl>

      <div v-if="detail.终态记录" class="terminal-block">
        <span class="detail-subtitle">不可变运行终态数据记录</span>
        <dl class="detail-grid">
          <template v-for="field in terminalFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ display((detail.终态记录 as Dict)[field]) }}</dd>
          </template>
        </dl>
      </div>

      <form
        v-if="detail.status === '运行中'"
        class="filter-bar throughput-bar"
        @submit.prevent="submitThroughput"
      >
        <label class="filter-item">
          <span>读取行数</span>
          <input v-model="throughput.读取行数" inputmode="numeric" placeholder="运行中的吞吐量" />
        </label>
        <label class="filter-item">
          <span>写入行数</span>
          <input v-model="throughput.写入行数" inputmode="numeric" placeholder="运行中的吞吐量" />
        </label>
        <button class="btn" type="submit">更新吞吐量</button>
        <span class="muted-text">结束动作会锁定吞吐量，迟到更新不再写入</span>
      </form>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条运行实例记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Dict = Record<string, unknown>
type Row = Dict & { id: number; status?: string }

const ENDPOINT = '/api/runtime'
const columns = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "读取行数", "写入行数", "错误信息"]
const statuses = ["草稿", "运行中", "成功", "失败", "已终止"]
const STATUS_ACTIONS: Record<string, string[]> = {
  草稿: ['启动作业'],
  运行中: ['标记成功', '标记失败', '终止作业'],
  成功: [],
  失败: [],
  已终止: [],
}
const createFields = ["记录编号", "作业名称", "开始时间"]
const detailFields = ["记录编号", "作业名称", "开始时间", "结束时间", "执行状态", "读取行数", "写入行数", "错误信息"]
const terminalFields = ["终态", "读取行数", "写入行数", "结束时间"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const keyword = ref('')
const statusFilter = ref('')
const createVisible = ref(false)
const createValues = ref<Record<string, string>>({})
const detail = ref<Dict | null>(null)
const throughput = ref<Record<string, string>>({})

const stats = computed(() => [
  { label: '当前页记录数', value: rows.value.length },
  { label: '运行中记录', value: rows.value.filter((row) => row.status === '运行中').length },
  {
    label: '失败或终止记录',
    value: rows.value.filter((row) => row.status === '失败' || row.status === '已终止').length,
  },
])

function display(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function availableActions(row: Row): string[] {
  return STATUS_ACTIONS[String(row.status ?? '')] ?? []
}

function toggleCreate() {
  createVisible.value = !createVisible.value
  createValues.value = {}
}

function resetFilters() {
  keyword.value = ''
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function readResult(response: Response): Promise<{ ok: boolean; message: string; entry?: Dict }> {
  const payload = (await response.json().catch(() => ({}))) as { ok?: boolean; message?: string; entry?: Dict }
  if (!response.ok) {
    throw new Error(payload.message || `接口返回 ${response.status}，操作未生效`)
  }
  return { ok: payload.ok !== false, message: payload.message ?? '', entry: payload.entry }
}

async function submitCreate() {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await readResult(
      await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }),
    )
    if (!result.ok) {
      throw new Error(result.message || '运行记录登记失败')
    }
    noticeMessage.value = result.message
    createVisible.value = false
    createValues.value = {}
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行记录登记失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const result = await readResult(
      await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      }),
    )
    if (!result.ok) {
      throw new Error(result.message || '运行实例动作未生效')
    }
    noticeMessage.value = result.message
    await reload()
    if (detail.value && Number(detail.value.id) === Number(row.id)) {
      await openDetail(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例操作失败'
  }
}

async function submitThroughput() {
  if (!detail.value) return
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const values: Record<string, string> = { action: '更新吞吐量' }
    if (throughput.value.读取行数) values.读取行数 = throughput.value.读取行数
    if (throughput.value.写入行数) values.写入行数 = throughput.value.写入行数
    const result = await readResult(
      await request(`${ENDPOINT}/${detail.value.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values }),
      }),
    )
    if (!result.ok) {
      throw new Error(result.message || '吞吐量未更新')
    }
    noticeMessage.value = result.message
    await reload()
    await openDetail({ id: Number(detail.value.id) } as Row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '吞吐量更新失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  throughput.value = {}
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('运行详情读取失败')
    }
    detail.value = (await response.json()) as Dict
  } catch (error) {
    detail.value = null
    errorMessage.value = error instanceof Error ? error.message : '运行详情读取失败'
  }
}

function closeDetail() {
  detail.value = null
  throughput.value = {}
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value.trim()) query.set('keyword', keyword.value.trim())
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('运行记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例列表读取失败'
  }
}

onMounted(reload)
</script>
