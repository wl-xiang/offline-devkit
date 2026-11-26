# Vue 3 + TypeScript + Vite Plus 启动模板

由 devcontainer 的 `enable_fe_framework=vue` 预置（仅 dev 镜像，含 node_modules）。

## 命令

```bash
pnpm dev            # vp dev（Vite 开发服务器，0.0.0.0:5173）
pnpm build          # vp build（产物 dist/，由 nginx 托管）
pnpm exec vp check  # 格式化 + lint + 类型检查
pnpm exec vp test   # Vitest 单元测试
pnpm test:e2e       # Playwright E2E
pnpm analyze        # 构建分析报告 dist/stats.html
```

## 技术栈

- Vue 3（Composition API）+ TypeScript
- Vue Router 4 / Pinia / pinia-plugin-persistedstate / Axios / VueUse
- Element Plus / Lucide / motion-v / Tailwind CSS v4 / Reka UI / shadcn-vue
- vue-draggable-plus / TipTap
- Vite Plus（Rolldown、Oxlint、Oxfmt、Vitest、vp check、Vite Task）
- vue-tsc / @vue/test-utils / Stylelint / Commitlint / Playwright / rollup-plugin-visualizer

环境变量只暴露 `VITE_` 前缀（见 `.env.example`）。VS Code 默认 formatter 为 Oxc
（`.vscode/settings.json`），推荐安装 `oxc.oxc-vscode`。
