<script setup lang="ts">
import { RouterLink, RouterView } from "vue-router";
</script>

<template>
  <div class="min-h-screen bg-slate-950 text-slate-100">
    <header class="border-b border-slate-800">
      <div class="mx-auto flex max-w-5xl items-center justify-between px-6 py-4">
        <span class="font-semibold tracking-wide">Vue 3 + Vite Plus</span>
        <nav class="flex gap-4 text-sm text-slate-300">
          <RouterLink class="hover:text-emerald-400" to="/">Home</RouterLink>
          <RouterLink class="hover:text-emerald-400" to="/about">About</RouterLink>
        </nav>
      </div>
    </header>
    <RouterView />
  </div>
</template>
