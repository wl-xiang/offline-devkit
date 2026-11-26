import { defineConfig } from "vite-plus";
import vue from "@vitejs/plugin-vue";
import tailwindcss from "@tailwindcss/vite";
import { visualizer } from "rollup-plugin-visualizer";
import { fileURLToPath, URL } from "node:url";

// Vite+ 统一工具链：vp dev / vp build / vp check / vp test / vp run。
// ANALYZE=1 时额外输出构建分析报告 dist/stats.html（rollup-plugin-visualizer）。
export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
    ...(process.env.ANALYZE
      ? [visualizer({ filename: "dist/stats.html", gzipSize: true, template: "treemap" })]
      : []),
  ],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    exclude: ["**/node_modules/**", "**/dist/**", "e2e/**"],
  },
});
