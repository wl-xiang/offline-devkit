"""生成 GITHUB_STEP_SUMMARY（构建摘要）与 GITHUB_OUTPUT（产物路径等输出）。"""

import hashlib
import os
from pathlib import Path

from .package import human_size
from .params import (
    FRAMEWORK_NAMES,
    FRAMEWORK_VENDOR_DIR,
    Params,
    VUE_STACK_NAME,
    VUE_TEMPLATE_DIR,
)


def _yes_no(flag):
    return "是" if flag else "否"


def _param_cell(value):
    """minimal 参数值渲染为表格单元格（布尔用小写 JSON 形式）。"""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value).replace("|", "\\|").replace("\n", " ")
    return f"`{text}`"


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_markdown(p, results):
    """按计划书 4.7 生成 Summary markdown。"""
    wheels_count = results.get("wheels_count", 0)
    images = results.get("images") or [p.app_image]
    src_tgz = results.get("src_tgz")
    images_tgz = results.get("images_tgz")
    install_sh = results.get("install_sh")
    readme_html = results.get("readme_html")
    devcontainer_json = results.get("devcontainer_json")
    clean_sh = results.get("clean_sh")
    images_txt = results.get("images_txt")
    framework_tgz = results.get("framework_tgz")
    skipped_docker = results.get("skipped_docker", False)
    bundle_name = p.bundle_name

    out = []
    out.append("## 离线开发环境构建摘要")
    out.append("")

    # devcontainer.json 的 minimal section：只含用户实际改动过的输入。
    # 放在摘要最前面，确保运行页面一眼可见（完整快照见产物）。
    minimal_params = results.get("workflow_params_minimal")
    out.append("### 本次改动输入（devcontainer.json minimal）")
    out.append("")
    if minimal_params:
        out.append("| 参数 | 值 |")
        out.append("| --- | --- |")
        for name, value in minimal_params.items():
            out.append(f"| `{name}` | {_param_cell(value)} |")
    else:
        out.append("- (无，全部为默认值)")
    out.append("")
    out.append("> 完整快照见产物 `devcontainer.json`（纯 JSON，随 install.sh 安装到"
               " 项目根目录）。")
    out.append("")

    out.append("### 构建参数")
    out.append("")
    out.append("| 项目 | 值 |")
    out.append("| --- | --- |")
    out.append(f"| 时间戳 (UTC+8) | `{p.ts}` |")
    out.append(f"| 镜像 TAG | `{p.tag}` ({p.image_tag}) |")
    out.append(f"| 目标平台 | `{p.docker_platform}` |")
    out.append(f"| 预装语言环境 (target_codec) | `{p.target_codec}` |")
    out.append(f"| glibc (wheel 标签) | `{p.glibc_version}` → `{p.pip_platform_tag}` |")
    out.append(f"| pip 镜像源 | `{p.pip_mirror}` ({p.mirror_url}) |")
    out.append(f"| wheels 数量 | {wheels_count} |")
    out.append(f"| 预装 Playwright (pip + chromium) | {_yes_no(p.enable_playwright)} |")
    if p.oracle_full:
        oracle_mode = (
            "完整安装（客户端已打进镜像，"
            f"`ORACLE_HOME={p.oracle_client_dir}`）"
        )
    elif p.oracle_enabled:
        oracle_mode = (
            "仅运行时依赖（客户端由 compose 从宿主机只读挂载到 "
            f"`{p.oracle_client_dir}`）"
        )
    else:
        oracle_mode = "未启用（none）"
    out.append(f"| Oracle Instant Client 19 | {oracle_mode} |")
    if p.enable_node:
        node_mode = f"Node.js {p.node_version} + npm + pnpm + npx"
        if p.framework_npm:
            node_mode += f"（含 {' '.join(p.framework_npm_packages)} 全局预装）"
        if p.npm_package_list:
            node_mode += f"（含 {', '.join(p.npm_package_list)} 全局预装）"
        if p.node_dev_only:
            node_mode += "（Vue 预设自动启用，仅 dev 镜像）"
    else:
        node_mode = "未启用"
    out.append(f"| Node.js / npm | {node_mode} |")
    if p.vue_enabled:
        out.append(
            f"| Vue 前端技术栈 | {VUE_STACK_NAME}（仅 dev 镜像，含 node_modules；"
            f"模板 `{VUE_TEMPLATE_DIR}`，`make vue-init` 初始化到 `{p.vue_dir}/`） |"
        )
    if p.enable_sync_tool:
        out.append(
            "| 代码同步工具 (v-push) | 已启用：`python:3.11-slim` + paramiko"
            f"（`{p.sync_image}`，固定镜像名；compose 服务 `v-push` / "
            "profile `v-push`，`make v-push` 容器内执行） |"
        )
    else:
        out.append("| 代码同步工具 (v-push) | 未启用（`make push` 在宿主机执行） |")
    if p.framework_pack:
        framework_name = (framework_tgz.name if framework_tgz
                          else f"{p.framework_vendor_label}-{p.framework_spec}.tgz")
        out.append(f"| 前端框架 {FRAMEWORK_NAMES[p.framework_name]} | pack in artifact"
                   f"（`{framework_name}` 随 Artifact 分发，install.sh 安装到 "
                   f"`src/{FRAMEWORK_VENDOR_DIR}/{p.framework_vendor_label}-<version>`） |")
    elif p.framework_npm:
        out.append(f"| 前端框架 {FRAMEWORK_NAMES[p.framework_name]} | install via npm"
                   f"（构建期镜像内全局安装 `{' '.join(p.framework_npm_packages)}`） |")
    if p.enable_node:
        npm_pkg_summary = ", ".join(f"`{x}`" for x in p.npm_package_list) or "(无)"
        out.append(f"| npm 全局预装包 | {npm_pkg_summary} |")
        out.append(
            f"| npm 离线包目录 | `${{NPM_PACKAGES_DIR:-{p.npm_packages_dir}}}`"
            " → `/opt/npm-packages`（只读挂载；相对值相对 docker/ 目录解析，"
            f"默认 docker/{p.npm_packages_dir}） |"
        )
    env_summary = ", ".join(f"`{k}`" for k, _v in p.preset_env_pairs) or "(无)"
    out.append(f"| 预设环境变量 (app/dev) | {env_summary} |")
    if p.dev_image_only:
        image_mode = "仅 dev 镜像（部署与开发共用，镜像归档只含该镜像）"
    elif p.build_dev_image:
        image_mode = "prod + dev 两个镜像"
    else:
        image_mode = "仅 prod 镜像"
    out.append(f"| 镜像模式 | {image_mode} |")
    out.append(f"| 应用端口 (host:container) | {('`' + p.app_port + '`') if p.has_app_port else '(未指定)'} |")
    if p.nginx_service_mode:
        nginx_mode = "独立 nginx 服务（宿主机 8080 → 容器 80，反代/托管 web 静态产物）"
    elif p.nginx_binary_mode:
        nginx_mode = "nginx 装进镜像（APT，docker-run.sh 启动时拉起，外部端口复用 app_port）"
    else:
        nginx_mode = "未启用"
    out.append(f"| nginx 托管 (nginx_mode) | {nginx_mode} |")
    out.append(f"| env_file | {', '.join(p.env_file_list) if p.env_file_list else '(无)'} |")
    out.append(f"| SELinux 重打标签 (:z) | {_yes_no(p.selinux_relabel)} |")
    out.append("| 时区（内置启用） | `TZ=Asia/Shanghai`（镜像内）+ `/etc/localtime` / `/etc/timezone` 只读挂载 |")
    out.append("")

    out.append("### pip 包清单（含预装组件）")
    out.append("")
    if p.preinstall_pip_packages:
        for x in p.preinstall_pip_packages:
            note = ""
            if x == "playwright" and p.enable_playwright:
                note = "（预装：浏览器自动化，chromium 已内置 /opt/ms-playwright）"
            elif x.startswith("oracledb") and p.oracle_enabled:
                note = "（预装：配合 Instant Client 19 连 Oracle 11g）"
            out.append(f"- `{x}`{note}")
    else:
        out.append("- (无)")
    out.append("")

    out.append("### APT 工具")
    out.append("")
    if p.apt_package_list:
        out.extend(f"- `{x}`" for x in p.apt_package_list)
    else:
        out.append("- (无)")
    out.append("")

    out.append("### 个性化安装命令")
    out.append("")
    if p.custom_install_cmd:
        out.append("```bash")
        out.append(p.custom_install_cmd)
        out.append("```")
    else:
        out.append("- (无)")
    out.append("")

    out.append("### 外挂 DB 服务")
    out.append("")
    if p.db_service != "none":
        depends_note = "app 自动 depends_on DB 服务（等待健康检查通过）" if p.has_db_service else "app 无 DB 依赖"
        out.append(f"| enable_db | `{p.db_service}`（{depends_note}） |")
    else:
        out.append("| enable_db | `none` |")
    out.append("")

    out.append("### 生成的镜像")
    out.append("")
    out.extend(f"- `{img}`" for img in images)
    if skipped_docker:
        out.append("")
        out.append("> 注意: 本次设置了 DEVCONTAINER_SKIP_DOCKER=1，未执行 docker"
                    " 构建/保存，因此没有镜像归档产物。")
    out.append("")

    out.append("### 产物清单")
    out.append("")
    out.append(f"唯一 Artifact：**{bundle_name}**（zip 封装、仅存储不压缩，"
               "解压即得以下文件）")
    out.append("")
    out.append("| 文件 | 大小 | SHA256 |")
    out.append("| --- | --- | --- |")
    if images_tgz:
        out.append(
            f"| {images_tgz.name}（镜像归档，docker load 用） "
            f"| {human_size(images_tgz.stat().st_size)} "
            f"| `{sha256_of(images_tgz)}` |"
        )
    if src_tgz:
        out.append(
            f"| {src_tgz.name} | {human_size(src_tgz.stat().st_size)} "
            f"| `{sha256_of(src_tgz)}` |"
        )
    if install_sh:
        out.append(
            f"| {install_sh.name}（安装脚本） | {human_size(install_sh.stat().st_size)} "
            f"| `{sha256_of(install_sh)}` |"
        )
    if readme_html:
        out.append(
            f"| {readme_html.name}（安装说明） | {human_size(readme_html.stat().st_size)} | (HTML) |"
        )
    if devcontainer_json:
        out.append(
            f"| {devcontainer_json.name}（workflow 输入参数快照 JSON，"
            "随 install.sh 与 README.md 一起安装到项目根目录） "
            f"| {human_size(devcontainer_json.stat().st_size)} "
            f"| `{sha256_of(devcontainer_json)}` |"
        )
    if clean_sh:
        out.append(
            f"| {clean_sh.name}（一键清理脚本：compose down + 按 images.txt "
            "删除容器/镜像，与 install.sh 同目录使用） "
            f"| {human_size(clean_sh.stat().st_size)} | `{sha256_of(clean_sh)}` |"
        )
    if images_txt:
        out.append(
            f"| {images_txt.name}（镜像清单，clean.sh 清理用） "
            f"| {human_size(images_txt.stat().st_size)} | `{sha256_of(images_txt)}` |"
        )
    if framework_tgz:
        out.append(
            f"| {framework_tgz.name}（前端框架离线包，install.sh 安装到"
            f" src/{FRAMEWORK_VENDOR_DIR}/{p.framework_vendor_label}-<version>） "
            f"| {human_size(framework_tgz.stat().st_size)} "
            f"| `{sha256_of(framework_tgz)}` |"
        )
    out.append("")

    out.append("### 内网使用步骤")
    out.append("")
    out.append("1. 从本次运行页面底部 **Artifacts** 下载唯一产物"
               f" **{bundle_name}**（zip 仅为封装、不压缩，解压得到"
               " 镜像归档 tgz、src tgz、install.sh、readme.html 与"
               " devcontainer.json 等文件），"
               "传输到与目标平台一致的内网机器，并用上表 SHA256 校验完整性。")
    out.append("")
    out.append("2. 在服务器上新建空的项目根目录，在其中创建 `downloads/` 文件夹，"
               "**把 artifact zip 放到 `downloads/` 并就地解压**，然后在"
               " `downloads/` 里执行安装："
               "create an empty project root dir, put the artifact zipball in"
               " `./downloads/`, unzip it there, then run `bash install.sh ../`")
    out.append("")
    out.append("```bash")
    out.append("mkdir -p /path/to/your-project/downloads   # 项目根目录下的 downloads/")
    out.append(f"# 把 {bundle_name}.zip 移入 downloads/ 后：")
    out.append(f"cd /path/to/your-project/downloads && unzip {bundle_name}.zip")
    out.append("bash install.sh --load ../              # --load 安装开始前自动 docker load 镜像归档")
    out.append("bash install.sh --load-only             # 只导入镜像归档，不做安装")
    out.append("bash install.sh ../                     # 参数 ../ 即项目根目录（或改用上一行的 --load 一步到位）")
    out.append("```")
    out.append("")
    app_dir_note = "api/ + web/ 两个源码目录" if p.is_febe_separation else "app/ 源码目录"
    out.append("   install.sh 会把 `Makefile` / `AGENTS.md` / `devcontainer.json` /"
               " `.dockerignore` / `.gitignore` 安装到项目根，把 `Dockerfile`"
               "（联网重建版）/ `Dockerfile.addon`（离线补装，build.sh 引用）/"
               " `build.sh` / `docker-run.sh` / `init-deps.sh` /"
               " `docker-compose.yml` / `.env.example` 安装到项目根 `docker/`"
               " 目录（compose 项目目录），把 `requirements.txt` 安装到应用源码"
               " 目录（同名文件已存在时终止安装），把部署辅助脚本 `manifest.txt` /"
               " `push-bundle.sh` / `apply-bundle.sh` / `rollback-bundle.sh` /"
               " `clean-images.sh` 安装到"
               " 项目根 `scripts/`（同名冲突时终止安装）；"
               f"准备 {app_dir_note}并放置占位入口，把快速上手说明安装为"
               " `docs/DEVCONTAINER.md`"
               + ("，创建 `nginx/` 配置模板目录" if p.enable_nginx else "") + "。")
    out.append("")
    out.append(f"3. 离线自检（回到**项目根目录**执行，确认镜像已导入、架构匹配；"
               "启动命令由镜像 ENTRYPOINT 的 docker-run.sh 承载，默认 cd /app 后"
               f"运行 {'python main.py' if p.python_enabled else 'go run main.go'}）:")
    out.append("")
    out.append("```bash")
    out.append("cd /path/to/your-project")
    out.append("make check")
    out.append("```")
    out.append("")
    if p.dev_tools_enabled:
        out.append("4. 开发调试（均在**项目根目录**执行）:")
    else:
        out.append("4. 运行服务（本次未构建 dev 镜像，无 make shell 目标）:")
    out.append("")
    out.append("```bash")
    out.append("make shell      # 交互式 bash")
    if p.python_enabled:
        out.append("make lint       # ruff check .")
        out.append("make compile    # python -m compileall -q .")
        out.append("make test       # pytest -q")
    out.append("make up")
    out.append("make logs")
    out.append("make down")
    out.append("```")
    out.append("")
    out.append("> 后续 make / build.sh 命令都在项目根目录执行；完整说明见"
               " `docs/DEVCONTAINER.md`，push/apply/rollback 的同步范围由"
               " `scripts/manifest.txt` 清单决定。")
    out.append("")
    if p.has_app_port:
        host_port, container_port = p.app_port_parts
        out.append(f"应用端口 `{host_port}:{container_port}`（host:container）: 已在 compose 中配置"
                   + (f"（app 仅容器网络 expose {container_port}，由 nginx 反代）。"
                      if p.enable_nginx else "（宿主机端口映射）。"))
        out.append("")
    if p.enable_postgres:
        out.append("postgres 已启用（compose 服务名 `db`）: 连接串 "
                   "`postgres://postgres:postgres@db:5432/postgres`，"
                   "数据持久化在项目根 `./volumes/pgdata` 目录（bind mount）。")
        out.append("")
    if p.enable_mysql:
        out.append("mysql 已启用（compose 服务名 `db`）: 连接 `mysql -h db -uroot -pmysql`"
                   "（root 密码 `mysql`，预置数据库 `app`），数据持久化在项目根 "
                   "`./volumes/mysqldata` 目录（bind mount）。")
        out.append("")
    if p.enable_mongo:
        out.append("mongo 已启用（compose 服务名 `db`）: 连接串 `mongodb://db:27017`，"
                   "数据持久化在项目根 `./volumes/mongodata` 目录（bind mount）。")
        out.append("")
    if p.nginx_service_mode:
        out.append("nginx 已启用（独立服务）: 宿主机 `http://<host>:8080` → 容器 80 端口；"
                   "配置模板在项目根 `nginx/default.conf`（挂载进 nginx 容器）。")
        if p.is_febe_separation:
            out.append("`web/dist` 静态产物挂载到 nginx 容器 `/usr/share/nginx/html`；"
                       "`/api/` 反代到 app 容器端口。")
        out.append("")
    elif p.nginx_binary_mode:
        out.append("nginx 已启用（装进镜像）: 镜像内已 APT 安装 nginx，"
                   "`make up` 时由 docker-run.sh 拉起；宿主机访问 app_port 映射的"
                   "宿主机端口（容器内 nginx 监听 80）。配置模板在项目根 "
                   "`nginx/default.conf`。")
        out.append("")
    if p.oracle_full:
        out.append("### Oracle Instant Client（已完整安装进镜像）")
        out.append("")
        out.append("镜像内**包含** Instant Client 19 Basic（构建期从 Oracle 官方直链"
                   "下载）与运行依赖 `libaio1` / `libnsl2`，并配置"
                   f" `ORACLE_HOME={p.oracle_client_dir}` / `LD_LIBRARY_PATH` /"
                   " `TNS_ADMIN`。客户端随镜像分发，无需宿主机挂载：")
        out.append("")
        out.append("```bash")
        out.append("# 验证 Thick 模式加载成功（输出 OCI OK）")
        out.append('docker compose run --rm app python -c "import oracledb; '
                   "oracledb.init_oracle_client(); print('OCI OK')\"")
        out.append("```")
        out.append("")
    elif p.oracle_enabled:
        out.append("### Oracle Instant Client（客户端由宿主机挂载）")
        out.append("")
        out.append("镜像内**不包含**客户端，仅安装 `libaio1` / `libnsl2` 并配置"
                   " `ORACLE_HOME` / `LD_LIBRARY_PATH` / `TNS_ADMIN`；客户端由"
                   " compose 只读挂载，同一服务器的多个容器共用一份：")
        out.append("")
        out.append("```bash")
        out.append(f"# 1. 宿主机准备 Instant Client 19 Basic 包，解压到 {p.oracle_client_dir}")
        out.append(f"ls -l {p.oracle_client_dir}/libclntsh.so.19.1")
        out.append("# 2. 非默认目录时（无需重建镜像）")
        out.append("export ORACLE_CLIENT_DIR=/data/instantclient19   # make up 前设置")
        out.append("make up")
        out.append("# 3. 验证 Thick 模式加载成功（输出 OCI OK）")
        out.append('docker compose run --rm app python -c "import oracledb; '
                   "oracledb.init_oracle_client(); print('OCI OK')\"")
        out.append("```")
        out.append("")
    if p.enable_node:
        out.append("### 前端离线开发（Node.js / npm）")
        out.append("")
        if p.vue_enabled:
            out.append("> Vue 预设：依赖已预装在 dev 镜像 `/opt/vue-template`（含"
                       " `node_modules`），`make vue-init` 后无需 Npm Download；下列流程"
                       " 用于额外依赖。")
            out.append("")
        out.append(f"镜像内置 Node.js {p.node_version} + npm + pnpm；Npm Download Action 的"
                   " `npm_packages` 产物（顶层 `packages/`）由 compose 只读挂载到"
                   " `/opt/npm-packages`：")
        out.append("")
        out.append("```bash")
        host_dir = (p.npm_packages_dir if p.npm_packages_dir.startswith("/")
                    else f"docker/{p.npm_packages_dir}")
        out.append(f"# 1. 把 Npm Download 产物的 packages/ 内容放到 {host_dir}/"
                   "（目录不存在先 bash docker/init-deps.sh）")
        out.append(f"ls {host_dir}/*.tgz")
        out.append("# 2. 离线安装依赖到 /app")
        out.append("make npm-install")
        out.append("# 3. 启动前端开发服务器：把 docker/docker-run.sh 的默认命令改为 npm run dev -- --host 0.0.0.0、")
        out.append("#    app_port 设为 5173:5173（vite）/ 8080:8080（webpack）后 make up；临时调试可用：")
        out.append("#    docker compose run --rm -p 5173:5173 dev npm run dev -- --host 0.0.0.0")
        out.append("```")
        out.append("")
        out.append(f"离线包目录可用 `NPM_PACKAGES_DIR` 覆盖（默认 `{p.npm_packages_dir}`，"
                   "相对值相对 docker/ 目录解析）。")
        out.append("")
    out.append("> 完整手册见仓库 `docs/offline-devcontainer.md`。")
    out.append("")
    return "\n".join(out)


def emit(p, results):
    """写入 GITHUB_STEP_SUMMARY 与 GITHUB_OUTPUT（不在 Actions 环境时打印到终端）。"""
    md = build_markdown(p, results)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md)
        print(f"== Summary 已写入 {summary_path} ==")
    else:
        print(md)

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        entries = [
            ("ts", p.ts),
            ("image_tag", p.tag),
            # 唯一 Artifact 名（GitHub Actions 上传后为
            # <prefix>_devcontainer_<arch>_<ts>.zip）
            ("bundle_name", p.bundle_name),
        ]
        src_tgz = results.get("src_tgz")
        images_tgz = results.get("images_tgz")
        install_sh = results.get("install_sh")
        if images_tgz:
            entries.append(("images_tgz", str(images_tgz)))
            entries.append(("images_name", images_tgz.name))
            entries.append(("images_sha256", sha256_of(images_tgz)))
        if src_tgz:
            entries.append(("src_tgz", str(src_tgz)))
            entries.append(("src_name", src_tgz.name))
            entries.append(("src_sha256", sha256_of(src_tgz)))
        if install_sh:
            entries.append(("install_sh", str(install_sh)))
            entries.append(("install_sha256", sha256_of(install_sh)))
        readme_html = results.get("readme_html")
        if readme_html:
            entries.append(("readme_html", str(readme_html)))
        devcontainer_json = results.get("devcontainer_json")
        if devcontainer_json:
            entries.append(("devcontainer_json", str(devcontainer_json)))
            entries.append(("devcontainer_json_name", devcontainer_json.name))
        clean_sh = results.get("clean_sh")
        if clean_sh:
            entries.append(("clean_sh", str(clean_sh)))
        images_txt = results.get("images_txt")
        if images_txt:
            entries.append(("images_txt", str(images_txt)))
        framework_tgz = results.get("framework_tgz")
        if framework_tgz:
            entries.append(("framework_tgz", str(framework_tgz)))
            entries.append(("framework_name", framework_tgz.name))
        entries.append(("images", ",".join(results.get("images") or [])))
        with open(output_path, "a", encoding="utf-8") as f:
            for key, value in entries:
                f.write(f"{key}={value}\n")
        print(f"== Outputs 已写入 {output_path} ==")
