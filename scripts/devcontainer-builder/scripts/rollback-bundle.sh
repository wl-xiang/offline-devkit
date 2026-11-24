#!/bin/sh
# 部署服务器端脚本：回滚 bundle——把 bundles/backups/ 下最新的 backup（由
# apply-bundle.sh 在按清单覆盖前自动创建的 backup_<mmdd-HHMM>.tgz，包内保留
# 相对项目根的条目路径）恢复到项目根对应路径。
# 纯 POSIX sh 实现，与 apply-bundle.sh 对应：apply 的每一次部署都会先把清单
# 中已存在的条目备份到 bundles/backups/backup_<时间戳>.tgz，本脚本取文件名
# 时间戳最新的一份恢复（回滚后再执行一次即可实现"回滚到更早"，或再次
# apply-bundle.sh 回到新版本——当前版本同样会被先备份）。
#
# 用法（在部署服务器项目根目录中执行；脚本位于 <项目根>/scripts/，也可用
# make rollback ARGS=...）:
#     bash scripts/rollback-bundle.sh        # 取最新 backup，确认后恢复清单条目
#     bash scripts/rollback-bundle.sh -y     # 跳过交互确认直接执行
#     bash scripts/rollback-bundle.sh -h     # 打印用法
#
# 流程:
#     1. 找到 ./bundles/backups/ 下文件名时间戳最新的 backup_*.tgz
#     2. 从备份包内列出顶层条目（与 apply-bundle.sh 的备份格式一致：
#        相对项目根的文件/目录路径）
#     3. 展示计划，等待手动输入 y 确认（-y 跳过）
#     4. 前置钩子: 自动 make down 下线服务（失败仅告警，不阻塞）
#     5. 解压到临时目录（备份损坏在此暴露，尚未触碰项目根）
#     6. 把当前同名条目备份为新的 backup_<mmdd-HHMM>.tgz（可再次回滚）
#     7. 删除当前条目后用备份包内容覆盖（不会自动运行任何代码，完成后提示
#        手动 make up）

set -eu

BACKUPS_DIR="bundles/backups"
BACKUP_PREFIX="backup_"

usage() {
    cat <<'USAGE'
用法: bash scripts/rollback-bundle.sh [选项]

从 ./bundles/backups/ 取文件名时间戳最新的 backup_*.tgz（apply-bundle.sh 部署
前自动创建的条目备份，包内保留相对项目根的路径），确认后把其中的条目恢复到
项目根对应路径（不会自动运行任何代码）。
执行前会自动 make down 下线服务；回滚前会把当前同名条目再备份为新的
backup_*.tgz，因此可以连续回滚，或随时用 bash scripts/apply-bundle.sh 回到
新版本。

选项:
  （无参数）        取最新 backup，确认后恢复其中的条目（含 make down 钩子）
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

# bundles/backups/ 下文件名时间戳最新的 backup_*.tgz（纯文件名，无则输出空）
find_newest_backup() {
    find "$BACKUPS_DIR" -maxdepth 1 -type f -name "${BACKUP_PREFIX}*.tgz" -printf '%f\n' 2>/dev/null \
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
    die "$BACKUPS_DIR/ 下未找到 ${BACKUP_PREFIX}*.tgz（备份由 apply-bundle.sh 在按清单覆盖前自动创建）"
fi
archive="$BACKUPS_DIR/$newest"
total=0
for f in "$BACKUPS_DIR"/${BACKUP_PREFIX}*.tgz; do
    [ -e "$f" ] || continue
    total=$((total + 1))
done

# ---------- 2. 从备份包列出顶层条目（文件/目录，相对项目根） ----------
entries_file=$(mktemp)
tmp=""
restore_list=$(mktemp)
trap 'rm -rf -- "$entries_file" "$restore_list" ${tmp:+"$tmp"}' EXIT
trap 'exit 1' INT TERM
tar tzf "$archive" > "$entries_file" || die "备份包损坏: $archive"
# 顶层条目 = 包内路径的第一段；兼容历史 src/ 包裹目录（视为 src 单一条目）
while IFS= read -r member; do
    [ -n "$member" ] || continue
    printf '%s\n' "${member%%/*}"
done < "$entries_file" | sort -u > "$restore_list"

while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    case "$entry" in
        /*) die "备份包内含绝对路径条目: $entry（拒绝恢复）" ;;
        *..*) die "备份包内条目不允许包含 ..: $entry" ;;
    esac
done < "$restore_list"

# ---------- 3. 展示计划，等待确认 ----------
printf '%s\n' "== 执行 rollback 将发生 =="
printf '  备份包: %s（目录下共 %d 份 backup）\n' "$archive" "$total"
printf '  恢复条目:\n'
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    printf '    %s\n' "$entry"
done < "$restore_list"
printf '  前置钩子: make down 下线服务\n'
printf '  当前同名条目会先备份为新的 %s%s<时间戳>.tgz（可再次回滚）\n' "$BACKUPS_DIR" "$BACKUP_PREFIX"
printf '  恢复后不会自动运行任何代码，需要手动 make up 上线\n'
if ! confirm "确认执行 rollback? 输入 y 继续: "; then
    die "已取消"
fi

# ---------- 4. 前置钩子: make down ----------
printf '%s\n' "== 前置钩子: make down（下线服务）=="
make_log=$(mktemp)
if make down >"$make_log" 2>&1; then
    printf '%s\n' "== 服务已下线 =="
else
    cat "$make_log" >&2
    printf '%s\n' "!! make down 未成功（服务可能本就未运行），继续 rollback"
fi
rm -f "$make_log"

# ---------- 5. 解压到临时目录（备份损坏在此暴露，尚未触碰项目根） ----------
tmp=$(mktemp -d)
tar -xzf "$archive" -C "$tmp"

# ---------- 6. 把当前同名条目备份为新 backup（可再次回滚） ----------
# 同一分钟内多次执行时追加 _N 后缀，避免覆盖刚要恢复的那份 backup
ts=$(date +%m%d-%H%M)
current_list=$(mktemp)
n=0
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    if [ -e "$entry" ]; then
        printf '%s\n' "$entry" >> "$current_list"
    fi
done < "$restore_list"
backup=""
if [ -s "$current_list" ]; then
    while : ; do
        n=$((n + 1))
        if [ "$n" -eq 1 ]; then
            candidate="$BACKUPS_DIR/${BACKUP_PREFIX}$ts.tgz"
        else
            candidate="$BACKUPS_DIR/${BACKUP_PREFIX}${ts}_$n.tgz"
        fi
        [ -e "$candidate" ] || break
    done
    mkdir -p "$BACKUPS_DIR"
    tar -czf "$candidate" -T "$current_list"
    backup=$candidate
    printf '  已备份当前版本: %s\n' "$backup"
else
    printf '  当前同名条目均不存在（首次部署前回滚），跳过备份\n'
fi
rm -f "$current_list"

# ---------- 7. 删除当前条目后用备份包内容覆盖 ----------
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    rm -rf -- "$entry"
    mkdir -p -- "$(dirname "$entry")"
done < "$restore_list"
tar -xzf "$archive" -C "$PWD"

printf '%s\n' "== rollback 完成: 已回滚到 $newest =="
if [ -n "$backup" ]; then
    printf '  回滚前版本已备份: %s（再次执行 bash scripts/rollback-bundle.sh 可回到该版本）\n' "$backup"
fi
printf '%s\n' "== 请手动执行 make up 上线服务 =="
