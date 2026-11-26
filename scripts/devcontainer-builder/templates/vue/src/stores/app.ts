import { ref } from "vue";
import { defineStore } from "pinia";

export const useAppStore = defineStore(
  "app",
  () => {
    const count = ref(0);
    const theme = ref<"light" | "dark">("dark");

    function increment() {
      count.value += 1;
    }

    function toggleTheme() {
      theme.value = theme.value === "dark" ? "light" : "dark";
    }

    return { count, theme, increment, toggleTheme };
  },
  { persist: true },
);
