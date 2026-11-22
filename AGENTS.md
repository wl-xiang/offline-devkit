# AGENTS.md

面向 AI 代理（Agent）的仓库协作规则。

## Workflow 参数数量上限

任何时候，workflow 的参数数量都不应该超过 25。

- 适用于 `.github/workflows/` 下的所有 workflow 的 `workflow_dispatch.inputs`（以及其他触发方式的输入参数），当前以 `.github/workflows/build-devcontainer.yml` 为基准（现有 25 个参数，已达到上限）。
- 新增参数前必须先合并或移除现有参数，保证总数始终 ≤ 25。
- 改动后需同步所有关联位置：`scripts/devcontainer-builder/lib/params.py` 的 `WORKFLOW_INPUTS` 表、workflow 内的校验步骤（`DEFAULTS` 与联动检查），并核对 `devcontainer.json` 快照的键数量一致。
