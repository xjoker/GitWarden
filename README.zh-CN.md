# GitWarden

> Mirror the present. Preserve the past.
>
> 镜像当前状态，保全已有历史。

GitWarden 是一个自托管的 Git 历史镜像工具。它按计划把来源仓库 fetch 到本地档案，再把已采集状态推送到一个或多个可写 Git 目标。核心流程不依赖托管平台 API，因此来源和目标可以是任意受支持的 Git URL，也可以手工配置 GitHub 仓库。

[English](README.md) · 简体中文

![终端演示：历史仓离线时来源被强推，主镜像整体暂停更新，旧 commit 仍被保全](docs/demo.gif)

*演示（约 50 秒，使用本地裸仓库，终端界面为英文）：GitWarden 先完成一次镜像；随后历史仓拒绝推送，来源对 `main` 执行 force push、对 `dev` 正常快进。安全门禁让整个主镜像暂停更新（两个分支都不推送），旧的 `main` commit 以 `refs/preserved/<ULID>` 留在本地档案；历史仓恢复后，先确认保全标签，再让主镜像追上来源。录屏中的 `gwctl` 是演示用的辅助脚本，提交的是与备份向导和 **Run now** 按钮相同的 WebUI 表单；GitWarden 本身不提供 CLI。*

![GitWarden 总览](docs/images/overview.jpg)

## 它保护什么

每个备份任务包含一个来源、一个持久化本地档案和一个或多个目标。每个目标由两个仓库组成：

- **主镜像仓**：保存当前的 `refs/heads/*` 和 `refs/tags/*`；
- **历史仓**：保存 GitWarden 的 `gw/*` 保全标签，必须设为私有。

当已采集的分支发生 force push 或删除，或者已采集的标签被改写时，GitWarden 会先把旧 Git 对象保存在本地档案，再更新镜像引用；之后先把旧对象推到历史仓，主镜像更新通过门禁后才会继续。普通快进不额外创建保全标签。整个过程自动执行，普通 Git 变更不需要人工审批。

如果来源仍能访问，但突然变成空仓或丢失了大部分 ref，GitWarden 会把它当作可疑情况，而不是把备份也清空。当一轮同步会删光主镜像的全部 ref、删掉全部分支（不论还剩多少 tag），或者删除至少一半且不少于 3 个 ref（可通过 `GW_DELETE_GUARD_RATIO` 和 `GW_DELETE_GUARD_MIN_REFS` 调整）时，被删的 ref 仍会保全到本地档案和历史仓，但这一轮主镜像不做任何改动，目标标为**需要处理**（`delete_guard`）。来源完全拉取失败（404、401、路径不存在）时不会删除任何内容；持续失败期间，目标显示为**来源失败**，不再显示为健康。

关于大批删除保护（详见 [Configuration → Mass-deletion guard](docs/wiki/Configuration.md#mass-deletion-guard)）：

- **放行：** 保护只按 ref 名字计数，不记录被拦状态。只要来源上同名的 ref 回来了，不管内容是什么，下一轮就会放行，主镜像会被改写成新内容；如果只回来一部分，剩下的删除低于门槛，这些删除会在这一轮直接生效。旧提交仍保存在历史仓里。如果删除是有意的，临时调高 `GW_DELETE_GUARD_RATIO` 或设置 `GW_DELETE_GUARD=off`（删光全部 ref 或删掉全部分支时只有 `off` 有效），重启，让任务跑完一轮，再改回原设置并重启。单纯点**重试**会再次被拦。
- **代价：** 这个设置是全局的，放宽或关闭期间，所有任务都没有大批删除保护。
- **误拦：** 合法地一次改名 3 个及以上分支、所有分支一起改名，或一次性大批清理分支，都会被拦。
- **拦不住：** 逐步删除、先建一个空占位分支再删、用垃圾 tag 稀释比例、只有两个 ref 的小仓库、force push 成无关历史。这些情况下，每个被删或被改写的 ref 仍会保全在历史仓里。
- `GW_DELETE_GUARD=off`（或 `0`）会连“删光全部 ref”的拦截一起关掉。

GitWarden 只保护已经采集到的状态。两次轮询之间出现又消失的变更无法恢复。范围包含 Git 对象，不包含 LFS 文件内容和子模块仓库；界面会识别这些引用并将任务标为备份不完整。

### 安全门禁拦截时会发生什么

GitWarden 修改主镜像上已有的 ref 之前，会先经过安全门禁。分支的快进直接放行；其他修改（分支的非快进更新、删除分支，以及标签的任何移动或删除）只有在被覆盖的旧对象已经在本轮运行中被该目标的历史仓确认时才放行：旧对象必须是某个已确认保全标签的对象，或者（仅限分支）被某个已确认的保全 commit 包含。因此历史仓正常时，force push、删分支和改 tag 在保全标签确认后会自动放行。如果旧对象无法在历史仓确认，例如历史仓不可达或拒绝推送，这次修改就会被拦下，目标显示 **Blocked by safety gate**（`gate_blocked`，门禁阻断）。如果历史仓里已有对象不一致的 `gw/*` 标签，目标会显示 **History conflict**（`history_conflict`，历史冲突），它优先于 `gate_blocked`。如果主镜像上任一 ref 与 GitWarden 上次推送的值不一致（**Mirror conflict**，`conflict`，通常意味着有其他写入方），主镜像同样会暂停。

拦截范围是该目标的整个主镜像，而不只是出问题的那个 ref：只要有一个 ref 被门禁拦下或存在冲突，本轮就不会向这个主镜像推送任何 ref 的更新或删除，其他分支和标签的普通快进也一样暂停。此时来源仍会继续采集到本地档案，旧对象以 `refs/preserved/<ULID>` 保留在本地，每轮仍会尝试把待推送的保全标签推到历史仓，同一任务的其他目标也会各自独立处理。恢复方法是先排除原因：让历史仓恢复可达并允许 GitWarden 写入，或者停掉意外的写入方，把冲突的 ref 恢复为 GitWarden 上次推送的值；然后在目标卡片上点 **Retry**，或等待下一次计划运行。GitWarden 会先确认待推送的 `gw/*` 标签，再把主镜像更新到最新状态。当前版本没有绕过门禁、强制推送主镜像的选项。详见 [Troubleshooting](docs/wiki/Troubleshooting.md#target-run-is-blocked-or-conflicted)。

历史仓不是第二个完整镜像。它保存被覆盖或删除的历史版本，当前引用仍在主镜像仓和本地档案中。需要最新已采集状态时，请保留主镜像仓或 GitWarden 数据卷。

## 直接试用发布镜像

不想先获取源码时，可以直接运行发布镜像：使用单独的数据卷、只绑定本机回环地址的端口，日志轮转与 Compose 相同。然后打开 <http://127.0.0.1:8080>：

```bash
stty -echo; printf 'GitWarden admin password: '; IFS= read -r GW_ADMIN_PASSWORD; stty echo; echo
export GW_ADMIN_PASSWORD
docker run -d --name gitwarden-trial --restart unless-stopped -e GW_ADMIN_PASSWORD -p 127.0.0.1:8080:8080 -v gitwarden-trial-data:/data --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 ghcr.io/xjoker/gitwarden:20261016.1
unset GW_ADMIN_PASSWORD
```

第一行在 bash 和 zsh 中都能运行：`stty -echo` 让输入不回显，`read` 让密码不进入 shell 历史。不带值的 `-e GW_ADMIN_PASSWORD` 会从当前环境传入密码，之后的 `unset` 会把它从当前 shell 中清除。如果通过 `sudo` 运行 Docker，请用 `sudo --preserve-env=GW_ADMIN_PASSWORD docker run …`：普通 `sudo` 会丢掉这个变量，容器会拒绝启动。不要用 `sudo -E`，它会把整套环境交给 root，包括 `DOCKER_HOST`、`DOCKER_CONTEXT` 和代理设置。GitWarden 只从环境变量 `GW_ADMIN_PASSWORD` 读取密码，所以无论用 `-e` 还是 `--env-file` 传入，能访问 Docker daemon 的人都可以用 `docker inspect` 看到它。按 [Deployment](docs/wiki/Deployment.md#plain-docker) 使用权限为 600 的环境变量文件加 `--env-file`，同样可以避免密码进入 shell 历史，但它是存在磁盘上的明文文件。不要把端口发布到公网接口，见 [Security](SECURITY.md)。

试用容器使用数据卷 `gitwarden-trial-data`，与 Compose 的 `gitwarden-data` 分开，但端口同样是 8080。启动 Compose 前先执行 `docker rm -f gitwarden-trial`；不再需要试用数据时，用 `docker volume rm gitwarden-trial-data` 删除。

## 用 Docker Compose 快速启动

准备好本仓库的工作副本后，在项目目录执行：

```bash
cp .env.example .env
```

在 `.env` 中设置必填的管理员密码：

```dotenv
GW_ADMIN_PASSWORD=replace-this-before-start
```

启动服务并打开 <http://127.0.0.1:8080>：

```bash
docker compose up -d --build
docker compose logs -f gitwarden
```

Compose 配置使用项目目录的 `.env`、名为 `gitwarden-data` 的 named volume，以及 `127.0.0.1:8080`。默认镜像在本地构建为 `gitwarden:local`，Compose 会按主机架构自动选择 amd64 或 arm64。容器以 UID/GID `10001` 运行，应用数据库、档案、凭据和临时工作数据都在 `/data` 下。

发布镜像在对应的 `vYYYYMMDD.N` 发布 tag 通过 CI 后推送到 `ghcr.io/xjoker/gitwarden`，每次发布推送版本 tag（不带 `v`，例如 `20261016.1`）和 `latest`。如需使用发布镜像，可设置 `GW_IMAGE` 并跳过本地构建：

```bash
GW_IMAGE=ghcr.io/xjoker/gitwarden:20261016.1 docker compose pull
GW_IMAGE=ghcr.io/xjoker/gitwarden:20261016.1 docker compose up -d --no-build
```

可用版本和发布说明见 [Releases](https://github.com/xjoker/GitWarden/releases)。

普通 Docker 部署见 [Deployment](docs/wiki/Deployment.md)；配置、数据卷备份和恢复见 [Wiki 源文件索引](docs/wiki/Home.md)。

### 监听地址

> **升级说明：** 直接运行时（`python -m gitwarden`、systemd，或者不基于本仓库 `Dockerfile` 的自制镜像），GitWarden 现在只监听 `127.0.0.1`。之前的版本总是监听 `0.0.0.0`。如果需要重新让其他机器访问，请设置 `GW_HOST=0.0.0.0`，或在 `gitwarden.toml` 的 `[server]` 下写 `host = "0.0.0.0"`，最好放在防火墙或反向代理之后。

官方镜像和 Compose 不受影响：镜像设置了 `GW_DEFAULT_HOST=0.0.0.0`，容器内仍监听所有接口；Compose 在宿主机上仍只把端口发布到 `127.0.0.1`（`GW_BIND`）。显式设置的 `GW_HOST` 或 `[server] host` 总是优先于镜像默认值。详见 [Listen address](docs/wiki/Configuration.md#listen-address)。

## 创建第一个备份

1. 使用 `GW_ADMIN_PASSWORD` 登录。WebUI 默认英文，也可以切换简体中文。
2. 打开 **Credentials**，为私有来源或目标保存 HTTPS 用户名/令牌，或者保存 SSH 私钥及对应的 `known_hosts` 条目。只有匿名端点才留空凭据。不要把凭据写进 Git URL。
3. 选择 **Back up a repository**。第 1 步填写来源 URL 和同步间隔；第 2 步填写两个手工目标 URL，或选择已配置的 Gitea/Forgejo 接入自动建目标；第 3 步确认目标安全前提后创建任务。
4. 手动运行一次任务，再检查目标卡片和 **Logs**。目标卡片会显示主镜像是否同步、历史仓已确认的保全记录数量以及上次审计时间。

手工目标的两个仓库在绑定时都必须已经存在且为空。主镜像仓和历史仓都应设为私有，并确保只有 GitWarden 能写入，且推送不会触发 CI 或 webhook。平台建库会创建 `<name>` 和 `<name>-history`，将两者设为私有，关闭 Actions 等仓库功能，并在历史仓保护 `gw/*` 后完成绑定。

![任务详情和历史状态](docs/images/task-detail.jpg)

## 端点和平台扩展

核心接受匿名、HTTPS、SSH、SCP 风格 SSH 和本地绝对路径 Git 端点。HTTPS 凭据通过 Git askpass 传递；SSH 使用临时私钥文件、严格主机密钥校验，操作结束后删除临时文件。

本地绝对路径在 GitWarden 进程内解析。用 Docker 部署时，没有挂进容器的宿主机路径会在同步时失败，报 `does not appear to be a git repository`。请把仓库挂进容器，并填写容器内路径，例如 `/repos/src.git`。属主和容器用户不一致的仓库会被 Git 拒绝（`detected dubious ownership`），所以挂进去的仓库属主必须是容器用户（默认 UID `10001`；Git 只检查顶层目录，例如 `sudo chown 10001 /srv/git/src.git`）。来源只需要只读挂载。`docker run` 和 Compose 示例，以及宿主机上仍有人推送时的做法，见 [Deployment](docs/wiki/Deployment.md#local-source-repositories)。

可选平台集成目前只支持 **Gitea** 和 **Forgejo**。它可以发现仓库、按 owner 和通配规则筛选、创建目标、加固目标，并报告当前令牌可见的仓库级/组织级/系统级 webhook 元数据。平台接入必须使用包含用户名和令牌的 HTTPS 凭据；按操作需要，令牌权限应包含 `read:admin`、`write:repository`、`write:organization` 和 `write:user`。

GitHub 没有发现或自动建库适配器。请在三步向导中手工填写 GitHub 来源和两个目标 URL，并遵守目标仓库为空、私有且单一写入方的规则。

## 运行方式

调度器默认使用 2 个 worker，同步间隔下限为 5 分钟，发现间隔下限为 15 分钟，连续 10 分钟无 Git 进度才判定卡死。大仓库只要持续有进度就可以长时间运行；最近运行耗时较长时，间隔会自动延长，连续失败时会退避。

总览页会突出显示备份不完整、目标冲突、门禁阻断、来源可见性变化和逾期任务。**Logs** 保存运行步骤、ref 变化、脱敏后的 Git 输出和管理员操作记录。运行与操作记录默认保留 90 天；保全事件、本地档案和 Git 数据不参与这项清理。

可用 `/health` 做只读的发布检查：

```bash
curl --fail http://127.0.0.1:8080/health
```

响应包含应用版本；如果构建镜像时注入了提交号，也会包含构建 Git commit。

## 文档

- [Wiki 首页](docs/wiki/Home.md)
- [入门](docs/wiki/Getting-started.md)
- [部署](docs/wiki/Deployment.md)
- [配置](docs/wiki/Configuration.md)
- [备份与恢复](docs/wiki/Backup-and-recovery.md)
- [平台接入](docs/wiki/Platforms.md)
- [故障排查](docs/wiki/Troubleshooting.md)
- [发布验证记录](docs/Validation.md)
- [发布流程（维护者）](docs/Publishing.md)

以上文档目前只有英文版。

## 许可证

GitWarden 采用 [Apache License 2.0](LICENSE)。第三方依赖保留各自的许可证。
