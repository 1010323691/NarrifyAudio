import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import AdminLayout from '@/layouts/AdminLayout.vue'
import Login from '@/views/Login.vue'
import AccessDenied from '@/views/AccessDenied.vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { getActiveProject } from '@/api/project'

// The customer workspace and admin console have separate route trees, layouts,
// login portals, and role guards. Neither shell links into the other.
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/login', name: 'user-login', component: Login, meta: { title: '用户登录', portal: 'user' } },
    { path: '/admin/login', name: 'admin-login', component: Login, meta: { title: '管理员登录', portal: 'admin' } },
    { path: '/access-denied', name: 'access-denied', component: AccessDenied, meta: { title: '无权访问' } },
    // Keep old links working after the admin console route was nested under /admin.
    { path: '/music', redirect: '/admin/music' },
    {
      path: '/',
      component: MainLayout,
      meta: { requiresUser: true },
      children: [
        { path: '', redirect: '/dashboard' },
        { path: 'dashboard', name: 'dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '项目' } },
        { path: 'trash', name: 'project-trash', component: () => import('@/views/ProjectTrash.vue'), meta: { title: '回收站' } },
        { path: 'tasks', name: 'task-center', component: () => import('@/views/TaskCenter.vue'), meta: { title: '任务中心' } },
        { path: 'projects/:projectId', name: 'project-overview', component: () => import('@/views/ProjectOverview.vue'), meta: { title: '项目工作台' } },
        { path: 'resources', name: 'resources', component: () => import('@/views/MyResources.vue'), meta: { title: '我的资源' } },
        { path: 'usage', name: 'usage', component: () => import('@/views/Usage.vue'), meta: { title: '使用量' } },
        { path: 'text', name: 'text', component: () => import('@/views/TextFormat.vue'), meta: { title: '排版与分册', projectStage: true } },
        { path: 'script', name: 'script', component: () => import('@/views/ScriptParse.vue'), meta: { title: '文本解析', projectStage: true } },
        { path: 'voices', name: 'voices', component: () => import('@/views/Voices.vue'), meta: { title: '角色配音', projectStage: true } },
        { path: 'batch', name: 'batch', component: () => import('@/views/BatchTTS.vue'), meta: { title: '音频合成', projectStage: true } },
        { path: 'merge', name: 'merge', component: () => import('@/views/Merge.vue'), meta: { title: '音频合并', projectStage: true } },
        { path: 'audio', name: 'audio', component: () => import('@/views/AudioSplit.vue'), meta: { title: '音频分集', projectStage: true } },
        { path: 'bgm', name: 'bgm', component: () => import('@/views/BGM.vue'), meta: { title: '背景音乐', projectStage: true } },
        { path: 'settings', name: 'settings', component: () => import('@/views/Settings.vue'), meta: { title: '设置' } },
      ],
    },
    {
      path: '/admin',
      component: AdminLayout,
      meta: { requiresAdmin: true },
      children: [
        { path: '', name: 'admin', component: () => import('@/views/Admin.vue'), meta: { title: '管理总览' } },
        { path: 'music', name: 'music', component: () => import('@/views/MusicLibrary.vue'), meta: { title: '音乐库管理' } },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const project = useProjectStore()
  await auth.load()

  if (to.path === '/login') {
    return auth.user?.role === 'user' ? '/dashboard' : true
  }
  if (to.path === '/admin/login') {
    return auth.user?.role === 'admin' ? '/admin' : true
  }
  if (to.name === 'access-denied') return true

  if (!auth.isAuthenticated) {
    return to.meta.requiresAdmin ? '/admin/login' : '/login'
  }
  if (to.meta.requiresAdmin && auth.user?.role !== 'admin') return '/access-denied'
  if (to.meta.requiresUser && auth.user?.role !== 'user') return '/access-denied'

  if (to.name === 'project-overview') {
    const projectId = String(to.params.projectId || '')
    if (!projectId) return '/dashboard'
    try {
      const current = await getActiveProject()
      if (current.project_id === projectId) project.setCurrent(current)
      else await project.select(projectId)
    } catch {
      return '/dashboard'
    }
  }
  if (to.meta.projectStage) {
    try {
      const current = await getActiveProject()
      project.setCurrent(current)
      if (!current.set || !current.project_id) return '/dashboard'
    } catch {
      return '/dashboard'
    }
  }
  return true
})

export default router
