# 睡前消息知识库

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

睡前消息知识库网站：既能向智能体提问，也能在站内浏览与阅读全部节目文稿。
问答由智能 RAG（检索增强生成）系统提供——自动路由、语义搜索、检索文稿
上下文与节目引用。

> **立即体验：** [bedtime.blog](https://bedtime.blog)

## 概述

文稿原文与智能系统分属两个仓库：文稿原文在
[睡前消息文稿库（BedtimeNews-Transcripts）](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts)中维护；本仓库（BedtimeNews-Agent）索引这些文稿，并运行同时提供聊天问答与站内文稿阅读的网站——索引到的文稿直接作为站内内容供浏览，回答中的引用也跳转到应用内阅读器。基于 LangGraph、任意 OpenAI-compatible 的对话/嵌入端点（默认模板使用 DeepSeek 对话模型与 SiliconFlow 的 Qwen3 embedding）以及 PostgreSQL + pgvector 构建。

**核心功能：**

- 自动查询路由（档案检索 vs 受限的直接处理）
- 查询优化与语义搜索
- 基于LLM的文档相关性评分
- 将检索文稿作为回答上下文，并提供 Markdown 引用与引用修复
- 自动化文档索引与增量更新
- 网页界面：聊天提问 + 站内浏览/阅读文稿（引用直接跳转到应用内阅读器）

## 架构

![睡前消息系统架构](docs/diagrams/system-architecture.svg)

**组件说明：**

- **[Frontend](frontend/README.md)**：自定义聊天与文稿阅读 UI（静态 HTML/CSS/JS，
  由轻量 FastAPI 服务托管；站内托管文稿列表与正文阅读器，文稿内容来自索引数据库）
- **[Agent](agent/README.md)**：基于 LangGraph 的智能 RAG 服务
- **[Indexer](indexer/README.md)**：自动化文档 embedding 流水线
- **Database**：PostgreSQL + pgvector 扩展的向量数据库

索引范围：每篇文稿仅 `## 正文` 进入检索；`## 附录`（事实订正、核对记录）与
`**发布日期**` 元数据行不索引。

整个服务栈仅提供纯 HTTP（8080 端口），不做 TLS。公网暴露与 TLS 终止由本仓库之外的工作处理。

## 快速开始

### 前置要求

- Docker
- 生成与嵌入端点的 API 密钥——填入 `config.yml`（任意 OpenAI-compatible 供应商；
  模板默认以 DeepSeek 对话、SiliconFlow Qwen3 embedding 为例）

### 安装步骤

1. **克隆仓库**

   ```bash
   git clone https://github.com/BedtimeNewsStudio/BedtimeNews-Agent.git
   cd BedtimeNews-Agent
   ```

2. **配置 `config.yml`**

   复制[`config.example.yml`](config.example.yml)为 `config.yml` 并填写（已被
   `.gitignore`，不会入库）：

   ```bash
   cp config.example.yml config.yml
   # 编辑 config.yml —— 填入 generation / embedding 两组端点：
   # api_key + base_url + model（任意 OpenAI-compatible 供应商，如
   # DeepSeek、SiliconFlow、OpenAI 或自建网关）
   ```

   同时复制 `.env.example` 为 `.env`（部署布线：端口、镜像 tag、postgres 凭据、
   `EMBEDDING_DIM` 等，仅这些内容仍走环境变量）：

   ```bash
   cp .env.example .env
   ```

   > **优先级：进程环境变量 > `config.yml`**（嵌套键用双下划线，如
   > `GENERATION__API_KEY`）。密钥等应用配置以 `config.yml` 为准，不再需要导出密钥。

3. **启动服务**

   ```bash
   docker compose up -d
   ```

4. **访问界面**

   打开 `http://localhost:8080`（纯 HTTP；可在 `.env` 中通过 `FRONTEND_PORT` 修改宿主机端口）。

   默认运行已发布的镜像。如果你修改了代码，请加 `--build`——参见
   [已发布镜像与本地代码](#已发布镜像与本地代码)。

### 验证安装

```bash
# 检查服务状态
docker compose ps

# 查看日志
docker compose logs -f
```

### 测试与覆盖率

根目录的测试命令会在相互隔离的进程中运行 agent、indexer 与 frontend：

```bash
uv run pytest
uv run pytest --cov
```

选项会转发给每个组件。如需只运行单个组件，请进入对应目录执行：

```bash
cd agent  # 或 indexer / frontend
uv run pytest --cov
```

## 发布版本

推送版本标签后，[`release.yml`](.github/workflows/release.yml) 会构建多架构
（amd64 + arm64）镜像并发布到 GHCR：

- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-agent`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-indexer`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-frontend`

要部署已发布的版本，先在 `.env` 中用 `IMAGE_TAG` 固定版本（默认 `latest`），
再拉取镜像：

```bash
# .env 中： IMAGE_TAG=0.1.0
docker compose pull
docker compose up -d
```

### 已发布镜像与本地代码

`docker compose up` **不会自动构建**，即使在有本地改动的源码目录中也是如此。
真正决定运行内容的是 `image:`：

| 情况                        | `docker compose up` 的行为   |
| --------------------------- | ---------------------------- |
| 本地已有该标签的镜像        | 直接复用——不拉取、不构建     |
| 本地没有该标签的镜像        | **从 GHCR 拉取**已发布的镜像 |
| `docker compose up --build` | 从本地源码构建               |

因此改完代码后必须显式重新构建，否则运行的仍是旧镜像：

```bash
docker compose up -d --build agent web
```

注意本地构建的镜像与已发布的版本共用同一个标签，后创建的会覆盖先前的：
`docker compose pull` 会覆盖本地构建，`--build` 会覆盖拉取到的发布版本。

发布新版本：推送 `v*` 标签即可（镜像标签会去掉前缀 `v`）：

```bash
git tag v0.1.0 && git push origin v0.1.0
```

> 发布说明应重点写明运维相关变更：新增/更名的环境变量、数据库 schema 变更
> （如 `EMBEDDING_DIM`，参见 [indexer/README.md](indexer/README.md) 中的
> 操作手册）、以及是否需要重新索引。`storage/postgres/init.sh` 只在全新数据卷
> 上执行，schema 变更不会自动应用到已有部署。

### 正文哈希 schema 升级

Indexer 现在以实际送入分块与 embedding 的规范化 `## 正文` SHA256 判断向量是否失效，同时保留完整源文件哈希。已有数据卷必须在运行新版 Indexer 前按顺序执行 `storage/postgres/migrations/` 中的迁移（`001_body_hashes.sql`，然后是创建阅读器 `rag.transcripts` 表的 `002_transcript_projection.sql`）；生产操作步骤见 [indexer/README.md](indexer/README.md)。`docker-compose.sample.yml` 提供固定小样本，本地运行时必须设置隔离的 `POSTGRES_DATA_DIR` / `INDEXER_DATA_DIR`。

## 服务专属文档

- **[Frontend](frontend/README.md)**：UI定制
- **[Agent](agent/README.md)**：API端点、Agentic RAG实现
- **[Indexer](indexer/README.md)**：文档处理

## 数据持久化

数据在重启后持久保存：

- **PostgreSQL 数据**（chunks 与 embedding）：绑定挂载到 `./storage/postgres/volume`
- **服务日志**：Docker 命名卷 `bedtimenews_indexer_logs` 与 `bedtimenews_agent_logs`

## 项目结构

```plaintext
BedtimeNews-Agent/
├── agent/              # LangGraph 智能RAG服务
│   ├── src/
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── frontend/           # 自定义 Web UI（静态 + FastAPI）
│   ├── server.py       # FastAPI：托管静态界面 + 代理 /chat SSE 与文稿 API
│   ├── starters.py     # 示例提问数据
│   ├── static/         # index.html、styles.css、app.js、logo
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── indexer/            # 文稿 embedding 流水线
│   ├── src/
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── docs/diagrams/      # SVG 架构图与工作流图
├── storage/            # 数据库初始化脚本
│   └── postgres/
├── docker-compose.yml  # 服务编排
├── config.yml          # 应用与密钥配置（不在 git 中，由 example 复制）
├── config.example.yml  # 应用配置模板
├── .env                # 部署布线配置（不在 git 中，由 .env.example 复制）
├── .env.example        # 部署布线模板
├── THIRD_PARTY_NOTICES.md  # 第三方组件许可证
├── README.md           # 默认自述文件（中文，本文件）
├── README.en.md        # 英文版自述文件
└── README.es-ES.md     # 西班牙语版自述文件
```

## 许可证

MIT License — 详见 [LICENSE](LICENSE) 文件。

本项目内置的第三方组件保留其各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
