#!/bin/sh
# 部署服务器端脚本：读取 sync.env，把代码 tgz 压缩包解压替换到 ./src 目录。
# 纯 POSIX sh 实现——部署服务器无需 python/jq：tgz 解压只需 tar，校验只需
# sha256sum，均为 Linux 系统自带。
#
# 用法（在接收 tgz 压缩包与 sync.env 的目录中执行）:
#     sh flush.sh            # 校验 + 展示计划 + 输入 y 确认后替换 ./src
#     sh flush.sh -y         # 跳过交互确认直接执行（也支持 --yes）
#
# 协议说明:
#     部署目录固定为 ./src —— 与 install.sh 默认把源码放入 ../src/
#     （项目根 ../ 下的 src）一致
#     （compose 模板中 ${SRC_ROOT:-./src/} 把该目录挂载为容器内 /app）。
#
# 流程:
#     1. 读取 sync.env（KEY=value 格式），取打包文件名与 SHA256 并校验压缩包
#     2. 展示将要发生什么，等待手动输入 y 确认（-y 跳过）
#     3. 清空 ./src 目录下的所有内容
#     4. 解压压缩包内容到 ./src（不会自动运行任何代码）

set -eu

META_NAME="sync.env"
SRC_DIR="src"

die() {
    printf '错误: %s\n' "$*" >&2
    exit 1
}

# ---------- 参数 ----------
assume_yes=0
if [ "$#" -gt 0 ]; then
    case $1 in
        -y|--yes) assume_yes=1 ;;
        *) die "未知参数: $1（用法: sh flush.sh [-y|--yes]）" ;;
    esac
fi

# ---------- 1. 读取 sync.env（逐行 KEY=value 白名单解析；不 source/eval，
# sync.env 是经网络传输的文件，source 它等于执行任意代码） ----------
here=$PWD
meta_path="$here/$META_NAME"
[ -f "$meta_path" ] || die "未找到 $meta_path（请在与 tgz 压缩包、sync.env 同级的目录中运行 flush.sh）"

pack_time=""
filename=""
digest=""
while IFS= read -r line || [ -n "$line" ]; do
    case $line in ''|'#'*) continue ;; esac
    case $line in *=*) ;; *) continue ;; esac
    key=${line%%=*}
    value=${line#*=}
    case $key in
        SYNC_PACK_TIME) pack_time=$value ;;
        SYNC_FILENAME)  filename=$value ;;
        SYNC_SHA256)    digest=$value ;;
    esac
done < "$meta_path"

[ -n "$filename" ] || die "sync.env 缺少 SYNC_FILENAME 字段"
archive="$here/$filename"
[ -f "$archive" ] || die "压缩包不存在: $archive"

# ---------- SHA256 校验（与 sync.env 中记录的 checksum 对比，损坏即中止）----------
command -v sha256sum >/dev/null 2>&1 || die "缺少 sha256sum 命令（coreutils）"
actual=$(sha256sum "$archive" | awk '{print $1}')
if [ -n "$digest" ] && [ "$actual" != "$digest" ]; then
    die "SHA256 校验失败，文件可能损坏或不完整，已中止:
  sync.env: $digest
  实际:     $actual"
fi
if [ -n "$digest" ]; then
    checksum_note="已通过校验"
else
    checksum_note="sync.env 未提供 sha256，跳过校验"
fi

# ---------- 2. 展示将要发生什么，等待手动确认 ----------
src="$here/$SRC_DIR"
printf '%s\n' "== 执行 flush 将发生 =="
printf '  压缩包: %s（tgz，打包时间 %s）\n' "$filename" "${pack_time:-未知}"
printf '  SHA256: %s\n' "$checksum_note"
printf '  目标目录: %s\n' "$src"
printf '  将清空 %s/ 下的所有内容，再解压新代码（不会自动运行任何代码）\n' "$SRC_DIR"
if [ "$assume_yes" -eq 0 ]; then
    printf '确认执行 flush? 输入 y 继续: '
    IFS= read -r answer || { printf '\n'; die "已取消"; }
    answer=$(printf '%s' "$answer" | tr -d ' \t' | tr '[:upper:]' '[:lower:]')
    [ "$answer" = "y" ] || die "已取消"
fi

# ---------- 3. 清空 ./src 下的所有内容（目录不存在则创建）----------
mkdir -p "$src"
(
    cd "$src"
    for f in .[!.]* ..?* *; do
        [ -e "$f" ] || [ -L "$f" ] || continue
        rm -rf -- "$f"
    done
)

# ---------- 4. 解压到临时目录，剥掉单层顶层目录后把内容移入 ./src ----------
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
trap 'exit 1' INT TERM

tar -xzf "$archive" -C "$tmp"

# sync.py 打包时顶层固定为项目根目录名：仅一个顶层条目且为目录时进入该目录
top=$tmp
if [ "$(find "$tmp" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 ]; then
    single=$(find "$tmp" -mindepth 1 -maxdepth 1)
    [ -d "$single" ] && top=$single
fi

(
    cd "$top"
    for f in .[!.]* ..?* *; do
        [ -e "$f" ] || [ -L "$f" ] || continue
        mv -- "$f" "$src/"
    done
)

printf '%s\n' "== flush 完成: 新代码已就位 $src =="
