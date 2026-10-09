# VerdantFlare App Blender

VerdantFlare 的 Blender 市场应用集成项目。

当前状态：已迁入 Blender 集成源码，正在完成 5090 dev 的构建与运行验收；未宣称已上线。版本以 `VERSION` 为准。

## 仓库范围

本仓库管理 Blender 市场应用的集成源码及构建配置。`app/` 是实例授权、MCP、编辑会话和持久操作记录服务，`runtime/` 是迁入的 Blender 插件、内部执行接口及图形运行环境集成。来源和第三方组件说明见 [UPSTREAM.md](UPSTREAM.md)；不代表维护 Blender 上游本体的 fork。

产品设计、API 契约、实施计划及部署事实源统一维护在 [verdantflare-design](https://github.com/verdantflarehub/verdantflare-design)。

## 分支

| 分支 | 用途 |
| --- | --- |
| `dev` | 日常开发、迁移与集成；本地工作分支。 |
| `release` | 从 `dev` 快进，承载后续发布构建。 |
| `main` | 默认展示分支，保存验证后的稳定版本。 |

`release` 的 CI 构建应用和 worker 两个镜像，不运行可在本地完成的测试。`main` 在部署验收后同步。流程遵循设计仓库的 [GitHub Actions 应用指南](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/GithubAction.md)。

## 本地检查与构建

Python 3.10+；Unix socket 运行时测试需要 Linux（Windows 可使用 WSL）。无需安装 Python 第三方依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s runtime/tests -v
bash -n runtime/blender-mcp-entrypoint.sh runtime/blender-run.sh scripts/check-image-absent.sh
docker build -f Dockerfile -t blender-app:local .
docker build -f Dockerfile.worker -t blender-worker:local .
```

真实 Blender 的 `runtime/tests/blender_adapter_smoke.py` 需在 Blender 内运行，并使用独立临时工作区；它会创建对象和保存工程。GPU/Wayland 验收与部署参数记录在设计仓库，不能用普通 Python 测试代替。

## 开发约定

- MCP 对外统一经 Studio 接入，内部服务不新增独立公网 MCP 入口。
- 凭据仅从环境或受控配置读取，不提交到源码仓库。
- 本地工作目录、Blender 工程、音视频与模型权重不提交到 Git。
- 开发和交付遵循设计仓库的 [工作区规范](https://github.com/verdantflarehub/verdantflare-design/blob/dev/AGENTS.md)。
