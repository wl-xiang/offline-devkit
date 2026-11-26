"""模板渲染：读取 templates/ 下的模板文件，拼装生成最终文件。

使用简单的 {{PLACEHOLDER}} 字符串替换（不依赖 jinja2）。块级内容
（APT 安装、个性化命令、dev 阶段、compose 服务、Makefile dev 目标）
由本模块计算后填入对应占位符。
"""

import json
import shutil
from pathlib import Path

from .params import (
    DB_SERVICE_IMAGES,
    DOCKER_RUN_CONTAINER_PATH,
    FRAMEWORK_NAMES,
    FRAMEWORK_VENDOR_DIR,
    GO_VERSION,
    NGINX_IMAGE,
    NPM_PACKAGES_CONTAINER_DIR,
    Params,
    PYTHON_VERSION,
    SYNC_BASE_IMAGE,
    SYNC_CONTAINER_HOME,
    SYNC_CONTAINER_WORKDIR,
    VUE_GLOBAL_CLI_PACKAGES,
    VUE_PLAYWRIGHT_DIR,
    VUE_STACK_NAME,
    VUE_TEMPLATE_DIR,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
BUILDER_DIR = TEMPLATES_DIR.parent

# 静态部署辅助脚本：随 src 产物分发（包内 scripts/ 目录），由 install.sh 安装
# 到项目根 scripts/ 目录
# （push-bundle.sh 在本地项目根执行，apply-bundle.sh / rollback-bundle.sh 在
#   部署服务器项目根执行，clean-images.sh 清除项目根 compose 声明的镜像，
#   .env.example 是 push-bundle.sh 读取的环境变量模板；
#   manifest.txt 由 render.py 按本次参数渲染后写入包内 scripts/，见 render_all）
SCRIPTS_FILES = (
    ".env.example",
    "push-bundle.sh",
    "apply-bundle.sh",
    "rollback-bundle.sh",
    "clean-images.sh",
)

# compose 中的 dev 服务（profile=dev，仅 make shell/lint/compile/test 时激活）
# build.dockerfile 指向 docker/Dockerfile（联网重建版，由 install.sh 安装到
# 项目根 docker/；离线补装用 Dockerfile.addon 也在 docker/，由 build.sh 引用）。
# context 为项目根（compose 项目目录是 docker/，.. 即项目根）
_DEV_SERVICE_TMPL = """\
  dev:
    image: {{DEV_IMAGE}}
    build:
      context: ..
      dockerfile: docker/Dockerfile
      target: dev
    working_dir: /app
    pull_policy: never
    user: "${HOST_UID:-0}:${HOST_GID:-0}"
    volumes:
      - {{APP_VOLUME}}
{{DEV_EXTRA_VOLUMES}}    networks:
      - app-net
{{DEV_ENV_BLOCK}}    profiles:
      - dev
"""

# compose 中的 v-push 服务（enable_sync_tool）：最小 Python 镜像（内置 paramiko，
# 固定名 devcontainer-vpush:py311，与 image_prefix/image_tag 无关），单独
# profile=v-push，仅 `make v-push` 激活；一次性运行（restart: "no"）。项目根
# （含 scripts/、scripts/manifest.txt、.env、bundles/）读写挂载到 /project，
# v-push 在容器内按清单打包并把 bundle 写回宿主机 ./bundles/。
# 宿主机 ~/.ssh 只读挂载供 sshkey 鉴权（password 方式不使用其内容）。
_PUSH_SERVICE_TMPL = """\
  # 代码同步工具运行环境（enable_sync_tool）：最小 Python 镜像（内置 paramiko），
  # 镜像名固定为 {{SYNC_IMAGE}}（不随 image_prefix / image_tag 变化，多套
  # devcontainer 共用）。
  # 仅 profile=v-push（`make v-push`）激活，一次性运行（restart: "no"）。
  v-push:
    image: {{SYNC_IMAGE}}
    working_dir: {{SYNC_WORKDIR}}
    restart: "no"
    pull_policy: never
    user: "${HOST_UID:-0}:${HOST_GID:-0}"
    environment:
      HOME: {{SYNC_HOME}}
    volumes:
      # 项目根读写挂载：包含 scripts/（push-bundle.sh / manifest.txt / .env）、
      # .gitignore 与 bundles/ 产物目录；v-push 在容器内按清单打包并写回宿主机
      - ../:{{SYNC_WORKDIR}}{{SELINUX_SUFFIX}}
      # sshkey 鉴权时用宿主机 ~/.ssh（password 用户名+密码方式不读取其内容）
      - ${HOME}/.ssh:{{SYNC_HOME}}/.ssh:ro
    networks:
      - app-net
    profiles:
      - v-push
    command: ["python3", "scripts/push-bundle.sh"]
"""

# Makefile 中代码同步工具（v-push）目标（enable_sync_tool 时生成）
_SYNC_MAKE_TARGETS = """\
check-v-push: ## 检查 v-push 运行镜像（{{SYNC_IMAGE}}）是否已导入
\t@docker image inspect {{SYNC_IMAGE}} >/dev/null 2>&1 || { \\
\t\techo "v-push 运行镜像 {{SYNC_IMAGE}} 未导入：请先 docker load 镜像归档"; \\
\t\texit 1; \\
\t}

v-push: check-v-push ## 在容器内执行 push（一次性，--rm；宿主机无需 Python/paramiko）
\t$(COMPOSE) --profile v-push run --rm v-push python3 scripts/push-bundle.sh $(ARGS)

"""

_SYNC_PHONY = "check-v-push v-push "

# Makefile 中依赖 dev 镜像的目标（需要 dev 工具时生成）
_DEV_MAKE_TARGETS = """\
check-dev: ## 检查 dev 镜像是否已导入（离线环境不会自行构建）
\t@docker image inspect {{DEV_IMAGE}} >/dev/null 2>&1 || { \\
\t\techo "dev 镜像 {{DEV_IMAGE}} 未导入：请先 docker load 镜像归档（或本次未构建 dev 镜像）"; \\
\t\texit 1; \\
\t}

# run 模式下 dev 也是参数（compose 服务名），抑制其作为目标执行
ifneq ($(RUN_MODE),1)
dev: check-dev ## 等价于 shell
\t$(DEV_RUN)
endif

run: check-dev ## 一次性运行容器内命令：make run dev <命令>（--rm，不重启）
\t$(COMPOSE) run --rm $(RUN_SERVICE) $(RUN_CMD)

shell: check-dev ## 进入 dev 容器交互式 bash
\t$(DEV_RUN)

"""

# Makefile 中 Python 开发目标（仅 target_codec 预装 Python 时生成）
_PY_MAKE_TARGETS = """\
lint: check-dev ## ruff 静态检查
\t$(DEV_RUN) ruff check .

compile: check-dev ## 字节码编译校验
\t$(DEV_RUN) python -m compileall -q .

test: check-dev ## 运行 pytest
\t$(DEV_RUN) pytest -q

"""

_DEV_PHONY = "check-dev shell dev run "

_PY_PHONY = "lint compile test "

# 前端离线开发（enable_node）：Makefile 变量与 npm 目标
_NPM_MAKE_VARS = """\
# npm 离线包目录 = Npm Download Action 产物 packages/ 的宿主机位置（相对值
# 相对 docker/ 目录解析，默认 deps/packages = docker/deps/packages）；
# compose 以只读方式挂载到容器 {{NPM_CONTAINER_DIR}}，多个容器可共用。
# 可在命令行覆盖：make npm-install NPM_PACKAGES_DIR=/data/packages
NPM_PACKAGES_DIR ?= {{NPM_PACKAGES_DIR}}
export NPM_PACKAGES_DIR
# 宿主机 shell 检查用的目录（相对项目根）：NPM_PACKAGES_DIR 为相对路径时
# 即 docker/<NPM_PACKAGES_DIR>；绝对路径时与 NPM_PACKAGES_DIR 一致
NPM_HOST_DIR ?= {{NPM_HOST_DIR}}

"""

_NPM_MAKE_TARGETS = """\
npm-install: check-dev ## 用离线 npm 包（{{NPM_CONTAINER_DIR}}）把依赖安装到 /app
\t$(DEV_RUN) bash -c 'set -e; \\
\t\tls {{NPM_CONTAINER_DIR}}/*.tgz >/dev/null 2>&1 || { \\
\t\t\techo "错误: {{NPM_CONTAINER_DIR}} 下没有 *.tgz"; \\
\t\t\techo "请把 Npm Download Action 的 npm_packages 产物解压到项目根目录 $(NPM_HOST_DIR)/"; \\
\t\t\techo "或用 make npm-install NPM_PACKAGES_DIR=/path/to/packages 指定目录"; \\
\t\t\texit 1; \\
\t\t}; \\
\t\tnpm install --offline --no-audit --no-fund {{NPM_CONTAINER_DIR}}/*.tgz'

npm-version: check-dev ## 查看容器内 node / npm 版本
\t$(DEV_RUN) bash -c 'node -v && npm -v'

"""

_NPM_PHONY = "npm-install npm-version "

# Vue 前端技术栈（enable_fe_framework=vue）：make 目标。Vue 依赖只安装在 dev
# 镜像，项目模板固定在镜像内 VUE_TEMPLATE_DIR（含 node_modules），
# make vue-init 离线复制到前端源码目录（single_app=app/，febe_separation=web/）。
_VUE_MAKE_TARGETS = """\
VUE_PORT ?= 5173

vue-init: check-dev ## 初始化 Vue 项目模板（从 dev 镜像 {{VUE_TEMPLATE_DIR}}，含 node_modules）
\t$(COMPOSE) run --rm --no-deps -v "$(CURDIR)/{{VUE_DIR}}:/vue-target" dev bash -c 'set -e; \\
\t\tif [ -f /vue-target/package.json ]; then \\
\t\t\techo "    /vue-target/package.json 已存在，跳过 Vue 模板初始化"; exit 0; \\
\t\tfi; \\
\t\tcp -a {{VUE_TEMPLATE_DIR}}/. /vue-target/; \\
\t\techo "    Vue 模板已初始化到 {{VUE_DIR}}/（容器内 /vue-target）"'

vue-dev: check-dev ## 启动 Vue 开发服务器（Vite+ vp dev，宿主机 $(VUE_PORT) → 容器 5173）
\t$(COMPOSE) run --rm -p "$(VUE_PORT):5173" -v "$(CURDIR)/{{VUE_DIR}}:/vue-target" -w /vue-target dev bash -c 'pnpm dev --host 0.0.0.0'

vue-check: check-dev ## Vue 项目检查（vp check：格式化 + lint + 类型检查）
\t$(COMPOSE) run --rm --no-deps -v "$(CURDIR)/{{VUE_DIR}}:/vue-target" -w /vue-target dev bash -c 'pnpm exec vp check'

vue-build: check-dev ## 构建 Vue 生产静态产物到 {{VUE_DIR}}/dist（nginx 托管）
\t$(COMPOSE) run --rm --no-deps -v "$(CURDIR)/{{VUE_DIR}}:/vue-target" -w /vue-target dev bash -c 'pnpm build'

vue-test: check-dev ## 运行 Vue 单元测试（Vite+ vp test / Vitest）
\t$(COMPOSE) run --rm --no-deps -v "$(CURDIR)/{{VUE_DIR}}:/vue-target" -w /vue-target dev bash -c 'pnpm exec vp test --run --passWithNoTests'

vue-e2e: check-dev ## 运行 Playwright E2E（chromium 已内置 /opt/ms-playwright）
\t$(COMPOSE) run --rm --no-deps -v "$(CURDIR)/{{VUE_DIR}}:/vue-target" -w /vue-target dev bash -c 'pnpm test:e2e'

"""

_VUE_PHONY = "vue-init vue-dev vue-check vue-build vue-test vue-e2e "

# Dockerfile.addon 中基于现有镜像的阶段（离线补装 Python 包，配合 build.sh）。
# requirements.txt 只在含有真实包名（非选项/注释/空行）时才按清单安装，
# 否则安装目录下全部 .whl——保证只有离线安装头（--no-index --find-links=./）
# 的空 requirements 不会静默装个寂寞。
_ADDON_STAGE_TMPL = """\
FROM {{BASE_IMAGE}} AS {{STAGE_NAME}}

# wheels 只在本层临时存在：install 与 rm 同层执行，不增大最终镜像
COPY wheels/ /tmp/addon-wheels/
RUN set -eux; \\
    if [ -f /tmp/addon-wheels/requirements.txt ] \\
        && grep -Ev '^[[:space:]]*(#|--|$)' /tmp/addon-wheels/requirements.txt \\
        | grep -q .; \\
    then \\
        pip install --no-compile --no-index --find-links=/tmp/addon-wheels \\
            -r /tmp/addon-wheels/requirements.txt; \\
    else \\
        pip install --no-compile --no-index --find-links=/tmp/addon-wheels \\
            /tmp/addon-wheels/*.whl; \\
    fi; \\
    rm -rf /tmp/addon-wheels
"""


def _addon_targets(p):
    """(Dockerfile target, 基础镜像, stage 名) 列表，与本次构建出的应用镜像一一对应。"""
    targets = []
    if p.dev_image_only:
        targets.append(("addon-dev", p.dev_image, "dev"))
    else:
        targets.append(("addon-prod", p.prod_image, "prod"))
        if p.build_dev_image:
            targets.append(("addon-dev", p.dev_image, "dev"))
    return targets


def render_dockerfile_addon(p):
    stages = "\n\n".join(
        _sub(_ADDON_STAGE_TMPL, {"BASE_IMAGE": image, "STAGE_NAME": target})
        for target, image, _stage in _addon_targets(p)
    )
    return _sub(_load("Dockerfile.addon.tmpl"), {**_base_mapping(p),
                                                 "ADDON_STAGES": stages})


def render_build_sh(p):
    """build.sh：deps/wheels 有 .whl 时 docker build Dockerfile.addon（默认新 TAG
    为 addon-<时间戳>，新镜像沿用镜像仓库名：prod <前缀>:<后缀>、dev <前缀>-dev:<后缀>）；
    enable_node 且 deps/packages 有 *.tgz（Npm Download Action 产物）时一键
    离线加载前端依赖。脚本由 install.sh 安装到 docker/，deps/ 位于 docker/deps
    （init-deps.sh 生成）。"""
    calls = "\n".join(
        f'build {target} "{image}" "$NEW_SUFFIX"'
        for target, image, _stage in _addon_targets(p)
    )
    return _sub(_load("build.sh.tmpl"), {
        **_base_mapping(p),
        "BUILD_CALLS": calls,
        "NPM_DEPS_BLOCK": _npm_deps_block(p),
        "NPM_LOAD_BLOCK": _npm_load_block(p),
    })


def _npm_deps_block(p):
    """build.sh 中 deps/packages/*.tgz（Npm Download Action 产物）检测块。"""
    if not p.enable_node:
        return ""
    return (
        'if ls "$NPM_DIR"/*.tgz >/dev/null 2>&1; then\n'
        "    have_npm=1\n"
        '    echo "== 检测到 $NPM_DIR/*.tgz，将一键加载前端依赖（容器内离线 npm install）=="\n'
        "fi\n"
    )


def _npm_load_block(p):
    """build.sh 中前端依赖一键加载块：通过 dev 服务离线安装到挂载的 /app。"""
    if not p.enable_node:
        return ""
    return (
        'if [ "$have_npm" -eq 1 ]; then\n'
        '    echo "== 一键加载前端依赖: 容器内 npm install --offline /opt/npm-packages/*.tgz -> /app =="\n'
        "    if ! docker compose -f \"$SRC_ROOT/docker/docker-compose.yml\" run --rm dev bash -c '\n"
        "        set -e\n"
        "        ls /opt/npm-packages/*.tgz >/dev/null 2>&1 || {\n"
        '            echo "错误: 容器内 /opt/npm-packages 下没有 *.tgz"\n'
        '            echo "  请确认 docker/deps/packages/ 放入了 Npm Download Action 产物的 packages/ 内容，"\n'
        '            echo "  或用 NPM_PACKAGES_DIR 指定目录（相对 docker/ 目录解析，需与 docker-compose.yml 的挂载一致）"\n'
        "            exit 1\n"
        "        }\n"
        "        npm install --offline --no-audit --no-fund /opt/npm-packages/*.tgz\n"
        "    '; then\n"
        '        echo "错误: 前端依赖离线安装失败。"\n'
        '        echo "  - 先 make check 确认镜像已导入；dev 服务仅在 prod+dev / dev-only 镜像模式下存在"\n'
        '        echo "  - prod-only 模式可改用 app 服务执行同样的 npm install --offline 命令"\n'
        "        exit 1\n"
        "    fi\n"
        '    echo "  前端依赖已安装到源码目录（node_modules），make up / make npm-install 后即可开发"\n'
        "fi\n"
    )


def _load(name):
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _sub(text, mapping):
    for key, value in mapping.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _base_mapping(p):
    return {
        # TS = 构建时间戳（UTC+8，用于产物文件名）；TAG = 镜像 TAG（latest 或时间戳）；
        # ARCH = 产物文件名中的架构标签（amd64 / arm64）
        "TS": p.ts,
        "ARCH": p.arch_label,
        "TAG": p.tag,
        "BUNDLE_NAME": p.bundle_name,
        "PLATFORM": p.platform,
        "DOCKER_PLATFORM": p.docker_platform,
        "PYTHON_VERSION": p.python_version,
        "TARGET_CODEC": p.target_codec,
        "SOURCE_LAYOUT": p.source_layout,
        "ENABLE_DB": p.db_service,
        "NGINX_MODE": p.nginx_mode,
        # 应用源码目录名：single_app=app（唯一），febe_separation=api（后端）；
        # web 为前端目录
        "APP_DIR": p.app_dir,
        "WEB_DIR": p.src_dirs[-1],
        "VENDOR_BASE_DIR": p.src_dirs[-1],
        "GO_VERSION": p.go_version,
        "GLIBC_VERSION": p.glibc_version,
        "MANYLINUX_LABEL": p.manylinux_label,
        "PIP_PLATFORM_TAG": p.pip_platform_tag,
        "PIP_MIRROR": p.pip_mirror,
        "MIRROR_URL": p.mirror_url,
        "IMAGE_PREFIX": p.image_prefix,
        "PROD_IMAGE": p.prod_image,
        "APP_IMAGE": p.app_image,
        "APP_BUILD_TARGET": p.app_build_target,
        "APP_SOURCE_DIR": p.app_source_dir,
        "APP_VOLUME": json.dumps(p.mount_path),
        # 占位入口按 target_codec 生成：python3.13 → main.py，golang1.26 → main.go
        "PYTHON_ENABLED": "1" if p.python_enabled else "0",
        "GO_ENABLED": "1" if p.go_enabled else "0",
        "IMAGE_LIST": " ".join(p.image_refs),
        "DEV_IMAGE": p.dev_image,
        # ---- 代码同步工具（push）运行环境：最小 Python 镜像（enable_sync_tool）----
        "SYNC_ENABLED": "1" if p.enable_sync_tool else "0",
        "SYNC_IMAGE": p.sync_image,
        "SYNC_BASE_IMAGE": SYNC_BASE_IMAGE,
        "SYNC_WORKDIR": SYNC_CONTAINER_WORKDIR,
        "SYNC_HOME": SYNC_CONTAINER_HOME,
        # SELinux Enforcing 主机：项目根读写挂载同样追加 :z（与 app 一致）
        "SELINUX_SUFFIX": ":z" if p.selinux_relabel else "",
        # 用户包 + 预装组件（playwright / oracledb）的合并清单
        "PIP_PACKAGES_LINES": "\n".join(p.preinstall_pip_packages),
        "DEV_PIP_TOOLS": " ".join(p.dev_pip_tools),
        # ---- 基础镜像与 Golang 工具链（target_codec 决定） ----
        "BASE_IMAGE": _base_image(p),
        "GO_STAGE": _go_stage(p),
        "GO_BLOCK": _go_block(p),
        # ---- 容器默认启动脚本（镜像 ENTRYPOINT；项目根同名脚本只读挂载覆盖） ----
        "DOCKER_RUN_CONTAINER_PATH": DOCKER_RUN_CONTAINER_PATH,
        "DOCKER_RUN_MOUNT": p.docker_run_mount,
        # ---- Oracle（runtime_deps_only：客户端由 compose 挂载；full：客户端内置） ----
        "ORACLE_MODE": p.oracle_instantclient_mode,
        "ORACLE_ENABLED": "1" if p.oracle_enabled else "0",
        "ORACLE_CLIENT_DIR": p.oracle_client_dir,
        "ORACLE_COMPOSE_HINT": _oracle_compose_hint(p),
        "EXTRA_VOLUMES": _extra_volumes(p),
        "APP_ENV_BLOCK": _env_block(p),
        "TZ_ENV_BLOCK": _tz_env_block(p),
        # ---- Node.js / npm 前端离线开发 ----
        "NODE_ENABLED": "1" if p.enable_node else "0",
        "NODE_VERSION": p.node_version,
        "NODE_STAGE": _node_stage(p),
        "NPM_CONTAINER_DIR": NPM_PACKAGES_CONTAINER_DIR,
        "NPM_PACKAGES_DIR": p.npm_packages_dir,
        "NPM_REGISTRY_URL": p.npm_registry_url,
        "NPM_GLOBAL_PACKAGES": " ".join(p.npm_package_list),
        "NPM_MAKE_VARS": _npm_make_vars(p),
        "NPM_MAKE_TARGETS": _npm_make_targets(p),
        "NPM_PHONY": _NPM_PHONY if (p.enable_node and p.dev_tools_enabled) else "",
        "NPM_CHECK": _npm_check(p),
        # ---- 前端完整技术栈（enable_fe_framework=vue，仅 dev 镜像） ----
        "FE_FRAMEWORK": p.fe_framework,
        "VUE_ENABLED": "1" if p.vue_enabled else "0",
        "VUE_DIR": p.vue_dir,
        "VUE_STACK_NAME": VUE_STACK_NAME,
        "VUE_TEMPLATE_DIR": VUE_TEMPLATE_DIR,
        # ---- 前端框架（Bootstrap / Tailwind CSS）----
        "FRAMEWORK_ENABLED": "1" if p.framework_enabled else "0",
        "FRAMEWORK_NAME": FRAMEWORK_NAMES[p.framework_name],
        "FRAMEWORK_MODE": p.framework_install,
        "FRAMEWORK_SPEC": p.framework_spec,
        "FRAMEWORK_VENDOR_LABEL": p.framework_vendor_label,
        "FRAMEWORK_VENDOR_DIR": FRAMEWORK_VENDOR_DIR,
        "FRAMEWORK_BLOCK": _framework_npm_block(p),
        # ---- install.sh --load-target：归档内的 sidecar 镜像（任何 target 都导入） ----
        "SIDECAR_IMAGES": _sidecar_images(p),
    }


def _base_image(p):
    """基础镜像：预装 Python 时用 python:slim-bookworm，纯 Golang 时用
    golang:slim-bookworm（python+go 组合在 python 基础上 COPY golang 工具链）。"""
    if p.python_enabled:
        return f"python:{PYTHON_VERSION}-slim-bookworm"
    return f"golang:{GO_VERSION}-slim-bookworm"


def _go_stage(p):
    """Golang 构建阶段：python+go 组合时从官方 golang 镜像复制工具链。"""
    if not (p.python_enabled and p.go_enabled):
        return ""
    return (
        f"# ---- Golang 构建阶段：仅用于复制 go 工具链，不进入最终镜像 ----\n"
        f"FROM --platform={p.docker_platform} golang:{GO_VERSION}-slim-bookworm AS golang\n"
        "\n"
    )


def _go_block(p):
    """Golang 工具链安装块：python+go 组合时 COPY --from=golang 到最终镜像
    （纯 golang 时工具链已在基础镜像内，无需本块）。"""
    if not (p.python_enabled and p.go_enabled):
        return ""
    return (
        f"\n# ---- Golang {GO_VERSION} 工具链（python+go 组合：从官方 golang 镜像复制） ----\n"
        "COPY --from=golang /usr/local/go /usr/local/go\n"
        "ENV PATH=/usr/local/go/bin:/go/bin:${PATH} \\\n"
        "    GOPATH=/go\n"
    )


def _sidecar_images(p):
    """本次构建打包进镜像归档的外挂服务镜像（install.sh --load-target 过滤时
    无论 dev/prod/app 都会一并导入）：独立 nginx 服务 + DB 服务。"""
    images = []
    if p.enable_sync_tool:
        # v-push 运行环境镜像：固定名 devcontainer-vpush:py311，本地构建，
        # 任何 --load-target 都会一并导入；install.sh 在本机已存在该镜像时
        # 只跳过这一个镜像的导入（其余镜像照常导入）
        images.append(p.sync_image)
    if p.nginx_service_mode:
        images.append(NGINX_IMAGE)
    for service in ("postgres16", "mysql8.4", "mongo9"):
        if service in p.db_service:
            images.append(DB_SERVICE_IMAGES[service])
    return " ".join(images)


# scripts/manifest.txt 的条目全集（install.sh 安装清单；目录条目在前，
# 文件条目在后，顺序即 push 打包顺序）。Dockerfile / docker-compose.yml /
# build.sh / docker-run.sh / .env.example / init-deps.sh 都在 docker/ 目录
# （整目录同步），requirements.txt 在应用源码目录内（随源码目录条目同步）
MANIFEST_ROOT_FILES = (
    "Makefile",
    "devcontainer.json",
    ".dockerignore",
    ".gitignore",
    "AGENTS.md",
)


def manifest_entries(p):
    """scripts/manifest.txt 的默认条目（按本次 source_layout / nginx_mode 渲染）。

    同步/备份/回滚只操作清单中的条目；条目内部按项目根 .gitignore 过滤
    （如 scripts/.env、node_modules/ 不会进 bundle）。
    """
    entries = [d + "/" for d in p.src_dirs]
    entries += ["docs/", "scripts/", "docker/"]
    if p.enable_nginx:
        entries.append("nginx/")
    if p.vue_enabled:
        # Vue 模板源码骨架：联网重建 Dockerfile 的 COPY 依赖，随清单同步
        entries.append("vue-template/")
    entries += list(MANIFEST_ROOT_FILES)
    return entries


def _framework_npm_block(p):
    """Dockerfile 中前端框架的 npm 全局安装块（install_via_npm 模式）。

    css_framework_install=*_install_via_npm 时 enable_node 被强制开启
    （params.load），因此本块只会在 NODE_BLOCK 之后渲染，npm 一定可用。
    """
    if not p.framework_npm:
        return ""
    pkgs = " ".join(p.framework_npm_packages)
    names = " ".join(x.rsplit("@", 1)[0] for x in p.framework_npm_packages)
    return (
        "\n# 预装前端框架（构建期联网 npm 全局安装，离线容器内可直接使用）\n"
        f"RUN npm install -g --no-audit --no-fund --registry {p.npm_registry_url} "
        f"{pkgs} \\\n"
        "    && npm cache clean --force \\\n"
        "    && rm -rf /tmp/npm-cache \\\n"
        f"    && npm ls -g --depth=0 {names}\n"
    )


def _apt_block(packages):
    if not packages:
        return ""
    return (
        "RUN apt-get update \\\n"
        "    && apt-get install -y --no-install-recommends \\\n"
        f"        {' '.join(packages)} \\\n"
        "    && apt-get clean \\\n"
        "    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*\n"
    )


def _custom_install_block(cmd):
    if not cmd:
        return ""
    return f"RUN {cmd}\n"


def _oracle_block(p):
    """Oracle Instant Client 块。

    - runtime_deps_only：只装 libaio 等运行库并配置环境变量，客户端由
      compose 从宿主机只读挂载到 ORACLE_CLIENT_DIR（同一服务器多容器共用）；
    - full：运行时依赖 + 构建期从 Oracle 官方直链下载 Basic 包解压进镜像
      （参考 https://github.com/wl-xiang/oracle2excel/blob/main/Dockerfile）。
    """
    if not p.oracle_enabled:
        return ""
    if p.oracle_full:
        return _load("Dockerfile.oracle.full.tmpl")
    return _load("Dockerfile.oracle.tmpl")


def _oracle_compose_hint(p):
    """compose 模板头部的 Oracle 挂载变量说明（仅 runtime_deps_only 模式）。"""
    if not p.oracle_client_mount:
        return ""
    return (
        f"#   ${{ORACLE_CLIENT_DIR:-{p.oracle_client_dir}}}\n"
        "#                         -> Oracle Instant Client 宿主机目录"
        "（runtime_deps_only）\n"
    )


def _extra_volumes(p):
    """app/dev 服务额外的只读挂载（Oracle 客户端、npm 离线包、宿主机时区、
    febe_separation + nginx_binary 模式的 web 静态产物目录）。
    相对路径相对 compose 项目目录 docker/ 解析（../ 即项目根）。"""
    lines = []
    if p.oracle_client_mount:
        lines.append(f"      - {p.oracle_client_mount}\n")
    if p.npm_packages_mount:
        lines.append(f"      - {p.npm_packages_mount}\n")
    if p.nginx_binary_mode and p.is_febe_separation:
        # web 静态产物挂进容器 /usr/share/nginx/html（容器内 nginx 托管）
        lines.append(f"      - ../{p.src_dirs[-1]}/dist:/usr/share/nginx/html:ro\n")
    for volume in p.timezone_volumes:
        lines.append(f"      - {volume}\n")
    return "".join(lines)


def _tz_env_block(p):
    """镜像内时区（内置启用）：写入 ENV TZ=Asia/Shanghai（配合 tzdata 生效）。"""
    if not p.tz_env:
        return ""
    return f"ENV TZ={p.tz_env}\n"


def _escape_dollar(value):
    """compose 会对 $ 做变量插值；预设环境变量的值是字面量，转义为 $$。"""
    return value.replace("$", "$$")


def _env_map(p):
    """app/dev 服务的 environment：Oracle 默认变量 + 预设环境变量（后者覆盖）。"""
    env = {}
    if p.oracle_enabled:
        env["ORACLE_HOME"] = p.oracle_client_dir
        env["LD_LIBRARY_PATH"] = p.oracle_client_dir
        env["TNS_ADMIN"] = "/app"
    for key, value in p.preset_env_pairs:
        env[key] = value
    return env


def _env_block(p):
    env = _env_map(p)
    if not env:
        return ""
    lines = ["    environment:\n"]
    for key, value in env.items():
        lines.append(f"      {key}: {json.dumps(_escape_dollar(value))}\n")
    return "".join(lines)


def _node_stage(p):
    """Node.js 构建阶段：从官方 node 镜像复制 node/npm（仅 enable_node 时）。"""
    if not p.enable_node:
        return ""
    return (
        "# ---- Node.js 构建阶段：仅用于复制 node/npm，不进入最终镜像 ----\n"
        f"FROM --platform={p.docker_platform} node:{p.node_version}-slim-bookworm AS node\n"
        "\n"
    )


def _node_block(p):
    """Node.js 运行时 + 全局工具（enable_node 时）：pnpm 必装（node24 = npm +
    pnpm + npx），可选追加用户 npm 包。"""
    if not p.enable_node:
        return ""
    text = _sub(_load("Dockerfile.node.tmpl"), {"NODE_VERSION": p.node_version})
    pkgs = ["pnpm", *p.npm_package_list]
    text += (
        "\n# 预装前端工具（构建期联网安装到全局，离线容器内可直接使用；"
        "node24 = npm + pnpm + npx）\n"
        f"RUN npm install -g --no-audit --no-fund --registry {p.npm_registry_url} "
        f"{' '.join(pkgs)} \\\n"
        "    && npm cache clean --force \\\n"
        "    && rm -rf /tmp/npm-cache \\\n"
        "    && pnpm --version\n"
    )
    return text


def _playwright_block(p):
    """Playwright 浏览器下载块（enable_playwright 时）。

    pip 包经 preinstall_pip_packages 走 requirements.txt/wheels 统一安装；
    这里只负责构建期下载 chromium（--with-deps 含系统依赖）。必须位于
    INSTALL_BLOCK 之后（依赖已装好的 playwright CLI）。
    """
    if not p.enable_playwright:
        return ""
    return _load("Dockerfile.playwright.tmpl")


def _npm_make_vars(p):
    if not p.enable_node:
        return ""
    if p.npm_packages_dir.startswith("/"):
        host_dir = p.npm_packages_dir
    else:
        host_dir = "docker/" + p.npm_packages_dir
    return _sub(_NPM_MAKE_VARS, {
        "NPM_CONTAINER_DIR": NPM_PACKAGES_CONTAINER_DIR,
        "NPM_PACKAGES_DIR": p.npm_packages_dir,
        "NPM_HOST_DIR": host_dir,
    })


def _npm_make_targets(p):
    if not (p.enable_node and p.dev_tools_enabled):
        return ""
    return _sub(_NPM_MAKE_TARGETS, {
        "NPM_CONTAINER_DIR": NPM_PACKAGES_CONTAINER_DIR,
        "NPM_PACKAGES_DIR": p.npm_packages_dir,
    })


def _npm_check(p):
    if p.vue_enabled:
        return (
            '\t@echo "  OK    Vue 前端模板: /opt/vue-template'
            '（make vue-init 初始化到前端目录，含 node_modules）"\n'
        )
    if not p.enable_node:
        return ""
    return (
        '\t@if ls "$(NPM_HOST_DIR)"/*.tgz >/dev/null 2>&1; then \\\n'
        '\t\techo "  OK    npm 离线包: $(NPM_HOST_DIR)/*.tgz"; \\\n'
        "\telse \\\n"
        '\t\techo "  WARN  未发现 npm 离线包（$(NPM_HOST_DIR)/*.tgz）——'
        '需要前端离线依赖时请解压 Npm Download Action 产物"; \\\n'
        "\tfi\n"
    )


def _vue_block(p):
    """Vue 前端技术栈块（enable_fe_framework=vue）。

    只插入 dev 镜像阶段：全局 CLI + 预构建 /opt/vue-template（含 node_modules）。
    prod 镜像保持只含后端依赖，前端静态产物由 nginx 托管。
    """
    if not p.vue_enabled:
        return ""
    return _sub(_load("Dockerfile.vue.tmpl"), {
        "NPM_REGISTRY_URL": p.npm_registry_url,
        "VUE_GLOBAL_PACKAGES": " ".join(VUE_GLOBAL_CLI_PACKAGES),
        "VUE_TEMPLATE_DIR": VUE_TEMPLATE_DIR,
        "VUE_PLAYWRIGHT_DIR": VUE_PLAYWRIGHT_DIR,
    })


def _install_block(p, local_wheels, req_path="requirements.txt"):
    # 纯 Golang 环境（无 Python/pip）：无 requirements.txt 安装层
    if not p.python_enabled:
        return "# 纯 Golang 环境：无 Python 依赖安装层（go run main.go 直接运行源码）\n"
    if local_wheels:
        return (
            "# wheels 经 BuildKit bind mount 只读挂载参与安装，不复制进任何镜像层：\n"
            "# 运行容器内没有 wheels（离线补装请用 docker/build.sh / docker/Dockerfile.addon）\n"
            "# --no-compile：不生成 .pyc（配合 PYTHONDONTWRITEBYTECODE=1），减小 site-packages\n"
            f"COPY {req_path} /app/requirements.txt\n"
            "\n"
            "RUN --mount=type=bind,source=wheels,target=/mnt/wheels \\\n"
            "    pip install --no-compile --no-index --find-links=/mnt/wheels -r /app/requirements.txt\n"
        )
    return (
        "# 发布包不含 wheels/（wheel 仅在构建时经 bind mount 安装，不落镜像层），\n"
        "# 重建时从 pip 源在线安装\n"
        f"COPY {req_path} /app/requirements.txt\n"
        "\n"
        "RUN pip install --no-compile -r /app/requirements.txt\n"
    )


def _dev_pip_block(p, local_wheels):
    tools = " ".join(p.dev_pip_tools)
    if not tools:
        return ""
    if local_wheels:
        return (
            "# dev 工具的 wheel 已随 wheels/ 一起下载，同样经 bind mount 安装，不落镜像层\n"
            "RUN --mount=type=bind,source=wheels,target=/mnt/wheels \\\n"
            f"    pip install --no-compile --no-index --find-links=/mnt/wheels {tools}\n"
        )
    return (
        "# 发布包不含 wheels/，重建时从 pip 源在线安装\n"
        f"RUN pip install --no-compile {tools}\n"
    )


def render_dockerfile(p, local_wheels=True):
    # 联网重建版 Dockerfile 由 install.sh 安装到 docker/（compose build 的
    # dockerfile 指向 docker/Dockerfile，context = 项目根），requirements.txt
    # 安装到应用源码目录（single_app=app/，febe_separation=api/），COPY 路径
    # 为 context（项目根）相对路径
    req_path = f"{p.app_dir}/requirements.txt"
    base = {
        **_base_mapping(p),
        # tzdata 内置补装（slim 镜像不带，内置 TZ=Asia/Shanghai 环境变量才生效）；
        # nginx_binary 模式经 APT 把 nginx 安装进镜像（docker-run.sh 启动时拉起）
        "APT_BLOCK": _apt_block(
            p.apt_package_list
            + (["nginx"] if p.nginx_binary_mode else [])
            + ["tzdata"]
        ),
        "CUSTOM_INSTALL_BLOCK": _custom_install_block(p.custom_install_cmd),
        "INSTALL_BLOCK": _install_block(p, local_wheels, req_path),
        "DEV_PIP_BLOCK": _dev_pip_block(p, local_wheels),
        "ORACLE_BLOCK": _oracle_block(p),
        # prod 镜像：仅 target_codec 显式含 node24 时安装 Node（保持既有行为）
        "NODE_BLOCK": _node_block(p) if p.node_in_prod else "",
        # dev 镜像：Vue 预设自动带出的 Node 只装在 dev 阶段
        "DEV_NODE_BLOCK": _node_block(p) if p.node_dev_only else "",
        # Vue 技术栈只插入 dev 阶段（prod 只保留后端依赖）
        "VUE_BLOCK": _vue_block(p),
        "PLAYWRIGHT_BLOCK": _playwright_block(p),
    }
    if p.dev_image_only:
        # 仅 dev 镜像模式：单阶段 Dockerfile，产物只有一个 dev 镜像；
        # 本阶段即 dev，Node 块直接安装在此
        devonly = {
            **base,
            "NODE_BLOCK": _node_block(p),
            "DEV_NODE_BLOCK": "",
        }
        return _sub(_load("Dockerfile.devonly.tmpl"), devonly)

    dev_stage = ""
    if p.build_dev_image:
        dev_stage = "\n" + _sub(_load("Dockerfile.dev.tmpl"), base)
    return _sub(
        _load("Dockerfile.prod.tmpl"),
        {**base, "DEV_STAGE": dev_stage},
    )


def render_src_dockerfile(p, staging):
    """把 src 产物内的 Dockerfile 改写为不含 wheels/ 的联网重建版本。"""
    (Path(staging) / "Dockerfile").write_text(
        render_dockerfile(p, local_wheels=False), encoding="utf-8"
    )


def _app_start_block(p):
    """app 服务的启动方式：启动命令由镜像 ENTRYPOINT（docker-run.sh）承载，
    compose 置空 command 并挂载 docker/docker-run.sh 覆盖镜像内副本。

    镜像内已 COPY 一份 docker-run.sh 并设为 ENTRYPOINT；项目根的同名脚本由
    install.sh 安装、compose 只读挂载到同一路径，用户改 docker/docker-run.sh 即可调整
    启动方式（默认 cd /app 后运行预置入口），无需重建镜像。
    """
    return (
        "    # ---- 启动方式：镜像 ENTRYPOINT 为 /usr/local/bin/docker-run.sh ----\n"
        "    # 默认行为：cd /app 后运行预置入口（python main.py / go run main.go）。\n"
        "    # docker/docker-run.sh 已只读挂载到镜像内同一路径，直接改 docker/ 的\n"
        "    # docker-run.sh 即可自定义启动方式（无需重建镜像）；command: [] 表示\n"
        "    # 不向 ENTRYPOINT 追加参数。临时调试可透传命令：\n"
        "    #   docker compose run --rm app <命令>   # docker-run.sh 会 exec 该命令\n"
        "    command: []\n"
    )


def _app_options_block(p):
    """app 服务的端口发布、env_file 与预设环境变量。

    端口发布规则：
    - nginx_service 模式：app 只对容器网络 expose 容器端口（由 nginx 反代，
      宿主机经 nginx 8080 访问）；
    - nginx_binary 模式：对外端口复用 app_port 的宿主机端口，发布到容器 80
      （容器内 nginx 监听 80，反代到应用容器端口）；
    - 未启用 nginx：常规 host:container 端口映射。
    """
    lines = []
    if p.has_app_port:
        host_port, container_port = p.app_port_parts
        if p.nginx_service_mode:
            lines.append("    expose:\n")
            lines.append(f"      - \"{container_port}\"\n")
        elif p.nginx_binary_mode:
            lines.append("    ports:\n")
            lines.append(f"      - \"{host_port}:80\"\n")
        else:
            lines.append("    ports:\n")
            lines.append(f"      - \"{host_port}:{container_port}\"\n")
    if p.env_file_list:
        lines.append("    env_file:\n")
        # env 文件相对应用根目录（docker-compose.yml 由 install.sh 复制到
        # docker/，${SRC_ROOT:-../app/} 默认为项目根下的 app/ 目录）；绝对路径
        # 原样保留
        lines.extend(
            f"      - {json.dumps(_env_file_path(p, x))}\n" for x in p.env_file_list
        )
    lines.append(_env_block(p))
    return "".join(lines)


def _env_file_path(p, path):
    if path.startswith("/"):
        return path
    return "${SRC_ROOT:-../" + p.app_dir + "/}/" + path


def _depends_on_block(p):
    """app 的启动依赖（内置逻辑）：enable_db 含 DB 服务时自动
    depends_on 该 DB 并等待健康检查通过，避免首次连接被拒。
    DB 在 compose 中的服务名固定为 `db`。"""
    if not p.has_db_service:
        return ""
    return "    depends_on:\n      db:\n        condition: service_healthy\n"


def _nginx_service(p):
    """独立 nginx 服务（nginx_service 模式）：挂载 nginx/ 配置模板，
    febe_separation 布局另挂载 web 静态产物目录（web/dist →
    /usr/share/nginx/html）。相对路径相对 compose 项目目录 docker/ 解析。"""
    mounts = [
        "    volumes:\n",
        "      - ../nginx/default.conf:/etc/nginx/conf.d/default.conf:ro\n",
    ]
    if p.is_febe_separation:
        mounts.append(
            f"      - ../{p.src_dirs[-1]}/dist:/usr/share/nginx/html:ro\n")
    return _sub(
        _load("compose.service.nginx.tmpl"),
        {**_base_mapping(p), "NGINX_CONF_MOUNT": "".join(mounts)},
    ).rstrip("\n")


def render_compose(p):
    dev_service = ""
    if p.dev_tools_enabled:
        dev_service = _sub(_DEV_SERVICE_TMPL, {
            **_base_mapping(p),
            "DEV_EXTRA_VOLUMES": _extra_volumes(p),
            "DEV_ENV_BLOCK": _env_block(p),
        })
    push_service = ""
    if p.enable_sync_tool:
        push_service = _sub(_PUSH_SERVICE_TMPL, _base_mapping(p))

    services = []
    if p.nginx_service_mode:
        services.append(_nginx_service(p))
    if p.enable_postgres:
        services.append(_load("compose.service.postgres.tmpl").rstrip("\n"))
    if p.enable_mysql:
        services.append(_load("compose.service.mysql.tmpl").rstrip("\n"))
    if p.enable_mongo:
        services.append(_load("compose.service.mongo.tmpl").rstrip("\n"))
    service_blocks = "\n".join(services)
    if service_blocks:
        service_blocks += "\n"

    # DB 数据落盘改为宿主机 bind mount（项目根 ./volumes/<name> 目录），
    # 不再使用 compose 命名卷，顶层 volumes: 声明随之移除。
    volumes_block = ""

    return _sub(
        _load("compose.base.tmpl"),
        {
            **_base_mapping(p),
            "APP_START_BLOCK": _app_start_block(p),
            "APP_OPTIONS_BLOCK": _app_options_block(p),
            "DEPENDS_ON_BLOCK": _depends_on_block(p),
            "DEV_SERVICE_BLOCK": dev_service,
            "PUSH_SERVICE_BLOCK": push_service,
            "SERVICE_BLOCKS": service_blocks,
            "VOLUMES_BLOCK": volumes_block,
        },
    )


def _nginx_proxy_location(p):
    """nginx 反代 location 块：upstream 按模式选择（binary=容器内
    127.0.0.1:<应用端口>；service=compose 服务名 app:<应用端口>）。"""
    upstream = (f"127.0.0.1:{p.app_container_port}" if p.nginx_binary_mode
                else f"app:{p.app_container_port}")
    return f"""\
    location / {{
        proxy_pass http://{upstream};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }}
"""


def render_nginx_conf(p):
    """项目根 nginx/default.conf 配置模板（nginx_mode 启用时生成）。

    - single 布局（single_app）：全部请求反代到应用；
    - febe_separation 布局：/api/ 反代到后端，/ 由静态产物目录托管
      （service 模式挂载进 nginx 容器 /usr/share/nginx/html，
        binary 模式挂载进 app 容器同名路径）。
    用户可直接修改该文件（manifest 已包含 nginx/，修改会随 bundle 同步）。
    """
    lines = []
    if p.is_febe_separation:
        if p.has_app_port:
            lines.append(_nginx_proxy_location(p).replace("location / {",
                                                          "location /api/ {", 1))
        lines.append("""\
    # 前端静态产物（web/dist 打包结果；目录不存在时先在前端项目内执行构建）
    location / {
        root /usr/share/nginx/html;
        index index.html;
        try_files $uri $uri/ /index.html;
    }
""")
    elif p.has_app_port:
        lines.append(_nginx_proxy_location(p))
    else:
        lines.append("    # 未指定 app_port：请自行补充反代/静态规则\n")
    return _sub(_load("nginx.conf.tmpl"), {
        "NGINX_MODE": p.nginx_mode,
        "SOURCE_LAYOUT": p.source_layout,
        "NGINX_LOCATIONS": "".join(lines),
    })


def render_makefile(p):
    # dev 目标块内部也用占位符（{{DEV_IMAGE}}），需先替换再插入主模板
    dev_targets = ""
    if p.dev_tools_enabled:
        dev_targets = _sub(_DEV_MAKE_TARGETS, _base_mapping(p))
        if p.python_enabled:
            dev_targets += _sub(_PY_MAKE_TARGETS, _base_mapping(p))
    phony_dev = _DEV_PHONY if p.dev_tools_enabled else ""
    if p.dev_tools_enabled and p.python_enabled:
        phony_dev += _PY_PHONY
    npm_phony = _NPM_PHONY if (p.enable_node and p.dev_tools_enabled) else ""
    vue_targets = ""
    vue_phony = ""
    if p.vue_enabled:
        vue_targets = _sub(_VUE_MAKE_TARGETS, _base_mapping(p))
        vue_phony = _VUE_PHONY
    sync_targets = ""
    sync_phony = ""
    if p.enable_sync_tool:
        sync_targets = _sub(_SYNC_MAKE_TARGETS, _base_mapping(p))
        sync_phony = _SYNC_PHONY
    return _sub(
        _load("Makefile.tmpl"),
        {
            **_base_mapping(p),
            "DEV_TARGETS": dev_targets,
            "PHONY_DEV": phony_dev,
            "NPM_TARGETS": _npm_make_targets(p),
            "PHONY_NPM": npm_phony,
            "VUE_TARGETS": vue_targets,
            "PHONY_VUE": vue_phony,
            "SYNC_TARGETS": sync_targets,
            "PHONY_SYNC": sync_phony,
        },
    )


def render_requirements(p):
    text = _sub(_load("requirements.txt.tmpl"), _base_mapping(p))
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def render_dockerignore(p):
    """项目根目录 .dockerignore（install.sh 安装/合并），防止 wheels/dist 进入构建上下文。"""
    return _sub(_load("dockerignore.tmpl"), _base_mapping(p))


def render_gitignore(p):
    """项目根目录 .gitignore（install.sh 安装/合并），忽略离线产物与本地补装缓存。"""
    return _sub(_load("gitignore.tmpl"), _base_mapping(p))


def _env_example_npm_block(p):
    """docker/.env.example 的 npm 变量段（enable_node 时）。"""
    if not p.enable_node:
        return ""
    return (
        "# npm 离线包目录（Npm Download Action 产物 packages/ 内容的宿主机位置；\n"
        "# 相对值相对 docker/ 目录解析，默认 deps/packages = docker/deps/packages；\n"
        "# compose 只读挂载到容器 /opt/npm-packages）\n"
        f"NPM_PACKAGES_DIR={p.npm_packages_dir}\n"
        "\n"
    )


def _env_example_oracle_block(p):
    """docker/.env.example 的 Oracle 变量段（runtime_deps_only 挂载模式时）。"""
    if not p.oracle_client_mount:
        return ""
    return (
        "# Oracle Instant Client 宿主机目录（runtime_deps_only 模式只读挂载进容器）\n"
        f"ORACLE_CLIENT_DIR={p.oracle_client_dir}\n"
        "\n"
    )


def render_env_example(p):
    """项目根目录 .env.example：docker-compose.yml 运行时变量模板，
    key 与 compose 中 ${VAR:-默认值} 一一对应，value 为内置默认值。"""
    return _sub(_load("env.example.tmpl"), {
        **_base_mapping(p),
        "NPM_BLOCK": _env_example_npm_block(p),
        "ORACLE_BLOCK": _env_example_oracle_block(p),
    })


def render_main_py():
    """占位入口（target_codec 预装 Python 时随包分发）：install.sh 在应用源码
    目录 <项目根>/app（febe_separation 布局为 api/）无 main.py 时复制过去。"""
    return _load("main.py.tmpl")


def render_main_go():
    """占位入口（target_codec 预装 Golang 时随包分发）：install.sh 在应用源码
    目录（<项目根>/app 或 febe_separation 布局的 api/）无 main.go 时复制过去。"""
    return _load("main.go.tmpl")


def render_init_deps_sh():
    """docker/init-deps.sh：在 docker/ 下生成 deps/ 离线依赖目录（静态脚本）。"""
    return _load("init-deps.sh.tmpl")


def render_agents_md(p):
    """项目根 AGENTS.md：告知 AI Agent 如何用本 devcontainer 机制离线开发
    测试部署（详细手册见 docs/DEVCONTAINER.md）。"""
    services = []
    if p.enable_nginx:
        services.append("nginx")
    for service in ("postgres16", "mysql8.4", "mongo9"):
        if service in p.db_service:
            services.append(service)
    db_summary = "、".join(services) if services else "无"
    if p.is_febe_separation:
        web_note = "，前端源码在 `web/`（静态产物输出到 `web/dist/`）；"
    else:
        web_note = "；"
    return _sub(_load("AGENTS.md.tmpl"), {
        **_base_mapping(p),
        "SERVICES_SUMMARY": db_summary,
        "WEB_DIR_NOTE": web_note,
        "VUE_NOTE": _vue_agents_note(p),
    })


def _vue_agents_note(p):
    """AGENTS.md 的 Vue 使用约定（enable_fe_framework=vue 时）。"""
    if not p.vue_enabled:
        return ""
    return (
        f"- **Vue 前端技术栈**（`enable_fe_framework=vue`，仅 dev 镜像）：首次执行 "
        f"`make vue-init` 初始化模板到 `{p.vue_dir}/`（含 node_modules）；"
        f"`make vue-dev` 启动开发服务器，`make vue-check` / `make vue-build` 做检查与"
        f"生产构建（产物 `{p.vue_dir}/dist` 由 nginx 托管）。\n"
    )


def render_clean_sh(p, out_file):
    """渲染 bundle 清理脚本 clean.sh 到 dist/（与 install.sh 同级分发）。"""
    content = _sub(_load("clean.sh.tmpl"), _base_mapping(p))
    path = Path(out_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def render_images_txt(p):
    """images.txt：本次 bundle 的镜像清单（clean.sh 按它停容器/删镜像）。"""
    return _sub(_load("images.txt.tmpl"), {
        "TS": p.ts,
        "IMAGE_LIST_LINES": "\n".join(p.image_refs),
    })


def render_manifest(p):
    """scripts/manifest.txt：push/apply/rollback 的目标清单（按本次参数渲染）。"""
    return _sub(_load("manifest.txt.tmpl"), {
        **_base_mapping(p),
        "NGINX_COMMENT": " / nginx 配置" if p.enable_nginx else "",
        "MANIFEST_ENTRIES": "\n".join(manifest_entries(p)),
    })


def render_web_index_html(p):
    """web/index.html 占位静态页（febe_separation 布局；nginx 托管 web/dist 产物）。"""
    return _sub(_load("index.html.tmpl"), {
        "WEB_DIR": p.src_dirs[-1],
        "NGINX_ENABLED": "1" if p.enable_nginx else "0",
    })


def render_web_package_json(p):
    """package.json 前端工程占位：febe_separation 布局放 web/，single_app
    布局放 app/（预装 Node 时）。"""
    return _load("package.json.tmpl")


def render_dockerfile_push(p):
    """代码同步工具（v-push）运行环境镜像：最小 python:3.11-slim + paramiko。

    仅供构建端使用（构建期联网 pip install，运行期离线）：镜像随镜像归档分发，
    名字固定为 devcontainer-vpush:py311（与 image_prefix / image_tag 无关），
    compose 的 v-push 服务（profile=v-push）直接使用，宿主机无需 Python/paramiko。
    运行环境变更（基础镜像 / Python 版本 / paramiko）时更新 SYNC_IMAGE_TAG 后
    重新构建即可。
    """
    if not p.enable_sync_tool:
        return ""
    return (
        "# 由 scripts/devcontainer-builder 生成：代码同步工具（push）运行环境\n"
        "# 最小 Python 镜像 + paramiko；构建期联网安装，运行期完全离线。\n"
        f"FROM {SYNC_BASE_IMAGE}\n"
        "RUN pip install --no-cache-dir --no-compile paramiko \\\n"
        "    && python -c \"import paramiko; print('paramiko', paramiko.__version__)\"\n"
    )


def render_docker_run_sh(p):
    """容器默认启动脚本（镜像 ENTRYPOINT）：cd /app 后运行预置入口。

    - 预装 Python → python main.py（python3.13 + golang1.26 组合同为 python 优先）；
    - 纯 Golang → go run main.go；
    - nginx_binary 模式：默认启动时先拉起容器内 nginx（APT 安装，反代/静态托管）。
    带参数调用时透传执行（`docker compose run --rm app <命令>` 仍可用）。
    SRC Bundle 随包分发，install.sh 安装到项目根，compose 只读挂载覆盖镜像内
    副本，改 docker/docker-run.sh 即可调整启动方式，无需重建镜像。
    """
    default_cmd = "python main.py" if p.python_enabled else "go run main.go"
    return _sub(_load("docker-run.sh.tmpl"), {
        "DOCKER_RUN_DEFAULT_COMMAND": default_cmd,
        "TARGET_CODEC": p.target_codec,
        "NGINX_START_BLOCK": _nginx_start_block(p),
    })


def _nginx_start_block(p):
    """docker-run.sh 中拉起容器内 nginx 的块（仅 nginx_binary 模式）。"""
    if not p.nginx_binary_mode:
        return ""
    return (
        "# nginx（binary 模式，APT 安装进镜像）：默认启动时拉起，反代/静态托管\n"
        "# 容器内监听 80（compose 已把 app_port 的宿主机端口发布到容器 80）。\n"
        "# 守护进程方式运行，应用进程仍是容器主进程；不要 nginx 时删掉本段即可\n"
        "if command -v nginx >/dev/null 2>&1; then\n"
        "    nginx\n"
        "fi\n"
        "\n"
    )


def _oracle_notice_block(p):
    """install.sh 中的 Oracle Instant Client 提示。"""
    if not p.oracle_enabled:
        return ""
    d = p.oracle_client_dir
    if p.oracle_full:
        return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Oracle Instant Client 19（本包已完整安装）${{C_RESET}}"
note "客户端（Basic 包）与运行时依赖已打进镜像：ORACLE_HOME={d}"
note "无需宿主机挂载客户端；验证：docker compose run --rm app python -c \\"import oracledb; oracledb.init_oracle_client(); print('OCI OK')\\""
note "升级客户端需重新构建镜像（full 模式），或改用 runtime_deps_only 模式从宿主机挂载"
'''
    return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Oracle Instant Client（本包已启用；客户端不在镜像内，由 compose 从宿主机挂载）${{C_RESET}}"
note "1. 宿主机解压 Instant Client 19 Basic 包，把其中的 instantclient_19_19 目录内容"
note "   放到 {d}（确保 {d}/libclntsh.so.19.1 直接存在，x64/arm64 与服务器一致）"
note "2. 客户端在其它目录时：export ORACLE_CLIENT_DIR=/your/instantclient19（make up 前设置）"
note "   或修改项目根目录 docker-compose.yml 中 ORACLE_CLIENT_DIR 的默认值"
note "3. 同一台服务器上的多个容器可共用该客户端（只读挂载，不复制进镜像）"
note "4. 验证：docker compose run --rm app python -c \\"import oracledb; oracledb.init_oracle_client(); print('OCI OK')\\""
'''


def _npm_notice_block(p):
    """install.sh 中的 npm 离线开发提示（enable_node 时）。"""
    if not p.enable_node:
        return ""
    d = p.npm_packages_dir
    if d.startswith("/"):
        target = d
    else:
        target = f"docker/ 目录下的 {d}（即 docker/{d}）"
    return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Node.js / npm 前端离线开发（本包已启用）${{C_RESET}}"
note "1. 在内网用 Npm Download Action 下载依赖，得到 npm_packages 产物（顶层 packages/）"
note "2. 解压产物后把 packages/ 内容放到 {target}（也可 export NPM_PACKAGES_DIR=/your/packages）"
note "   目录不存在时先执行: bash docker/init-deps.sh"
note "3. bash docker/build.sh 一键离线加载，或 make npm-install    # 用 {NPM_PACKAGES_CONTAINER_DIR} 里的离线 tarball 把依赖装到 /app"
note "4. make shell 后运行 npm run dev -- --host 0.0.0.0（vite 默认 5173，webpack 默认 8080；按需设置 app_port）"
'''


def _sync_notice_block(p):
    """install.sh 中的代码同步工具（v-push 运行环境）提示。"""
    if not p.enable_sync_tool:
        return ""
    return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}代码同步工具运行环境（v-push profile，本包已启用）${{C_RESET}}"
note "镜像 {p.sync_image}（python:3.11-slim + paramiko）随镜像归档导入，本机无需 Python/paramiko"
note "1. cp scripts/.env.example scripts/.env 填写 SYNC_REMOTE_HOST/USER/PASSWORD(或 KEY)/PATH"
note "2. make v-push ARGS=\\"-y\\"  # 在 v-push 容器内执行 scripts/push-bundle.sh（一次性，--rm）"
note "本地已装 Python 3.8+ 与 paramiko 时仍可用 make push（宿主机直接执行）"
'''


def _framework_notice_block(p):
    """install.sh 中的前端框架提示（css_framework_install 非 none 时）。"""
    if not p.framework_enabled:
        return ""
    vendor = (f"$INSTALL_PATH/{p.src_dirs[-1]}/{FRAMEWORK_VENDOR_DIR}/"
              f"{p.framework_vendor_label}-<version>")
    container = (f"/app/{FRAMEWORK_VENDOR_DIR}/"
                 f"{p.framework_vendor_label}-<version>")
    if p.framework_pack:
        if p.framework_name == "bootstrap":
            usage = (
                f'note "  （容器内路径 {container}/，Django/Jinja 可直接引用 '
                f'dist/ 下的 css/js 静态资源）"'
            )
        else:
            usage = (
                f'note "  （容器内文件 {container}/dist/index.global.js，在模板中以'
                f'静态目录 URL 引用即可在浏览器端编译 Tailwind 类名，无需构建）"'
            )
        return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}{FRAMEWORK_NAMES[p.framework_name]}（pack in artifact，本包自带离线 tgz）${{C_RESET}}"
note "已把 {p.framework_vendor_label}-<version>.tgz 解压到应用源码根目录 {vendor}/"
{usage}
note "后续升级：重新构建（自动拉取最新版），替换 vendor 目录"
'''
    if p.framework_name == "bootstrap":
        return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Bootstrap 前端库（install via npm，已打进镜像）${{C_RESET}}"
note "镜像构建期已用 npm 全局安装 bootstrap@{p.framework_spec}"
note "容器内查看/引用：npm ls -g bootstrap；静态资源在 /usr/local/lib/node_modules/bootstrap/dist/"
note "应用按需从该目录复制到自己的 static 目录（离线 tgz 模式见 css_framework_install=*_pack_in_artifact）"
'''
    return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Tailwind CSS（install via npm，已打进镜像）${{C_RESET}}"
note "镜像构建期已用 npm 全局安装 {' '.join(p.framework_npm_packages)}"
note "容器内查看：tailwindcss --version；离线构建示例：tailwindcss -i ./src/input.css -o ./src/output.css --minify"
note "  （入口 input.css 需含 @import 'tailwindcss';，CLI 扫描模板类名按需生成 CSS）"
note "升级版本：重新构建即可拉取最新版（tailwindcss 与 @tailwindcss/cli 同步发布）"
'''


def _vue_notice_block(p):
    """install.sh 中的 Vue 前端技术栈提示（enable_fe_framework=vue）。"""
    if not p.vue_enabled:
        return ""
    return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}{VUE_STACK_NAME}（本包已启用，仅 dev 镜像）${{C_RESET}}"
note "前端依赖已预装在 dev 镜像的 {VUE_TEMPLATE_DIR}（含 node_modules）；prod 镜像只保留后端依赖"
note "1. 初始化前端工程: make vue-init   # 复制模板到 {p.vue_dir}/（已存在 package.json 时跳过）"
note "2. 开发调试: make vue-dev        # vp dev --host 0.0.0.0，宿主机 5173"
note "3. 检查/构建: make vue-check / make vue-build（产物 {p.vue_dir}/dist，nginx 托管）"
note "4. 测试: make vue-test / make vue-e2e（Vitest / Playwright）"
note "5. Vite+ 统一提供 dev/build/lint/fmt/test/check/run（vp），无需 ESLint/Prettier"
'''


def _vue_readme_section(p):
    """src README 的 Vue 前端技术栈章节。"""
    if not p.vue_enabled:
        return ""
    return f"""
## Vue 前端技术栈（enable_fe_framework=vue）

本次构建只在 **dev 镜像**中预装 {VUE_STACK_NAME}（含 node_modules）；prod 镜像
只保留后端依赖，前端 `dist/` 由 nginx 托管。项目模板固定在镜像内
`{VUE_TEMPLATE_DIR}`，首次使用时初始化到 `{p.vue_dir}/`：

```bash
make vue-init     # 复制模板（含 node_modules）到 {p.vue_dir}/，已存在 package.json 时跳过
make vue-dev      # vp dev --host 0.0.0.0（宿主机 5173）
make vue-check    # vp check（格式化 + lint + 类型检查）
make vue-build    # vp build，静态产物输出到 {p.vue_dir}/dist
make vue-test     # vp test（Vitest）
make vue-e2e      # Playwright E2E（chromium 已内置）
```

技术栈：Vue 3 + TypeScript、Vue Router 4、Pinia + persistedstate、Axios、VueUse、
Element Plus、Lucide、motion-v、Tailwind CSS v4、Reka UI + shadcn-vue、
vue-draggable-plus、TipTap；工具链 Vite Plus（Rolldown/Oxlint/Oxfmt/Vitest/Vite Task）、
vue-tsc、@vue/test-utils、Stylelint、Commitlint、Playwright（chromium 已下载到
`{VUE_PLAYWRIGHT_DIR}`）、rollup-plugin-visualizer。VS Code 默认 formatter 为 Oxc
（`.vscode/settings.json`），推荐安装 `oxc.oxc-vscode`。

静态托管：`febe_separation` 布局产物在 `web/dist`，启用 nginx 时由 nginx 托管；
`single_app` 布局产物在 `app/dist`，请由后端或自定义 nginx 规则托管。
"""


def _vue_html_section(p):
    """readme.html 的 Vue 前端技术栈卡片。"""
    if not p.vue_enabled:
        return ""
    return f"""
    <section class="card">
      <h2><span class="step-dot">Vue</span>Vue 前端技术栈（仅 dev 镜像）</h2>
      <p>本次构建在 <strong>dev 镜像</strong>预装 {VUE_STACK_NAME}（含 node_modules）；
      prod 镜像只保留后端依赖，前端 <code>dist/</code> 由 nginx 托管。项目模板位于镜像内
      <code>{VUE_TEMPLATE_DIR}</code>，首次使用初始化到 <code>{p.vue_dir}/</code>。</p>
      <div class="codeblock">
        <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
        <pre><code>make vue-init    # 初始化 Vue 模板（含 node_modules）
make vue-dev     # vp dev --host 0.0.0.0（宿主机 5173）
make vue-build   # vp build（产物 {p.vue_dir}/dist，nginx 托管）</code></pre>
      </div>
      <div class="tip"><strong>Vite+：</strong>Vue 技术栈内置 Rolldown / Oxlint / Oxfmt / Vitest /
      Vite Task，统一通过 <code>vp dev</code> / <code>vp build</code> / <code>vp check</code> /
      <code>vp test</code> / <code>vp run</code> 使用，无需 ESLint / Prettier。</div>
    </section>
"""


def render_install_sh(p, out_file):
    """渲染安装脚本到 dist/（独立 artifact，不进 src tgz）。"""
    content = _sub(
        _load("install.sh.tmpl"),
        {
            **_base_mapping(p),
            "ORACLE_NOTICE_BLOCK": _oracle_notice_block(p),
            "NPM_NOTICE_BLOCK": _npm_notice_block(p),
            "SYNC_NOTICE_BLOCK": _sync_notice_block(p),
            "FRAMEWORK_NOTICE_BLOCK": _framework_notice_block(p),
            "VUE_NOTICE_BLOCK": _vue_notice_block(p),
        },
    )
    path = Path(out_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _oracle_html_section(p):
    if not p.oracle_enabled:
        return ""
    d = p.oracle_client_dir
    if p.oracle_full:
        return f"""
    <section class="card">
      <h2><span class="step-dot">06</span>Oracle Instant Client（本包已完整安装）</h2>
      <p>镜像已包含 <strong>Instant Client 19 Basic</strong>（构建期从 Oracle 官方直链下载）
      与运行依赖 <code>libaio1</code> / <code>libnsl2</code>，并配置
      <code>ORACLE_HOME={d}</code> / <code>LD_LIBRARY_PATH</code> /
      <code>TNS_ADMIN</code>。客户端随镜像分发，<strong>无需宿主机挂载</strong>。</p>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">1</span>
          <div>
            <h3>验证 OCI 可用</h3>
            <p>输出 <code>OCI OK</code> 即 Thick 模式加载成功。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>docker compose run --rm app python -c "import oracledb; oracledb.init_oracle_client(); print('OCI OK')"</code></pre>
        </div>
      </div>

      <div class="tip"><strong>注意：</strong>客户端已固定在镜像内，升级客户端需要重新构建镜像
      （或改用 <code>runtime_deps_only</code> 模式，由宿主机挂载客户端）。</div>
    </section>
"""
    return f"""
    <section class="card">
      <h2><span class="step-dot">06</span>Oracle Instant Client（本包已启用）</h2>
      <p>镜像内<strong>不包含</strong> Oracle 客户端：只安装了 <code>libaio1</code> /
      <code>libnsl2</code> 等依赖并配置 <code>ORACLE_HOME</code> /
      <code>LD_LIBRARY_PATH</code> / <code>TNS_ADMIN</code>。客户端由 <code>docker-compose.yml</code>
      以只读卷从宿主机挂载，同一台服务器上的多个容器共用同一份客户端。</p>
      <p class="muted">Instant Client 19 Basic 包（官方直链目录
      <code>https://download.oracle.com/otn_software/linux/instantclient/1919000/</code>）：
      x64 用 <code>instantclient-basic-linux.x64-19.19.0.0.0dbru.zip</code>，
      arm64 用 <code>instantclient-basic-linux.arm64-19.19.0.0.0dbru.zip</code>。</p>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">1</span>
          <div>
            <h3>宿主机准备客户端（默认路径）</h3>
            <p>下载 Instant Client 19 Basic 包并解压，把其中的 <code>instantclient_19_19</code>
            目录内容放到 <code>{d}</code>，确保 <code>{d}/libclntsh.so.19.1</code>
            直接存在（x64 / arm64 需与服务器架构一致）。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">宿主机</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>ls -l {d}/libclntsh.so.19.1</code></pre>
        </div>
      </div>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">2</span>
          <div>
            <h3>客户端在其它目录</h3>
            <p>无需重新构建镜像：运行 compose 前设置 <code>ORACLE_CLIENT_DIR</code>，或修改项目根目录
            <code>docker-compose.yml</code> 中挂载的默认值。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>ORACLE_CLIENT_DIR=/data/instantclient19 make up</code></pre>
        </div>
      </div>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">3</span>
          <div>
            <h3>验证 OCI 可用</h3>
            <p>输出 <code>OCI OK</code> 即 Thick 模式加载成功。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>docker compose run --rm app python -c "import oracledb; oracledb.init_oracle_client(); print('OCI OK')"</code></pre>
        </div>
      </div>

      <div class="tip"><strong>注意：</strong>宿主机目录为空或路径不对时会报
      <code>DPI-1047</code> 或找不到 <code>libclntsh.so</code>。挂载是只读的，升级客户端只需替换
      宿主机目录内容后重启容器（<code>make down &amp;&amp; make up</code>）。</div>
    </section>
"""


def _npm_html_section(p):
    if not p.enable_node:
        return ""
    d = p.npm_packages_dir
    host = d if d.startswith("/") else f"docker/{d}"
    step_no = "07" if p.oracle_enabled else "06"
    return f"""
    <section class="card">
      <h2><span class="step-dot">{step_no}</span>前端离线开发（Node.js / npm）</h2>
      <p>镜像内置 Node.js {p.node_version} + npm，可离线开发 vite / webpack / vue / react 项目。
      Npm Download Action 的 <code>npm_packages</code> 产物（顶层 <code>packages/</code>）以只读卷
      挂载到容器 <code>{NPM_PACKAGES_CONTAINER_DIR}</code>，多个容器可共用。</p>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">1</span>
          <div>
            <h3>准备离线 npm 包</h3>
            <p>在内网用 <strong>Npm Download</strong> Action 下载依赖，把产物中的
            <code>packages/</code> 内容放到 <code>{host}</code>（默认 docker/deps/packages，
            目录不存在时先执行 <code>bash docker/init-deps.sh</code>），
            或直接在项目根执行 <code>bash docker/build.sh</code> 一键离线加载。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>ls {host}/*.tgz</code></pre>
        </div>
      </div>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">2</span>
          <div>
            <h3>离线安装依赖到 /app</h3>
            <p><code>make npm-install</code> 等价于容器内
            <code>npm install --offline {NPM_PACKAGES_CONTAINER_DIR}/*.tgz</code>。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>make npm-install</code></pre>
        </div>
      </div>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">3</span>
          <div>
            <h3>启动前端开发服务器</h3>
            <p>推荐把项目根 <code>docker-run.sh</code> 的默认命令改为
            <code>npm run dev -- --host 0.0.0.0</code>、<code>app_port</code> 设为前端端口映射
            （vite 5173 / webpack 8080），<code>make up</code> 即带宿主机端口映射并支持热更新。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>make up</code></pre>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录 · 临时调试</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>docker compose run --rm -p 5173:5173 dev npm run dev -- --host 0.0.0.0</code></pre>
        </div>
      </div>

      <div class="tip"><strong>自定义目录：</strong>离线包不在项目根时，用
      <code>export NPM_PACKAGES_DIR=/data/packages</code> 或
      <code>make npm-install NPM_PACKAGES_DIR=/data/packages</code> 覆盖挂载源，无需改 compose。</div>
    </section>
"""


def _framework_readme_section(p):
    """bundle README 的前端框架段落（仅 pack in artifact 模式；npm 模式提示见
    install.sh 安装完成输出）。"""
    if not p.framework_pack:
        return ""
    label = p.framework_vendor_label
    vendor = f"{p.src_dirs[-1]}/{FRAMEWORK_VENDOR_DIR}/{label}"
    tgz = f"{label}-<version>.tgz"
    if p.framework_name == "bootstrap":
        return f"""
## Bootstrap 前端库（pack in artifact，本包自带离线 tgz）

构建期已从 npm 源下载 `{tgz}` 随本 bundle 分发，install.sh 会自动把它解压到
应用源码根目录 `{vendor}-<version>/`（容器内路径
`/app/{FRAMEWORK_VENDOR_DIR}/{label}-<version>/`）。

1. 确认安装结果：`ls {vendor}-*/dist/css/bootstrap.min.css`
2. 模板中按需引用 `<version>` 对应的 dist 静态资源（css/js）。
3. 升级版本：重新构建（自动拉取最新版），替换 `{vendor}/` 下对应目录。
"""
    return f"""
## Tailwind CSS（pack in artifact，本包自带离线 tgz）

构建期已从 npm 源下载 `{tgz}`（Tailwind 官方浏览器端运行时
`@tailwindcss/browser`）随本 bundle 分发，install.sh 会自动把它解压到应用源码
根目录 `{vendor}-<version>/`，容器内文件
`/app/{FRAMEWORK_VENDOR_DIR}/{label}-<version>/dist/index.global.js`。

1. 确认安装结果：`ls {vendor}-*/dist/index.global.js`
2. 模板中以静态目录 URL 引用该 JS（路径按应用静态目录调整），即可在浏览器端
   编译 Tailwind 类名，无需 Node 构建：

   ```html
   <script src="/static/{FRAMEWORK_VENDOR_DIR}/{label}-<version>/dist/index.global.js"></script>
   ```

3. 升级版本：重新构建（自动拉取最新版），替换 `{vendor}/` 下对应目录。
"""


def _framework_html_section(p):
    """readme.html 中的前端框架卡片（仅 pack in artifact 模式）。"""
    if not p.framework_pack:
        return ""
    label = p.framework_vendor_label
    vendor = f"{p.src_dirs[-1]}/{FRAMEWORK_VENDOR_DIR}/{label}"
    step_no = "08" if p.oracle_enabled else "07"
    if p.framework_name == "bootstrap":
        check_cmd = f"ls {vendor}-*/dist/css/bootstrap.min.css"
        usage_html = (
            "Django / Jinja 模板可直接引用其中的 <code>dist/</code> 静态资源\n"
            "      （css/js）。"
        )
        extra_step = ""
    else:
        check_cmd = f"ls {vendor}-*/dist/index.global.js"
        usage_html = (
            "该包是 Tailwind 官方浏览器端运行时 <code>@tailwindcss/browser</code>：\n"
            "      模板中以静态目录 URL 引用 <code>dist/index.global.js</code>"
            "（路径按应用静态目录调整），即可在浏览器端编译 Tailwind 类名，"
            "无需 Node 构建。"
        )
        extra_step = f"""
      <div class="step">
        <div class="step-head">
          <span class="step-badge">2</span>
          <div>
            <h3>模板引用（URL 按应用静态目录调整）</h3>
            <p>&lt;script src="/static/{FRAMEWORK_VENDOR_DIR}/{label}-&lt;version&gt;/dist/index.global.js"&gt;&lt;/script&gt;</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">模板</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>&lt;script src="/static/{FRAMEWORK_VENDOR_DIR}/{label}-&lt;version&gt;/dist/index.global.js"&gt;&lt;/script&gt;</code></pre>
        </div>
      </div>
"""
    return f"""
    <section class="card">
      <h2><span class="step-dot">{step_no}</span>{FRAMEWORK_NAMES[p.framework_name]}（本包自带离线 tgz）</h2>
      <p>构建期已从 npm 源下载 <code>{label}-*.tgz</code> 随 bundle 分发，install.sh
      会自动解压到应用源码根目录 <code>{vendor}-&lt;version&gt;/</code>
      （容器内 <code>/app/{FRAMEWORK_VENDOR_DIR}/{label}-&lt;version&gt;/</code>）。
      {usage_html}</p>

      <div class="step">
        <div class="step-head">
          <span class="step-badge">1</span>
          <div>
            <h3>确认安装结果</h3>
            <p>检查 vendor 目录与静态资源是否就位。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>{check_cmd}</code></pre>
        </div>
      </div>
{extra_step}
      <div class="tip"><strong>升级版本：</strong>重新构建即可拉取最新版，
      install.sh 会安装到新的 <code>{label}-&lt;version&gt;</code> 目录。</div>
    </section>
"""


def render_readme_html(p, out_file):
    """渲染 bundle 安装说明 readme.html 到 dist/（随 install.sh 等一起进 Artifact zip）。"""
    content = _sub(
        _load("readme.html.tmpl"),
        {
            **_base_mapping(p),
            "ORACLE_SECTION": _oracle_html_section(p),
            "NPM_SECTION": _npm_html_section(p),
            "FRAMEWORK_SECTION": _framework_html_section(p),
            "VUE_SECTION": _vue_html_section(p),
            "AGENT_HTML_SECTION": _agent_html_section(p),
        },
    )
    path = Path(out_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _oracle_readme_section(p):
    if not p.oracle_enabled:
        return ""
    d = p.oracle_client_dir
    if p.oracle_full:
        return f"""
## Oracle Instant Client（已完整安装进镜像）

镜像已包含 Instant Client 19 Basic（构建期从 Oracle 官方直链下载）与运行依赖
`libaio1` / `libnsl2`，并设置 `ORACLE_HOME={d}` / `LD_LIBRARY_PATH` /
`TNS_ADMIN`。客户端随镜像分发，无需宿主机挂载。

验证：
```bash
docker compose run --rm app python -c "import oracledb; oracledb.init_oracle_client(); print('OCI OK')"
```
输出 `OCI OK` 即 Thick 模式加载成功。升级客户端需重新构建镜像；如需在宿主机
替换客户端，请重新构建时选择 `runtime_deps_only` 模式。
"""
    return f"""
## Oracle Instant Client（客户端由宿主机挂载，不在镜像内）

镜像只安装了 `libaio1` / `libnsl2` 运行依赖，并设置 `ORACLE_HOME` /
`LD_LIBRARY_PATH` / `TNS_ADMIN`；客户端本体由 compose 从宿主机只读挂载，
同一台服务器上的多个容器共用同一份客户端。

1. 在宿主机解压 Instant Client 19 Basic 包，把其中的 `instantclient_19_19`
   目录内容放到 `{d}`，确保 `{d}/libclntsh.so.19.1` 直接存在
   （x64 / arm64 与服务器一致）。
   官方直链目录 `https://download.oracle.com/otn_software/linux/instantclient/1919000/`：
   x64 用 `instantclient-basic-linux.x64-19.19.0.0.0dbru.zip`，arm64 用
   `instantclient-basic-linux.arm64-19.19.0.0.0dbru.zip`。
2. 客户端在其它目录时：
   ```bash
   export ORACLE_CLIENT_DIR=/data/instantclient19
   make up
   ```
   对应 compose 挂载：
   `${{ORACLE_CLIENT_DIR:-{d}}}:{d}:ro`；也可直接修改项目根
   `docker-compose.yml` 中的默认值。
3. 验证：
   ```bash
   docker compose run --rm app python -c "import oracledb; oracledb.init_oracle_client(); print('OCI OK')"
   ```
   若报 `DPI-1047` / `libclntsh.so` 找不到，说明宿主机目录为空或路径不对。
"""


def _npm_readme_section(p):
    if not p.enable_node:
        return ""
    d = p.npm_packages_dir
    host = d if d.startswith("/") else f"docker/{d}"
    return f"""
## 前端离线开发（Node.js / npm）

镜像内置 Node.js {p.node_version} + npm；Npm Download Action 的 `npm_packages`
产物（顶层 `packages/`）以只读卷挂载到容器 `{NPM_PACKAGES_CONTAINER_DIR}`。

1. 在内网用 Npm Download Action 下载依赖（见 `docs/npm-offline-package-guide.md`），
   把产物中的 `packages/` 内容放到 `{host}`（默认 docker/deps/packages，
   由 `bash docker/init-deps.sh` 生成目录结构）。
2. 安装依赖到 `/app`（二选一）：
   - 一键加载（推荐）：项目根执行 `bash docker/build.sh`（检测到
     `docker/deps/packages/*.tgz` 即通过容器离线安装）；
   - 或：
   ```bash
   make npm-install
   ```
   等价于容器内 `npm install --offline {NPM_PACKAGES_CONTAINER_DIR}/*.tgz`。
3. 开发调试（二选一）：
   - 推荐：把 `docker/docker-run.sh` 的默认命令改为 `npm run dev -- --host 0.0.0.0`，
     `app_port` 设为前端端口映射（vite `5173:5173` / webpack `8080:8080`），`make up` 即带
     宿主机端口映射；源码经 `/app` 挂载，支持热更新；
   - 临时：`docker compose run --rm -p 5173:5173 dev npm run dev -- --host 0.0.0.0`
     （`make shell` 进入容器手动启动时需要自行发布端口）。
4. 离线包目录不在 docker/ 下时：`export NPM_PACKAGES_DIR=/data/packages`（绝对路径），
   或 `make npm-install NPM_PACKAGES_DIR=/data/packages`；相对路径相对 docker/ 目录解析。
"""


def _agent_readme_section(p):
    """bundle README 的 AI Agent 约定：开发/测试统一在容器内执行，不依赖宿主机环境。"""
    if p.dev_tools_enabled:
        run = "make run dev"
        equiv = "（等价 `docker compose run --rm dev <命令>`）"
        intro = (
            "本项目的运行环境**只在容器内**（dev 镜像按 target_codec 预装语言"
            "环境，Python 环境已内置 ruff / pytest）。宿主机无需安装 Python，"
            "也不要用宿主机的 `python` / `pip` / `pytest` / `npm` 代替容器内"
            "环境——版本与依赖都可能不一致。"
        )
        rows = [
            ("查看 Python 版本", f"`{run} python -V`"),
            ("执行 Python 脚本", f"`{run} python path/to/script.py`"),
            ("交互式 shell", "`make shell`"),
            ("启动应用 / 查看日志", "`make up` / `make logs`"),
        ]
        if p.python_enabled:
            rows[2:2] = [
                ("运行全部测试", "`make test`"),
                ("运行单个测试文件", f"`{run} pytest -q tests/test_x.py`"),
                ("静态检查 / 编译检查", "`make lint` / `make compile`"),
            ]
    else:
        run = "docker compose run --rm app"
        equiv = ""
        intro = (
            "本次**未构建 dev 镜像**（没有 ruff / pytest），但运行环境仍然只在"
            "容器内。宿主机无需安装 Python，也不要用宿主机的 `python` / `pip` "
            "代替容器内环境。"
        )
        rows = [
            ("查看 Python 版本", f"`{run} python -V`"),
            ("执行 Python 脚本", f"`{run} python path/to/script.py`"),
            ("交互式 shell", f"`{run} bash`"),
            ("启动应用 / 查看日志", "`make up` / `make logs`"),
        ]
        if not p.python_enabled:
            rows = [row for row in rows if "python" not in row[1].lower()
                    and "Python" not in row[0]]
    if p.enable_node and p.dev_tools_enabled:
        rows.append(("安装前端离线依赖", "`make npm-install`"))
        rows.append(("运行 npm / pnpm / node", f"`{run} npm ...` / `{run} pnpm ...`"))
    table = "\n".join(f"| {purpose} | {cmd} |" for purpose, cmd in rows)
    return f"""
## 给 AI Agent 的使用约定（重要）

{intro}

所有命令都在**项目根目录**执行，统一通过 docker compose 进入容器：

| 目的 | 推荐命令 |
| --- | --- |
{table}

- 容器把应用源码目录（`app/`，febe_separation 布局为 `api/`；app_source_dir
  指定子目录时以该子目录为准）挂载为 `/app`，工作目录固定 `/app`；容器内修改
  会直接写回宿主机。命令里的脚本路径相对该目录写（`app/test1.py` → `test1.py`）。
- **宿主机即使存在 `python` / `pytest` / `npm`，也不要用来运行项目代码或安装依赖**：
  宿主环境与镜像不一致，所有验证都应以容器内结果为准。
- 非交互场景（Agent / CI）请用 `{run} <命令>`{equiv}；`make shell` / `bash` 交互式
  终端会等待输入，不要用于自动化。
- 首次使用先执行 `make check` 确认镜像已导入；离线机器不要执行 `make build` 或
  `docker compose build`。
- 缺 Python 包时不要在宿主机 `pip install`：把 wheel 放进 `docker/deps/wheels/`
  后执行 `bash docker/build.sh`（见下文「离线补装 Python 包」）。
- 不要用宿主机 `python -m venv` / `conda` / 全局 `pytest` / 全局 `npm install`
  代替容器内环境。
"""


def _agent_html_section(p):
    """readme.html 中的 AI Agent 使用约定卡片。"""
    if p.dev_tools_enabled:
        run = "make run dev"
        intro = (
            "运行环境只在容器内（dev 镜像已内置 ruff / pytest）。宿主机无需安装 "
            "Python，请勿用宿主机的 <code>python</code> / <code>pip</code> / "
            "<code>pytest</code> / <code>npm</code> 代替容器内环境。"
        )
        rows = [
            ("查看 Python 版本", f"{run} python -V"),
            ("执行 Python 脚本", f"{run} python path/to/script.py"),
            ("交互式 shell", "make shell"),
            ("运行全部测试", "make test"),
            ("运行单个测试文件", f"{run} pytest -q tests/test_x.py"),
            ("静态检查 / 编译检查", "make lint / make compile"),
            ("启动应用 / 查看日志", "make up / make logs"),
        ]
    else:
        run = "docker compose run --rm app"
        intro = (
            "本次未构建 dev 镜像（没有 ruff / pytest），但 Python 运行环境仍然只在"
            "容器内。宿主机无需安装 Python，请勿用宿主机的 <code>python</code> / "
            "<code>pip</code> 代替容器内环境。"
        )
        rows = [
            ("查看 Python 版本", f"{run} python -V"),
            ("执行 Python 脚本", f"{run} python path/to/script.py"),
            ("交互式 shell", f"{run} bash"),
            ("启动应用 / 查看日志", "make up / make logs"),
        ]
    if p.enable_node and p.dev_tools_enabled:
        rows.append(("安装前端离线依赖", "make npm-install"))
        rows.append(("运行 npm / node", f"{run} npm ... / {run} node ..."))
    rows_html = "\n".join(
        f"          <tr><td>{purpose}</td><td><code>{cmd}</code></td></tr>"
        for purpose, cmd in rows
    )
    return f"""
    <section class="card">
      <h2><span class="step-dot">AI</span>给 AI Agent 的使用约定</h2>
      <p>{intro}</p>
      <div class="table-wrap">
        <table>
          <tr><th>目的</th><th>命令（项目根目录执行）</th></tr>
{rows_html}
        </table>
      </div>
      <div class="tip"><strong>禁止：</strong>在宿主机执行 <code>python</code> /
      <code>pip</code> / <code>pytest</code> / <code>npm</code>（宿主机即使存在这些命令，
      版本与依赖也与镜像不一致，所有验证以容器内结果为准）；缺 Python 包时不要
      在宿主机安装，请把 wheel 放进 <code>docker/deps/wheels/</code> 后执行
      <code>bash docker/build.sh</code> 离线补装。离线机器也不要执行 <code>make build</code>。</div>
    </section>
"""


def render_src_readme(p):
    services = []
    if p.enable_nginx:
        services.append("nginx:stable (宿主机 8080 → 容器 80)")
    if p.enable_postgres:
        services.append("postgres:16 (compose 服务名 db, 数据目录 ./volumes/pgdata)")
    if p.enable_mysql:
        services.append("mysql:8.4 (compose 服务名 db, 数据目录 ./volumes/mysqldata)")
    if p.enable_mongo:
        services.append("mongo:9 (compose 服务名 db, 数据目录 ./volumes/mongodata)")
    services_summary = "、".join(services) if services else "无"

    if p.dev_image_only:
        image_summary = f"`{p.dev_image}`（仅 dev 镜像，部署与开发共用）"
    elif p.build_dev_image:
        image_summary = f"`{p.prod_image}` / `{p.dev_image}`"
    else:
        image_summary = f"`{p.prod_image}`"

    if p.dev_image_only:
        dev_note = (
            f"- 本次仅构建 dev 镜像 `{p.dev_image}`，部署（`make up`）与开发"
            "（`make shell / lint / compile / test`）共用该镜像。"
        )
    elif p.build_dev_image:
        dev_note = (
            f"- dev 镜像 `{p.dev_image}` 已包含 ruff/pytest 等工具，"
            "`make shell / lint / compile / test` 可直接使用。"
        )
    else:
        dev_note = "- 本次未构建 dev 镜像，`make shell / lint / compile / test` 目标不可用。"

    if p.oracle_full:
        oracle_summary = (
            f"已完整安装（客户端内置在镜像，ORACLE_HOME=`{p.oracle_client_dir}`）"
        )
    elif p.oracle_enabled:
        oracle_summary = (
            f"仅运行时依赖（客户端由 compose 从宿主机只读挂载到 `{p.oracle_client_dir}`）"
        )
    else:
        oracle_summary = "未启用"
    if p.enable_node:
        node_summary = f"Node.js {p.node_version} + npm + pnpm + npx"
        if p.npm_package_list:
            node_summary += f"（全局预装：{', '.join(p.npm_package_list)}）"
    else:
        node_summary = "未启用"
    if p.framework_pack:
        framework_summary = (
            f"{FRAMEWORK_NAMES[p.framework_name]}（pack in artifact，install.sh "
            f"安装到 `{p.src_dirs[-1]}/{FRAMEWORK_VENDOR_DIR}/{p.framework_vendor_label}-<version>`，"
            f"版本 latest）"
        )
    elif p.framework_npm:
        framework_summary = (
            f"{FRAMEWORK_NAMES[p.framework_name]}（install via npm，已打进镜像，"
            f"{' '.join(p.framework_npm_packages)}）"
        )
    else:
        framework_summary = "未启用"
    if p.vue_enabled:
        vue_summary = (
            f"{VUE_STACK_NAME}（仅 dev 镜像，含 node_modules；"
            f"`make vue-init` 初始化到 `{p.vue_dir}/`，`make vue-build` 产物 `{p.vue_dir}/dist` "
            f"由 nginx 托管）"
        )
    else:
        vue_summary = "未启用"

    return _sub(
        _load("README.src.tmpl"),
        {
            **_base_mapping(p),
            "IMAGE_SUMMARY": image_summary,
            "SERVICES_SUMMARY": services_summary,
            "DEV_TOOLS_NOTE": dev_note,
            "ORACLE_SUMMARY": oracle_summary,
            "NODE_SUMMARY": node_summary,
            "FRAMEWORK_SUMMARY": framework_summary,
            "VUE_SUMMARY": vue_summary,
            "ORACLE_SECTION": _oracle_readme_section(p),
            "FRAMEWORK_SECTION": _framework_readme_section(p),
            "NPM_SECTION": _npm_readme_section(p),
            "VUE_SECTION": _vue_readme_section(p),
            "AGENT_SECTION": _agent_readme_section(p),
        },
    )


def render_all(p, staging):
    """渲染全部文件到 staging 目录，返回 {文件名: 内容}。"""
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "wheels").mkdir(exist_ok=True)

    files = {
        "Dockerfile": render_dockerfile(p),
        # .tpl 后缀：不会被 docker compose 自动发现/被 -f 误用，
        # 由 install.sh 原样复制到 docker/ 目录为 docker-compose.yml
        # （模板内 ${SRC_ROOT:-../app/} / ${SRC_ROOT_NAME:-app} 由 compose 运行时解析）
        "docker-compose.yml.tpl": render_compose(p),
        "Makefile": render_makefile(p),
        # 容器默认启动脚本（镜像 ENTRYPOINT；install.sh 安装到 docker/，
        # compose 只读挂载覆盖镜像内副本，用户可随时改写）
        "docker-run.sh": render_docker_run_sh(p),
        # 快速上手说明（install.sh 安装为 docs/DEVCONTAINER.md，根目录不放 README）
        "README.md": render_src_readme(p),
        # 联网重建版 Dockerfile（install.sh 安装到 docker/；compose build 使用）
        # 离线补装：wheel 打进现有镜像生成 addon 镜像（build.sh 引用）
        "Dockerfile.addon": render_dockerfile_addon(p),
        "build.sh": render_build_sh(p),
        # docker/init-deps.sh：需要离线依赖目录时由用户在项目根执行
        # `bash docker/init-deps.sh` 生成 docker/deps/（不再默认安装 deps/）
        "init-deps.sh": render_init_deps_sh(),
        # 项目根 AGENTS.md：AI Agent 使用约定（完全离线开发 / 容器内操作）
        "AGENTS.md": render_agents_md(p),
        # .gitignore 随 install.sh 安装/合并到项目根目录（src root）。
        # 注意：.dockerignore 不能在这里写入——staging 即 CI 构建上下文，
        # 其 wheels/ 排除规则会让构建期的 wheels 引用失败；
        # 它在 docker 构建完成后由 main.py 写入，仅供 src 产物分发
        # （.dockerignore 保留在项目根：compose build 的 context 是项目根，
        #  docker 只按构建上下文根目录查找 .dockerignore）
        ".gitignore": render_gitignore(p),
        # docker/.env.example：docker-compose.yml 运行时变量模板（默认值一致，
        # 仅缺失时安装）。部署脚本的 SYNC_REMOTE_* 变量模板是 scripts/.env.example
        ".env.example": render_env_example(p),
    }
    # 占位入口按 target_codec 分发（install.sh 在应用源码目录缺失时复制过去）
    if p.python_enabled:
        files["main.py"] = render_main_py()
    if p.go_enabled:
        files["main.go"] = render_main_go()
    # 前端占位：febe_separation 布局 web/ 放 index.html（静态页样例），
    # package.json（预装 Node 时）为前端工程样例；single_app 布局的
    # package.json 由 install.sh 放入 app/
    if p.is_febe_separation:
        files["index.html"] = render_web_index_html(p)
    if p.enable_node and not p.vue_enabled:
        files["package.json"] = render_web_package_json(p)
    for name, content in files.items():
        (staging / name).write_text(content, encoding="utf-8")
    # requirements.txt 写入应用源码目录子目录（single_app=app/，
    # febe_separation=api/；CI 构建上下文与安装布局一致，Dockerfile 的
    # COPY 路径与 pip download -r 均按此路径）
    req_content = render_requirements(p)
    if req_content and not req_content.endswith("\n"):
        req_content += "\n"
    app_dir = staging / p.app_dir
    app_dir.mkdir(exist_ok=True)
    (app_dir / "requirements.txt").write_text(req_content, encoding="utf-8")
    # 保留可执行位（tar 会随包保留；artifact zip 解压可能丢失，文档引导 bash 调用）
    (staging / "build.sh").chmod(0o755)
    (staging / "docker-run.sh").chmod(0o755)
    # 部署辅助脚本（静态复制，不渲染）：随 src 产物分发到包内 scripts/ 目录，
    # install.sh 安装到项目根 scripts/（clean-images.sh 由 make clean 调用）
    scripts_dir = staging / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for name in SCRIPTS_FILES:
        shutil.copy2(BUILDER_DIR / "scripts" / name, scripts_dir / name)
    # manifest.txt：同步/备份/回滚目标清单（按 source_layout / nginx_mode 渲染），
    # install.sh 安装到项目根 scripts/manifest.txt
    (scripts_dir / "manifest.txt").write_text(render_manifest(p), encoding="utf-8")
    # nginx 配置模板（nginx_mode 启用时）：install.sh 安装到项目根 nginx/default.conf
    if p.enable_nginx:
        nginx_dir = staging / "nginx"
        nginx_dir.mkdir(exist_ok=True)
        (nginx_dir / "default.conf").write_text(
            render_nginx_conf(p), encoding="utf-8"
        )
    # Vue 项目模板（enable_fe_framework=vue）：作为 dev 镜像构建上下文，
    # Dockerfile COPY 到 /opt/vue-template 并在构建期 pnpm install（含 node_modules）；
    # make vue-init 在运行期离线复制到前端源码目录。
    if p.vue_enabled:
        shutil.copytree(
            TEMPLATES_DIR / "vue", staging / "vue-template", dirs_exist_ok=True
        )
    return files
