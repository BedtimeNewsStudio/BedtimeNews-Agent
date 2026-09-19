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
- `retriever.py`：语义搜索（embedding + pgvector）
- `cache.py`：查询结果的 LRU 缓存
- `chat.py`：FastAPI 端点处理器
- `main.py`：FastAPI 服务器
- `vector_db.py`：PostgreSQL + pgvector 数据库操作

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

对话与 embedding 通过 `LLM_PROVIDER` 与 `EMBEDDING_PROVIDER` 独立配置。
模型名从带提供方前缀的环境变量读取，因此键名取决于所选的提供方。使用
默认值（`LLM_PROVIDER=deepseek`、`EMBEDDING_PROVIDER=siliconflow`）时，
在 `.env` 中配置：

```bash
# 快速模型（路由、查询改写、评分）
DEEPSEEK_FAST_MODEL=deepseek-v4-flash

# 生成模型（最终回答）
DEEPSEEK_GENERATION_MODEL=deepseek-v4-flash

# Embedding 模型
SILICONFLOW_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-4B
```

**说明：**

- 改用 OpenAI 时，设置 `LLM_PROVIDER=openai` /
  `EMBEDDING_PROVIDER=openai`，并提供 `OPENAI_FAST_MODEL`、
  `OPENAI_GENERATION_MODEL`、`OPENAI_EMBEDDING_MODEL`（以及
  `OPENAI_API_KEY`）。
- **Embedding 维度必须与数据库列匹配。** `embedding halfvec(N)` 列的 N
  取自 `EMBEDDING_DIM`（`.env`，默认 `2560`，对应
  `Qwen/Qwen3-Embedding-4B`）。切换到维度不同的模型需要变更 schema 并
  完整重新 embedding——参见 `indexer/README.md` 中的“更换 Embedding
  模型”操作手册。

**检索设置**：

- `match_count`：默认 30（`RETRIEVAL_MATCH_COUNT`），增大可提高召回
- `match_threshold`：默认 0.4（`MATCH_THRESHOLD`），增大可提高精确率
  （但结果更少）
- `top_k`：默认 15（`RETRIEVAL_TOP_K`），送入评分的最大去重 chunk 数
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
├── retriever.py       # 带缓存的语义搜索
├── cache.py           # LRU 缓存实现
├── vector_db.py       # 数据库操作
├── models.py          # Pydantic 模型
├── settings.py        # 配置
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

标题优先取自检索时 LEFT JOIN 出来的 `rag.documents.title`（由 indexer 从上游
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
