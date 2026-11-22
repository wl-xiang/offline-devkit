"""前端框架离线 tgz 的构建期下载（frontend_install=pack_in_artifact 模式）。

从配置的 npm registry 拉取所选前端框架（bootstrap / @tailwindcss/browser）的
npm 包 tgz（版本默认取 latest dist-tag），保存为 <框架标签>-<version>.tgz，
随 Artifact 分发；install.sh 在离线机器上把它解压到应用源码根目录
<项目根>/src/vendor/<框架标签>-<version>。
"""

import json
import re
import urllib.request
from pathlib import Path

from .params import Params

# 版本号取自 registry 元数据，同时用于产物文件名 <框架标签>-<version>.tgz
_VERSION_RE = re.compile(r"^[A-Za-z0-9._+~-]+$")


def _http_get(url, timeout=60):
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


def resolve_version(p):
    """解析要下载的框架包版本：显式指定时直接返回，否则查 latest dist-tag。"""
    if p.frontend_version:
        return p.frontend_version
    package = p.framework_package
    # scoped 包（@tailwindcss/browser）的 dist-tag 路径需把 scope 编码为 %2f
    url = (f"{p.npm_registry_url.rstrip('/')}/"
           f"{package.replace('/', '%2f')}/latest")
    meta = json.loads(_http_get(url).decode("utf-8"))
    version = str(meta.get("version", "")).strip()
    if not _VERSION_RE.match(version):
        raise RuntimeError(f"registry 返回的 {package} 版本号异常: {version!r}")
    return version


def download_framework_tgz(p, out_dir):
    """下载框架 npm 包 tgz 到 out_dir/<框架标签>-<version>.tgz，返回路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    package = p.framework_package
    label = p.framework_vendor_label
    version = resolve_version(p)
    # npm 包 tgz 固定为 /<包名>/-/<包名末段>-<version>.tgz
    # （scoped 包如 @tailwindcss/browser -> browser-<version>.tgz）
    name_part = package.rsplit("/", 1)[-1]
    url = (f"{p.npm_registry_url.rstrip('/')}"
           f"/{package.replace('/', '%2f')}/-/{name_part}-{version}.tgz")
    dest = out_dir / f"{label}-{version}.tgz"
    print(f"== 下载 {label}: {url} ==")
    data = _http_get(url)
    if not data:
        raise RuntimeError(f"{label} tgz 下载为空: {url}")
    dest.write_bytes(data)
    print(f"== {label} 产物: {dest} ({dest.stat().st_size} bytes) ==")
    return dest
