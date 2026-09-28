<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import Sidebar from '@/components/sidebar/Sidebar.vue'
import { useProjectStore } from '@/stores/project'
import { useAuthStore } from '@/stores/auth'

const project = useProjectStore()
const auth = useAuthStore()
const route = useRoute()
// 工作台型页面（整章预览）请求全宽容器：内容撑满 app-main，不再受 1440px 居中约束。
const fullBleed = computed(() => route.meta.fullBleed === true)
const projectScope = computed(() => `${auth.user?.id || 'guest'}:${project.activeProjectId}`)

// 跨标签页失鲜自愈：另一标签可能已切换/删除活动项目（PR 评审 #1），本标签回到
// 前台时重新解析一次，让 keep-alive 作用域、侧边栏与任务过滤跟服务端对齐。
// refresh() 内部合流，与页面 onMounted 的 refresh 重叠时不会重复发请求。
function onVisibilityChange() {
  if (document.visibilityState === 'visible' && project.loaded && project.current) {
    void project.refresh()
  }
}

onMounted(() => {
  if (!project.loaded) void project.refresh()
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => document.removeEventListener('visibilitychange', onVisibilityChange))
</script>

<template>
  <div class="app-shell">
    <Sidebar />
    <main id="main-content" class="app-main">
      <div class="app-content" :class="fullBleed && 'app-content--full'">
        <!-- keep-alive: each module's inputs/toggles/preview survive navigation -->
        <router-view v-slot="{ Component }">
          <keep-alive :key="projectScope">
            <component :is="Component" />
          </keep-alive>
        </router-view>
      </div>
    </main>
  </div>
</template>
