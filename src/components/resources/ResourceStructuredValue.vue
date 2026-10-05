<script setup lang="ts">
import { computed } from 'vue'
const props = withDefaults(defineProps<{ value: unknown; depth?: number }>(), { depth: 0 })
const labels: Record<string, string> = { chapter: '章节', lines: '段落', text: '正文', speaker: '说话角色', role: '角色', emotion: '情绪', segments: '段落', chapters: '章节', content: '内容', name: '名称', title: '标题', index: '序号', voice: '声音', description: '描述', duration: '时长', language: '语言' }
const items = computed(() => Array.isArray(props.value) ? props.value.slice(0, 40).map((value, index) => ({ key: `第${index + 1}项`, value })) : props.value !== null && typeof props.value === 'object' ? Object.entries(props.value).slice(0, 40).map(([key, value]) => ({ key: labels[key] || key, value })) : [])
const count = computed(() => Array.isArray(props.value) ? props.value.length : props.value !== null && typeof props.value === 'object' ? Object.keys(props.value).length : 0)
</script>

<template>
  <div v-if="items.length && depth < 5" class="rc-structured">
    <section v-for="item in items" :key="item.key" class="rc-structured-field">
      <h4>{{ item.key }}</h4>
      <ResourceStructuredValue :value="item.value" :depth="depth + 1" />
    </section>
    <p v-if="count > 40" class="rc-preview-hint">共{{ count }}项，预览前40项。完整检查请前往制作工作台。</p>
  </div>
  <p v-else class="rc-structured-text">{{ value === null ? '未设置' : typeof value === 'object' ? (Array.isArray(value) ? `${value.length}项资料，请前往制作工作台检查。` : '更多结构，请前往制作工作台检查。') : String(value) }}</p>
</template>
