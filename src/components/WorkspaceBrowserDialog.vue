<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  ArrowLeft,
  ArrowUp,
  Check,
  ChevronRight,
  Folder,
  FolderPlus,
  Pencil,
  Pin,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Input from '@/components/ui/Input.vue'
import {
  createFolder,
  deleteFolder,
  addShortcut,
  getDrives,
  getShortcuts,
  listDirectories,
  removeShortcut,
  renameFolder,
  type DirectoryListing,
  type FileSystemDrive,
  type FileSystemFolder,
  type FileSystemShortcut,
} from '@/api/filesystem'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  close: []
  select: [path: string]
}>()

const dialog = ref<HTMLElement | null>(null)
const drives = ref<FileSystemDrive[]>([])
const shortcuts = ref<FileSystemShortcut[]>([])
const listing = ref<DirectoryListing | null>(null)
const selected = ref<FileSystemFolder | null>(null)
const loading = ref(false)
const actionBusy = ref(false)
const error = ref('')
const backPaths = ref<string[]>([])
const forwardPaths = ref<string[]>([])
const action = ref<'new' | 'rename' | 'delete' | null>(null)
const actionName = ref('')
const lastFocused = ref<HTMLElement | null>(null)
const shortcutBusy = ref(false)

const breadcrumbs = computed(() => {
  const path = listing.value?.path || ''
  if (!path) return []
  const normalized = path.replace(/\//g, '\\')
  const match = normalized.match(/^([A-Za-z]:\\|\\\\[^\\]+\\[^\\]+\\|\\)/)
  const root = match?.[1] || ''
  const rest = normalized.slice(root.length).split('\\').filter(Boolean)
  const items = root
    ? [{ label: root.endsWith('\\') ? root.slice(0, -1) : root, path: root }]
    : []
  let current = root
  for (const segment of rest) {
    current = current ? `${current.replace(/[\\/]$/, '')}\\${segment}` : segment
    items.push({ label: segment, path: current })
  }
  return items
})

const canGoBack = computed(() => backPaths.value.length > 0)
const canGoForward = computed(() => forwardPaths.value.length > 0)
const canGoUp = computed(() => !!listing.value?.parent_path)
const actionTitle = computed(() => action.value === 'new' ? '新建文件夹' : action.value === 'rename' ? '重命名文件夹' : '删除文件夹')
const targetPath = computed(() => selected.value?.path || listing.value?.path || '')
const targetShortcut = computed(() => {
  const target = targetPath.value
  return target ? shortcuts.value.find((item) => item.path.toLocaleLowerCase() === target.toLocaleLowerCase()) : undefined
})

function clearState() {
  drives.value = []
  listing.value = null
  selected.value = null
  error.value = ''
  backPaths.value = []
  forwardPaths.value = []
  action.value = null
  actionName.value = ''
  shortcutBusy.value = false
}

async function loadDrives() {
  loading.value = true
  error.value = ''
  try {
    drives.value = (await getDrives()).drives
  } catch (err: any) {
    error.value = err?.message || '无法读取系统磁盘列表'
  } finally {
    loading.value = false
  }
}

async function loadShortcuts() {
  try {
    shortcuts.value = (await getShortcuts()).shortcuts
  } catch (err: any) {
    error.value = err?.message || '无法读取快捷入口'
  }
}

async function loadPath(path: string, mode: 'push' | 'back' | 'forward' | 'replace' = 'push') {
  if (loading.value || !path) return
  const previous = listing.value?.path
  if (mode === 'push' && previous && previous !== path) {
    backPaths.value.push(previous)
    forwardPaths.value = []
  } else if (mode === 'back' && previous) {
    forwardPaths.value.push(previous)
  } else if (mode === 'forward' && previous) {
    backPaths.value.push(previous)
  }
  loading.value = true
  error.value = ''
  selected.value = null
  try {
    listing.value = await listDirectories(path)
  } catch (err: any) {
    error.value = err?.message || '无法读取此目录'
    if (mode !== 'replace') {
      if (mode === 'push') backPaths.value.pop()
      if (mode === 'back') forwardPaths.value.pop()
      if (mode === 'forward') backPaths.value.pop()
    }
  } finally {
    loading.value = false
  }
}

function openDrive(path: string) { void loadPath(path) }
function goBack() {
  const path = backPaths.value.pop()
  if (path) void loadPath(path, 'back')
}
function goForward() {
  const path = forwardPaths.value.pop()
  if (path) void loadPath(path, 'forward')
}
function goUp() {
  if (listing.value?.parent_path) void loadPath(listing.value.parent_path)
}
function refresh() {
  if (listing.value?.path) void loadPath(listing.value.path, 'replace')
  else void loadDrives()
  void loadShortcuts()
}
function selectFolder(folder: FileSystemFolder) {
  selected.value = folder
}
function enterFolder(folder = selected.value) {
  if (folder) void loadPath(folder.path)
}
function chooseCurrent() {
  if (targetPath.value && !loading.value) emit('select', targetPath.value)
}
async function mountCurrent() {
  const target = targetPath.value
  if (!target || shortcutBusy.value || targetShortcut.value) return
  shortcutBusy.value = true
  error.value = ''
  try {
    await addShortcut(target)
    await loadShortcuts()
  } catch (err: any) {
    error.value = err?.message || '加入快捷入口失败'
  } finally {
    shortcutBusy.value = false
  }
}
async function unmountShortcut(path: string) {
  if (shortcutBusy.value) return
  shortcutBusy.value = true
  error.value = ''
  try {
    shortcuts.value = (await removeShortcut(path)).shortcuts
  } catch (err: any) {
    error.value = err?.message || '移除快捷入口失败'
  } finally {
    shortcutBusy.value = false
  }
}
function startAction(kind: 'new' | 'rename' | 'delete') {
  if (kind !== 'new' && (!selected.value || (kind === 'rename' ? !selected.value.can_rename : !selected.value.can_delete))) return
  action.value = kind
  actionName.value = kind === 'rename' ? selected.value?.name || '' : ''
  error.value = ''
  void nextTick(() => document.getElementById('workspace-action-name')?.focus())
}
function cancelAction() {
  if (!actionBusy.value) {
    action.value = null
    actionName.value = ''
  }
}
async function submitAction() {
  if (!listing.value || actionBusy.value) return
  if (action.value === 'delete') {
    if (!selected.value) return
    actionBusy.value = true
    try {
      await deleteFolder(selected.value.path)
      action.value = null
      selected.value = null
      await loadPath(listing.value.path, 'replace')
    } catch (err: any) {
      error.value = err?.message || '删除文件夹失败'
    } finally {
      actionBusy.value = false
    }
    return
  }
  const name = actionName.value.trim()
  if (!name) return
  actionBusy.value = true
  try {
    if (action.value === 'new') await createFolder(listing.value.path, name)
    else if (action.value === 'rename' && selected.value) await renameFolder(selected.value.path, name)
    action.value = null
    actionName.value = ''
    await loadPath(listing.value.path, 'replace')
  } catch (err: any) {
    error.value = err?.message || '文件夹操作失败'
  } finally {
    actionBusy.value = false
  }
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    if (action.value) cancelAction()
    else emit('close')
  } else if (event.key === 'Enter' && !action.value && selected.value) {
    event.preventDefault()
    enterFolder()
  } else if (event.key === 'Tab' && dialog.value) {
    const focusable = [...dialog.value.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled])')]
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus()
    }
  }
}

watch(() => props.open, (open) => {
  if (open) {
    lastFocused.value = document.activeElement as HTMLElement
    clearState()
    void loadDrives()
    void loadShortcuts()
    void nextTick(() => dialog.value?.focus())
  } else {
    lastFocused.value?.focus?.()
  }
})
</script>

<template>
  <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4" @keydown="onKeydown">
    <section
      ref="dialog"
      class="relative flex h-[min(720px,calc(100vh-2rem))] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-2xl focus:outline-none"
      role="dialog"
      aria-modal="true"
      aria-labelledby="workspace-browser-title"
      tabindex="-1"
    >
      <header class="flex items-center justify-between gap-3 border-b border-border px-5 py-4">
        <div>
          <h2 id="workspace-browser-title" class="flex items-center gap-2 text-lg font-semibold"><Folder class="h-5 w-5 text-primary" aria-hidden="true" />选择工作空间</h2>
          <p class="mt-1 text-xs text-muted-foreground">浏览服务器上的文件夹；不会读取或上传文件。</p>
        </div>
        <Button variant="ghost" size="icon" aria-label="关闭文件夹选择器" @click="emit('close')"><X class="h-5 w-5" aria-hidden="true" /></Button>
      </header>

      <div class="flex min-h-0 flex-1 flex-col md:flex-row">
        <aside class="max-h-40 min-h-0 w-full shrink-0 overflow-y-auto border-b border-border bg-muted/20 p-3 md:max-h-none md:w-52 md:border-b-0 md:border-r">
          <div class="mb-2 px-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">磁盘 / 挂载点</div>
          <div v-if="!drives.length && loading" class="px-2 py-3 text-sm text-muted-foreground">读取中…</div>
          <button
            v-for="drive in drives"
            :key="drive.path"
            type="button"
            class="flex min-h-11 w-full items-center gap-2 rounded-lg px-3 text-left text-sm transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :class="listing?.path === drive.path ? 'bg-accent text-accent-foreground' : 'text-foreground'"
            @click="openDrive(drive.path)"
          >
            <Folder class="h-4 w-4 text-primary" aria-hidden="true" />
            <span class="min-w-0 truncate">{{ drive.name }}</span>
            <span class="ml-auto text-[11px] text-muted-foreground">{{ drive.kind }}</span>
          </button>
          <p v-if="!loading && !drives.length" class="px-2 py-3 text-sm text-muted-foreground">没有发现可用磁盘。</p>
          <div class="mt-4 border-t border-border pt-3">
            <div class="mb-2 flex items-center justify-between gap-2 px-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              <span>快捷入口</span><Pin class="h-3.5 w-3.5" aria-hidden="true" />
            </div>
            <p v-if="!shortcuts.length" class="px-2 py-2 text-xs text-muted-foreground">浏览到目录后，可从底部挂载。</p>
            <div v-for="shortcut in shortcuts" :key="shortcut.path" class="group flex items-center gap-1 rounded-lg hover:bg-accent">
              <button type="button" class="flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-lg px-3 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" :disabled="loading" @click="loadPath(shortcut.path)">
                <Pin class="h-4 w-4 shrink-0 text-primary" aria-hidden="true" />
                <span class="min-w-0 flex-1">
                  <span class="block truncate">{{ shortcut.display_name }}</span>
                  <span class="block truncate font-mono text-[10px] text-muted-foreground">{{ shortcut.path }}</span>
                </span>
                <span v-if="!shortcut.exists" class="shrink-0 text-[10px] text-destructive">不存在</span>
              </button>
              <Button variant="ghost" size="icon" class="mr-1 h-8 w-8 shrink-0 opacity-70 group-hover:opacity-100" :aria-label="`移除快捷入口 ${shortcut.display_name}`" :disabled="shortcutBusy" @click.stop="unmountShortcut(shortcut.path)"><X class="h-4 w-4" aria-hidden="true" /></Button>
            </div>
          </div>
        </aside>

        <div class="flex min-h-0 flex-1 flex-col">
          <div class="flex flex-wrap items-center gap-1 border-b border-border px-3 py-2">
            <Button variant="ghost" size="icon" aria-label="返回上一位置" :disabled="!canGoBack || loading" @click="goBack"><ArrowLeft class="h-4 w-4" aria-hidden="true" /></Button>
            <Button variant="ghost" size="icon" aria-label="前进到下一位置" :disabled="!canGoForward || loading" @click="goForward"><ChevronRight class="h-4 w-4" aria-hidden="true" /></Button>
            <Button variant="ghost" size="icon" aria-label="返回上一级目录" :disabled="!canGoUp || loading" @click="goUp"><ArrowUp class="h-4 w-4" aria-hidden="true" /></Button>
            <Button variant="ghost" size="icon" aria-label="刷新目录" :disabled="loading" @click="refresh"><RefreshCw class="h-4 w-4" :class="loading ? 'animate-spin' : ''" aria-hidden="true" /></Button>
            <nav v-if="breadcrumbs.length" class="ml-1 flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto text-[15px]" aria-label="当前路径">
              <template v-for="(crumb, index) in breadcrumbs" :key="crumb.path">
                <ChevronRight v-if="index" class="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                <button type="button" class="max-w-48 shrink-0 truncate rounded px-1.5 py-1.5 font-medium tracking-tight text-foreground hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" :class="index === breadcrumbs.length - 1 ? 'font-semibold' : 'text-muted-foreground'" @click="loadPath(crumb.path)">{{ crumb.label }}</button>
              </template>
            </nav>
            <span v-else class="flex-1 px-2 text-sm text-muted-foreground">请选择一个磁盘</span>
          </div>

          <div class="min-h-0 flex-1 overflow-y-auto p-3">
            <div v-if="error" class="mb-3 flex items-start justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
              <span>{{ error }}</span><button type="button" class="font-semibold underline" @click="error = ''">关闭</button>
            </div>
            <div v-if="!listing && !loading" class="flex h-full min-h-48 items-center justify-center rounded-xl border border-dashed border-border text-sm text-muted-foreground">从左侧选择磁盘开始浏览</div>
            <div v-else-if="loading" class="flex h-full min-h-48 items-center justify-center text-sm text-muted-foreground">读取目录中…</div>
            <div v-else-if="listing && !listing.folders.length" class="flex h-full min-h-48 items-center justify-center rounded-xl border border-dashed border-border text-sm text-muted-foreground">此目录没有子文件夹</div>
            <div v-else class="grid grid-cols-1 gap-1 sm:grid-cols-2">
              <button
                v-for="folder in listing?.folders"
                :key="folder.path"
                type="button"
                class="group flex min-h-12 items-center gap-3 rounded-lg border border-transparent px-3 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :class="selected?.path === folder.path ? 'border-primary/40 bg-primary/10 text-foreground' : 'text-foreground'"
                @click="selectFolder(folder)"
                @dblclick="enterFolder(folder)"
              >
                <Folder class="h-5 w-5 shrink-0 text-primary" aria-hidden="true" />
                <span class="min-w-0 flex-1 truncate">{{ folder.name }}</span>
                <span v-if="!folder.can_delete" class="text-[11px] text-muted-foreground">受保护</span>
              </button>
            </div>
          </div>

          <div class="flex flex-wrap items-center gap-2 border-t border-border px-4 py-3">
            <div class="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground" :title="listing?.path || ''">{{ listing?.path || '未选择目录' }}</div>
            <Button variant="outline" size="sm" :disabled="!listing?.can_create || loading" @click="startAction('new')"><FolderPlus class="h-4 w-4" aria-hidden="true" />新建</Button>
            <Button variant="outline" size="sm" :disabled="!selected?.can_rename || loading" @click="startAction('rename')"><Pencil class="h-4 w-4" aria-hidden="true" />改名</Button>
            <Button variant="outline" size="sm" :disabled="!selected?.can_delete || loading" @click="startAction('delete')"><Trash2 class="h-4 w-4" aria-hidden="true" />删除</Button>
            <Button variant="outline" size="sm" :disabled="!targetPath || !!targetShortcut || shortcutBusy || loading" @click="mountCurrent"><Pin class="h-4 w-4" aria-hidden="true" />{{ targetShortcut ? '已加入快捷入口' : '加入快捷入口' }}</Button>
            <Button size="sm" :disabled="!targetPath || loading" @click="chooseCurrent"><Check class="h-4 w-4" aria-hidden="true" />选择此文件夹</Button>
          </div>
        </div>
      </div>

      <div v-if="action" class="absolute inset-0 z-10 flex items-center justify-center rounded-2xl bg-black/35 p-4">
        <div class="w-full max-w-md rounded-xl border border-border bg-card p-5 shadow-xl" role="alertdialog" aria-modal="true" aria-labelledby="workspace-action-title">
          <h3 id="workspace-action-title" class="text-base font-semibold">{{ actionTitle }}</h3>
          <p v-if="action === 'delete'" class="mt-2 text-sm text-muted-foreground">确定永久删除“{{ selected?.name }}”及其中全部内容吗？此操作不可撤销。</p>
          <Input v-else id="workspace-action-name" v-model="actionName" class="mt-4" :placeholder="action === 'new' ? '文件夹名称' : '新的文件夹名称'" :disabled="actionBusy" @keyup.enter="submitAction" />
          <div class="mt-5 flex justify-end gap-2">
            <Button variant="outline" :disabled="actionBusy" @click="cancelAction">取消</Button>
            <Button :variant="action === 'delete' ? 'destructive' : 'default'" :disabled="actionBusy || (action !== 'delete' && !actionName.trim())" @click="submitAction">{{ actionBusy ? '处理中…' : action === 'delete' ? '永久删除' : '确定' }}</Button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>
