# AGENTS.md

面向 AI 代理（Agent）的仓库协作规则。

## 改动后必须用 GH CLI 跑 Workflow 验证

每一次改动完成后（包括 `scripts/devcontainer-builder/`、`.github/workflows/`、模板与其他脚本），Agent 必须自己用 GitHub CLI 触发并跟踪一次 `Build DevContainer` workflow run，端到端验证改动没有 bug、确实可用，不能只做本地检查就宣布完成：

1. 提交并推送改动所在分支，然后按改动内容选择能覆盖该改动的最小参数组合触发：
   `gh workflow run build-devcontainer.yml --ref <branch> -f <覆盖改动的 inputs...>`
2. 用 `gh run watch <run-id> --exit-status`（或轮询 `gh run view <run-id>`）等待 run 结束（注意：完整构建最长约 90 分钟，需耐心跟踪到结束）；
3. run 失败时用 `gh run view <run-id> --log-failed` 定位原因，修复后重跑，直到 run 全绿；
4. 只有对应 run 通过后，才能向用户报告"改动已验证/已完成"。

## Workflow 参数数量上限

任何时候，workflow 的参数数量都不应该超过 25。

- 适用于 `.github/workflows/` 下的所有 workflow 的 `workflow_dispatch.inputs`（以及其他触发方式的输入参数），当前以 `.github/workflows/build-devcontainer.yml` 为基准（现有 21 个参数）。
- 新增参数前必须先合并或移除现有参数，保证总数始终 ≤ 25。
- 改动后需同步所有关联位置：`scripts/devcontainer-builder/lib/params.py` 的 `WORKFLOW_INPUTS` 表、workflow 内的校验步骤（`DEFAULTS` 与联动检查），并核对 `devcontainer.json` 快照的键数量一致。
