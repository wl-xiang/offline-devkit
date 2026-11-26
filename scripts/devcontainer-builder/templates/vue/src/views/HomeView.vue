<script setup lang="ts">
import { ElButton } from "element-plus";
import { Sparkles } from "lucide-vue-next";
import { motion } from "motion-v";
import { storeToRefs } from "pinia";
import { useAppStore } from "@/stores/app";

const store = useAppStore();
const { count } = storeToRefs(store);
</script>

<template>
  <main
    class="mx-auto flex min-h-[calc(100vh-65px)] max-w-5xl items-center justify-center px-6 py-12"
  >
    <motion.section
      :initial="{ opacity: 0, y: 12 }"
      :animate="{ opacity: 1, y: 0 }"
      class="w-full max-w-xl rounded-2xl border border-slate-800 bg-slate-900/70 p-8 shadow-xl"
    >
      <h1 class="flex items-center gap-2 text-2xl font-semibold">
        <Sparkles class="h-6 w-6 text-emerald-400" />
        Vue 3 + TypeScript + Vite Plus
      </h1>
      <p class="mt-3 text-slate-400">
        Element Plus、Pinia、Vue Router、VueUse、Tailwind CSS v4、Reka UI、TipTap 已预装。
      </p>
      <div class="mt-6 flex items-center gap-3">
        <ElButton type="primary" @click="store.increment()">计数 {{ count }}</ElButton>
        <ElButton @click="store.toggleTheme()">主题 {{ store.theme }}</ElButton>
      </div>
    </motion.section>
  </main>
</template>
