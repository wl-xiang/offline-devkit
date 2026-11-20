"""bootstrap 前端库的构建期下载（bootstrap_install=pack_in_artifact 模式）。

从配置的 npm registry 拉取 bootstrap 的 npm 包 tgz（版本默认取 latest dist-tag），
保存为 bootstrap-<version>.tgz，随 Artifact 分发；install.sh 在离线机器上把它
解压到应用源码根目录 <项目根>/src/vendor/bootstrap-<version>。
"""

import json
import re
import urllib.request
from pathlib import Path

from .params import Params

BOOTSTRAP_PACKAGE = "bootstrap"

# 版本号取自 registry 元数据，同时用于产物文件名 bootstrap-<version>.tgz
_VERSION_RE = re.compile(r"^[A-Za-z0-9._+~-]+$")


def _http_get(url, timeout=60):
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def resolve_version(p):
    """解析要下载的 bootstrap 版本：显式指定时直接返回，否则查 latest dist-tag。"""
    if p.bootstrap_version:
        return p.bootstrap_version
    url = f"{p.npm_registry_url.rstrip('/')}/{BOOTSTRAP_PACKAGE}/latest"
    meta = json.loads(_http_get(url).decode("utf-8"))
    version = str(meta.get("version", "")).strip()
    if not _VERSION_RE.match(version):
        raise RuntimeError(f"registry 返回的 bootstrap 版本号异常: {version!r}")
    return version


def download_bootstrap_tgz(p, out_dir):
    """下载 bootstrap npm 包 tgz 到 out_dir/bootstrap-<version>.tgz，返回路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    version = resolve_version(p)
    url = (f"{p.npm_registry_url.rstrip('/')}/{BOOTSTRAP_PACKAGE}"
           f"/-/bootstrap-{version}.tgz")
    dest = out_dir / f"bootstrap-{version}.tgz"
    print(f"== 下载 bootstrap: {url} ==")
    data = _http_get(url)
    if not data:
        raise RuntimeError(f"bootstrap tgz 下载为空: {url}")
    dest.write_bytes(data)
    print(f"== bootstrap 产物: {dest} ({dest.stat().st_size} bytes) ==")
    return dest
