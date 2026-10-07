# Agent 服务

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

智能 RAG 服务，实现路由、查询优化、语义检索、基于检索文档的回答生成与节目引用。

安装与配置说明参见[主 README](../README.md)。

## 架构

### Agentic RAG 工作流

![Agentic RAG 工作流](../docs/diagrams/agent-workflow.svg)

**组件：**

- `agent.py`：对外 API（`agent_query()`、`agent_stream_query()`）
- `graph.py`：带智能路由的 LangGraph 工作流
- `retriever.py`：针对单个快照的语义搜索（embedding + pgvector）；结果缓存的键包含快照 ID，
  切换快照后不会返回旧结果
- `cache.py`：查询结果的 LRU 缓存
- `chat.py`：FastAPI 端点处理器
- `main.py`：FastAPI 服务器（`/chat`、`/transcripts`、`/health`）
- `snapshots.py`：选择读取哪个 RAG 快照：选择规则、每 15 秒轮询注册表、就绪状态
- `vector_db.py`：只读的 PostgreSQL + pgvector 查询；每条查询都显式带上快照 schema（不使用 `search_path`）

### RAG 快照

Indexer 把知识库发布为不可变的快照（`rag_s<id>` schema），登记在
`rag_meta.snapshots` 中；参见 [Indexer README](../indexer/README.md#数据库结构)。Agent：

- **选择**已发布快照中 `format_version` 受支持（`SUPPORTED_FORMATS`，目前为 `{1}`）、
  且向量空间（`<embedding.model>@<EMBEDDING_DIM>`，或 `embedding.space_id`）与自身一致的快照——
  先取最高格式，再取最新发布的。设置 `RAG_SNAPSHOT` 时直接固定使用该快照 ID。
  在 Indexer 创建 `rag_meta` 之前，原有的 `rag` schema 被当作隐式快照 `legacy` 读取；
- 把当前快照保存在**进程内存**中，后台线程每 15 秒重新应用选择规则，因此新发布或回滚的快照
  无需重启即可生效。处理请求时从不查询 `rag_meta`；
- **每个请求固定在开始时的快照上**：一次 `/chat` 回答中的每次检索、每个阅读器响应都只读一个快照，
  即使期间发布了新快照；
- 数据库或可读快照不可用时不会退出：此时处于未就绪状态（`/health` 返回 `503`），`/chat` 和
  `/transcripts` 返回 `503`，轮询线程持续重试；
- 设置了 `POSTGRES_AGENT_PASSWORD` 时以只读角色 `rag_agent` 连接（即使遭受 prompt 注入也无法修改任何数据）；
  否则回退到数据库超级用户并记录警告。

### 路由行为

路由器将用户输入分为三类，分别进入两条执行路径：

**直接路径 - 问候语**（不检索）：

- 简单问候：“hi”、“hello”、“你好”
- 元问题：“你是谁”、“你能做什么”

**直接路径 - 超出范围**（不检索）：

- 通识问题：“1+1等于几”、“法国首都是哪里”
- 无关话题：“今天天气怎么样”、“怎么煮面”
- 实时数据：天气、股价、时事
- 对这些问题，助手不依据模型知识作答，而是简要说明它们超出文稿档案
  范围，并引导回档案相关话题

**RAG 路径**（检索增强）：

- 睡前消息相关问题（默认）
- 中国国内事务、政策、经济
- 国际关系、地缘政治、冲突
- 科技、科学、AI、基础设施
- 社会问题、教育、医疗、人口

路由之前，压缩（condense）步骤会依据客户端提供的最多八轮历史对话消解
追问中的指代。RAG 查询若未检索到相关文档，会改写查询并重试一次，之后
生成器才返回无结果响应。

### 事实约束（Grounding）边界

- 生成 prompt 指示模型用当前一轮检索到的文档支撑事实性陈述。更早的
  对话只是参考上下文，不作为证据。
- 无检索路径指示模型拒答档案之外的事实性问题。
- 引用后处理会修复已知的节目链接；若模型未输出任何引用，则追加来源
  列表。
- 这是基于 prompt 与引用的事实约束，不是逐条声明的验证。本服务不检验
  每条生成陈述是否被其引用所蕴含。API 的 `grounded` 标志只表示已向
  生成提供相关文档，并非蕴含得分。

## API 参考

### POST /chat

**请求：**

```json
{
  "question": "独山县的债务问题有多严重？",
  "history": [],
  "stream": false
}
```

**参数：**

- `question`（必填）：用户查询
- `history`（可选，默认 `[]`）：最多八轮先前的
  `{question, answer, grounded}` 对话
- `stream`（可选，默认 false）：启用 SSE 流式输出

**响应（非流式）：**

```json
{
  "answer": "根据[[睡前消息588]](/transcripts/ShuiQianXiaoXi/0501-0600/0588.md)...",
  "followups": ["独山县后来如何化解债务？"],
  "grounded": true
}
```

**响应（流式）：**

```plaintext
data: {"type": "step", "step": "route", "content": "..."}
data: {"type": "citations", "urls": {"ShuiQianXiaoXi/0501-0600/0588.md": {"title": "睡前消息588", "url": "/transcripts/ShuiQianXiaoXi/0501-0600/0588.md"}}}
data: {"type": "answer_chunk", "content": "根据"}
data: {"type": "answer_chunk", "content": "睡前"}
data: {"type": "answer_meta", "grounded": true}
data: {"type": "followups", "items": ["独山县后来如何化解债务？"]}
...
data: [DONE]
```

后处理修改了已流式输出的回答时，流中还会出现 `answer_final`；失败时出现
`error`；流水线静默阶段会发送 `: ping` 心跳注释。

### GET /transcripts

阅读器导航索引，来自当前快照的 `transcripts` 表：
`{"items": [{doc_id, canonical_title, source_title, channel, publication_date, source_hash, updated_at}, ...]}`，
按栏目排序，栏目内按 `publication_date` 从新到旧。响应带 `ETag`
（`Cache-Control: no-cache`），对 `If-None-Match` 返回 `304`；数据库或可读快照不可用时返回 `503`。

### GET /transcripts/{doc_id}

按精确 URI 返回一篇渲染后的文稿（如
`/transcripts/ShuiQianXiaoXi/0501-0600/0588.md`）：字段同上，另加 `body_html`。
只接受规范的相对 `.md` URI，其它输入或未知 URI 返回 `404`。`ETag`/`304`/`503` 行为相同。

### GET /health

就绪探针。数据库可达且已选中可读快照时返回 `200`；否则返回 `503` 并给出 `reason`
（数据库不可达、本 Agent 的向量空间中没有已发布快照、固定的快照已退役或不存在等）。
结果来自快照轮询线程的上一次结果（最多 15 秒前），不查询数据库，也不调用模型。

```json
{
  "ready": true,
  "snapshot": {
    "id": "s20261007t091512z_a1b2c3d",
    "schema": "rag_s20261007t091512z_a1b2c3d",
    "format_version": 1,
    "embedding_space": "Qwen/Qwen3-Embedding-4B@2560",
    "published_at": "2026-10-07T09:15:40Z",
    "data_age_seconds": 3600,
    "source_commit": "a1b2c3d..."
  },
  "embedding_space": "Qwen/Qwen3-Embedding-4B@2560",
  "supported_formats": [1],
  "pinned_by_config": null,
  "checked_at": "2026-10-07T10:15:38Z",
  "indexer_status": {
    "last_run_at": "...",
    "last_result": "no_change",
    "last_error": null,
    "last_published_at": "...",
    "consecutive_failures": 0
  }
}
```

`indexer_status` 不影响就绪状态（旧快照仍可服务），可用于告警，例如连续 3 次失败或超过
3 小时没有运行。compose 文件以 `/health` 作为 Agent 容器的健康检查。

## 评估

这些是手动评估工具（会访问真实的数据库/LLM），不是自动化单元测试。
单元测试见本组件的 `tests/` 目录（运行方式：`cd agent && uv run pytest`）。

### 评估 Agent（完整 Agentic RAG 流程）

```bash
# 测试单个自定义查询
docker compose exec agent python -m src.eval_agent -q "独山县的债务问题"
docker compose exec agent python -m src.eval_agent --query "王文银的创业故事有哪些可疑之处"

# 列出查询类别
docker compose exec agent python -m src.eval_agent --list-categories

# 测试指定类别
docker compose exec agent python -m src.eval_agent --category education

# 随机抽样
docker compose exec agent python -m src.eval_agent --random 10

# 只取前 N 条查询
docker compose exec agent python -m src.eval_agent --limit 3
```

### 评估检索器（仅检索）

```bash
# 对固定的 20 条已标注查询集打分。会访问真实的 embedding API 与数据库，
# 然后把 recall@k 与每条查询的排名追加到宿主机上的历史文件
# agent/eval_results/retriever.json（该文件纳入版本管理）。
docker compose run --rm --build \
  --volume ./agent/eval_results:/app/eval_results \
  agent python -m src.eval_retriever --labelled

# 可选：为这次运行在历史中标注一个标识。
docker compose run --rm --build \
  --volume ./agent/eval_results:/app/eval_results \
  agent python -m src.eval_retriever --labelled --run-label grader-change

# 测试单个自定义查询
docker compose exec agent python -m src.eval_retriever -q "独山县"
docker compose exec agent python -m src.eval_retriever --query "你的问题"

# 用自定义参数测试检索
docker compose exec agent python -m src.eval_retriever \
  --category education \
  --match-count 10 \
  --threshold 0.3

# 随机抽样
docker compose exec agent python -m src.eval_retriever --random 20
```

## 配置

### 模型选择

对话与 embedding 各自是一组「OpenAI-compatible 端点 + 模型 + 密钥」，在
`config.yml` 的 `generation` / `embedding` 下独立配置（供应商不限——
DeepSeek、SiliconFlow、OpenAI 或自建网关只是三行取值不同）：

```yaml
generation:
  api_key: "..."
  base_url: "https://api.deepseek.com"
  model: "deepseek-v4-flash"        # 生成模型（最终回答）
  fast_model: "deepseek-v4-flash"   # 快速模型（路由、查询改写、评分）

embedding:
  api_key: "..."
  base_url: "https://api.siliconflow.com/v1"
  model: "Qwen/Qwen3-Embedding-4B"
```

**说明：**

- 改用其它供应商（OpenAI、自建网关等）只改 `base_url` / `model` /
  `api_key` 三行的取值，代码不需要任何变更。
- **向量空间必须与快照一致。** 查询向量必须来自构建快照时使用的模型，因此 Agent 只读取
  自身向量空间中的快照：`<embedding.model>@<EMBEDDING_DIM>`（`.env`，默认 `2560`，对应
  `Qwen/Qwen3-Embedding-4B`），设置了 `embedding.space_id` 时以其为准。换模型时需由
  Indexer 先构建新快照——参见 `indexer/README.md` 中的“更换 Embedding 模型”操作手册。

**数据库与快照设置**（环境变量，由 `docker-compose.yml` 从 `.env` 传入）：

- `POSTGRES_AGENT_PASSWORD`：以只读角色 `rag_agent` 连接（推荐）。未设置时回退到
  `POSTGRES_USER` 并记录警告
- `EMBEDDING_DIM`：查询向量的维度（向量空间的一部分）
- `RAG_SNAPSHOT`：可选，固定使用的快照 ID（排障、长期固定）；此时 Agent 不跟随新快照，
  该快照被退役或删除时进入未就绪状态

**检索设置**：

- `match_count`：默认 30（`config.yml` 的 `retrieval_match_count`），增大可提高召回
- `match_threshold`：默认 0.4（`config.yml` 的 `match_threshold`），增大可提高精确率
  （但结果更少）
- `top_k`：默认 15（`config.yml` 的 `retrieval_top_k`），送入评分的最大去重 chunk 数
- 查询改写重试目前在 `create_initial_state()` 中固定为一次，不通过
  环境变量配置

## 开发

### 项目结构

```plaintext
agent/src/
├── main.py            # FastAPI 服务器
├── chat.py            # 端点处理器
├── agent.py           # Agentic RAG API
├── graph.py           # LangGraph 工作流
├── retriever.py       # 按快照缓存的语义搜索
├── cache.py           # LRU 缓存实现
├── snapshots.py       # 快照选择、轮询、就绪状态
├── vector_db.py       # 只读、带快照 schema 的数据库查询
├── models.py          # Pydantic 模型
├── settings.py        # 配置
├── uri_mapping.py     # 由 URI 推导的后备引用标题
├── eval_agent.py      # 手动流水线评估工具
├── eval_retriever.py  # 手动检索评估工具
└── eval_queries.py    # 评估查询类别与示例
```

### 网络访问

Agent 服务**仅运行在 Docker 内部网络**（不对宿主机暴露）：

```bash
# 从宿主机访问（经由 docker exec）
docker compose exec agent curl http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "test"}'

# 从其它容器访问（经由服务名）
curl http://agent:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "test"}'
```

Web 前端是唯一发布到宿主机的服务——纯 HTTP，端口 8080，无 TLS（公网
暴露与 TLS 终止由本仓库之外处理）。前端通过内部 Docker 网络将 `/chat`
代理给 agent；agent 本身从不暴露给宿主机。

### 调试

```bash
# 查看日志
docker compose logs -f agent

# 进入容器
docker compose exec agent sh

# 就绪状态与正在服务的快照
docker compose exec agent curl -s http://localhost:8000/health

# 测试数据库连接（辅助工具位于 indexer 服务）
docker compose exec indexer python -m src.debugger test

# 测试单条查询
docker compose exec agent python -m src.eval_agent --limit 1
```

## 引用标题的来源

模型在原始输出里写的是文稿 **URI**（如
`[[ShuiQianXiaoXi/0501-0600/0588.md]]`），不写链接也不写中文台名——URI 是
上下文里已有的字符串，模型无需臆造期号或 URL。生成结束后由
`_repair_citations` 统一改写成 `[[标准化标题]](链接)`。

标题优先取自检索时 LEFT JOIN 出来的快照 `documents.title`（由 indexer 从上游
`URI映射.md` 写入）。该行缺失时退回通则推导：

| 栏目目录           | 标准化标题前缀 |
| ------------------ | -------------- |
| `ShuiQianXiaoXi/`  | 睡前消息       |
| `CanKaoXinXi/`     | 参考信息       |
| `GaoJian/`         | 高见           |
| `JiangDianHeiHua/` | 讲点黑话       |
| `ChanJingPoBiJi/`  | 产经破壁机     |

通则推不出来的（`misc/` 特辑等 29 篇例外）最终退回显示 URI 本身——标签不好看，
但链接依然正确可点。

引用链接指向应用内阅读器：
`/transcripts/<URI>`（应用内阅读；源文件见 GitHub `blob/main/contents/<URI>`）
