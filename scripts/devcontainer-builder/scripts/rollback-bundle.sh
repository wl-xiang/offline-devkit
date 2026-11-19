#!/bin/sh
# 部署服务器端脚本：回滚 SRC Bundle——把 bundles/ 下最新的 backup（由
# apply-bundle.sh 在替换 ./src 前自动创建的 backup_src_<mmdd-HHMM>.tgz）
# 覆盖回 ./src 目录。
# 纯 POSIX sh 实现，与 apply-bundle.sh 对应：apply 的每一次部署都会先把旧
# 版本备份为 bundles/backup_src_<时间戳>.tgz，本脚本取文件名时间戳最新的
# 一份恢复（回滚后再执行一次即可实现"回滚到更早"，或再次 apply-bundle.sh
# 回到新版本——当前版本同样会被先备份）。
#
# 用法（在部署服务器项目根目录中执行；脚本位于 <项目根>/scripts/，也可用
# make rollback ARGS=...）:
#     bash scripts/rollback-bundle.sh        # 取最新 backup，确认后覆盖 ./src
#     bash scripts/rollback-bundle.sh -y     # 跳过交互确认直接执行
#     bash scripts/rollback-bundle.sh -h     # 打印用法
#
# 流程:
#     1. 找到 ./bundles/ 下文件名时间戳最新的 backup_src_*.tgz
#     2. 校验备份包内含 src/ 顶层目录（与 apply-bundle.sh 的备份格式一致）
#     3. 展示计划，等待手动输入 y 确认（-y 跳过）
#     4. 前置钩子: 自动 make down 下线服务（失败仅告警，不阻塞）
#     5. 解压到临时目录（备份损坏在此暴露，尚未触碰 ./src）
#     6. 把当前 ./src 备份为 bundles/backup_src_<mmdd-HHMM>.tgz（可再次回滚）
#     7. 用备份包中的 src 覆盖 ./src（不会自动运行任何代码，完成后提示手动 make up）

set -eu

SRC_DIR="src"
BUNDLES_DIR="bundles"
BACKUP_PREFIX="backup_src_"

usage() {
    cat <<'USAGE'
用法: bash scripts/rollback-bundle.sh [选项]

从 ./bundles/ 取文件名时间戳最新的 backup_src_*.tgz（apply-bundle.sh 部署前
自动创建的旧版本备份），确认后覆盖 ./src 目录（不会自动运行任何代码）。
部署目录固定为 ./src —— compose 模板中 ${SRC_ROOT:-./src/} 把该目录挂载为容器内 /app。
执行前会自动 make down 下线服务；完成后请手动 make up 上线。
回滚前会把当前 ./src 再备份为新的 backup_src_*.tgz，因此可以连续回滚，
或随时用 bash scripts/apply-bundle.sh 回到新版本。

选项:
  （无参数）        取最新 backup，确认后覆盖 ./src（含 make down 钩子）
  -y, --yes         跳过交互确认直接执行
  -h, --help        显示本帮助并退出

示例:
  bash scripts/rollback-bundle.sh        # 回滚到最近一次部署前的版本
  bash scripts/rollback-bundle.sh -y     # 自动确认，直接回滚
USAGE
}

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

# bundles/ 下文件名时间戳最新的 backup_src_*.tgz（纯文件名，无则输出空）
find_newest_backup() {
    find "$BUNDLES_DIR" -maxdepth 1 -type f -name "${BACKUP_PREFIX}*.tgz" -printf '%f\n' 2>/dev/null \
        | sort -r | head -n 1
}

confirm() {
    if [ "$assume_yes" -eq 1 ]; then
        return 0
    fi
    printf '%s' "$1"
    IFS= read -r answer || { printf '\n'; return 1; }
    answer=$(printf '%s' "$answer" | tr -d ' \t' | tr '[:upper:]' '[:lower:]')
    [ "$answer" = "y" ]
}

# ---------- 参数 ----------
assume_yes=0
while [ "$#" -gt 0 ]; do
    case $1 in
        -y|--yes)  assume_yes=1 ;;
        -h|--help) usage; exit 0 ;;
        --)        shift; break ;;
        *)         die "未知参数: $1（用 -h 查看用法）" ;;
    esac
    shift
done

# ---------- 1. 找最新 backup ----------
newest=$(find_newest_backup)
if [ -z "$newest" ]; then
    die "$BUNDLES_DIR/ 下未找到 ${BACKUP_PREFIX}*.tgz（备份由 apply-bundle.sh 在部署替换 ./src 前自动创建）"
fi
archive="$BUNDLES_DIR/$newest"
total=0
for f in "$BUNDLES_DIR"/${BACKUP_PREFIX}*.tgz; do
    [ -e "$f" ] || continue
    total=$((total + 1))
done

# ---------- 2. 展示计划，等待确认 ----------
src="$PWD/$SRC_DIR"
printf '%s\n' "== 执行 rollback 将发生 =="
printf '  备份包: %s（目录下共 %d 份 backup）\n' "$archive" "$total"
printf '  目标目录: %s\n' "$src"
printf '  前置钩子: make down 下线服务\n'
printf '  当前 %s/ 会先备份为新的 backup_src_<时间戳>.tgz（可再次回滚）\n' "$SRC_DIR"
printf '  恢复后不会自动运行任何代码，需要手动 make up 上线\n'
if ! confirm "确认执行 rollback? 输入 y 继续: "; then
    die "已取消"
fi

# ---------- 3. 前置钩子: make down ----------
printf '%s\n' "== 前置钩子: make down（下线服务）=="
make_log=$(mktemp)
if make down >"$make_log" 2>&1; then
    printf '%s\n' "== 服务已下线 =="
else
    cat "$make_log" >&2
    printf '%s\n' "!! make down 未成功（服务可能本就未运行），继续 rollback"
fi
rm -f "$make_log"

# ---------- 4. 解压到临时目录（备份损坏在此暴露，尚未触碰 ./src） ----------
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
trap 'exit 1' INT TERM

tar -xzf "$archive" -C "$tmp"

# 备份包由 apply-bundle.sh 从项目根打包（顶层为 src/ 目录）；兼容无 src/
# 包裹目录的历史包（顶层只有文件时视为 src 内容本身）
if [ -d "$tmp/$SRC_DIR" ]; then
    restored_root=$tmp/$SRC_DIR
elif [ "$(find "$tmp" -mindepth 1 -maxdepth 1 | wc -l)" -ge 1 ]; then
    printf '%s\n' "!! 备份包顶层没有 $SRC_DIR/ 目录（历史格式），按 src 内容本身处理"
    restored_root=$tmp
else
    die "备份包为空或损坏: $archive"
fi

# ---------- 5. 把当前 ./src 备份为新 backup（可再次回滚） ----------
# 同一分钟内多次执行时追加 _N 后缀，避免覆盖刚要恢复的那份 backup
ts=$(date +%m%d-%H%M)
backup="$BUNDLES_DIR/${BACKUP_PREFIX}$ts.tgz"
n=1
while [ -e "$backup" ]; do
    backup="$BUNDLES_DIR/${BACKUP_PREFIX}${ts}_$n.tgz"
    n=$((n + 1))
done
if [ -d "$src" ] && [ -n "$(ls -A "$src" 2>/dev/null)" ]; then
    mkdir -p "$BUNDLES_DIR"
    tar -czf "$backup" "$SRC_DIR"
    printf '  已备份当前版本: %s\n' "$backup"
    backup_for_hint=$backup
else
    printf '  %s/ 不存在或为空（首次部署前回滚），跳过备份\n' "$SRC_DIR"
    backup_for_hint=""
fi

# ---------- 6. 用备份包中的 src 覆盖 ./src ----------
rm -rf -- "$src"
mv -- "$restored_root" "$src"

printf '%s\n' "== rollback 完成: 已回滚到 $newest，代码就位 $src =="
if [ -n "$backup_for_hint" ]; then
    printf '  回滚前版本已备份: %s（再次执行 bash scripts/rollback-bundle.sh 可回到该版本）\n' "$backup_for_hint"
fi
printf '%s\n' "== 请手动执行 make up 上线服务 =="
