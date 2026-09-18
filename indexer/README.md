# Indexer 服务

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

睡前消息档案库的自动化文档 embedding 流水线。克隆文稿仓库、处理
markdown 文件、生成 embedding，并存入 PostgreSQL + pgvector。

安装与配置说明参见[主 README](../README.md)。

## 功能

- **自动同步**：从 [BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts) 克隆/更新
- **正文感知增量处理**：分别记录完整 Markdown 与实际送入 embedding 的规范化 `## 正文` 的 SHA256；仅标题、日期或附录变化时不会调用 embedding
- **定时执行**：进程内调度器，支持可配置的 cron 表达式（默认：每小时）
- **只索引正文**：每篇文稿只抽取 `## 正文` 到 `## 附录` 之间的内容，
  丢弃标题行、`**发布日期**` 元数据与附录的订正/核对记录
- **URI 作为 doc_id**：文稿相对 `contents/` 的路径（含 `.md`）即其标识
- **标准化标题**：解析上游 `URI映射.md`，把 URI → 标题写入 `rag.documents`
- **智能分块**：感知 markdown 的语义分块
- **批量 embedding**：高效的 embedding API 批量调用
- **可监控**：内置调试工具与统计信息

## 流水线阶段

![Indexer 流水线](../docs/diagrams/indexer-pipeline.svg)

新增和正文有变化的文件会先完成分块与 embedding，再用单个事务替换 chunk 和历史，因此 embedding 提供方失败时旧版本仍可检索。仅源文件其它部分变化时只更新历史，不触碰向量；删除也在单个事务内移除 chunk、标题和历史。

## 配置

### Cron 调度

在 `.env` 中设置：

```bash
# 每小时（默认）
INDEXER_CRON_SCHEDULE="0 * * * *"

# 每 30 分钟
INDEXER_CRON_SCHEDULE="*/30 * * * *"

# 每天凌晨 2 点
INDEXER_CRON_SCHEDULE="0 2 * * *"
```

### 本地小样本模式

`index_config.sample.yml` 固定选择七篇文稿，覆盖普通期号、小数期号、`misc` 与多个栏目。仅可在 `INDEXER_SCOPE=sample`、`INDEX_CONFIG_FILE=/app/index_config.sample.yml`、隔离的数据目录以及以 `_local` 结尾的 `POSTGRES_DB` 下运行；否则 Indexer 会拒绝启动。`docker-compose.sample.yml` 提供服务覆盖配置。

### 文档过滤规则

编辑 `index_config.yml`：

```yaml
# 包含规则（先处理）
# 匹配对象是文稿的 URI —— 相对 contents/ 的路径，含 .md 后缀。
include:
  # 睡前消息
  - "ShuiQianXiaoXi/*/*.md"

  # 参考信息
  - "CanKaoXinXi/*/*.md"

  # 高见
  - "GaoJian/*/*.md"

  # 讲点黑话
  - "JiangDianHeiHua/*/*.md"

  # 产经破壁机
  - "ChanJingPoBiJi/*/*.md"

# 排除规则（在包含之后处理）
exclude:
  # 各栏目的导航页，不是文稿
  - "*/INDEX.md"

# 文件校验规则
validation:
  # 最小文件大小（字节）（跳过空文件或极小文件）
  min_file_size: 100

  # 最大文件大小（字节）（跳过超大文件）
  max_file_size: 10485760 # 10 MB
```

## 调试工具

### 测试连接

```bash
docker compose exec indexer python -m src.debugger test
```

### 查看统计

```bash
# 数据库统计
docker compose exec indexer python -m src.debugger stats

# 最近的文件操作
docker compose exec indexer python -m src.debugger recent --limit 20

# 所有文件的索引历史
docker compose exec indexer python -m src.debugger history

# 指定文件的历史
docker compose exec indexer python -m src.debugger history ShuiQianXiaoXi/0901-1000/0960.md
```

### 检查文档

```bash
# 查看某个文档的 chunk
docker compose exec indexer python -m src.debugger inspect ShuiQianXiaoXi/0901-1000/0960.md
```

### 查看日志

```bash
# 最近一次定时运行的日志
docker compose exec indexer python -m src.debugger logs

# 最后 100 行
docker compose exec indexer python -m src.debugger logs --lines 100

# 全部日志
docker compose exec indexer python -m src.debugger logs --all
```

### 手动执行

```bash
# 手动运行流水线（一次性）
docker compose exec indexer python -m src.pipeline
```

### 清除数据

```bash
# 危险：清除全部已索引数据
docker compose exec indexer python -m src.debugger clear
```

## 数据库 Schema

Indexer 管理 `rag` schema 中的四张表：

**`rag.document_chunks`**：存储 chunk 与 embedding

- `chunk_id`：唯一标识，形如 `ShuiQianXiaoXi_0501-0600_0588_chunk_000`
  （URI 去掉 `.md`、`/` 换成 `_`，再加 chunk 序号）
- `doc_id`：文稿 URI，**含 `.md`**，如 `ShuiQianXiaoXi/0501-0600/0588.md`；
  与上游 `URI映射.md` 的键逐字一致
- `chunk_index`：文档内从 0 开始的序号
- `heading`：小节标题（如有）
- `text`：chunk 内容
- `word_count`：词数
- `embedding`：`halfvec(N)` 向量——N 取自 `EMBEDDING_DIM`（`.env`），
  由 `storage/postgres/init.sh` 在首次初始化数据库时应用，且**必须等于
  embedding 模型的输出维度**（默认 `2560`，对应
  `Qwen/Qwen3-Embedding-4B`）。参见[更换 Embedding 模型](#更换-embedding-模型)。
  列**类型**刻意固定为 `halfvec`（不可配置）：4000 维以内的模型都适用
  ——包括维度更低的 OpenAI 模型——存储减半且召回损失可忽略；
  `{agent,indexer}/src/vector_db.py` 中的插入/查询转换也使用
  `::halfvec`。只有在需要完整 float32 精度、超过 4000 维或二进制/稀疏
  embedding 时才应更换类型（还需要同时修改索引 opclass 与那些转换）。
- `created_at`：时间戳

**`rag.documents`**：URI → 标准化标题

- `doc_id`：文稿 URI（主键，含 `.md`）
- `title`：标准化标题，如 `睡前消息588`
- `updated_at`：时间戳

标题取自上游 `URI映射.md`。通则（`{栏目}/{百期文件夹}/{期号}.md` →
`{栏目中文}{去零期号}`）覆盖绝大多数文稿，但 `misc/` 特辑、官方重号期、
产经破壁机负数期等 29 篇例外无法由规则推导，因此以该文件为准。Agent 检索时
LEFT JOIN 这张表，把引用渲染成标准化标题而不是原始 URI。每次流水线运行都会
刷新全部标题——上游可能只改标题而不动文稿。

**`rag.indexing_history`**：记录当前索引真正代表的内容

- `file_path`：文稿 URI
- `source_hash`：完整原始 Markdown 的 SHA256
- `body_hash`：当前 chunk/向量所代表的规范化正文 SHA256
- `body_normalization_version`：正文规范化逻辑版本，版本变化会强制重建
- `indexed_at`：正文最后成功索引时间
- `source_observed_at`：最后接受源文件变化的时间

**`rag.file_actions`**：审计日志

- `action_type`：`ADD`、`MODIFY`、`SOURCE_ONLY` 或 `DELETE`
- `source_hash` / `body_hash`：两个明确的哈希（删除时为 `NULL`）
- `run_timestamp` / `processed_at`：记录与完成时间

已有 v0.2 数据卷必须在新 Indexer 启动前执行 `storage/postgres/migrations/001_body_hashes.sql`。`body_hash` 为空的旧记录会保守地重新索引一次。若要保证完全一致，应先备份 PostgreSQL，执行迁移后清空四张 RAG 表并重新填充全部语料。新 schema 写入后不可再运行旧版 Indexer。

## 更换 Embedding 模型

在 `.env` 中更换 `EMBEDDING_PROVIDER` / `*_EMBEDDING_MODEL` **不是**
即插即用的替换。必须对整个语料库重新 embedding，原因：

- 不同模型的向量**不可比较**，即使维度相同——因此更换模型总是需要
  重新 embedding。
- 每个模型输出**固定维度**（如 `Qwen/Qwen3-Embedding-4B` = 2560、
  `text-embedding-3-small` = 1536、`text-embedding-3-large` = 3072、
  `text-embedding-004` = 768）。`embedding halfvec(N)` 列的 N 由
  `storage/postgres/init.sh` 依据 `EMBEDDING_DIM`（`.env`）确定。若新
  模型维度不同，**列类型本身必须变更**，否则插入会报
  `expected N dimensions, not M`。
- `init.sh` **只在 Postgres 初始化空数据卷时运行**，且使用
  `CREATE TABLE IF NOT EXISTS`。事后修改 `EMBEDDING_DIM` **不会**改动
  已有数据库。

### 操作手册

```bash
# 1. 停止服务
docker compose down

# 2. 编辑 .env：设置新的 EMBEDDING_PROVIDER / *_EMBEDDING_MODEL（及 API 密钥）。

# 3. 查出新模型的输出维度（提供方文档），记为 N。

# 4. 在 .env 中设置 EMBEDDING_DIM=N（init.sh 在全新初始化时据此确定列宽）。
```

然后把维度应用到数据库——二选一：

**方案 A——重建数据卷（最简单；清空数据库，使 `init.sh` 重新运行）：**

Postgres 数据放在**绑定挂载**（`./storage/postgres/volume`）中，因此
`docker compose down -v` 并**不会**清除它——必须自行移除该目录的内容。
建议先移到一旁（可回滚）而不是直接删除：

```bash
docker compose down
mv storage/postgres/volume storage/postgres/volume.bak   # 可回滚的还原点
docker compose up -d postgres   # init.sh 全新运行，按 EMBEDDING_DIM 确定列宽
# 数据库此时为空，变化检测会把所有文件视为新文件（第 6 步变为可选）。
# 重新 embedding 验证无误后，删除备份：rm -rf storage/postgres/volume.bak
```

**方案 B——就地修改现有表（保留其它表）：**

```bash
docker compose up -d postgres
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
  DROP INDEX IF EXISTS rag.idx_embedding_hnsw;
  TRUNCATE rag.document_chunks;
  ALTER TABLE rag.document_chunks ALTER COLUMN embedding TYPE halfvec(N);
  CREATE INDEX idx_embedding_hnsw ON rag.document_chunks
    USING hnsw (embedding halfvec_cosine_ops);
"
```

最后重新 embedding：

```bash
# 6. 重置变化检测状态，使 indexer 重新 embedding 全部内容。
#    方案 B 之后必需（indexing_history 仍保留旧哈希，否则 indexer 会
#    视为"无变化"而跳过）。方案 A 之后无害。
docker compose up -d indexer
docker compose exec indexer python -m src.debugger clear --force

# 7. 对全部语料重新 embedding
docker compose exec indexer python -m src.pipeline

# 8. 校验维度 + 行数
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "\d rag.document_chunks" | grep embedding
docker compose exec indexer python -m src.debugger stats
```

> `debugger clear` 会清空 `document_chunks`、`indexing_history` 与
> `file_actions`，并删除本地内容克隆（下次运行时重新克隆）。

## 数据备份与恢复

PostgreSQL 数据卷存放在代码库中一个被 gitignore 的目录里。可以用标准
tar 归档备份和恢复整个数据库。

### 备份 PostgreSQL 数据

**前置条件**：停止所有服务以保证数据一致性：

```bash
docker compose down
```

**（可选）查看数据卷大小（未压缩）**：

```bash
du -h -d 0 storage/postgres/volume
```

**创建备份**：

```bash
# 创建带时间戳的备份归档
tar czf /path/to/backup/postgres-volume-$(date +%F).tar.gz storage/postgres/volume/

# 示例输出：/path/to/backup/postgres-volume-2025-11-27.tar.gz
```

备份包含：

- 全部已索引的文档 chunk 与 embedding
- 索引历史与文件操作记录
- 数据库配置与元数据

### 恢复 PostgreSQL 数据

**前置条件**：停止所有服务：

```bash
docker compose down
```

**从备份恢复**：

```bash
# 移除现有数据（如有）
rm -rf storage/postgres/volume

# 将备份解压到 postgres 数据目录（在项目根目录执行）
tar xzf /path/to/backup/postgres-volume-2025-11-27.tar.gz -C .
# 确认文件解压为 ./storage/postgres/volume/18/docker/...

# 启动服务
docker compose up -d
```

**验证恢复**：

```bash
# 查看数据库统计
docker compose exec indexer python -m src.debugger stats

# 查看最近的文件操作
docker compose exec indexer python -m src.debugger recent --limit 10
```

### 说明

- **仅本地**：postgres 数据目录在 `.gitignore` 中，不受版本控制
- **迁移**：备份可移植，可在不同机器上恢复
- **磁盘占用**：每份备份通常在 100MB 到数 GB 之间，取决于已索引内容

## 项目结构

```plaintext
indexer/src/
├── entrypoint.py        # 主入口
├── pipeline.py          # 索引流水线编排
├── scheduler.py         # Cron 调度器
├── git_sync.py          # 仓库同步
├── file_scanner.py      # 文件系统扫描
├── document_loader.py   # Markdown 处理
├── change_detector.py   # 内容哈希比对
├── chunker.py           # 语义分块
├── embeddings.py        # Embedding 生成（提供方抽象）
├── vector_db.py         # 数据库操作
├── debugger.py          # 调试工具
├── stats.py             # 统计计算
├── models.py            # 数据模型
├── paths.py             # 路径管理
└── settings.py          # 配置
```

## 监控

### 检查服务状态

```bash
# 查看日志
docker compose logs -f indexer

# 检查非特权调度器进程
docker compose top indexer

# 确认数据库中有文档
docker compose exec indexer python -m src.debugger stats
```

### 预期输出

首次运行后应看到：

- 仓库克隆到 `indexer/data/BedtimeNews-Transcripts/`
- chunk 存入 `rag.document_chunks`
- 文件操作记录在 `rag.file_actions`

### 性能指标

流水线每次运行后输出：

- 处理的文档总数
- 创建的 chunk 总数
- 处理的总 token 数
- 每 chunk 平均 token 数
- 预估 embedding API 调用次数

## 故障排查

**没有索引任何文档：**

```bash
# 查看日志中的错误
docker compose logs indexer | grep -i error

# 手动运行流水线
docker compose exec indexer python -m src.pipeline

# 确认 git clone 成功
docker compose exec indexer ls -la data/BedtimeNews-Transcripts/
```

**Embedding API 报错：**

- 检查环境中的 embedding 提供方 API 密钥（如 `SILICONFLOW_API_KEY`）
- 确认未超出速率限制
- 在提供方控制台查看 API 用量
- `expected N dimensions, not M`：模型输出维度与 `embedding halfvec(N)`
  列（由 `EMBEDDING_DIM` 确定）不匹配——参见
  [更换 Embedding 模型](#更换-embedding-模型)

**数据库连接失败：**

- 确认 postgres 在运行：`docker compose ps postgres`
- 检查 `.env` 中的凭据
- 测试连接：`docker compose exec indexer python -m src.debugger test`

**调度器未运行：**

```bash
# 检查调度器进程
docker compose top indexer

# 查看定时运行日志
docker compose exec indexer python -m src.debugger logs

# 重启服务
docker compose restart indexer
```
