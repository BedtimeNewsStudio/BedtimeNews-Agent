# 睡前消息智能体

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

睡前消息知识库的智能 RAG（检索增强生成）系统。提供自动路由、语义搜索、
检索文稿上下文与节目引用功能。

> **立即体验：** [chat.bedtime.blog](https://chat.bedtime.blog)

## 概述

本系统对[睡前消息档案库](https://archive.bedtime.news/)的视频文稿进行索引，并通过LLM驱动的问答实现语义搜索。基于LangGraph、可插拔的 LLM/embedding 提供方（默认使用 DeepSeek 对话模型与 SiliconFlow 的 Qwen3 embedding）以及 PostgreSQL + pgvector 构建。

**核心功能：**

- 自动查询路由（档案检索 vs 受限的直接处理）
- 查询优化与语义搜索
- 基于LLM的文档相关性评分
- 将检索文稿作为回答上下文，并提供 Markdown 引用与引用修复
- 自动化文档索引与增量更新
- 网页聊天界面

## 内容覆盖

本系统索引来自[bedtimenews-archive-contents](https://github.com/bedtimenews/bedtimenews-archive-contents)的视频文稿，涵盖多个节目的多元主题：

**节目目录：**

| 目录          | 节目名称   | 描述                     |
| ------------- | ---------- | ------------------------ |
| `main/`       | 睡前消息   | 全面覆盖所有主题         |
| `reference/`  | 参考信息   | 每日新闻聚合             |
| `business/`   | 产经破壁机 | 经济、产业、商业、技术   |
| `commercial/` | 讲点黑话   | 国际关系、地缘政治       |
| `opinion/`    | 高见       | 技术分析、基础设施、工程 |
| `daily/`      | 每日新闻   | 每日新闻更新             |
| `others/`     | 其它文稿   | 直播问答及其它相关内容   |

**主题分类：**

1. **国内经济与产业** - 经济政策、产业发展、房地产、地方政府债务、城市发展
2. **科技创新** - 人工智能、芯片、半导体、自动驾驶、航天、工程技术
3. **跨境电商与出海** - SHEIN、TikTok、中国制造优势、全球市场
4. **企业治理与监管** - 企业丑闻、审计、金融监管、食品安全、税收监管
5. **国际关系与地缘政治** - 中美关系、俄乌冲突、中东局势、朝鲜半岛、印太地区
6. **社会民生** - 教育、医疗、人口问题、社会福利、城市治理
7. **加密货币与金融科技** - 比特币、区块链、去中心化金融、数字资产
8. **人口与社会政策** - 人口危机、社会化抚养、教育体系、社会福利改革
9. **基础设施与工程** - 铁路建设、能源基础设施、城市发展、公用事业
10. **法律与司法事务** - 企业纠纷、刑事司法、消费者权益保护、监管框架

## 架构

![睡前消息系统架构](docs/diagrams/system-architecture.svg)

**组件说明：**

- **[Frontend](frontend/README.md)**：自定义聊天 UI（静态 HTML/CSS/JS，由轻量 FastAPI 服务托管）
- **[Agent](agent/README.md)**：基于 LangGraph 的智能 RAG 服务
- **[Indexer](indexer/README.md)**：自动化文档 embedding 流水线
- **Database**：PostgreSQL + pgvector 扩展的向量数据库

整个服务栈仅提供纯 HTTP（8080 端口），不做 TLS。公网暴露与 TLS 终止由本仓库之外的工作处理。

## 快速开始

### 前置要求

- Docker
- 所选提供方的 API 密钥（默认：对话用 `DEEPSEEK_API_KEY`，embedding 用 `SILICONFLOW_API_KEY`）

### 安装步骤

1. **克隆仓库**

   ```bash
   git clone https://github.com/BedtimeNewsStudio/BedtimeNews-Agent.git
   cd BedtimeNews-Agent
   ```

2. **配置环境变量**

   复制[`.env.example`](.env.example)到`.env`并配置：

   ```bash
   cp .env.example .env
   # 编辑 .env
   ```

   > **API 密钥从 shell 环境变量读取，而非 `.env` 文件。** `.env` 仅保存非敏感配置
   > （提供方/模型选择、端口、数据库设置）；请在 shell 中导出密钥，例如：
   >
   > ```bash
   > export DEEPSEEK_API_KEY=...      # 对话提供方
   > export SILICONFLOW_API_KEY=...   # embedding 提供方
   > ```

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
IMAGE_TAG=0.1.0   # 写在 .env 中，或保持 latest
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
│   ├── server.py       # FastAPI：托管静态界面 + 代理 /chat SSE
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
├── .env                # 环境配置（不在 git 中）
├── .env.example        # 环境配置模板
├── THIRD_PARTY_NOTICES.md  # 第三方组件许可证
├── README.md           # 默认自述文件（中文，本文件）
├── README.en.md        # 英文版自述文件
└── README.es-ES.md     # 西班牙语版自述文件
```

## 许可证

MIT License — 详见 [LICENSE](LICENSE) 文件。

本项目内置的第三方组件保留其各自许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
