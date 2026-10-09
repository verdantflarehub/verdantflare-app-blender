# VerdantFlare App Blender

VerdantFlare 的 Blender 市场应用集成项目。

当前状态：仓库初始化，尚未迁入应用源码，尚无可运行或已发布版本。

## 仓库范围

本仓库用于管理 Blender 市场应用的集成源码及构建配置。Desktop 中的 Blender MCP 实现作为迁移参考，具体迁移范围待源码分析后确定；当前不代表维护 Blender 上游本体的 fork。

产品设计、API 契约、实施计划及部署事实源统一维护在 [verdantflare-design](https://github.com/verdantflarehub/verdantflare-design)。

## 分支

| 分支 | 用途 |
| --- | --- |
| `dev` | 日常开发、迁移与集成；本地工作分支。 |
| `release` | 从 `dev` 快进，承载后续发布构建。 |
| `main` | 默认展示分支，保存验证后的稳定版本。 |

初始化阶段三个分支指向同一个骨架提交，不表示应用已经发布。当前未配置 CI/CD；后续发布流程遵循设计仓库的 [GitHub Actions 应用指南](https://github.com/verdantflarehub/verdantflare-design/blob/dev/docs/GithubAction.md)。

## 开发约定

- MCP 对外统一经 Studio 接入，内部服务不新增独立公网 MCP 入口。
- 凭据仅从环境或受控配置读取，不提交到源码仓库。
- 本地工作目录、Blender 工程、音视频与模型权重不提交到 Git。
- 开发和交付遵循设计仓库的 [工作区规范](https://github.com/verdantflarehub/verdantflare-design/blob/dev/AGENTS.md)。
