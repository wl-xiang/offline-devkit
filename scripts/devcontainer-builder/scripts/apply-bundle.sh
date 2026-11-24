#!/bin/sh
# 部署服务器端脚本：应用 bundle——从 ./bundles/ 取文件名时间戳最新的 bundle
# （或用位置参数指定 bundle 路径），用 bundle.meta 校验完整性后按
# scripts/manifest.txt 清单把各条目解压覆盖到项目根对应路径。
# 纯 POSIX sh 实现——部署服务器无需 python/jq：tgz 解压只需 tar，SHA256 只需
# sha256sum（find -printf 为 GNU 扩展，Linux 均可用）。
#
# 用法（在部署服务器项目根目录中执行，目录下应有 push-bundle.sh 上传的
# bundles/ 目录；脚本位于 <项目根>/scripts/，也可用 make apply ARGS=...）:
#     bash scripts/apply-bundle.sh                    # 校验最新 bundle 后按清单覆盖
#     bash scripts/apply-bundle.sh bundles/xxx.tgz    # 应用指定的 bundle 文件
#     bash scripts/apply-bundle.sh -y                 # 跳过交互确认直接执行
#     bash scripts/apply-bundle.sh -c                 # 保留最新一份 bundle，删除其余
#     bash scripts/apply-bundle.sh --clean-all        # 删除全部 bundle 与遗留 sync.env
#     bash scripts/apply-bundle.sh -h                 # 打印用法
#
# 协议说明:
#     bundle 与 bundle.meta 固定位于 ./bundles/（push-bundle.sh 上传到远端
#     <项目路径>/bundles/）："最新"按 bundle 文件名中的时间戳判定
#     （bundle_<项目名>_<mmdd-HHMM>.tgz），不依赖文件修改时间。
#     bundle.meta 记录 BUNDLE_FILE / SHA256 / MANIFEST 等元信息，应用前强制
#     校验：meta 与最新 bundle 文件名必须一致，且 SHA256 必须匹配，否则拒绝
#     部署。meta 的 MANIFEST 字段是打包时的清单快照，与本地
#     scripts/manifest.txt 不一致时仅告警（以本地清单为准）。
#     目标路径由 scripts/manifest.txt 决定（目录条目以 / 结尾，保留相对项目
#     根的路径结构，如 app/、api/、nginx/default.conf 等）——不再固定替换
#     单个源码目录。bundle 中缺失的条目保留远端现有内容并告警。
#
# 流程（默认模式）:
#     1. 确定要应用的 bundle（位置参数优先，否则取 ./bundles/ 下最新的）
#     2. 读取 bundle.meta 校验 BUNDLE_FILE 一致性 + SHA256 完整性
#     3. 读取 scripts/manifest.txt 清单（缺失时回退 bundle.meta 的 MANIFEST）
#     4. 展示计划，等待手动输入 y 确认（-y 跳过）
#     5. 前置钩子: 自动 make down 下线服务（失败仅告警，不阻塞）
#     6. 解压到临时目录（bundle 损坏在此暴露，尚未触碰项目根）
#     7. 把清单中已存在的条目备份到 bundles/backups/backup_<时间戳>.tgz
#     8. 按清单逐条目替换（不会自动运行任何代码，完成后提示手动 make up）

set -eu

BUNDLES_DIR="bundles"
BACKUPS_DIR="bundles/backups"
BACKUP_PREFIX="backup_"
META_FILE="bundle.meta"
MANIFEST_FILE="scripts/manifest.txt"

usage() {
    cat <<'USAGE'
用法: bash scripts/apply-bundle.sh [选项] [BUNDLE路径]

从 ./bundles/ 取文件名时间戳最新的 bundle_*.tgz（或用位置参数指定 bundle
文件路径），用 bundle.meta 校验完整性后按 scripts/manifest.txt 清单把各条目
解压覆盖到项目根对应路径（不会自动运行任何代码）。
执行前会自动 make down 下线服务；覆盖前把清单中已存在的条目备份到
bundles/backups/backup_<时间戳>.tgz；完成后请手动 make up 上线。

参数:
  BUNDLE路径        要应用的 bundle 文件路径（相对当前目录或绝对路径）。
                    省略时取 ./bundles/ 下文件名时间戳最新的 bundle_*.tgz。

选项:
  （无参数）        取最新 bundle，校验后按清单覆盖（含 make down 钩子与备份）
  -y, --yes         跳过交互确认直接执行
  -c, --clean       保留最新一份 bundle_*.tgz，删除其余
      --clean-all   删除全部 bundle_*.tgz 与遗留 sync.env
  -h, --help        显示本帮助并退出

示例:
  bash scripts/apply-bundle.sh                        # 常规部署：最新 bundle -> 清单条目
  bash scripts/apply-bundle.sh bundles/bundle_xxx.tgz # 应用指定的 bundle
  bash scripts/apply-bundle.sh -y                     # 自动确认，直接部署
  bash scripts/apply-bundle.sh -c                     # bundles/ 瘦身：只保留最新一份 bundle
  bash scripts/apply-bundle.sh --clean-all            # 清空所有 bundle
USAGE
}

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

# bundles/ 下文件名时间戳最新的 bundle_*.tgz（纯文件名，无则输出空）
find_newest_bundle() {
    find "$BUNDLES_DIR" -maxdepth 1 -type f -name 'bundle_*.tgz' -printf '%f\n' 2>/dev/null \
        | sort -r | head -n 1
}

# 从 bundles/bundle.meta 读取字段值（KEY=VALUE；取首个命中，去掉行尾 CR）
meta_get() {
    grep -E "^$1=" "$BUNDLES_DIR/$META_FILE" 2>/dev/null \
        | head -n 1 | cut -d '=' -f2- | tr -d '\r'
}

# 读取清单条目（每行一个；目录以 / 结尾；跳过空行与 # 注释）
# 用法: load_manifest_entries <清单文件路径>，条目逐行输出到 stdout
load_manifest_entries() {
    grep -v '^[[:space:]]*#' "$1" 2>/dev/null | grep -v '^[[:space:]]*$' \
        | sed 's/[[:space:]]*$//' | sed '/^$/d'
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
do_clean=0
do_clean_all=0
bundle_path=""
while [ "$#" -gt 0 ]; do
    case $1 in
        -y|--yes)    assume_yes=1 ;;
        -c|--clean)  do_clean=1 ;;
        --clean-all) do_clean_all=1 ;;
        -h|--help)   usage; exit 0 ;;
        --)          shift; break ;;
        -*)          die "未知参数: $1（用 -h 查看用法）" ;;
        *)           bundle_path=$1 ;;
    esac
    shift
done
while [ "$#" -gt 0 ]; do
    # -- 之后的参数也视为 bundle 路径；出现第二个位置参数时报错
    if [ -n "$bundle_path" ]; then
        die "最多指定一个 bundle 路径: $1"
    fi
    bundle_path=$1
    shift
done
if [ "$do_clean" -eq 1 ] && [ "$do_clean_all" -eq 1 ]; then
    die "-c/--clean 与 --clean-all 不能同时使用"
fi
if [ -n "$bundle_path" ] && { [ "$do_clean" -eq 1 ] || [ "$do_clean_all" -eq 1 ]; }; then
    die "bundle 路径不能与 -c/--clean / --clean-all 同时使用"
fi

# ---------- 清理模式：--clean-all ----------
if [ "$do_clean_all" -eq 1 ]; then
    printf '%s\n' "== --clean-all 将删除 =="
    found=0
    for f in "$BUNDLES_DIR"/bundle_*.tgz; do
        [ -e "$f" ] || continue
        printf '  %s\n' "$f"
        found=$((found + 1))
    done
    if [ -e "$BUNDLES_DIR/sync.env" ]; then
        printf '  %s\n' "$BUNDLES_DIR/sync.env（旧版遗留元信息文件）"
        found=$((found + 1))
    fi
    if [ "$found" -eq 0 ]; then
        printf '%s\n' "== $BUNDLES_DIR/ 下没有可清理的 bundle / sync.env =="
        exit 0
    fi
    if ! confirm "确认删除以上 $found 个文件? 输入 y 继续: "; then
        die "已取消"
    fi
    for f in "$BUNDLES_DIR"/bundle_*.tgz; do
        [ -e "$f" ] || continue
        rm -f -- "$f"
    done
    if [ -e "$BUNDLES_DIR/sync.env" ]; then
        rm -f -- "$BUNDLES_DIR/sync.env"
    fi
    printf '%s\n' "== 清理完成 =="
    exit 0
fi

# ---------- 清理模式：--clean（保留最新一份） ----------
if [ "$do_clean" -eq 1 ]; then
    newest=$(find_newest_bundle)
    if [ -z "$newest" ]; then
        die "$BUNDLES_DIR/ 下没有 bundle_*.tgz"
    fi
    printf '%s\n' "== --clean 将保留最新 bundle，删除其余 =="
    printf '  保留: %s/%s\n' "$BUNDLES_DIR" "$newest"
    found=0
    for f in "$BUNDLES_DIR"/bundle_*.tgz; do
        [ -e "$f" ] || continue
        if [ "${f##*/}" = "$newest" ]; then
            continue
        fi
        printf '  删除: %s\n' "$f"
        found=$((found + 1))
    done
    if [ "$found" -eq 0 ]; then
        printf '%s\n' "== 只有最新一份 bundle，无需清理 =="
        exit 0
    fi
    if ! confirm "确认删除以上 $found 个文件? 输入 y 继续: "; then
        die "已取消"
    fi
    for f in "$BUNDLES_DIR"/bundle_*.tgz; do
        [ -e "$f" ] || continue
        if [ "${f##*/}" = "$newest" ]; then
            continue
        fi
        rm -f -- "$f"
    done
    printf '%s\n' "== 清理完成 =="
    exit 0
fi

# ---------- 默认模式：校验 bundle 后按清单覆盖 ----------

total=0
for f in "$BUNDLES_DIR"/bundle_*.tgz; do
    [ -e "$f" ] || continue
    total=$((total + 1))
done

if [ -n "$bundle_path" ]; then
    # 位置参数指定的 bundle：解析路径并做基础校验
    case "$bundle_path" in
        /*) archive=$bundle_path ;;
        *)  archive="$PWD/$bundle_path" ;;
    esac
    [ -f "$archive" ] || die "指定的 bundle 不存在: $archive"
    newest=${archive##*/}
    case "$newest" in
        bundle_*.tgz) ;;
        *) printf '%s\n' "!! 文件名不符合 bundle_*.tgz 约定: $newest（仍按指定路径继续）" ;;
    esac
else
    # 1. 按文件名时间戳找最新 bundle
    newest=$(find_newest_bundle)
    if [ -z "$newest" ]; then
        die "$BUNDLES_DIR/ 下未找到 bundle_*.tgz（请先在本地项目根运行 push-bundle.sh 上传，或用位置参数指定 bundle 路径，或用 -h 查看用法）"
    fi
    archive="$BUNDLES_DIR/$newest"
fi

# 2. bundle.meta 完整性校验（文件名一致性 + SHA256）
command -v sha256sum >/dev/null 2>&1 || die "缺少 sha256sum 命令（coreutils）"
meta_checked=0
if [ -f "$BUNDLES_DIR/$META_FILE" ]; then
    meta_bundle=$(meta_get BUNDLE_FILE)
    if [ -n "$meta_bundle" ] && [ "$meta_bundle" = "$newest" ]; then
        meta_sha=$(meta_get SHA256)
        [ -n "$meta_sha" ] || die "$META_FILE 缺少 SHA256 字段（请重新 push）"
        meta_time=$(meta_get PUBLISHED_AT)
        digest=$(sha256sum "$archive" | awk '{print $1}')
        if [ "$digest" != "$meta_sha" ]; then
            die "SHA256 校验失败: bundle=$digest meta=$meta_sha（传输可能损坏或不完整，请重新 push）"
        fi
        meta_checked=1
    else
        printf '%s\n' "!! $META_FILE 记录的是 $meta_bundle，与要应用的 bundle($newest) 不一致，跳过 meta 校验"
    fi
else
    printf '%s\n' "!! 缺少 $BUNDLES_DIR/$META_FILE，跳过 meta 校验（完整性仅靠解压验证）"
fi

# 3. 读取清单：优先本地 scripts/manifest.txt，缺失时回退 meta 的 MANIFEST 快照
entries_file=$(mktemp)
tmp=""
trap 'rm -rf -- "$entries_file" ${tmp:+"$tmp"}' EXIT
trap 'exit 1' INT TERM
if [ -f "$MANIFEST_FILE" ]; then
    load_manifest_entries "$MANIFEST_FILE" > "$entries_file"
    if [ "$meta_checked" = "1" ]; then
        meta_manifest=$(meta_get MANIFEST)
        local_manifest=$(tr '\n' ',' < "$entries_file" | sed 's/,$//')
        if [ -n "$meta_manifest" ] && [ "$meta_manifest" != "$local_manifest" ]; then
            printf '%s\n' "!! bundle.meta 的 MANIFEST 与本地 scripts/manifest.txt 不一致（以本地清单为准）"
            printf '   meta:   %s\n' "$meta_manifest"
            printf '   local:  %s\n' "$local_manifest"
        fi
    fi
elif [ "$meta_checked" = "1" ] && [ -n "$(meta_get MANIFEST)" ]; then
    printf '%s\n' "!! 本地缺少 $MANIFEST_FILE，使用 bundle.meta 的 MANIFEST 快照"
    meta_get MANIFEST | tr ',' '\n' > "$entries_file"
else
    die "缺少 $MANIFEST_FILE 且 bundle.meta 无 MANIFEST 快照，无法确定部署目标（请先在本地项目根运行 push-bundle.sh，或手动恢复 manifest.txt）"
fi
if ! [ -s "$entries_file" ]; then
    die "清单没有有效条目（$MANIFEST_FILE 每行一个文件或目录，目录以 / 结尾）"
fi

# 校验清单条目安全性：禁止绝对路径 / .. / 空路径
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    case "$entry" in
        /*) die "清单条目必须是相对路径: $entry" ;;
        *..*) die "清单条目不允许包含 ..: $entry" ;;
    esac
done < "$entries_file"

# 4. 展示将要发生什么，等待手动确认
printf '%s\n' "== 执行 apply 将发生 =="
printf '  压缩包: %s%s\n' "$archive" \
    "$([ "$bundle_path" = "" ] && printf '（最新一份，目录下共 %d 份）' "$total" || printf '（指定路径）')"
if [ "$meta_checked" = "1" ]; then
    printf '  发布时间: %s\n' "${meta_time:-(meta 未记录)}"
    printf '  SHA256: 校验通过（%s）\n' "$digest"
else
    printf '  SHA256: 未校验（无匹配的 bundle.meta）\n'
fi
printf '  目标条目（来自清单）:\n'
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    printf '    %s\n' "$entry"
done < "$entries_file"
printf '  前置钩子: make down 下线服务\n'
printf '  备份: 清单中已存在的条目打包为 %s/%s<时间戳>.tgz\n' "$BACKUPS_DIR" "$BACKUP_PREFIX"
printf '  覆盖后不会自动运行任何代码，需要手动 make up 上线\n'
if ! confirm "确认执行 apply? 输入 y 继续: "; then
    die "已取消"
fi

# 5. 前置钩子: make down 下线服务（失败仅告警——服务可能本就未运行）
printf '%s\n' "== 前置钩子: make down（下线服务）=="
make_log=$(mktemp)
if make down >"$make_log" 2>&1; then
    printf '%s\n' "== 服务已下线 =="
else
    cat "$make_log" >&2
    printf '%s\n' "!! make down 未成功（服务可能本就未运行），继续 apply"
fi
rm -f "$make_log"

# 6. 解压到临时目录（bundle 损坏在此暴露，尚未触碰项目根）
tmp=$(mktemp -d)
tar -xzf "$archive" -C "$tmp"

# push-bundle.sh 打包的是清单条目的相对路径（无包裹目录）；历史 src/ 包仅
# 有一个顶层目录 src/ 时进入该目录按其内容处理
if [ -d "$tmp/src" ] && [ ! -e "$tmp/app" ]; then
    printf '%s\n' "!! bundle 顶层为 src/（旧版格式），按 src 内容本身处理"
    mv "$tmp/src" "$tmp/.legacy-root"
    mv "$tmp/.legacy-root"/* "$tmp/" 2>/dev/null || true
    mv "$tmp/.legacy-root"/.[!.]* "$tmp/" 2>/dev/null || true
    rmdir "$tmp/.legacy-root" 2>/dev/null || true
fi

# 7. 备份清单中已存在的条目到 bundles/backups/backup_<时间戳>.tgz
# （同一分钟内多次 apply 时追加 _N 后缀，避免覆盖上一份 backup）
ts=$(date +%m%d-%H%M)
mkdir -p "$BACKUPS_DIR"
backup_list=$(mktemp)
backup=""
n=0
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    rel=${entry%/}
    if [ -e "$rel" ]; then
        printf '%s\n' "$rel" >> "$backup_list"
    fi
done < "$entries_file"
if [ -s "$backup_list" ]; then
    while : ; do
        n=$((n + 1))
        if [ "$n" -eq 1 ]; then
            candidate="$BACKUPS_DIR/${BACKUP_PREFIX}$ts.tgz"
        else
            candidate="$BACKUPS_DIR/${BACKUP_PREFIX}${ts}_$n.tgz"
        fi
        [ -e "$candidate" ] || break
    done
    # tar -T 清单从项目根打包（bundle 内保留相对路径）
    tar -czf "$candidate" -T "$backup_list"
    backup=$candidate
    printf '  已备份旧条目: %s\n' "$backup"
else
    printf '  清单条目在项目根均不存在（首次部署），跳过备份\n'
fi
rm -f "$backup_list"

# 8. 按清单逐条目替换（bundle 中缺失的条目保留远端现有内容并告警）
while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    if [ ! -e "$tmp/$entry" ] && [ ! -L "$tmp/$entry" ]; then
        printf '  !! bundle 中缺少条目 %s，保留远端现有内容\n' "$entry"
        continue
    fi
    rel=${entry%/}
    rm -rf -- "$rel"
    mkdir -p -- "$(dirname "$rel")"
    mv -- "$tmp/$entry" "$rel"
    printf '  已覆盖 %s\n' "$entry"
done < "$entries_file"

printf '%s\n' "== apply 完成: 清单条目已按 bundle 内容覆盖 =="
if [ -n "$backup" ]; then
    printf '  旧条目备份: %s（回滚: 在项目根执行 bash scripts/rollback-bundle.sh）\n' "$backup"
fi
printf '%s\n' "== 请手动执行 make up 上线服务 =="
