#!/usr/bin/env bash
# 一键清除本 DevContainer 使用的全部镜像。
# 镜像清单取自 docker/docker-compose.yml（本脚本位于 <项目根>/scripts/，即脚本
# 所在目录的上一级再进 docker/）的 services 声明（image: 行）：app（prod / dev）
# 与 nginx / postgres 等外挂服务。镜像被运行中的容器占用时无法删除，请先
# make down；清除后可用 install.sh --load-only 或 docker load -i 重新导入
# 镜像归档。
#
# 用法（在项目根目录执行）:
#     bash scripts/clean-images.sh           # 列出 compose 声明的镜像，确认后删除
#     make clean                             # 等价写法（Makefile 目标，支持 ARGS 透传）
#     bash scripts/clean-images.sh --no-name # 清除名称与 Tag 均为 <none> 的悬空镜像
#     bash scripts/clean-images.sh -y        # 跳过确认直接删除
#     bash scripts/clean-images.sh -h        # 查看帮助

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P)
COMPOSE_FILE="$SCRIPT_DIR/../docker/docker-compose.yml"

usage() {
    cat <<'EOF'
用法: bash scripts/clean-images.sh [-y|--yes] [--no-name] [-h|--help]

删除本 DevContainer 使用的镜像：镜像清单取自 docker/docker-compose.yml
（脚本所在目录的上一级再进 docker/）的 services 声明（app / dev /
nginx / postgres 等服务的 image: 值），镜像不存在时自动跳过。

选项:
  --no-name    清除 Docker images 中名称（REPOSITORY）与 Tag 均为 <none>
               的悬空镜像（dangling images，通常是旧构建残留）；该模式
               不需要 docker-compose.yml
  -y, --yes    跳过确认，直接删除
  -h, --help   显示本帮助并退出

示例:
  bash scripts/clean-images.sh                  # 确认后清除 compose 中声明的镜像
  make clean                                    # 等价写法（make clean ARGS="--no-name"）
  bash scripts/clean-images.sh --no-name        # 清除 <none>:<none> 悬空镜像
  bash scripts/clean-images.sh --no-name -y     # 直接清除悬空镜像（适合脚本 / CI）

说明:
  镜像被运行中的容器占用时无法删除，请先执行 make down。
  清除后可用 install.sh --load-only 或 docker load -i 镜像归档重新导入。
EOF
}

assume_yes=0
no_name=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        -y|--yes)  assume_yes=1 ;;
        --no-name) no_name=1 ;;
        -h|--help) usage; exit 0 ;;
        *)         printf '错误: 未知参数: %s（用 -h 查看用法）\n' "$1" >&2; exit 2 ;;
    esac
    shift
done

command -v docker >/dev/null 2>&1 \
    || { printf '错误: 未检测到 docker 命令\n' >&2; exit 1; }

# ---------- --no-name：名称与 Tag 均为 <none> 的悬空镜像 ----------
if [ "$no_name" -eq 1 ]; then
    mapfile -t dangling < <(docker images --filter dangling=true --format '{{.ID}}' | sort -u)
    if [ "${#dangling[@]}" -eq 0 ]; then
        printf '== 没有名称与 Tag 均为 <none> 的镜像，无需清理 ==\n'
        exit 0
    fi
    printf '== 悬空镜像清单（REPOSITORY 与 TAG 均为 <none>）==\n'
    docker images --filter dangling=true --format '  {{.ID}}  {{.Repository}}:{{.Tag}}  {{.Size}}'
    if [ "$assume_yes" -ne 1 ]; then
        printf '确认删除以上 %d 个镜像? 输入 y 继续: ' "${#dangling[@]}"
        IFS= read -r answer || { printf '\n'; printf '已取消\n'; exit 1; }
        answer=$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')
        [ "$answer" = "y" ] || { printf '已取消\n'; exit 1; }
    fi
    failed=0
    for img in "${dangling[@]}"; do
        if docker rmi -f "$img" >/dev/null; then
            printf '  已删除  %s\n' "$img"
        else
            printf '  失败    %s（可能仍被容器使用，请先 make down）\n' "$img" >&2
            failed=1
        fi
    done
    if [ "$failed" -ne 0 ]; then
        printf '== 部分镜像删除失败 ==\n' >&2
        exit 1
    fi
    printf '== 清理完成 ==\n'
    exit 0
fi

[ -f "$COMPOSE_FILE" ] \
    || { printf '错误: 未找到 %s（请在项目根目录的 scripts/ 下安装本脚本后再执行）\n' "$COMPOSE_FILE" >&2; exit 1; }

# 从 compose 的 services 块中提取 image: 值（支持引号与行尾注释）
parse_images() {
    awk '
        BEGIN { SQ = sprintf("%c", 39) }
        /^[[:space:]]*#/ { next }
        /^services:[[:space:]]*$/ { in_services = 1; next }
        /^[^[:space:]]/ { in_services = 0 }
        in_services && /^[[:space:]]+image:[[:space:]]*/ {
            line = $0
            sub(/^[[:space:]]+image:[[:space:]]*/, "", line)
            sub(/[[:space:]]+#.*$/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (substr(line, 1, 1) == SQ) line = substr(line, 2)
            if (substr(line, length(line), 1) == SQ) line = substr(line, 1, length(line) - 1)
            if (substr(line, 1, 1) == "\"") line = substr(line, 2)
            if (substr(line, length(line), 1) == "\"") line = substr(line, 1, length(line) - 1)
            if (line != "") print line
        }
    ' "$1"
}

declare -A seen=()
images=()
while IFS= read -r img; do
    [ -n "$img" ] || continue
    [ -n "${seen[$img]:-}" ] && continue
    seen[$img]=1
    images+=("$img")
done < <(parse_images "$COMPOSE_FILE")

if [ "${#images[@]}" -eq 0 ]; then
    printf '错误: %s 的 services 下未找到 image 声明\n' "$COMPOSE_FILE" >&2
    exit 1
fi

# 仅处理本机真实存在的镜像，不存在的直接跳过
existing=()
printf '== 镜像清单（来自 %s）==\n' "$COMPOSE_FILE"
for img in "${images[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1; then
        existing+=("$img")
        printf '  存在  %s\n' "$img"
    else
        printf '  跳过  %s（本机不存在）\n' "$img"
    fi
done

if [ "${#existing[@]}" -eq 0 ]; then
    printf '== 清单中的镜像均不存在，无需清理 ==\n'
    exit 0
fi

if [ "$assume_yes" -ne 1 ]; then
    printf '确认删除以上 %d 个镜像? 输入 y 继续: ' "${#existing[@]}"
    IFS= read -r answer || { printf '\n'; printf '已取消\n'; exit 1; }
    answer=$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')
    [ "$answer" = "y" ] || { printf '已取消\n'; exit 1; }
fi

failed=0
for img in "${existing[@]}"; do
    if docker rmi -f "$img" >/dev/null; then
        printf '  已删除  %s\n' "$img"
    else
        printf '  失败    %s（可能仍被容器使用，请先 make down）\n' "$img" >&2
        failed=1
    fi
done

if [ "$failed" -ne 0 ]; then
    printf '== 部分镜像删除失败 ==\n' >&2
    exit 1
fi
printf '== 清理完成 ==\n'
