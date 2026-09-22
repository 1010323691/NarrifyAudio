<script setup lang="ts">
/**
 * Shared workspace gate banner for the pipeline views. Hidden once a workspace
 * is set (see the dashboard / 开始); otherwise it explains that the pipeline is
 * locked and links back to the dashboard to pick a folder.
 */
import { useRouter } from 'vue-router'
import { FolderX } from 'lucide-vue-next'
import Alert from '@/components/ui/Alert.vue'
import Button from '@/components/ui/Button.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'

const { workspaceSet } = useWorkspaceGate()
const router = useRouter()
</script>

<template>
  <Alert v-if="!workspaceSet" variant="warning">
    <template #icon>
      <FolderX class="h-5 w-5 shrink-0" />
    </template>
    <p class="font-medium">尚未设置工作空间，流程已锁定</p>
    <template #description>
      请先在「开始」页选择一个文件夹，所有产物都会保存到其中。
      <Button size="sm" class="ml-2 align-middle" @click="router.push('/')">前往开始</Button>
    </template>
  </Alert>
</template>
