#!/usr/bin/env python3
"""把本地项目 ./src 源码打包为 SRC Bundle（含 bundle.meta）并推送到远端部署服务器。

打包协议:
    - 只打包项目根目录下 ./src 的全部内容（真正的 SRC Bundle，不携带项目
      其它文件，不会污染远端源码目录）
    - 产物写入本地 ./bundles/（被 .gitignore 忽略）:
        bundles/bundle_<项目名>_<mmdd-HHMM>.tgz     源码包
        bundles/bundle.meta                          元信息（KV，与 .env 同格式）
    - bundle.meta 与 tgz 一起上传到远端 <SYNC_REMOTE_PATH>/bundles/，
      stage-bundle.sh 据此校验完整性（BUNDLE_FILE 匹配 + SHA256 比对）

用法（在本地项目根目录内执行）:
    ./publish-bundle.sh                 # 按 .gitignore 忽略打包 + 确认后上传
    ./publish-bundle.sh -y              # 跳过交互确认直接上传
    ./publish-bundle.sh --no-ignore     # 不读 .gitignore，全量打包 ./src
    ./publish-bundle.sh --no-ignore --rm  # 同上，上传结束后删除本地 bundle

选项:
    -y, --yes        跳过交互确认直接执行
    --no-ignore      不读取项目根目录 .gitignore，全量打包（默认会读取
                     .gitignore 并忽略其中文件，同时始终排除 .git/ 目录；
                     忽略规则按 src/ 内的相对路径匹配）
        --rm         上传操作结束后删除本地 bundle 与 meta（无论传输成功与否）
    -h, --help       打印本用法

环境变量（见同目录 .env.example，可复制为 .env 填写）:
    SYNC_REMOTE_HOST               远端服务器 IP / 主机名（必填）
    SYNC_REMOTE_PORT               SSH 端口（留空默认 22）
    SYNC_REMOTE_USER               SSH 用户名（必填）
    SYNC_REMOTE_AUTH_TYPE          鉴权类型: password（默认）| sshkey
    SYNC_REMOTE_PASSWORD           SSH 密码（AUTH_TYPE=password 时必填；
                                   SYNC_REMOTE_PASSWORD_ENCRYPTED=true 时
                                   必须是 Base64 编码值）
    SYNC_REMOTE_PASSWORD_ENCRYPTED  密码是否为 Base64 编码（默认 true）
    SYNC_REMOTE_KEY_PATH           SSH 私钥路径（sshkey 时用，留空默认
                                   ~/.ssh/id_rsa）
    SYNC_REMOTE_PATH               远端项目部署路径（必填；bundle 上传到
                                   其下的 bundles/ 子目录）

流程:
    1. 校验当前项目根目录存在 src/（源码目录）
    2. 打包 src/ 内容到 bundles/bundle_<项目名>_<时间戳>.tgz，生成 bundle.meta
    3. 展示将要发生什么，等待手动输入 y 确认（-y 跳过）
    4. 通过 SFTP 把 tgz + bundle.meta 上传到远端 <部署路径>/bundles/
       （上传后请在部署服务器上运行 stage-bundle.sh 完成解压部署）
"""

import argparse
import base64
import hashlib
import os
import re
import sys
import tarfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ARCHIVE_PREFIX = "bundle"
META_NAME = "bundle.meta"
BUNDLES_DIR = "bundles"
SRC_DIR = "src"

# 产物时间戳统一东八区（与构建端约定一致）
TZ_CN = timezone(timedelta(hours=8))

AUTH_TYPES = ("password", "sshkey")

BOOL_TRUE = {"1", "true", "yes", "on"}

ENV_KEYS = (
    "SYNC_REMOTE_HOST",
    "SYNC_REMOTE_PORT",
    "SYNC_REMOTE_USER",
    "SYNC_REMOTE_PASSWORD",
    "SYNC_REMOTE_PASSWORD_ENCRYPTED",
    "SYNC_REMOTE_AUTH_TYPE",
    "SYNC_REMOTE_KEY_PATH",
    "SYNC_REMOTE_PATH",
)


def die(msg):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(1)


def load_env():
    """加载 .env（先当前目录，再脚本所在目录）；已有的进程环境变量优先。"""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in BOOL_TRUE


def decode_password(raw, encrypted):
    """读取 SSH 密码：encrypted=true 时按 Base64 解码（.env 不落明文）。"""
    if not encrypted:
        return raw
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except Exception:
        die(
            "SYNC_REMOTE_PASSWORD 不是有效的 Base64 编码值"
            "（SYNC_REMOTE_PASSWORD_ENCRYPTED=true 时密码必须先编码）。\n"
            "  生成方式: echo -n '你的密码' | base64\n"
            "  或在 .env 中设 SYNC_REMOTE_PASSWORD_ENCRYPTED=false 并填明文（不推荐）"
        )


def load_config():
    env = {k: os.environ.get(k, "").strip() for k in ENV_KEYS}
    missing = [
        k
        for k in ("SYNC_REMOTE_HOST", "SYNC_REMOTE_USER", "SYNC_REMOTE_PATH")
        if not env[k]
    ]
    if missing:
        die(
            "缺少必填环境变量: " + ", ".join(missing)
            + "（参考 .env.example，复制为 .env 填写）"
        )
    try:
        port = int(env["SYNC_REMOTE_PORT"]) if env["SYNC_REMOTE_PORT"] else 22
    except ValueError:
        die(f"SYNC_REMOTE_PORT 必须是整数，当前: {env['SYNC_REMOTE_PORT']!r}")

    auth_type = env["SYNC_REMOTE_AUTH_TYPE"] or "password"
    if auth_type not in AUTH_TYPES:
        die(
            f"SYNC_REMOTE_AUTH_TYPE 必须是 {' / '.join(AUTH_TYPES)}，"
            f"当前: {auth_type!r}"
        )

    password = ""
    key_path = ""
    if auth_type == "password":
        if not env["SYNC_REMOTE_PASSWORD"]:
            die(
                "SYNC_REMOTE_AUTH_TYPE=password 需要配置 SYNC_REMOTE_PASSWORD"
                "（或改用 sshkey 免密鉴权）"
            )
        encrypted = _env_bool("SYNC_REMOTE_PASSWORD_ENCRYPTED", True)
        password = decode_password(env["SYNC_REMOTE_PASSWORD"], encrypted)
    else:
        key_path = os.path.expanduser(env["SYNC_REMOTE_KEY_PATH"] or "~/.ssh/id_rsa")
        if not Path(key_path).is_file():
            die(
                f"SYNC_REMOTE_AUTH_TYPE=sshkey 但私钥不存在: {key_path}\n"
                "  请先用 ssh-keygen 生成密钥并把公钥加入远端服务器的 "
                "~/.ssh/authorized_keys，或在 .env 配置 SYNC_REMOTE_KEY_PATH"
            )

    return {
        "host": env["SYNC_REMOTE_HOST"],
        "port": port,
        "user": env["SYNC_REMOTE_USER"],
        "auth_type": auth_type,
        "password": password,
        "key_path": key_path,
        "path": env["SYNC_REMOTE_PATH"].rstrip("/"),
    }


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- 忽略规则：最小 .gitignore 实现 ----------

def _ignore_regex(pat):
    """把一条 gitignore 模式翻译为 fullmatch 用的正则。

    支持常用语法：* ? [..] 不跨 /；段首 **/ 与 /** 匹配任意层级；
    \\ 转义按字面处理；其余字符按字面处理。
    """
    if pat == "**":
        return re.compile(r".+")
    out = ""
    i = 0
    n = len(pat)
    while i < n:
        c = pat[i]
        if c == "\\" and i + 1 < n:
            out += re.escape(pat[i + 1])
            i += 2
        elif pat.startswith("/**/", i):
            out += "/(?:.*/)?"
            i += 4
        elif pat.startswith("/**", i):
            out += "/.*"
            i += 3
        elif pat.startswith("**/", i):
            out += "(?:[^/]+/)*"
            i += 3
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        elif c == "[":
            end = pat.find("]", i + 1)
            if end == -1:
                out += re.escape("[")
                i += 1
            else:
                inner = pat[i + 1 : end]
                if not inner:
                    out += re.escape("[]")
                elif inner.startswith("!"):
                    out += "[^" + inner[1:] + "]"
                else:
                    out += "[" + inner + "]"
                i = end + 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(out)


def load_ignore(project_dir):
    """读取项目根目录 .gitignore，返回规则列表
    (模式, 是否取反, 仅目录, 是否锚定, 正则)；无论是否存在都排除 .git/。
    规则按 src/ 内的相对路径匹配（锚定模式相对 src/ 根）。"""
    patterns = [(".git", False, True, True, re.compile(r"\.git"))]
    path = project_dir / ".gitignore"
    if not path.is_file():
        print("  注意: 未找到 .gitignore，仅排除 .git/ 目录")
        return patterns
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
            if not line:
                continue
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
            if not line:
                continue
        anchored = line.startswith("/") or "/" in line
        line = line.lstrip("/")
        if not line:
            continue
        patterns.append((line, negated, dir_only, anchored, _ignore_regex(line)))
    return patterns


def _is_ignored(rel, is_dir, patterns):
    """gitignore 语义：按文件顺序最后一条命中的规则决定是否忽略。"""
    ignored = False
    for _pat, negated, dir_only, anchored, regex in patterns:
        if dir_only and not is_dir:
            continue
        target = rel if anchored else rel.rpartition("/")[2]
        if regex.fullmatch(target):
            ignored = not negated
    return ignored


def _walk_filtered(top, patterns):
    """自顶向下遍历目录；被忽略的目录整棵剪枝。
    产出 (绝对路径, 相对 posix 路径)。"""
    stack = [("", top)]
    while stack:
        rel, d = stack.pop()
        for entry in sorted(d.iterdir()):
            erel = f"{rel}/{entry.name}" if rel else entry.name
            eis_dir = entry.is_dir() and not entry.is_symlink()
            if _is_ignored(erel, eis_dir, patterns):
                continue
            yield entry, erel
            if eis_dir:
                stack.append((erel, entry))


def pack(src_dir, out_file, patterns=None):
    """把 src/ 目录的全部内容打包（包内顶层 = src 的内容，无额外包裹目录）。

    patterns 为 None 时全量打包；否则按忽略规则剪枝（相对 src/ 匹配）。
    """
    with tarfile.open(out_file, "w:gz") as tar:
        if patterns is None:
            for entry in sorted(src_dir.iterdir()):
                tar.add(entry, arcname=entry.name)
            return
        for entry, rel in _walk_filtered(src_dir, patterns):
            tar.add(entry, arcname=rel, recursive=False)


def write_meta(meta_path, fields):
    """生成 bundle.meta（KV 键值对，与 .env 同格式；stage-bundle.sh 解析校验）。"""
    lines = [
        "# bundle.meta —— publish-bundle.sh 自动生成（与 bundle_*.tgz 配套上传）",
        "# stage-bundle.sh 读取本文件校验 bundle 完整性（BUNDLE_FILE + SHA256）；请勿手工编辑。",
    ]
    for key, value in fields:
        lines.append(f"{key}={value}")
    meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_remote_dir(sftp, remote_path):
    """远端目录不存在时逐级创建（等效 mkdir -p，仅支持 POSIX 路径）。"""
    cur = ""
    for part in [p for p in remote_path.rstrip("/").split("/") if p]:
        cur += "/" + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def upload(cfg, local_files):
    try:
        import paramiko
    except ImportError:
        die("需要 paramiko 库: pip install paramiko")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"== 连接 {cfg['user']}@{cfg['host']}:{cfg['port']} (auth={cfg['auth_type']}) ==")
    connect_kwargs = {
        "host": cfg["host"],
        "port": cfg["port"],
        "username": cfg["user"],
        "timeout": 15,
        "allow_agent": False,
        "look_for_keys": False,
    }
    if cfg["auth_type"] == "password":
        connect_kwargs["password"] = cfg["password"]
    else:
        connect_kwargs["key_filename"] = cfg["key_path"]
    client.connect(**connect_kwargs)
    try:
        sftp = client.open_sftp()
        remote_dir = cfg["path"] + "/" + BUNDLES_DIR
        ensure_remote_dir(sftp, remote_dir)
        for local in local_files:
            remote = remote_dir + "/" + local.name
            print(f"  上传 {local.name} -> {remote}")
            sftp.put(str(local), remote)
        sftp.close()
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(
        description="把本地项目 src/ 打包为 SRC Bundle（含 bundle.meta）并推送到远端"
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="跳过交互确认直接执行"
    )
    parser.add_argument(
        "--no-ignore",
        action="store_true",
        help="不读取 .gitignore，全量打包 src/（默认按 .gitignore 忽略文件）",
    )
    parser.add_argument(
        "--rm",
        action="store_true",
        help="上传操作结束后删除本地 bundle 与 bundle.meta（无论传输成功与否）",
    )
    args = parser.parse_args()

    load_env()
    cfg = load_config()

    # 1. 校验源码目录：SRC Bundle 只打包 ./src 的内容
    project_dir = Path.cwd().resolve()
    if project_dir.parent == project_dir:
        die("请在项目目录内运行 publish-bundle.sh（当前位于文件系统根目录）")
    src_dir = project_dir / SRC_DIR
    if not src_dir.is_dir():
        die(
            f"当前项目根目录没有 {SRC_DIR}/ 源码目录（install.sh 默认把应用源码"
            f"放入 <项目根>/{SRC_DIR}）。请确认目录存在后在项目根目录运行。"
        )
    if not any(src_dir.iterdir()):
        die(f"{SRC_DIR}/ 目录为空，没有可打包的源码")

    bundles_dir = project_dir / BUNDLES_DIR
    bundles_dir.mkdir(exist_ok=True)

    # 2. 打包 src/ 内容 + 生成 bundle.meta
    #    文件名: bundle_<根目录名>_<mmdd-HHMM>.tgz（"最新"按文件名时间戳判定）
    now = datetime.now(TZ_CN)
    ts = now.strftime("%m%d-%H%M")
    archive = bundles_dir / f"{ARCHIVE_PREFIX}_{project_dir.name}_{ts}.tgz"
    patterns = None if args.no_ignore else load_ignore(project_dir)
    pack_mode = "full" if args.no_ignore else "gitignore"
    print(f"== 打包 {SRC_DIR}/ -> {BUNDLES_DIR}/{archive.name} (tgz, {pack_mode}) ==")
    pack(src_dir, archive, patterns)
    digest = sha256_of(archive)
    size = archive.stat().st_size
    print(f"  {archive}  ({size / 1024 / 1024:.1f} MB)")
    print(f"  SHA256: {digest}")
    meta_path = bundles_dir / META_NAME
    write_meta(meta_path, [
        ("BUNDLE_FILE", archive.name),
        ("SHA256", digest),
        ("PUBLISHED_AT", now.isoformat(timespec="seconds")),
        ("PROJECT", project_dir.name),
        ("PACK_MODE", pack_mode),
        ("SIZE_BYTES", str(size)),
    ])
    print(f"  {meta_path}（与 bundle 一起上传，供 stage-bundle.sh 校验完整性）")

    # 3. 展示将要发生什么，等待手动确认
    print("== 执行 publish 将发生 ==")
    print(f"  打包方式: tgz（{'全量' if args.no_ignore else '.gitignore 忽略规则'}）")
    print(f"  打包时间: {now.isoformat(timespec='seconds')} (UTC+8)")
    print(f"  上传文件: {BUNDLES_DIR}/{archive.name} + {META_NAME}")
    if args.rm:
        print(f"  传输后删除本地 bundle 与 meta（无论传输成功与否）")
    print(f"  目标服务器: {cfg['user']}@{cfg['host']}:{cfg['port']}")
    print(f"  远端 Bundle 目录: {cfg['path']}/{BUNDLES_DIR}/")
    if not args.yes:
        try:
            answer = input("确认执行 publish? 输入 y 继续: ")
        except (EOFError, KeyboardInterrupt):
            print()
            die("已取消")
        if answer.strip().lower() != "y":
            die("已取消")

    # 4. SFTP 上传 tgz + bundle.meta（--rm: 结束后删除本地产物，无论成功与否）
    if args.rm:
        try:
            upload(cfg, [archive, meta_path])
        finally:
            for f in (archive, meta_path):
                f.unlink(missing_ok=True)
            print("== 已按 --rm 删除本地 bundle 与 meta ==")
    else:
        upload(cfg, [archive, meta_path])
    print("== 上传完成，请在部署服务器上运行 stage-bundle.sh 完成解压部署 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
