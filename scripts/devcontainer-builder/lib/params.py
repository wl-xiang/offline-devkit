"""输入参数解析、规范化与校验。

环境变量与 .github/workflows/build-devcontainer.yml 的 inputs 一一对应
（大写 + 下划线）。优先级：环境变量 > 默认值。
"""

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# workflow platform 选项 → Docker --platform
PLATFORM_MAP = {
    "linux-amd64": "linux/amd64",
    "linux-arm64": "linux/arm64",
}

# workflow platform 选项 → (docker arch, pip arch)
ARCH_MAP = {
    "linux-amd64": ("amd64", "x86_64"),
    "linux-arm64": ("arm64", "aarch64"),
}

# 预装语言环境（target_codec，单个下拉参数）：python / golang / node24 的组合。
# node24 含 npm + 全局 pnpm + npx（npx 随 npm）。python 版本固定 3.13、
# golang 固定 1.26、node 固定 24（不再单独暴露为输入）。
TARGET_CODEC_CHOICES = (
    "python3.13",
    "python3.13 + node24(npm+pnpm+npx)",
    "golang1.26",
    "golang1.26 + node24(npm+pnpm+npx)",
    "python3.13 + golang1.26",
    "python3.13 + golang1.26 + node24(npm+pnpm+npx)",
)

PYTHON_VERSION = "3.13"
GO_VERSION = "1.26"
NODE_VERSION = "24"

# 外挂 DB 服务（enable_db，单个下拉参数；nginx 不在此列，由 nginx_mode 决定）。
# DB 服务启用时 app 服务自动 depends_on（等待健康检查通过），无需用户配置；
# DB 在 compose 中的服务名固定为 `db`。
DB_SERVICE_CHOICES = (
    "none",
    "postgres16",
    "mysql8.4",
    "mongo9",
)

# 外挂 DB 服务选项 → 镜像引用
DB_SERVICE_IMAGES = {
    "postgres16": "postgres:16",
    "mysql8.4": "mysql:8.4",
    "mongo9": "mongo:9",
}

# nginx 托管模式（nginx_mode，单个下拉参数）：
#   none          = 不启用 nginx（默认）
#   nginx_binary  = nginx 经 APT 安装进镜像内，容器启动时由 docker-run.sh 拉起，
#                   对外端口复用 app_port（容器内 nginx 监听 80 反代到应用端口）
#   nginx_service = 独立 nginx compose 服务（nginx:stable，宿主机 8080 → 容器 80）
# 两种启用模式下根目录都会生成 nginx/ 配置模板目录；web 静态产物
# （febe_separation 布局的 web/dist/）统一托管到 nginx。
NGINX_MODE_CHOICES = ("none", "nginx_binary", "nginx_service")

NGINX_IMAGE = "nginx:stable"

# 源码布局（source_layout，单个下拉参数）：
#   single_app = 单体服务：应用源码只在 app/ 目录（compose 默认挂载 ./app/ → /app）
#   febe_separation = 前后端分离：后端源码在 api/（app 服务挂载 ./api/ → /app），
#             前端源码在 web/（打包出静态产物 web/dist/ 托管到 nginx）
SOURCE_LAYOUT_CHOICES = ("single_app", "febe_separation")

# 各布局的应用源码目录（项目根下的相对路径）：app 服务挂载其中第一个，
# web/ 为前端目录（仅 febe_separation）
SOURCE_LAYOUT_DIRS = {
    "single_app": ("app",),
    "febe_separation": ("api", "web"),
}

# glibc → manylinux 标签（pip 实际使用 manylinux_<glibc>_<arch> 形式，
# 其中 manylinux2014 与 manylinux_2_17 等价）
GLIBC_MANYLINUX = {
    "2.17": "manylinux2014",
    "2.28": "manylinux_2_28",
    "2.34": "manylinux_2_34",
}

PIP_MIRRORS = {
    "pypi.org": "https://pypi.org/simple",
    "aliyun": "https://mirrors.aliyun.com/pypi/simple",
    "thu": "https://pypi.tuna.tsinghua.edu.cn/simple",
}

# dev 镜像额外安装的工具（wheel 会随 wheels/ 一起下载；仅 Python 环境时安装）
DEV_PIP_TOOLS = ("ruff", "pytest", "pytest-cov")

# 预装 Playwright（pip 包 + chromium 浏览器，有头/无头共用同一浏览器二进制）。
# pip 包随 requirements.txt/wheels 走统一安装；浏览器由 Dockerfile 的
# playwright install --with-deps chromium 在构建期下载到 /opt/ms-playwright
PLAYWRIGHT_PIP_PACKAGE = "playwright"

# 预装 Oracle Instant Client 19 时随装的 python-oracledb 版本。
# 锁定 2.5.1：与 Instant Client 19 + Oracle 11.2 Thick 模式的组合经过生产
# 环境验证（参考 https://github.com/wl-xiang/oracle2excel 的 Dockerfile）。
ORACLEDB_PIP_PACKAGE = "oracledb==2.5.1"

# Oracle Instant Client 19 的安装模式：
#   none              = 不安装
#   runtime_deps_only = 只安装运行时依赖并配置环境变量，客户端由 compose 从宿主机挂载
#   full              = 完整安装（构建期下载官方 Basic 包解压进镜像 + 运行时依赖）
ORACLE_MODES = ("none", "runtime_deps_only", "full")

# Oracle Instant Client 的容器内固定挂载点与环境变量地址。
# runtime_deps_only 模式下客户端本体不打进镜像（减小镜像体积），由 compose
# 从宿主机只读挂载；full 模式把客户端解压到同一路径，保持环境变量一致。
ORACLE_CLIENT_DIR = "/opt/oracle/instantclient19"

# npm download Action 产物（packages/）在容器内的只读挂载点
NPM_PACKAGES_CONTAINER_DIR = "/opt/npm-packages"

# 代码同步工具（v-push）运行环境：enable_sync_tool 勾选时构建的最小 Python 镜像
# （构建期联网安装 paramiko），以 compose 服务 `v-push`（profile=v-push）运行
# scripts/push-bundle.sh，宿主机无需 Python/paramiko。
# 镜像名固定为 devcontainer-vpush:py311，与 image_prefix / image_tag 无关：
# 该镜像只提供 Python 3.11 + paramiko，可在多个 devcontainer 之间复用；仅当
# 运行环境本身变更（基础镜像 / Python 版本 / paramiko 依赖）时才更新 tag。
# install.sh 据此判定本机是否已装同版本镜像并跳过其导入（只作用于该镜像）。
SYNC_BASE_IMAGE = "python:3.11-slim-bookworm"
SYNC_IMAGE_NAME = "devcontainer-vpush"
SYNC_IMAGE_TAG = "py311"
SYNC_CONTAINER_WORKDIR = "/project"
SYNC_CONTAINER_HOME = "/home/push"

# 预装前端框架 + 安装方式（css_framework_install，单个下拉参数）：
#   none                        = 不安装（默认）
#   bootstrap_pack_in_artifact  = Bootstrap：构建期下载 bootstrap-<version>.tgz
#                                 随 Artifact 分发，install.sh 安装到
#                                 <项目根>/src/vendor/bootstrap-<version>，
#                                 模板直接引用 dist/ 下的 css/js 静态资源
#   bootstrap_install_via_npm   = Bootstrap：构建期在镜像内 npm 全局安装
#                                 （等效 enable_node：未勾选时自动补装 node+npm 层）
#   tailwind_pack_in_artifact   = Tailwind：构建期下载 tailwindcss-browser-<version>.tgz
#                                 （官方 @tailwindcss/browser 浏览器端运行时，
#                                 <script src> 引入即用，无需 Node 构建）
#   tailwind_install_via_npm    = Tailwind：镜像内全局安装 tailwindcss +
#                                 @tailwindcss/cli 离线构建 CLI
# 版本固定拉最新版（构建期从 registry 解析具体版本号）
CSS_FRAMEWORK_INSTALL_CHOICES = (
    "none",
    "bootstrap_pack_in_artifact",
    "bootstrap_install_via_npm",
    "tailwind_pack_in_artifact",
    "tailwind_install_via_npm",
)

# 框架名（css_framework_install 前半段）→ 展示名（摘要 / README / 提示文案）
FRAMEWORK_NAMES = {
    "none": "",
    "bootstrap": "Bootstrap",
    "tailwind": "Tailwind CSS",
}

# 框架名 → pack_in_artifact 模式下载的 npm 包名。
# tailwind 取 @tailwindcss/browser：Tailwind 官方的浏览器端运行时，
# dist/index.global.js 单文件自包含，<script src> 引入即在浏览器端编译
# Tailwind 类名，离线可用、无需 Node 构建步骤
FRAMEWORK_PACK_PACKAGES = {
    "none": "",
    "bootstrap": "bootstrap",
    "tailwind": "@tailwindcss/browser",
}

# 框架名 → vendor 目录 / 产物文件名标签。npm scoped 包名含 @ 与 /
# 不能直接做文件名，tailwind 统一使用 tailwindcss-browser
FRAMEWORK_VENDOR_LABELS = {
    "none": "",
    "bootstrap": "bootstrap",
    "tailwind": "tailwindcss-browser",
}

# 框架名 → install_via_npm 模式在镜像内 npm 全局安装的包。
# Tailwind v4 的 CLI 独立发布为 @tailwindcss/cli（提供 tailwindcss 命令），
# 与引擎 tailwindcss 按相同版本号发布
FRAMEWORK_NPM_PACKAGES = {
    "none": (),
    "bootstrap": ("bootstrap",),
    "tailwind": ("tailwindcss", "@tailwindcss/cli"),
}

# pack_in_artifact 模式下前端框架在应用源码根目录（<项目根>/src，容器 /app）
# 内的安装位置
FRAMEWORK_VENDOR_DIR = "vendor"

# 前端完整技术栈（enable_fe_framework，单个下拉参数）：
#   none = 不安装
#   vue  = 自动启用 Node.js 24（npm + pnpm + npx），并在 **dev 镜像** 内预装
#          Vue 3 + TypeScript + Vite Plus 完整技术栈；prod 镜像只保留后端依赖，
#          静态产物由 nginx 托管（前端依赖不进 prod 镜像，减小体积）
FE_FRAMEWORK_CHOICES = ("none", "vue")

VUE_STACK_NAME = "Vue 3 + TypeScript + Vite Plus"

# 全局安装的 Vue 工具链 CLI（构建期联网安装，运行期离线可用）
VUE_GLOBAL_CLI_PACKAGES = (
    "vite-plus",
    "vue-tsc",
    "stylelint",
    "@commitlint/cli",
    "@commitlint/config-conventional",
    "@playwright/test",
    "rollup-plugin-visualizer",
    "shadcn-vue",
    "tailwindcss",
    "@tailwindcss/cli",
)

# Vue 项目模板在镜像内的固定位置（含 node_modules，make vue-init 从此复制）
VUE_TEMPLATE_DIR = "/opt/vue-template"

# Playwright 浏览器固定目录（与 enable_playwright 的 pip Playwright 共用）
VUE_PLAYWRIGHT_DIR = "/opt/ms-playwright"

# 预设环境变量的分隔符（workflow 输入框无法换行）
ENV_PRESET_SEPARATOR = "》"

# 与 .github/workflows/npm_download.yml 的 registry 选项保持一致
NPM_REGISTRIES = {
    "npmmirror (taobao)": "https://registry.npmmirror.com",
    "Tencent": "https://mirrors.cloud.tencent.com/npm/",
    "Huawei": "https://repo.huaweicloud.com/repository/npm/",
    "THU (tuna)": "https://mirrors.tuna.tsinghua.edu.cn/npm/",
    "USTC": "https://mirrors.ustc.edu.cn/npm/",
    "Official (npmjs.org)": "https://registry.npmjs.org",
}

NPM_OTHER_REGISTRY = "Other (custom URL)"

# 自定义 npm 源会拼进 Dockerfile 的 RUN 命令，限制为安全的 URL 字符
NPM_REGISTRY_URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/\[\]@%+=,-]+$")

# npm 离线包目录会拼进 compose / Makefile / install.sh，限制为安全路径字符
NPM_PACKAGES_DIR_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# npm 包名（可带 @scope/ 前缀与 @version 后缀）；刻意排除空格与 shell 元字符，
# 因为包名会直接拼进 Dockerfile 的 RUN 命令
NPM_PACKAGE_RE = re.compile(
    r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(@[a-z0-9][a-z0-9._-]*)?$",
    re.IGNORECASE,
)

# 产物/镜像时间戳统一使用东八区（UTC+8）
TZ_CN = timezone(timedelta(hours=8), name="UTC+8")

# image_tag 选项：latest（默认）或本次构建时间戳
IMAGE_TAG_CHOICES = ("latest", "timestamp")

IMAGE_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

BOOL_TRUE = {"1", "true", "yes", "on"}

# 内置（不可配置）：app/dev 服务只读挂载的宿主机时区配置文件，
# 以及镜像内时区（构建期自动补装 tzdata）
TIMEZONE_MOUNTS = ("/etc/localtime:/etc/localtime:ro",
                   "/etc/timezone:/etc/timezone:ro")
TZ_SHANGHAI = "Asia/Shanghai"

# 容器默认启动脚本（SRC Bundle 内 docker-run.sh，安装到项目根后由 compose
# 只读挂载覆盖镜像内副本，Dockerfile ENTRYPOINT 指向它）
DOCKER_RUN_CONTAINER_PATH = "/usr/local/bin/docker-run.sh"


class ParamError(ValueError):
    """参数校验失败。"""


def _env(name, default=""):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in BOOL_TRUE


@dataclass
class Params:
    platform: str
    target_codec: str
    glibc_version: str
    pip_mirror: str
    pip_packages: str
    apt_packages: str
    custom_install_cmd: str
    enable_playwright: bool
    oracle_instantclient_mode: str
    preset_envs: str
    node_version: str
    go_version: str
    npm_packages: str
    npm_registry: str
    npm_custom_registry_url: str
    npm_packages_dir: str
    css_framework_install: str
    fe_framework: str
    source_layout: str
    nginx_mode: str
    db_service: str
    build_dev_image: bool
    dev_image_only: bool
    image_prefix: str
    app_source_dir: str
    app_port: str
    env_file: str
    selinux_relabel: bool
    enable_sync_tool: bool
    image_tag: str
    ts: str

    # ---------- 派生值 ----------

    @property
    def docker_platform(self):
        return PLATFORM_MAP[self.platform]

    @property
    def arch(self):
        return ARCH_MAP[self.platform][0]

    @property
    def pip_arch(self):
        return ARCH_MAP[self.platform][1]

    @property
    def python_version(self):
        return PYTHON_VERSION

    @property
    def python_enabled(self):
        """target_codec 预装 Python（python3.13）。"""
        return "python3.13" in self.target_codec

    @property
    def go_enabled(self):
        """target_codec 预装 Golang（golang1.26）。"""
        return "golang1.26" in self.target_codec

    @property
    def vue_enabled(self):
        """enable_fe_framework=vue：安装 Vue 3 + TypeScript + Vite Plus 技术栈。"""
        return self.fe_framework == "vue"

    @property
    def node_in_target(self):
        """target_codec 显式预装 Node.js 24（既有行为：prod/dev 都带 Node）。"""
        return "node24" in self.target_codec

    @property
    def enable_node(self):
        """有效启用 Node.js 24（npm + 全局 pnpm + npx）。

        target_codec 含 node24，或 enable_fe_framework=vue（自动激活 Node）。
        """
        return self.node_in_target or self.vue_enabled

    @property
    def node_in_prod(self):
        """prod 镜像是否包含 Node：仅 target_codec 显式含 node24 时。"""
        return self.node_in_target

    @property
    def node_dev_only(self):
        """Node 由 Vue 预设自动带出且 target_codec 未显式含 node24：只装进 dev 镜像。"""
        return self.vue_enabled and not self.node_in_target

    @property
    def vue_dir(self):
        """Vue 项目目录：single_app=app/，febe_separation=web/。"""
        return "web" if self.is_febe_separation else self.app_dir

    @property
    def is_febe_separation(self):
        """前后端分离布局（api/ + web/ 目录）。"""
        return self.source_layout == "febe_separation"

    @property
    def app_dir(self):
        """app 服务挂载的源码目录（项目根下相对路径）：single_app=app，
        febe_separation=api。"""
        return SOURCE_LAYOUT_DIRS[self.source_layout][0]

    @property
    def src_dirs(self):
        """本布局的应用源码目录元组：single_app=(app,)，
        febe_separation=(api, web)。"""
        return SOURCE_LAYOUT_DIRS[self.source_layout]

    @property
    def enable_nginx(self):
        """nginx_mode 启用（binary 或 service）：根目录生成 nginx/ 配置模板。"""
        return self.nginx_mode != "none"

    @property
    def nginx_binary_mode(self):
        """nginx 安装进镜像内（APT），容器启动时由 docker-run.sh 拉起。"""
        return self.nginx_mode == "nginx_binary"

    @property
    def nginx_service_mode(self):
        """独立 nginx compose 服务（nginx:stable，宿主机 8080 → 容器 80）。"""
        return self.nginx_mode == "nginx_service"

    @property
    def enable_postgres(self):
        return "postgres16" in self.db_service

    @property
    def enable_mysql(self):
        return "mysql8.4" in self.db_service

    @property
    def enable_mongo(self):
        return "mongo9" in self.db_service

    @property
    def has_db_service(self):
        """enable_db 是否含 DB 服务（app 自动 depends_on 的依据）。"""
        return self.enable_postgres or self.enable_mysql or self.enable_mongo

    @property
    def pip_platform_tag(self):
        glibc = self.glibc_version.replace(".", "_")
        return f"manylinux_{glibc}_{self.pip_arch}"

    @property
    def manylinux_label(self):
        return GLIBC_MANYLINUX[self.glibc_version]

    @property
    def mirror_url(self):
        return PIP_MIRRORS[self.pip_mirror]

    @property
    def pip_package_list(self):
        return [x.strip() for x in self.pip_packages.split("$") if x.strip()]

    @property
    def preinstall_pip_packages(self):
        """用户 pip 包 + 预装组件（playwright / oracledb）的合并清单。

        用于 requirements.txt 渲染与 pip download；用户已显式写出同名包时
        不重复追加（版本以其声明为准）。仅 target_codec 预装 Python 时使用。
        """
        pkgs = list(self.pip_package_list)
        existing = {x.lower().split("=")[0].split("<")[0].split(">")[0].split("!")[0].strip()
                    for x in pkgs}
        if self.enable_playwright and "playwright" not in existing:
            pkgs.append(PLAYWRIGHT_PIP_PACKAGE)
        if (self.oracle_enabled
                and "oracledb" not in existing):
            pkgs.append(ORACLEDB_PIP_PACKAGE)
        return pkgs

    @property
    def apt_package_list(self):
        return self.apt_packages.split()

    @property
    def dev_tools_enabled(self):
        """是否需要 dev 阶段（仅 dev 镜像模式下唯一镜像即 dev 镜像）。"""
        return self.dev_image_only or self.build_dev_image

    @property
    def dev_pip_tools(self):
        """dev 镜像 Python 工具（ruff/pytest），仅预装 Python 时安装。"""
        return list(DEV_PIP_TOOLS) if (self.dev_tools_enabled
                                       and self.python_enabled) else []

    @property
    def tag(self):
        """镜像 TAG：latest（默认）或本次构建时间戳（UTC+8）。"""
        return self.ts if self.image_tag == "timestamp" else "latest"

    @property
    def prod_image(self):
        """prod 镜像：<前缀>:<TAG>（如 mydevc1:latest）。"""
        return f"{self.image_prefix}:{self.tag}"

    @property
    def dev_image(self):
        """dev 镜像：<前缀>-dev:<TAG>（如 mydevc1-dev:latest）。"""
        return f"{self.image_prefix}-dev:{self.tag}"

    @property
    def sync_image(self):
        """代码同步工具（v-push）镜像：devcontainer-vpush:py311
        （python3.11-slim + paramiko；镜像名与 image_prefix/tag 无关，多套
        devcontainer 共用，运行环境变更时才更新 tag）。"""
        return f"{SYNC_IMAGE_NAME}:{SYNC_IMAGE_TAG}"

    @property
    def arch_label(self):
        """产物文件名中的架构标签：amd64 / arm64（不含 linux- 前缀）。"""
        return self.arch

    @property
    def bundle_name(self):
        """唯一 Artifact 名：<镜像前缀>_devcontainer_<架构>_<时间戳>。"""
        return f"{self.image_prefix}_devcontainer_{self.arch_label}_{self.ts}"

    @property
    def app_image(self):
        """部署（app 服务）使用的镜像：仅 dev 镜像模式下直接用 dev 镜像。"""
        return self.dev_image if self.dev_image_only else self.prod_image

    @property
    def app_build_target(self):
        return "dev" if self.dev_image_only else "prod"

    @property
    def image_refs(self):
        """本次导入/自检需要的镜像清单（与 docker.image_list 保持一致）。"""
        if self.dev_image_only:
            images = [self.dev_image]
        else:
            images = [self.prod_image]
            if self.build_dev_image:
                images.append(self.dev_image)
        if self.enable_sync_tool:
            images.append(self.sync_image)
        if self.nginx_service_mode:
            images.append(NGINX_IMAGE)
        for service in ("postgres16", "mysql8.4", "mongo9"):
            if service in self.db_service:
                images.append(DB_SERVICE_IMAGES[service])
        return images

    @property
    def oracle_enabled(self):
        return self.oracle_instantclient_mode != "none"

    @property
    def oracle_full(self):
        """full 模式：客户端随镜像分发，不需要宿主机挂载。"""
        return self.oracle_instantclient_mode == "full"

    @property
    def app_port_parts(self):
        """(宿主机端口, 容器端口)；app_port 支持 3000 或 3000:3000 两种写法。"""
        if not self.app_port:
            return None
        if ":" in self.app_port:
            host, container = self.app_port.split(":", 1)
        else:
            host = container = self.app_port
        return host, container

    @property
    def has_app_port(self):
        return bool(self.app_port)

    @property
    def app_container_port(self):
        """应用在容器内监听的端口（nginx 反代/expose 使用）。"""
        parts = self.app_port_parts
        return parts[1] if parts else ""

    @property
    def env_file_list(self):
        """env_file 支持逗号或空格分隔。"""
        return [x for x in self.env_file.replace(",", " ").split() if x]

    @property
    def preset_env_pairs(self):
        """预设环境变量：KEY=VALUE，多个用 》 分隔。"""
        pairs = []
        for item in self.preset_envs.split(ENV_PRESET_SEPARATOR):
            item = item.strip()
            if not item:
                continue
            key, _, value = item.partition("=")
            pairs.append((key.strip(), value.strip()))
        return pairs

    @property
    def npm_package_list(self):
        return [x.strip() for x in self.npm_packages.split("$") if x.strip()]

    @property
    def npm_registry_url(self):
        if self.npm_registry == NPM_OTHER_REGISTRY:
            return self.npm_custom_registry_url
        return NPM_REGISTRIES[self.npm_registry]

    @property
    def oracle_client_dir(self):
        """Oracle 客户端在容器内的固定路径（也是 ORACLE_HOME/LD_LIBRARY_PATH）。"""
        return ORACLE_CLIENT_DIR

    @property
    def npm_packages_mount(self):
        """npm 离线包目录：空字符串表示不挂载。"""
        if not self.enable_node or not self.npm_packages_dir:
            return ""
        return f"{self.npm_packages_mount_source}:{NPM_PACKAGES_CONTAINER_DIR}:ro"

    @property
    def framework_name(self):
        """none / bootstrap / tailwind（由 css_framework_install 解析）。"""
        if self.css_framework_install == "none":
            return "none"
        return self.css_framework_install.split("_", 1)[0]

    @property
    def framework_install(self):
        """pack_in_artifact / install_via_npm（css_framework_install=none 时为 none）。"""
        if self.css_framework_install == "none":
            return "none"
        return self.css_framework_install.split("_", 1)[1]

    @property
    def framework_enabled(self):
        """css_framework_install != none：本次构建预装前端框架。"""
        return self.css_framework_install != "none"

    @property
    def framework_pack(self):
        """pack_in_artifact：构建期下载框架离线 tgz 随 Artifact 分发。"""
        return self.framework_install == "pack_in_artifact"

    @property
    def framework_npm(self):
        """install_via_npm：构建期在镜像内 npm 全局安装框架。"""
        return self.framework_install == "install_via_npm"

    @property
    def framework_spec(self):
        """npm / registry 使用的框架版本描述：固定 latest（版本参数已移除）。"""
        return "latest"

    @property
    def framework_package(self):
        """pack_in_artifact 模式下载的 npm 包名（如 bootstrap / @tailwindcss/browser）。"""
        return FRAMEWORK_PACK_PACKAGES[self.framework_name]

    @property
    def framework_vendor_label(self):
        """vendor 目录与产物文件名中的框架标签（如 bootstrap / tailwindcss-browser）。"""
        return FRAMEWORK_VENDOR_LABELS[self.framework_name]

    @property
    def framework_npm_packages(self):
        """install_via_npm 模式在镜像内全局安装的 pkg@<spec> 清单。"""
        return [f"{name}@{self.framework_spec}"
                for name in FRAMEWORK_NPM_PACKAGES[self.framework_name]]

    @property
    def npm_packages_mount_source(self):
        """宿主机目录（相对 docker/ 目录或绝对路径），compose 中可用
        NPM_PACKAGES_DIR 覆盖。compose 项目目录为 docker/（docker-compose.yml
        所在目录），默认 deps/packages 即 docker/deps/packages（init-deps.sh
        生成的离线依赖目录）。"""
        path = self.npm_packages_dir.removeprefix("./")
        if self.npm_packages_dir.startswith("/"):
            return "${NPM_PACKAGES_DIR:-" + self.npm_packages_dir + "}"
        return "${NPM_PACKAGES_DIR:-" + path + "}"

    @property
    def oracle_client_mount(self):
        """Oracle 客户端只读挂载：仅 runtime_deps_only 模式需要。"""
        if self.oracle_instantclient_mode != "runtime_deps_only":
            return ""
        return f"{self.oracle_client_mount_source}:{ORACLE_CLIENT_DIR}:ro"

    @property
    def oracle_client_mount_source(self):
        return "${ORACLE_CLIENT_DIR:-" + ORACLE_CLIENT_DIR + "}"

    @property
    def mount_path(self):
        """容器 /app 的挂载源。${SRC_ROOT:-../app/}（febe_separation 布局为
        ${SRC_ROOT:-../api/}）由 docker compose 在运行时解析（默认 = 项目根下
        的应用源码目录；SRC_ROOT 相对 compose 项目目录 docker/ 解析，../ 即
        项目根）；app_source_dir 为相对 SRC_ROOT 的子目录（或绝对路径）。
        SELinux Enforcing 主机可追加 :z 重打标签。"""
        default_root = "../" + self.app_dir + "/"
        if self.app_source_dir == ".":
            src = "${SRC_ROOT:-" + default_root + "}"
        elif self.app_source_dir.startswith("/"):
            src = self.app_source_dir.rstrip("/") or "/"
        else:
            src = "${SRC_ROOT:-" + default_root + "}/" + self.app_source_dir.strip("/")
        return f"{src}:/app{':z' if self.selinux_relabel else ''}"

    @property
    def timezone_volumes(self):
        """宿主机时区配置只读挂载（内置启用，不可配置）。"""
        return list(TIMEZONE_MOUNTS)

    @property
    def tz_env(self):
        """镜像内时区（内置启用，不可配置）。"""
        return TZ_SHANGHAI

    @property
    def docker_run_mount(self):
        """app/dev 服务把 docker/docker-run.sh 只读挂载覆盖镜像内副本的 compose
        卷（相对路径相对 compose 项目目录 docker/ 解析）。"""
        return f"./docker-run.sh:{DOCKER_RUN_CONTAINER_PATH}:ro"


def load():
    """从环境变量读取参数并校验；失败抛出 ParamError。"""
    params = Params(
        platform=_env("PLATFORM", "linux-arm64"),
        target_codec=_env("TARGET_CODEC", "python3.13"),
        glibc_version=_env("GLIBC_VERSION", "2.28"),
        pip_mirror=_env("PIP_MIRROR", "pypi.org"),
        pip_packages=_env("PIP_PACKAGES", ""),
        apt_packages=_env("APT_PACKAGES", ""),
        custom_install_cmd=_env("CUSTOM_INSTALL_CMD", ""),
        enable_playwright=_env_bool("ENABLE_PLAYWRIGHT", False),
        oracle_instantclient_mode=_env(
            "ENABLE_ORACLE_INSTANTCLIENT", "none"
        ).lower(),
        preset_envs=_env("PRESET_ENVS", ""),
        node_version=_env("NODE_VERSION", NODE_VERSION),
        go_version=_env("GO_VERSION", GO_VERSION),
        npm_packages=_env("NPM_PACKAGES", ""),
        npm_registry=_env("NPM_REGISTRY", "Official (npmjs.org)"),
        npm_custom_registry_url=_env("NPM_CUSTOM_REGISTRY_URL", ""),
        npm_packages_dir=_env("NPM_PACKAGES_DIR", "deps/packages"),
        css_framework_install=_env("CSS_FRAMEWORK_INSTALL", "none").strip().lower(),
        fe_framework=_env("ENABLE_FE_FRAMEWORK", "none").strip().lower(),
        source_layout=_env("SOURCE_LAYOUT", "single_app").strip().lower(),
        nginx_mode=_env("NGINX_MODE", "none").strip().lower(),
        db_service=_env("ENABLE_DB", "none"),
        build_dev_image=_env_bool("BUILD_DEV_IMAGE", True),
        dev_image_only=_env_bool("DEV_IMAGE_ONLY", False),
        image_prefix=_env("IMAGE_PREFIX", "mydevc1"),
        app_source_dir=_env("APP_SOURCE_DIR", "."),
        # app_port 显式置空 = 不映射端口（workflow 总会设置该变量）
        app_port=(os.environ["APP_PORT"].strip()
                  if "APP_PORT" in os.environ else "3000:3000"),
        env_file=_env("ENV_FILE", ""),
        selinux_relabel=_env_bool("SELINUX_RELABEL", False),
        enable_sync_tool=_env_bool("ENABLE_SYNC_TOOL", False),
        image_tag=_env("IMAGE_TAG", "latest"),
        ts=datetime.now(TZ_CN).strftime("%Y%m%d-%H%M%S"),
    )
    return validate(params)


def validate(p):
    if p.platform not in PLATFORM_MAP:
        raise ParamError(
            f"不支持的 platform: {p.platform!r}，可选: {', '.join(PLATFORM_MAP)}"
        )
    if p.target_codec not in TARGET_CODEC_CHOICES:
        raise ParamError(
            f"不支持的 target_codec: {p.target_codec!r}，"
            f"可选: {', '.join(TARGET_CODEC_CHOICES)}"
        )
    if p.db_service not in DB_SERVICE_CHOICES:
        raise ParamError(
            f"不支持的 enable_db: {p.db_service!r}，"
            f"可选: {', '.join(DB_SERVICE_CHOICES)}"
        )
    if p.source_layout not in SOURCE_LAYOUT_CHOICES:
        raise ParamError(
            f"不支持的 source_layout: {p.source_layout!r}，"
            f"可选: {', '.join(SOURCE_LAYOUT_CHOICES)}"
        )
    if p.nginx_mode not in NGINX_MODE_CHOICES:
        raise ParamError(
            f"不支持的 nginx_mode: {p.nginx_mode!r}，"
            f"可选: {', '.join(NGINX_MODE_CHOICES)}"
        )
    if p.glibc_version not in GLIBC_MANYLINUX:
        raise ParamError(
            f"不支持的 glibc_version: {p.glibc_version!r}，"
            f"可选: {', '.join(GLIBC_MANYLINUX)}"
        )
    if p.pip_mirror not in PIP_MIRRORS:
        raise ParamError(
            f"不支持的 pip_mirror: {p.pip_mirror!r}，可选: {', '.join(PIP_MIRRORS)}"
        )
    if p.image_tag not in IMAGE_TAG_CHOICES:
        raise ParamError(
            f"不支持的 image_tag: {p.image_tag!r}，可选: {', '.join(IMAGE_TAG_CHOICES)}"
        )
    if p.oracle_instantclient_mode not in ORACLE_MODES:
        raise ParamError(
            f"不支持的 enable_oracle_instantclient: {p.oracle_instantclient_mode!r}，"
            f"可选: {', '.join(ORACLE_MODES)}"
        )
    if p.css_framework_install not in CSS_FRAMEWORK_INSTALL_CHOICES:
        raise ParamError(
            f"不支持的 css_framework_install: {p.css_framework_install!r}，"
            f"可选: {', '.join(CSS_FRAMEWORK_INSTALL_CHOICES)}"
        )
    if p.fe_framework not in FE_FRAMEWORK_CHOICES:
        raise ParamError(
            f"不支持的 enable_fe_framework: {p.fe_framework!r}，"
            f"可选: {', '.join(FE_FRAMEWORK_CHOICES)}"
        )
    # Python 生态参数仅在 target_codec 预装 Python 时可用
    if not p.python_enabled:
        if p.pip_package_list:
            raise ParamError(
                "pip_packages 需要 target_codec 预装 Python"
                "（python3.13 / python3.13 + golang1.26 系列）"
            )
        if p.enable_playwright:
            raise ParamError(
                "enable_playwright 需要 target_codec 预装 Python"
                "（python3.13 / python3.13 + golang1.26 系列）"
            )
        if p.oracle_enabled:
            raise ParamError(
                "enable_oracle_instantclient 需要 target_codec 预装 Python"
                "（python3.13 / python3.13 + golang1.26 系列）"
            )
    # *_install_via_npm 需要镜像内有 npm：target_codec 须含 node24
    if (p.css_framework_install.endswith("_install_via_npm")
            and not p.enable_node):
        raise ParamError(
            f"css_framework_install={p.css_framework_install} 需要 target_codec "
            "预装 Node.js（node24 系列）"
        )
    if p.npm_registry not in NPM_REGISTRIES and p.npm_registry != NPM_OTHER_REGISTRY:
        raise ParamError(
            f"不支持的 npm_registry: {p.npm_registry!r}，可选: "
            f"{', '.join(NPM_REGISTRIES)}, {NPM_OTHER_REGISTRY}"
        )
    if p.node_version != NODE_VERSION:
        raise ParamError(
            f"不支持的 node_version: {p.node_version!r}（固定 {NODE_VERSION}）"
        )
    if p.npm_registry == NPM_OTHER_REGISTRY and not p.npm_custom_registry_url:
        raise ParamError(
            "npm_registry 为 'Other (custom URL)' 时必须填写 npm_custom_registry_url"
        )
    if p.npm_registry == NPM_OTHER_REGISTRY and not NPM_REGISTRY_URL_RE.match(
        p.npm_custom_registry_url
    ):
        raise ParamError(
            "npm_custom_registry_url 非法（需 http(s):// 开头，且不含空白/引号/$ 等字符）: "
            f"{p.npm_custom_registry_url!r}"
        )
    if p.npm_packages and not p.enable_node:
        raise ParamError(
            "npm_packages 需要 target_codec 预装 Node.js（node24 系列）"
        )
    for pkg in p.npm_package_list:
        if not NPM_PACKAGE_RE.match(pkg):
            raise ParamError(
                f"npm 包名非法（仅允许 name / @scope/name，可带 @版本；"
                f"多个包请用 $ 分隔）: {pkg!r}"
            )
    if not NPM_PACKAGES_DIR_RE.match(p.npm_packages_dir):
        raise ParamError(
            f"npm_packages_dir 非法（仅允许字母/数字/./_/-，相对项目根或绝对路径，"
            f"不支持 ~ 展开）: {p.npm_packages_dir!r}"
        )

    # 预设环境变量：KEY=VALUE，多个用 》 分隔；校验名称并拒绝重复键
    seen_env = set()
    for item in p.preset_envs.split(ENV_PRESET_SEPARATOR):
        item = item.strip()
        if not item:
            continue
        key, sep, _value = item.partition("=")
        key = key.strip()
        if not sep:
            raise ParamError(
                f"预设环境变量必须为 KEY=VALUE 形式: {item!r}"
                "（多个变量用 》 分隔）"
            )
        if not ENV_NAME_RE.match(key):
            raise ParamError(f"预设环境变量名非法: {key!r}")
        if key in seen_env:
            raise ParamError(f"预设环境变量重复: {key!r}")
        seen_env.add(key)

    if not IMAGE_PREFIX_RE.match(p.image_prefix):
        raise ParamError(
            f"image_prefix 非法（仅允许小写字母/数字/./_/-，且以字母或数字开头）: "
            f"{p.image_prefix!r}"
        )
    if not p.app_source_dir.strip():
        raise ParamError("app_source_dir 不能为空")
    if p.app_source_dir.startswith("~"):
        raise ParamError(
            "app_source_dir 不支持 ~ 展开，请写绝对路径或在应用根目录使用 '.'"
        )
    if p.app_port:
        parts = p.app_port.split(":")
        if len(parts) > 2 or not all(
            x.isdigit() and 1 <= int(x) <= 65535 for x in parts
        ):
            raise ParamError(
                "app_port 必须是 1-65535 的端口号，或 host:container 端口映射"
                f"（如 3000:3000）: {p.app_port!r}"
            )
        if len(parts) == 1:
            p.app_port = f"{int(parts[0])}:{int(parts[0])}"
        else:
            p.app_port = f"{int(parts[0])}:{int(parts[1])}"
    for path in p.env_file_list:
        if path.startswith("~"):
            raise ParamError(f"env_file 不支持 ~ 展开: {path!r}")

    for pkg in p.pip_package_list:
        if any(ch.isspace() for ch in pkg):
            raise ParamError(f"pip 包名含空白字符（多个包请用 $ 分隔）: {pkg!r}")

    # 规范化挂载路径：去掉多余的尾部斜杠
    if len(p.app_source_dir) > 1 and p.app_source_dir != "/":
        p.app_source_dir = p.app_source_dir.rstrip("/") or "."
    # Vue 前端技术栈只安装在 dev 镜像；prod-only 时自动补建 dev 镜像，
    # 保证 prod 镜像保持只含后端依赖（前端静态产物由 nginx 托管）
    if p.vue_enabled and not p.dev_tools_enabled:
        p.build_dev_image = True
    return p


# ---------------------------------------------------------------------------
# Workflow 输入参数快照（devcontainer.json）
#
# 与 .github/workflows/build-devcontainer.yml 的 workflow_dispatch inputs
# 一一对应：(输入名, 环境变量, 类型, workflow 默认值)。新增/修改 workflow
# 输入时必须同步本表（含默认值），否则快照过滤会失真。enable_db 在 build
# 步骤以原始值透传（enable_postgres 等派生布尔不含原始值，不在此列）。
# ---------------------------------------------------------------------------
WORKFLOW_INPUTS = (
    ("platform", "PLATFORM", "choice", "linux-amd64"),
    ("target_codec", "TARGET_CODEC", "choice", "python3.13"),
    ("source_layout", "SOURCE_LAYOUT", "choice", "single_app"),
    ("pip_packages", "PIP_PACKAGES", "string", ""),
    ("apt_packages", "APT_PACKAGES", "string", ""),
    ("custom_install_cmd", "CUSTOM_INSTALL_CMD", "string", ""),
    ("enable_playwright", "ENABLE_PLAYWRIGHT", "boolean", "false"),
    ("enable_oracle_instantclient", "ENABLE_ORACLE_INSTANTCLIENT", "choice", "none"),
    ("preset_envs", "PRESET_ENVS", "string", ""),
    ("npm_packages", "NPM_PACKAGES", "string", ""),
    ("npm_packages_dir", "NPM_PACKAGES_DIR", "string", "deps/packages"),
    ("css_framework_install", "CSS_FRAMEWORK_INSTALL", "choice", "none"),
    ("enable_fe_framework", "ENABLE_FE_FRAMEWORK", "choice", "none"),
    ("nginx_mode", "NGINX_MODE", "choice", "none"),
    ("enable_db", "ENABLE_DB", "choice", "none"),
    ("image_mode", "IMAGE_MODE", "choice", "prod-only"),
    ("image_prefix", "IMAGE_PREFIX", "string", "mydevc1"),
    ("image_tag", "IMAGE_TAG", "choice", "latest"),
    ("app_source_dir", "APP_SOURCE_DIR", "string", "."),
    ("app_port", "APP_PORT", "string", "3000:3000"),
    ("env_file", "ENV_FILE", "string", ""),
    ("selinux_relabel", "SELINUX_RELABEL", "boolean", "false"),
    ("enable_sync_tool", "ENABLE_SYNC_TOOL", "boolean", "false"),
)


def workflow_inputs_snapshot():
    """读取原始 workflow 输入，生成 minimal/normal/full 三层参数快照 + meta。

    值为 GitHub 前端提交的原始字符串（checkbox 解析为布尔，其余不做规范化）；
    本地联调未设置的环境变量视为未提供（字符串记 ""、checkbox 记 false）。
    键顺序与 workflow inputs 定义顺序一致。

    - full：记录全部输入（留空字符串记 ""、未勾选 checkbox 记 false）
    - normal：省略留空字符串与默认值 checkbox
    - minimal：再省略取默认值的参数（含 choice），只保留用户实际改动过的输入

    另附顶层 `meta` section（非输入参数）：记录本次构建的来源信息，当前包含
    `repo` —— 构建仓库的 GitHub URL（来自 CI 环境自动提供的
    GITHUB_SERVER_URL / GITHUB_REPOSITORY；本地联调无该环境变量时记 ""），
    告知用户本 devcontainer 是在哪个仓库构建的。
    """
    full = {}
    for name, env, kind, _default in WORKFLOW_INPUTS:
        raw = (os.environ.get(env) or "").strip()
        full[name] = raw.lower() in BOOL_TRUE if kind == "boolean" else raw

    normal = {}
    minimal = {}
    for name, _env, kind, default in WORKFLOW_INPUTS:
        value = full[name]
        default_value = (default.lower() in BOOL_TRUE if kind == "boolean"
                         else default)
        if kind == "boolean":
            if value != default_value:  # 默认值（未勾选）checkbox 不记录
                normal[name] = value
        elif value != "":  # 留空字符串不记录
            normal[name] = value
        if value != default_value and value != "":
            minimal[name] = value
    meta = {"repo": _github_repo_url()}
    # meta 排在最前：一眼可见构建来源（repo），其后才是三层输入快照
    return {"meta": meta, "minimal": minimal, "normal": normal, "full": full}


def _github_repo_url():
    """本次构建所在仓库的 GitHub URL（无 GITHUB_REPOSITORY 时返回空串）。"""
    repository = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not repository:
        return ""
    server = (os.environ.get("GITHUB_SERVER_URL") or "").strip() \
        or "https://github.com"
    return f"{server}/{repository}"


def workflow_params_json(snapshot):
    """把 workflow_inputs_snapshot() 的结果渲染为纯 JSON 文本（无注释）。"""
    return json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
