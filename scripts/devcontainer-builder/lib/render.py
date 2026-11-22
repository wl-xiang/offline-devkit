"""模板渲染：读取 templates/ 下的模板文件，拼装生成最终文件。

使用简单的 {{PLACEHOLDER}} 字符串替换（不依赖 jinja2）。块级内容
（APT 安装、个性化命令、dev 阶段、compose 服务、Makefile dev 目标）
由本模块计算后填入对应占位符。
"""

import json
import shutil
from pathlib import Path

from .params import (
    FRAMEWORK_NAMES,
    FRAMEWORK_VENDOR_DIR,
    NPM_PACKAGES_CONTAINER_DIR,
    Params,
)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
BUILDER_DIR = TEMPLATES_DIR.parent

# 静态部署辅助脚本：随 src 产物分发（包内 scripts/ 目录），由 install.sh 安装
# 到项目根 scripts/ 目录
# （push-bundle.sh 在本地项目根执行，apply-bundle.sh / rollback-bundle.sh 在
#   部署服务器项目根执行，clean-images.sh 清除项目根 compose 声明的镜像，
#   .env.example 是 push-bundle.sh 读取的环境变量模板）
SCRIPTS_FILES = (
    ".env.example",
    "push-bundle.sh",
    "apply-bundle.sh",
    "rollback-bundle.sh",
    "clean-images.sh",
)

# compose 中的 dev 服务（profile=dev，仅 make shell/lint/compile/test 时激活）
# build.dockerfile 指向 docs/devcontainer/Dockerfile（联网重建版 Dockerfile 位于
# 该目录，context 为项目根目录）
_DEV_SERVICE_TMPL = """\
  dev:
    image: {{DEV_IMAGE}}
    build:
      context: .
      dockerfile: docs/devcontainer/Dockerfile
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

# Makefile 中依赖 dev 镜像的目标（需要 dev 工具时生成）
_DEV_MAKE_TARGETS = """\
check-dev: ## 检查 dev 镜像是否已导入（离线环境不会自行构建）
\t@docker image inspect {{DEV_IMAGE}} >/dev/null 2>&1 || { \\
\t\techo "dev 镜像 {{DEV_IMAGE}} 未导入：请先 docker load 镜像归档（或本次未构建 dev 镜像）"; \\
\t\texit 1; \\
\t}

shell: check-dev ## 进入 dev 容器交互式 bash
\t$(DEV_RUN)

dev: check-dev ## 等价于 shell
\t$(DEV_RUN)

lint: check-dev ## ruff 静态检查
\t$(DEV_RUN) ruff check .

compile: check-dev ## 字节码编译校验
\t$(DEV_RUN) python -m compileall -q .

test: check-dev ## 运行 pytest
\t$(DEV_RUN) pytest -q

"""

_DEV_PHONY = "check-dev shell dev lint compile test "

# 前端离线开发（enable_node）：Makefile 变量与 npm 目标
_NPM_MAKE_VARS = """\
# npm 离线包目录 = Npm Download Action 产物 packages/ 的宿主机位置；
# compose 以只读方式挂载到容器 {{NPM_CONTAINER_DIR}}，多个容器可共用。
# 可在命令行覆盖：make npm-install NPM_PACKAGES_DIR=/data/packages
NPM_PACKAGES_DIR ?= {{NPM_PACKAGES_DIR}}
export NPM_PACKAGES_DIR

"""

_NPM_MAKE_TARGETS = """\
npm-install: check-dev ## 用离线 npm 包（{{NPM_CONTAINER_DIR}}）把依赖安装到 /app
\t$(DEV_RUN) bash -c 'set -e; \\
\t\tls {{NPM_CONTAINER_DIR}}/*.tgz >/dev/null 2>&1 || { \\
\t\t\techo "错误: {{NPM_CONTAINER_DIR}} 下没有 *.tgz"; \\
\t\t\techo "请把 Npm Download Action 的 npm_packages 产物解压到项目根目录 $(NPM_PACKAGES_DIR)/"; \\
\t\t\techo "或用 make npm-install NPM_PACKAGES_DIR=/path/to/packages 指定目录"; \\
\t\t\texit 1; \\
\t\t}; \\
\t\tnpm install --offline --no-audit --no-fund {{NPM_CONTAINER_DIR}}/*.tgz'

npm-version: check-dev ## 查看容器内 node / npm 版本
\t$(DEV_RUN) bash -c 'node -v && npm -v'

"""

_NPM_PHONY = "npm-install npm-version "

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
    离线加载前端依赖。"""
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
        "    if ! docker compose -f \"$SRC_ROOT/docker-compose.yml\" run --rm dev bash -c '\n"
        "        set -e\n"
        "        ls /opt/npm-packages/*.tgz >/dev/null 2>&1 || {\n"
        '            echo "错误: 容器内 /opt/npm-packages 下没有 *.tgz"\n'
        '            echo "  请确认 deps/packages/ 放入了 Npm Download Action 产物的 packages/ 内容，"\n'
        '            echo "  或用 NPM_PACKAGES_DIR 指定目录（需与 docker-compose.yml 的挂载一致）"\n'
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
        "NEEDS_MAIN_PY": "1" if p.prod_command == "python main.py" else "0",
        "IMAGE_LIST": " ".join(p.image_refs),
        "DEV_IMAGE": p.dev_image,
        "PROD_COMMAND": p.prod_command,
        # YAML 双引号标量（json.dumps 的转义规则与 YAML 兼容）
        "PROD_COMMAND_YAML": json.dumps(p.prod_command),
        "PROD_COMMAND_JSON": p.prod_command_json,
        # 用户包 + 预装组件（lucide / oracledb）的合并清单
        "PIP_PACKAGES_LINES": "\n".join(p.preinstall_pip_packages),
        "DEV_PIP_TOOLS": " ".join(p.dev_pip_tools),
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
        # ---- 前端框架（Bootstrap / Tailwind CSS）----
        "FRAMEWORK_ENABLED": "1" if p.framework_enabled else "0",
        "FRAMEWORK_NAME": FRAMEWORK_NAMES[p.frontend_framework],
        "FRAMEWORK_MODE": p.frontend_install,
        "FRAMEWORK_SPEC": p.framework_spec,
        "FRAMEWORK_VENDOR_LABEL": p.framework_vendor_label,
        "FRAMEWORK_VENDOR_DIR": FRAMEWORK_VENDOR_DIR,
        "FRAMEWORK_BLOCK": _framework_npm_block(p),
        # ---- install.sh --load-target：归档内的 sidecar 镜像（任何 target 都导入） ----
        "SIDECAR_IMAGES": _sidecar_images(p),
    }


def _sidecar_images(p):
    """本次构建打包进镜像归档的外挂服务镜像（install.sh --load-target 过滤时
    无论 dev/prod/app 都会一并导入）。"""
    images = []
    if p.enable_nginx:
        images.append("nginx:stable")
    if p.enable_postgres:
        images.append("postgres:16")
    return " ".join(images)


def _framework_npm_block(p):
    """Dockerfile 中前端框架的 npm 全局安装块（install_via_npm 模式）。

    frontend_install=install_via_npm 时 enable_node 被强制开启（params.load），
    因此本块只会在 NODE_BLOCK 之后渲染，npm 一定可用。
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
    """app/dev 服务额外的只读挂载（Oracle 客户端、npm 离线包、宿主机时区）。"""
    lines = []
    if p.oracle_client_mount:
        lines.append(f"      - {p.oracle_client_mount}\n")
    if p.npm_packages_mount:
        lines.append(f"      - {p.npm_packages_mount}\n")
    for volume in p.timezone_volumes:
        lines.append(f"      - {volume}\n")
    return "".join(lines)


def _tz_env_block(p):
    """镜像内时区：勾选 enable_tz_shanghai 时写入 ENV TZ（配合 tzdata 生效）。"""
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
    """Node.js 运行时 + 可选的全局 npm 包（enable_node 时）。"""
    if not p.enable_node:
        return ""
    text = _sub(_load("Dockerfile.node.tmpl"), {"NODE_VERSION": p.node_version})
    if p.npm_package_list:
        text += (
            "\n# 预装前端工具（构建期联网安装到全局，离线容器内可直接使用）\n"
            f"RUN npm install -g --no-audit --no-fund --registry {p.npm_registry_url} "
            f"{' '.join(p.npm_package_list)} \\\n"
            "    && npm cache clean --force \\\n"
            "    && rm -rf /tmp/npm-cache\n"
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
    return _sub(_NPM_MAKE_VARS, {
        "NPM_CONTAINER_DIR": NPM_PACKAGES_CONTAINER_DIR,
        "NPM_PACKAGES_DIR": p.npm_packages_dir,
    })


def _npm_make_targets(p):
    if not (p.enable_node and p.dev_tools_enabled):
        return ""
    return _sub(_NPM_MAKE_TARGETS, {
        "NPM_CONTAINER_DIR": NPM_PACKAGES_CONTAINER_DIR,
        "NPM_PACKAGES_DIR": p.npm_packages_dir,
    })


def _npm_check(p):
    if not p.enable_node:
        return ""
    return (
        '\t@if ls "$(NPM_PACKAGES_DIR)"/*.tgz >/dev/null 2>&1; then \\\n'
        '\t\techo "  OK    npm 离线包: $(NPM_PACKAGES_DIR)/*.tgz"; \\\n'
        "\telse \\\n"
        '\t\techo "  WARN  未发现 npm 离线包（$(NPM_PACKAGES_DIR)/*.tgz）——'
        '需要前端离线依赖时请解压 Npm Download Action 产物"; \\\n'
        "\tfi\n"
    )


def _install_block(local_wheels, req_path="requirements.txt"):
    if local_wheels:
        return (
            "# wheels 经 BuildKit bind mount 只读挂载参与安装，不复制进任何镜像层：\n"
            "# 运行容器内没有 wheels（离线补装请用项目根目录 build.sh / Dockerfile.addon）\n"
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
    # 发布包内的重建版 Dockerfile 由 compose 在项目根目录构建（context=项目根），
    # requirements.txt 位于 docs/devcontainer/，COPY 路径相应调整
    req_path = ("requirements.txt" if local_wheels
                else "docs/devcontainer/requirements.txt")
    base = {
        **_base_mapping(p),
        # 勾选 enable_tz_shanghai 时补装 tzdata（slim 镜像不带，TZ 环境变量才生效）
        "APT_BLOCK": _apt_block(
            p.apt_package_list + (["tzdata"] if p.enable_tz_shanghai else [])
        ),
        "CUSTOM_INSTALL_BLOCK": _custom_install_block(p.custom_install_cmd),
        "INSTALL_BLOCK": _install_block(local_wheels, req_path),
        "DEV_PIP_BLOCK": _dev_pip_block(p, local_wheels),
        "ORACLE_BLOCK": _oracle_block(p),
        "NODE_BLOCK": _node_block(p),
        "PLAYWRIGHT_BLOCK": _playwright_block(p),
    }
    if p.dev_image_only:
        # 仅 dev 镜像模式：单阶段 Dockerfile，产物只有一个 dev 镜像
        return _sub(_load("Dockerfile.devonly.tmpl"), base)

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
    """app 服务的启动方式：显式覆盖镜像 ENTRYPOINT/CMD，便于直接改 compose。

    镜像默认命令由 Dockerfile 的 CMD 承载；这里固定写入 entrypoint: []
    （清空镜像 ENTRYPOINT）与 command（默认 prod_command），用户无需重建镜像
    即可调整启动命令，或按注释换成自定义启动脚本。
    """
    return (
        "    # ---- 启动方式：直接改下面两项即可，无需重建镜像 ----\n"
        "    # 镜像自带的 ENTRYPOINT / CMD 已被显式覆盖：entrypoint 为空 = 清空镜像\n"
        "    # ENTRYPOINT；command 承载默认启动命令（构建参数 prod_command）。\n"
        "    # 需要自定义启动脚本时，在 app 已有 volumes 下追加脚本挂载并修改：\n"
        "    #     - ./my-start.sh:/opt/my-start.sh:ro\n"
        "    #   entrypoint: [\"/bin/bash\", \"/opt/my-start.sh\"]\n"
        "    #   command: []   # 按需清空，避免镜像 CMD 被追加为脚本参数\n"
        "    entrypoint: []\n"
        f"    command: {p.prod_command_json}  # 使用自定义 entrypoint 时改为 []\n"
    )


def _app_options_block(p):
    """app 服务的端口发布、env_file 与预设环境变量。"""
    lines = []
    if p.has_app_port:
        host_port, container_port = p.app_port_parts
        if p.enable_nginx:
            # 已由 nginx 反代，app 只对容器网络暴露容器端口，避免与宿主机端口冲突
            lines.append("    expose:\n")
            lines.append(f"      - \"{container_port}\"\n")
        else:
            lines.append("    ports:\n")
            lines.append(f"      - \"{host_port}:{container_port}\"\n")
    if p.env_file_list:
        lines.append("    env_file:\n")
        # env 文件相对应用根目录（docker-compose.yml 由 install.sh 复制到项目根，
        # ${SRC_ROOT:-./src/} 默认为项目根下的 src/ 目录）；绝对路径原样保留
        lines.extend(
            f"      - {json.dumps(_env_file_path(x))}\n" for x in p.env_file_list
        )
    lines.append(_env_block(p))
    return "".join(lines)


def _env_file_path(path):
    if path.startswith("/"):
        return path
    return "${SRC_ROOT:-./src/}/" + path


def _depends_on_block(p):
    """app 的启动依赖；postgres 等待健康检查通过，避免首次连接被拒。"""
    if not p.app_depends_on_nginx and not p.app_depends_on_postgres:
        return ""
    lines = ["    depends_on:\n"]
    if p.app_depends_on_nginx:
        lines.append("      nginx:\n        condition: service_started\n")
    if p.app_depends_on_postgres:
        lines.append("      postgres:\n        condition: service_healthy\n")
    return "".join(lines)


def _nginx_service(p):
    mount = ""
    if p.has_app_port:
        # docker-compose.yml 位于项目根目录（install.sh 复制），反代配置相对其
        # 所在的 docs/devcontainer 目录
        mount = ("    volumes:\n"
                 "      - ./docs/devcontainer/nginx/conf.d:/etc/nginx/conf.d:ro\n")
    return _sub(
        _load("compose.service.nginx.tmpl"),
        {**_base_mapping(p), "NGINX_CONF_MOUNT": mount},
    ).rstrip("\n")


def render_compose(p):
    dev_service = ""
    if p.dev_tools_enabled:
        dev_service = _sub(_DEV_SERVICE_TMPL, {
            **_base_mapping(p),
            "DEV_EXTRA_VOLUMES": _extra_volumes(p),
            "DEV_ENV_BLOCK": _env_block(p),
        })

    services = []
    if p.enable_nginx:
        services.append(_nginx_service(p))
    if p.enable_postgres:
        services.append(_load("compose.service.postgres.tmpl").rstrip("\n"))
    service_blocks = "\n".join(services)
    if service_blocks:
        service_blocks += "\n"

    volumes_block = "volumes:\n  pgdata: {}\n" if p.enable_postgres else ""

    return _sub(
        _load("compose.base.tmpl"),
        {
            **_base_mapping(p),
            "APP_START_BLOCK": _app_start_block(p),
            "APP_OPTIONS_BLOCK": _app_options_block(p),
            "DEPENDS_ON_BLOCK": _depends_on_block(p),
            "DEV_SERVICE_BLOCK": dev_service,
            "SERVICE_BLOCKS": service_blocks,
            "VOLUMES_BLOCK": volumes_block,
        },
    )


def render_nginx_conf(p):
    """nginx 反代到 app:<容器端口>；仅在启用 nginx 且指定 app_port 时生成。"""
    return _sub(_load("nginx.conf.tmpl"), {"APP_PORT": p.app_container_port})


def render_makefile(p):
    # dev 目标块内部也用占位符（{{DEV_IMAGE}}），需先替换再插入主模板
    dev_targets = ""
    if p.dev_tools_enabled:
        dev_targets = _sub(_DEV_MAKE_TARGETS, _base_mapping(p))
    npm_phony = _NPM_PHONY if (p.enable_node and p.dev_tools_enabled) else ""
    return _sub(
        _load("Makefile.tmpl"),
        {
            **_base_mapping(p),
            "DEV_TARGETS": dev_targets,
            "PHONY_DEV": _DEV_PHONY if p.dev_tools_enabled else "",
            "NPM_TARGETS": _npm_make_targets(p),
            "PHONY_NPM": npm_phony,
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
    """根目录 .env.example 的 npm 变量段（enable_node 时）。"""
    if not p.enable_node:
        return ""
    return (
        "# npm 离线包目录（Npm Download Action 产物 packages/ 内容的宿主机位置，\n"
        "# compose 只读挂载到容器 /opt/npm-packages）\n"
        f"NPM_PACKAGES_DIR={p.npm_packages_dir}\n"
        "\n"
    )


def _env_example_oracle_block(p):
    """根目录 .env.example 的 Oracle 变量段（runtime_deps_only 挂载模式时）。"""
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
    """占位入口：install.sh 在应用源码目录 <项目根>/src 无 main.py 且启动
    命令为默认值时复制过去。"""
    return _load("main.py.tmpl")


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
        target = f"项目根目录下的 {d}"
    return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Node.js / npm 前端离线开发（本包已启用）${{C_RESET}}"
note "1. 在内网用 Npm Download Action 下载依赖，得到 npm_packages 产物（顶层 packages/）"
note "2. 解压产物后把 packages/ 内容放到 {target}（也可 export NPM_PACKAGES_DIR=/your/packages）"
note "3. bash build.sh 一键离线加载，或 make npm-install    # 用 {NPM_PACKAGES_CONTAINER_DIR} 里的离线 tarball 把依赖装到 /app"
note "4. make shell 后运行 npm run dev -- --host 0.0.0.0（vite 默认 5173，webpack 默认 8080；按需设置 app_port）"
'''


def _framework_notice_block(p):
    """install.sh 中的前端框架提示（frontend_framework 非 none 时）。"""
    if not p.framework_enabled:
        return ""
    vendor = (f"$SRC_DIR/{FRAMEWORK_VENDOR_DIR}/"
              f"{p.framework_vendor_label}-<version>")
    container = (f"/app/{FRAMEWORK_VENDOR_DIR}/"
                 f"{p.framework_vendor_label}-<version>")
    if p.framework_pack:
        if p.frontend_framework == "bootstrap":
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
printf '%s\\n' "${{C_BOLD}}{FRAMEWORK_NAMES[p.frontend_framework]}（pack in artifact，本包自带离线 tgz）${{C_RESET}}"
note "已把 {p.framework_vendor_label}-<version>.tgz 解压到应用源码根目录 {vendor}/"
{usage}
note "后续升级：重新构建时选择 pack_in_artifact 并指定 frontend_version，替换 vendor 目录"
'''
    if p.frontend_framework == "bootstrap":
        return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Bootstrap 前端库（install via npm，已打进镜像）${{C_RESET}}"
note "镜像构建期已用 npm 全局安装 bootstrap@{p.framework_spec}"
note "容器内查看/引用：npm ls -g bootstrap；静态资源在 /usr/local/lib/node_modules/bootstrap/dist/"
note "应用按需从该目录复制到自己的 static 目录（离线 tgz 模式见 frontend_install=pack_in_artifact）"
'''
    return f'''
echo ""
printf '%s\\n' "${{C_BOLD}}Tailwind CSS（install via npm，已打进镜像）${{C_RESET}}"
note "镜像构建期已用 npm 全局安装 {' '.join(p.framework_npm_packages)}"
note "容器内查看：tailwindcss --version；离线构建示例：tailwindcss -i ./src/input.css -o ./src/output.css --minify"
note "  （入口 input.css 需含 @import 'tailwindcss';，CLI 扫描模板类名按需生成 CSS）"
note "升级版本：重新构建时指定 frontend_version（tailwindcss 与 @tailwindcss/cli 同步发布）"
'''


def render_install_sh(p, out_file):
    """渲染安装脚本到 dist/（独立 artifact，不进 src tgz）。"""
    content = _sub(
        _load("install.sh.tmpl"),
        {
            **_base_mapping(p),
            "ORACLE_NOTICE_BLOCK": _oracle_notice_block(p),
            "NPM_NOTICE_BLOCK": _npm_notice_block(p),
            "FRAMEWORK_NOTICE_BLOCK": _framework_notice_block(p),
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
            <code>packages/</code> 内容放到 <code>{d}</code>（默认项目根目录下），
            或直接在项目根执行 <code>bash build.sh</code> 一键离线加载。</p>
          </div>
        </div>
        <div class="codeblock">
          <div class="codebar"><span class="code-label">项目根目录</span><button class="copy-btn" type="button" aria-label="复制命令">复制</button></div>
          <pre><code>ls {d}/*.tgz</code></pre>
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
            <p>推荐把构建参数 <code>prod_command</code> 设为
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
    vendor = f"src/{FRAMEWORK_VENDOR_DIR}/{label}"
    tgz = f"{label}-{p.framework_spec if p.frontend_version else '<latest>'}.tgz"
    if p.frontend_framework == "bootstrap":
        return f"""
## Bootstrap 前端库（pack in artifact，本包自带离线 tgz）

构建期已从 npm 源下载 `{tgz}` 随本 bundle 分发，install.sh 会自动把它解压到
应用源码根目录 `{vendor}-<version>/`（容器内路径
`/app/{FRAMEWORK_VENDOR_DIR}/{label}-<version>/`）。

1. 确认安装结果：`ls {vendor}-*/dist/css/bootstrap.min.css`
2. 模板中按需引用 `<version>` 对应的 dist 静态资源（css/js）。
3. 升级版本：重新构建时指定 `frontend_version`，替换 `{vendor}/` 下对应目录。
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

3. 升级版本：重新构建时指定 `frontend_version`，替换 `{vendor}/` 下对应目录。
"""


def _framework_html_section(p):
    """readme.html 中的前端框架卡片（仅 pack in artifact 模式）。"""
    if not p.framework_pack:
        return ""
    label = p.framework_vendor_label
    vendor = f"src/{FRAMEWORK_VENDOR_DIR}/{label}"
    step_no = "08" if p.oracle_enabled else "07"
    if p.frontend_framework == "bootstrap":
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
      <h2><span class="step-dot">{step_no}</span>{FRAMEWORK_NAMES[p.frontend_framework]}（本包自带离线 tgz）</h2>
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
      <div class="tip"><strong>升级版本：</strong>重新构建时填写 <code>frontend_version</code>
      （留空拉最新版），install.sh 会安装到新的 <code>{label}-&lt;version&gt;</code> 目录。</div>
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
    return f"""
## 前端离线开发（Node.js / npm）

镜像内置 Node.js {p.node_version} + npm；Npm Download Action 的 `npm_packages`
产物（顶层 `packages/`）以只读卷挂载到容器 `{NPM_PACKAGES_CONTAINER_DIR}`。

1. 在内网用 Npm Download Action 下载依赖（见 `docs/npm-offline-package-guide.md`），
   把产物中的 `packages/` 内容放到 `{d}`（默认项目根目录下）。
2. 安装依赖到 `/app`（二选一）：
   - 一键加载（推荐）：项目根执行 `bash build.sh`（检测到 `deps/packages/*.tgz`
     即通过容器离线安装）；
   - 或：
   ```bash
   make npm-install
   ```
   等价于容器内 `npm install --offline {NPM_PACKAGES_CONTAINER_DIR}/*.tgz`。
3. 开发调试（二选一）：
   - 推荐：构建参数 `prod_command` 设为 `npm run dev -- --host 0.0.0.0`，
     `app_port` 设为前端端口映射（vite `5173:5173` / webpack `8080:8080`），`make up` 即带
     宿主机端口映射；源码经 `/app` 挂载，支持热更新；
   - 临时：`docker compose run --rm -p 5173:5173 dev npm run dev -- --host 0.0.0.0`
     （`make shell` 进入容器手动启动时需要自行发布端口）。
4. 离线包目录不在项目根时：`export NPM_PACKAGES_DIR=/data/packages`，或
   `make npm-install NPM_PACKAGES_DIR=/data/packages`。
"""


def _agent_readme_section(p):
    """bundle README 的 AI Agent 约定：开发/测试统一在容器内执行，不依赖宿主机环境。"""
    if p.dev_tools_enabled:
        run = "docker compose run --rm dev"
        intro = (
            "本项目的 Python / Node 运行环境**只在容器内**（dev 镜像已内置 "
            "ruff / pytest）。宿主机无需安装 Python，也不要用宿主机的 "
            "`python` / `pip` / `pytest` / `npm` 代替容器内环境——版本与依赖"
            "都可能与镜像不一致。"
        )
        rows = [
            ("查看 Python 版本", f"`{run} python -V`"),
            ("执行 Python 脚本", f"`{run} python path/to/script.py`"),
            ("交互式 shell", "`make shell`"),
            ("运行全部测试", "`make test`"),
            ("运行单个测试文件", f"`{run} pytest -q tests/test_x.py`"),
            ("静态检查 / 编译检查", "`make lint` / `make compile`"),
            ("启动应用 / 查看日志", "`make up` / `make logs`"),
        ]
    else:
        run = "docker compose run --rm app"
        intro = (
            "本次**未构建 dev 镜像**，镜像内没有 ruff / pytest；但 Python 运行环境"
            "仍然只在容器内。宿主机无需安装 Python，也不要用宿主机的 "
            "`python` / `pip` 代替容器内环境。"
        )
        rows = [
            ("查看 Python 版本", f"`{run} python -V`"),
            ("执行 Python 脚本", f"`{run} python path/to/script.py`"),
            ("交互式 shell", f"`{run} bash`"),
            ("启动应用 / 查看日志", "`make up` / `make logs`"),
        ]
    if p.enable_node and p.dev_tools_enabled:
        rows.append(("安装前端离线依赖", "`make npm-install`"))
        rows.append(("运行 npm / node", f"`{run} npm ...` / `{run} node ...`"))
    table = "\n".join(f"| {purpose} | {cmd} |" for purpose, cmd in rows)
    return f"""
## 给 AI Agent 的使用约定（重要）

{intro}

所有命令都在**项目根目录**执行，统一通过 docker compose 进入容器：

| 目的 | 推荐命令 |
| --- | --- |
{table}

- 容器把项目根目录挂载为 `/app`，工作目录固定 `/app`；容器内修改会直接写回宿主机。
- **宿主机即使存在 `python` / `pytest` / `npm`，也不要用来运行项目代码或安装依赖**：
  宿主环境与镜像不一致，所有验证都应以容器内结果为准。
- 非交互场景（Agent / CI）请用 `{run} <命令>`；`make shell` / `bash` 交互式
  终端会等待输入，不要用于自动化。
- 首次使用先执行 `make check` 确认镜像已导入；离线机器不要执行 `make build` 或
  `docker compose build`。
- 缺 Python 包时不要在宿主机 `pip install`：把 wheel 放进项目根 `deps/wheels/` 后执行
  `bash build.sh`（见下文「离线补装 Python 包」）。
- 不要用宿主机 `python -m venv` / `conda` / 全局 `pytest` / 全局 `npm install`
  代替容器内环境。
"""


def _agent_html_section(p):
    """readme.html 中的 AI Agent 使用约定卡片。"""
    if p.dev_tools_enabled:
        run = "docker compose run --rm dev"
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
      在宿主机安装，请把 wheel 放进项目根 <code>deps/wheels/</code> 后执行
      <code>bash build.sh</code> 离线补装。离线机器也不要执行 <code>make build</code>。</div>
    </section>
"""


def render_src_readme(p):
    services = []
    if p.enable_nginx:
        services.append("nginx:stable (宿主机 8080 → 容器 80)")
    if p.enable_postgres:
        services.append("postgres:16 (数据卷 pgdata)")
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
        node_summary = f"Node.js {p.node_version} + npm"
        if p.npm_package_list:
            node_summary += f"（全局预装：{', '.join(p.npm_package_list)}）"
    else:
        node_summary = "未启用"
    if p.framework_pack:
        framework_summary = (
            f"{FRAMEWORK_NAMES[p.frontend_framework]}（pack in artifact，install.sh "
            f"安装到 `src/{FRAMEWORK_VENDOR_DIR}/{p.framework_vendor_label}-<version>`，"
            f"版本 {p.framework_spec}）"
        )
    elif p.framework_npm:
        framework_summary = (
            f"{FRAMEWORK_NAMES[p.frontend_framework]}（install via npm，已打进镜像，"
            f"{' '.join(p.framework_npm_packages)}）"
        )
    else:
        framework_summary = "未启用"

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
            "ORACLE_SECTION": _oracle_readme_section(p),
            "FRAMEWORK_SECTION": _framework_readme_section(p),
            "NPM_SECTION": _npm_readme_section(p),
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
        # 由 install.sh 原样复制到项目根目录为 docker-compose.yml
        # （模板内 ${SRC_ROOT:-./src/} / ${SRC_ROOT_NAME:-app} 由 compose 运行时解析）
        "docker-compose.yml.tpl": render_compose(p),
        "Makefile": render_makefile(p),
        "requirements.txt": render_requirements(p),
        "main.py": render_main_py(),
        "README.md": render_src_readme(p),
        # 离线补装：wheel 打进现有镜像生成 addon 镜像
        "Dockerfile.addon": render_dockerfile_addon(p),
        "build.sh": render_build_sh(p),
        # .gitignore 随 install.sh 安装/合并到项目根目录（src root）。
        # 注意：.dockerignore 不能在这里写入——staging 即 CI 构建上下文，
        # 其 wheels/ 排除规则会让构建期的 wheels 引用失败；
        # 它在 docker 构建完成后由 main.py 写入，仅供 src 产物分发
        ".gitignore": render_gitignore(p),
        # 根目录 .env.example：docker-compose.yml 运行时变量模板（默认值一致，
        # 仅缺失时安装）。部署脚本的 SYNC_REMOTE_* 变量模板是 scripts/.env.example
        ".env.example": render_env_example(p),
    }
    for name, content in files.items():
        (staging / name).write_text(content, encoding="utf-8")
    # 保留可执行位（tar 会随包保留；artifact zip 解压可能丢失，文档引导 bash 调用）
    (staging / "build.sh").chmod(0o755)
    # 部署辅助脚本（静态复制，不渲染）：随 src 产物分发到包内 scripts/ 目录，
    # install.sh 安装到项目根 scripts/（clean-images.sh 由 make clean 调用）
    scripts_dir = staging / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for name in SCRIPTS_FILES:
        shutil.copy2(BUILDER_DIR / "scripts" / name, scripts_dir / name)
    if p.enable_nginx and p.has_app_port:
        conf_dir = staging / "nginx" / "conf.d"
        conf_dir.mkdir(parents=True, exist_ok=True)
        (conf_dir / "default.conf").write_text(
            render_nginx_conf(p), encoding="utf-8"
        )
    return files
