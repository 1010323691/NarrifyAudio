<script setup lang="ts">
/**
 * Shared project gate banner for the pipeline views. Hidden once an active
 * project is set (see the dashboard / 项目); otherwise it explains that the
 * pipeline is locked and links back to the dashboard to create or open one.
 */
import { useRouter } from 'vue-router'
import { FolderX } from 'lucide-vue-next'
import Alert from '@/components/ui/Alert.vue'
import Button from '@/components/ui/Button.vue'
import { useProjectGate } from '@/composables/useProjectGate'

const { projectSet } = useProjectGate()
const router = useRouter()
</script>

<template>
  <Alert v-if="!projectSet" variant="warning">
    <template #icon>
      <FolderX class="h-5 w-5 shrink-0" />
    </template>
    <p class="font-medium">尚未选择项目，流程已锁定</p>
    <template #description>
      请先在「项目」页创建并打开一个项目，所有产物都会保存到该项目的工作空间。
      <Button size="sm" class="ml-2 align-middle" @click="router.push('/')">前往项目</Button>
    </template>
  </Alert>
</template>
