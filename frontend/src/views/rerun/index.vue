<template>
  <section class="page" data-module="rerun">
    <header class="page-head">
      <div>
        <h2>异常重跑管理</h2>
        <p class="page-desc">重跑范围来自原始运行的唯一终态记录；运行终态与吞吐量变化时清单随运行实例一起重算。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="toggleCreate">登记重跑任务</button>
        <button class="btn" type="button" @click="exportRows">导出异常重跑清单</button>
      </div>
    </header>

    <form v-if="createVisible" class="filter-bar create-bar" @submit.prevent="submitCreate">
      <label class="filter-item">
        <span>原始运行编号</span>
        <input v-model="createValues.原始运行编号" placeholder="请输入已结束的运行编号" />
      </label>
      <label class="filter-item">
        <span>重跑区间开始</span>
        <input v-model="createValues.重跑区间开始" placeholder="留空则按运行开始时间" />
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
        <span>任务编号 / 原始运行编号</span>
        <input v-model="keyword" placeholder="按任务编号或原始运行编号检索" />
      </label>
      <label class="filter-item">
        <span>重跑状态</span>
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
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
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
            <span v-if="!availableActions(row).length" class="muted-text">已结束</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无异常重跑数据，可先登记重跑任务</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <strong>重跑详情 · {{ detail.任务编号 }}</strong>
        <button class="link" type="button" @click="detail = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <template v-for="field in detailFields" :key="field">
          <dt>{{ field }}</dt>
          <dd>{{ display(detail, field) }}</dd>
        </template>
      </dl>
      <div v-if="detail.终态记录" class="terminal-block">
        <span class="detail-subtitle">原始运行不可变终态数据记录</span>
        <dl class="detail-grid">
          <template v-for="field in terminalFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ display(detail.终态记录 as Dict, field) }}</dd>
          </template>
        </dl>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条异常重跑记录</span>
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

const ENDPOINT = '/api/rerun'
const columns = ["任务编号", "原始运行编号", "重跑区间开始", "重跑区间结束", "重跑原因", "覆盖策略", "依赖级联", "重跑状态"]
const statuses = ["草稿", "重跑审批中", "执行中", "已完成", "已取消"]
const STATUS_ACTIONS: Record<string, string[]> = {
  草稿: ['提交重跑审批', '取消重跑'],
  重跑审批中: ['启动重跑'],
  执行中: [],
  已完成: [],
  已取消: [],
}
const detailFields = [
  "任务编号",
  "原始运行编号",
  "原始运行终态",
  "重跑区间开始",
  "重跑区间结束",
  "重跑原因",
  "覆盖策略",
  "依赖级联",
  "重跑状态",
]
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

const stats = computed(() => [
  { label: '当前页任务数', value: rows.value.length },
  { label: '执行中任务', value: rows.value.filter((row) => row.status === '执行中').length },
  { label: '失败终态来源', value: rows.value.filter((row) => row.原始运行终态 === '失败').length },
])

function display(row: Dict, field: string): string {
  const value = row[field]
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
      throw new Error(result.message || '重跑任务登记失败')
    }
    noticeMessage.value = result.message
    createVisible.value = false
    createValues.value = {}
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重跑任务登记失败'
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
      throw new Error(result.message || '异常重跑动作未生效')
    }
    noticeMessage.value = result.message
    await reload()
    if (detail.value && Number(detail.value.id) === Number(row.id)) {
      await openDetail(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常重跑操作失败'
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('重跑详情读取失败')
    }
    detail.value = (await response.json()) as Dict
  } catch (error) {
    detail.value = null
    errorMessage.value = error instanceof Error ? error.message : '重跑详情读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams()
  if (keyword.value.trim()) query.set('keyword', keyword.value.trim())
  if (statusFilter.value) query.set('status', statusFilter.value)
  try {
    const response = await request(`${ENDPOINT}?${query.toString()}`)
    if (!response.ok) {
      throw new Error('重跑任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常重跑列表读取失败'
  }
}

onMounted(reload)
</script>
