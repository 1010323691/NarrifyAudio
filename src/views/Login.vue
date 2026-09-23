<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from '@/components/ui/Button.vue'
import Input from '@/components/ui/Input.vue'
import Label from '@/components/ui/Label.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const isAdminPortal = computed(() => route.path === '/admin/login')
const registerMode = ref(false)
const email = ref('')
const username = ref('')
const password = ref('')
const displayName = ref('')
const submitted = ref(false)

watch(isAdminPortal, () => {
  registerMode.value = false
  submitted.value = false
  auth.error = ''
})

async function submit() {
  submitted.value = true
  if (!email.value || password.value.length < 12 || (registerMode.value && (!displayName.value.trim() || !username.value.trim()))) return
  try {
    if (registerMode.value) {
      await auth.signUp(email.value, password.value, username.value, displayName.value)
    } else {
      await auth.signIn(email.value, password.value)
    }
    const expectedRole = isAdminPortal.value ? 'admin' : 'user'
    if (auth.user?.role !== expectedRole) {
      try { await auth.signOut() } catch { auth.user = null }
      auth.error = isAdminPortal.value ? '此账号没有管理员权限。' : '管理员账号不能登录用户工作台。'
      return
    }
    await router.replace(isAdminPortal.value ? '/admin' : '/dashboard')
  } catch {
    // The store exposes the server message beside the form.
  }
}
</script>

<template>
  <main class="flex min-h-dvh items-center justify-center bg-background px-4 py-10">
    <section class="w-full max-w-md rounded-2xl border bg-card p-8 shadow-sm" aria-labelledby="auth-title">
      <div class="mb-8">
        <p class="mb-2 text-xs font-bold uppercase tracking-[0.18em] text-primary">NarrifyAudio</p>
        <h1 id="auth-title" class="text-2xl font-bold tracking-tight">{{ isAdminPortal ? '管理员登录' : registerMode ? '创建你的工作空间' : '用户工作台登录' }}</h1>
        <p class="mt-2 text-sm text-muted-foreground">{{ isAdminPortal ? '进入系统管理控制台。此入口仅接受管理员账号。' : '登录后管理自己的项目、文件和制作任务。' }}</p>
      </div>

      <form class="space-y-5" novalidate @submit.prevent="submit">
        <div v-if="registerMode && !isAdminPortal" class="space-y-2">
          <Label for="display-name">显示名称</Label>
          <Input id="display-name" v-model="displayName" autocomplete="name" :aria-invalid="submitted && !displayName.trim()" />
          <p v-if="submitted && !displayName.trim()" class="text-xs text-destructive">请输入显示名称。</p>
        </div>
        <div v-if="registerMode && !isAdminPortal" class="space-y-2">
          <Label for="username">用户名</Label>
          <Input id="username" v-model="username" autocomplete="username" pattern="[a-z0-9][a-z0-9._-]{2,63}" :aria-invalid="submitted && !username.trim()" />
          <p class="text-xs text-muted-foreground">用于工作空间目录，只能使用小写字母、数字、点、下划线或连字符。</p>
          <p v-if="submitted && !username.trim()" class="text-xs text-destructive">请输入用户名。</p>
        </div>
        <div class="space-y-2">
          <Label for="email">邮箱</Label>
          <Input id="email" v-model="email" type="email" autocomplete="email" required :aria-invalid="submitted && !email" />
          <p v-if="submitted && !email" class="text-xs text-destructive">请输入邮箱。</p>
        </div>
        <div class="space-y-2">
          <Label for="password">密码</Label>
          <Input id="password" v-model="password" type="password" autocomplete="current-password" minlength="12" required :aria-invalid="submitted && password.length < 12" />
          <p v-if="submitted && password.length < 12" class="text-xs text-destructive">密码至少需要 12 个字符。</p>
        </div>
        <p v-if="auth.error" role="alert" class="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">{{ auth.error }}</p>
        <Button class="w-full" size="lg" type="submit" :disabled="auth.busy">{{ auth.busy ? '处理中…' : registerMode ? '创建账户' : '登录' }}</Button>
      </form>

        <button v-if="!isAdminPortal" class="mt-6 min-h-11 w-full rounded-lg px-3 text-sm font-semibold text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" type="button" @click="registerMode = !registerMode; submitted = false; auth.error = ''">
        {{ registerMode ? '已有账户？返回登录' : '还没有账户？创建账户' }}
      </button>
    </section>
  </main>
</template>
