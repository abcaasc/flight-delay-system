<script setup lang="ts">
import type { Component } from "vue";
defineProps<{ title: string; subtitle?: string; icon?: Component }>();
</script>
<template>
  <section class="card">
    <header class="card-header">
      <div class="card-title-group">
        <span v-if="icon" class="section-icon"
          ><component
            :is="icon"
            :size="18"
            :stroke-width="1.7"
            aria-hidden="true"
        /></span>
        <div>
          <h2>{{ title }}</h2>
          <p v-if="subtitle">{{ subtitle }}</p>
        </div>
      </div>
      <div v-if="$slots.actions" class="card-actions">
        <slot name="actions" />
      </div>
    </header>
    <slot />
    <div v-if="$slots.footer" class="card-footer"><slot name="footer" /></div>
  </section>
</template>
