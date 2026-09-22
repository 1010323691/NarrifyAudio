<script setup lang="ts">
/**
 * List-based single-select directory picker. Scans a workspace pipeline directory
 * (``GET /api/files/list/{module}``) and renders its entries as a selectable list —
 * the same style as the 文本解析 file list — replacing the old dropdown / native
 * file-dialog inputs on the downstream pipeline pages.
 *
 * One entry per selection (each downstream stage takes a single input). ``modelValue``
 * is the chosen entry's name; ``''`` means "let the backend pick the default / most
 * recent" (offered as the leading row when ``showDefault`` is on). ``pickDirs`` lists
 * sub-folders instead of files (音频合并 picks an audio package). After each scan,
 * ``scanned`` fires with the raw ``DirListResult`` so a caller can read the
 * directory's absolute ``path``.
 */
import { onMounted, ref, watch } from 'vue'
import { listDir } from '@/api/files'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import { formatBytes } from '@/utils/format'
import type { DirListResult, FileItem } from '@/types'
import Label from '@/components/ui/Label.vue'
import Button from '@/components/ui/Button.vue'
import { RefreshCw, Folder } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    /** The selected entry's name; ``''`` = default / most recent. */
    modelValue?: string
    /** Workspace pipeline directory to scan (e.g. ``03_parsed_json``). */
    module: string
    /** Field label. */
    label?: string
    /** Restrict the file list to these extensions (without dots); ignored for dirs. */
    extensions?: string[]
    /** Hide files whose name ends with this suffix (e.g. ``_checked.json``). */
    excludeSuffix?: string
    /** List sub-folders instead of files (音频合并's package list). */
    pickDirs?: boolean
    /** Offer a leading "（最近/默认）" row (modelValue ``''``). */
    showDefault?: boolean
    defaultLabel?: string
    /** Empty-state message; defaults to a per-module hint. */
    emptyHint?: string
    /** Offer an "全部文件" row selecting ``allValue`` (角色配音's whole-book "all files"). */
    showAll?: boolean
    /** The modelValue the "全部文件" row selects (the backend "all" sentinel). */
    allValue?: string
    /** Label of the "全部文件" row. */
    allLabel?: string
  }>(),
  {
    modelValue: '',
    label: '',
    extensions: () => [],
    excludeSuffix: '',
    pickDirs: false,
    showDefault: true,
    defaultLabel: '（最近/默认）',
    emptyHint: '',
    showAll: false,
    allValue: '__all__',
    allLabel: '全部文件',
  },
)

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
  (e: 'scanned', r: DirListResult): void
}>()

const { workspaceSet } = useWorkspaceGate()

const entries = ref<FileItem[]>([])
const loading = ref(false)

function matches(i: FileItem): boolean {
  if (props.pickDirs) return i.is_dir
  if (i.is_dir) return false
  if (props.extensions.length) {
    const low = i.name.toLowerCase()
    if (!props.extensions.some((e) => low.endsWith('.' + e.toLowerCase()))) return false
  }
  if (props.excludeSuffix && i.name.toLowerCase().endsWith(props.excludeSuffix.toLowerCase())) return false
  return true
}

async function scan() {
  if (!workspaceSet.value) {
    entries.value = []
    return
  }
  loading.value = true
  try {
    const r = await listDir(props.module)
    entries.value = r.items.filter(matches)
    emit('scanned', r)
  } catch {
    entries.value = []
  } finally {
    loading.value = false
  }
}

function select(name: string) {
  emit('update:modelValue', name)
}

onMounted(scan)
watch(workspaceSet, scan)
watch(() => props.module, scan)
</script>

<template>
  <div v-if="workspaceSet" class="space-y-1.5">
    <Label v-if="label">{{ label }}</Label>
    <div class="flex items-stretch gap-2">
      <div class="max-h-80 min-h-[6rem] flex-1 space-y-1 overflow-y-auto rounded-md border p-2">
        <label
          v-if="showDefault"
          class="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-accent/50"
          :class="{ 'bg-accent/60': modelValue === '' }"
        >
          <input
            type="radio"
            class="h-4 w-4 shrink-0 accent-primary"
            :name="'dir-' + module"
            :checked="modelValue === ''"
            :disabled="loading"
            @change="select('')"
          />
          <span class="min-w-0 flex-1 truncate text-sm text-muted-foreground">{{ defaultLabel }}</span>
        </label>

        <label
          v-if="showAll && entries.length"
          class="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-accent/50"
          :class="{ 'bg-accent/60': modelValue === allValue }"
        >
          <input
            type="radio"
            class="h-4 w-4 shrink-0 accent-primary"
            :name="'dir-' + module"
            :checked="modelValue === allValue"
            :disabled="loading"
            @change="select(allValue)"
          />
          <span class="min-w-0 flex-1 truncate text-sm">{{ allLabel }}（{{ entries.length }} 个文件）</span>
        </label>

        <label
          v-for="f in entries"
          :key="f.name"
          class="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-accent/50"
          :class="{ 'bg-accent/60': modelValue === f.name }"
        >
          <input
            type="radio"
            class="h-4 w-4 shrink-0 accent-primary"
            :name="'dir-' + module"
            :checked="modelValue === f.name"
            :disabled="loading"
            @change="select(f.name)"
          />
          <span class="min-w-0 flex-1 truncate text-sm">{{ f.name }}</span>
          <Folder v-if="f.is_dir" class="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <span v-else class="shrink-0 text-xs text-muted-foreground">{{ formatBytes(f.size) }}</span>
        </label>

        <p v-if="!entries.length" class="px-2 py-1.5 text-sm text-muted-foreground">
          {{ emptyHint || (pickDirs ? `${module}/ 下暂无可选项` : `${module}/ 下暂无文件`) }}
        </p>
      </div>
      <Button variant="outline" size="sm" class="h-8 shrink-0" :disabled="loading" @click="scan">
        <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />刷新
      </Button>
    </div>
  </div>
</template>
