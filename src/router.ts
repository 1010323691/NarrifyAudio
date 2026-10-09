import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import AdminLayout from '@/layouts/AdminLayout.vue'
import Login from '@/views/Login.vue'
import AccessDenied from '@/views/AccessDenied.vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'

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
        { path: 'preview', name: 'preview', component: () => import('@/views/ChapterPreview.vue'), meta: { title: '整章预览', projectStage: true, fullBleed: true } },
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

  // 进入用户工作区前先把活动项目解析好：MainLayout 的 keep-alive 按
  // 「用户:活动项目」做 key，若组件挂载后才拿到活动项目，key 中途变化会触发
  // 整页重挂载，首次进入时全部加载请求都要再跑一轮。refresh() 内部合流，
  // 守卫、布局、页面三方共享同一波请求。
  if (to.meta.requiresUser && !project.loaded) await project.refresh()

  if (to.name === 'project-overview') {
    const projectId = String(to.params.projectId || '')
    if (!projectId) return '/dashboard'
    if (!project.current) {
      await project.refresh()
      if (!project.current) return '/dashboard'
    }
    if (project.current.project_id !== projectId) {
      try {
        await project.select(projectId)
      } catch {
        return '/dashboard'
      }
      // select() returns quietly when another selection is still in flight —
      // don't land on the workbench with the active scope still pointing
      // elsewhere (every stage entry would sit disabled and do nothing).
      if (project.current?.project_id !== projectId) return '/dashboard'
    }
    return true
  }
  if (to.meta.projectStage) {
    if (!project.current) await project.refresh()
    if (!project.current?.set || !project.current?.project_id) return '/dashboard'
    return true
  }
  return true
})

export default router
