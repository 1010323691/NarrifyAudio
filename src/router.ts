import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import Login from '@/views/Login.vue'
import { useAuthStore } from '@/stores/auth'

// Hash history: works in a plain browser with no server-side route handling.
// All modules live under the persistent MainLayout so the left sidebar stays
// fixed while the workspace swaps.
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'login', component: Login, meta: { title: '登录' } },
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: '', redirect: '/dashboard' },
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/Dashboard.vue'),
          meta: { title: '开始' },
        },
        {
          path: 'text',
          name: 'text',
          component: () => import('@/views/TextFormat.vue'),
          meta: { title: '排版与分册' },
        },
        {
          path: 'script',
          name: 'script',
          component: () => import('@/views/ScriptParse.vue'),
          meta: { title: '文本解析' },
        },
        {
          path: 'voices',
          name: 'voices',
          component: () => import('@/views/Voices.vue'),
          meta: { title: '角色配音' },
        },
        {
          path: 'batch',
          name: 'batch',
          component: () => import('@/views/BatchTTS.vue'),
          meta: { title: '音频合成' },
        },
        {
          path: 'merge',
          name: 'merge',
          component: () => import('@/views/Merge.vue'),
          meta: { title: '音频合并' },
        },
        {
          path: 'audio',
          name: 'audio',
          component: () => import('@/views/AudioSplit.vue'),
          meta: { title: '音频分集' },
        },
        {
          path: 'bgm',
          name: 'bgm',
          component: () => import('@/views/BGM.vue'),
          meta: { title: '背景音乐' },
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/Settings.vue'),
          meta: { title: '设置' },
        },
        {
          path: 'admin',
          name: 'admin',
          component: () => import('@/views/Admin.vue'),
          meta: { title: '管理后台', requiresAdmin: true },
        },
        {
          path: 'music',
          name: 'music',
          component: () => import('@/views/MusicLibrary.vue'),
          meta: { title: '音乐库' },
        },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.load()
  if (to.path === '/login') {
    return auth.isAuthenticated ? '/dashboard' : true
  }
  if (!auth.isAuthenticated) return '/login'
  if (to.meta.requiresAdmin && auth.user?.role !== 'admin') return '/dashboard'
  return true
})

export default router
