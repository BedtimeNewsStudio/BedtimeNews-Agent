# Frontend 服务

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

睡前消息智能 RAG 系统的自定义聊天 UI。一个静态单页应用（HTML/CSS/JS），
由轻量 FastAPI 服务托管，并将聊天流代理到内部 agent 后端。

全栈安装说明参见[主 README](../README.md)。

## 设计

- **主题：** 配色源自节目 logo——深藏青底色、宝蓝色主强调色，以及用于
  直播/进行中信号的金黄色强调色。浅色与深色主题默认跟随操作系统的
  `prefers-color-scheme`。页头切换按钮会写入持久的 `localStorage` 覆盖。
- **颜色令牌**是语义化、可换肤的（`--bg`、`--surface`、`--line`、
  `--text`、`--text-dim`、`--muted`、`--accent`、`--accent-2`），深色
  定义在 `:root`，浅色在 `[data-theme="light"]` 下覆盖。
- **字体：** 正文使用系统 CJK 字体栈（PingFang SC / Microsoft YaHei /
  Noto Sans SC），标签/数据使用等宽字体栈。刻意只用系统字体——不加载
  webfont CDN，保证页面在中国大陆可靠加载。
- **信号采集日志：** RAG 流水线的各个阶段（condense → route → rewrite →
  retrieve → grade → generate）渲染为实时日志，回答开始后锁定并折叠。
  只有当对话历史消解了追问指代时才显示 condense 阶段。

## 功能

- 匿名聊天（无需登录）
- 感知系统的浅色/深色主题，带持久的手动切换
- 按类别分组的示例问题（完整问题文本即可点击）
- 实时 SSE 流式输出，流水线步骤可见
- 使用 [markdown-it](https://github.com/markdown-it/markdown-it) 渲染
  Markdown 回答（本地内置；`html:false` 防 XSS），并附加应用专属的
  引用标签
- 会话仅存在于当前页面（刷新即清空）
- 适配移动端；支持键盘操作；尊重 `prefers-reduced-motion`

## 架构

![Frontend 请求架构](../docs/diagrams/frontend-architecture.svg)

Frontend：

- 运行在 Docker 容器中，通过纯 HTTP 对外提供服务，端口 8080（无 TLS——
  公网暴露与 TLS 终止由本仓库之外处理）
- 是唯一发布到宿主机的服务（`FRONTEND_PORT`，默认 8080）
- 通过内部 Docker 网络将 `/chat` 代理给 agent；agent 从不暴露给宿主机

## 组件

- **server.py** — FastAPI 应用：托管 `static/`、暴露 `/api/starters`、
  并把 `/chat` SSE 代理给 agent
- **starters.py** — 示例问题数据（类别 + 问题）；纯数据，不依赖任何 UI
  框架
- **static/index.html** — 页面标记、主题引导脚本与对话轮次模板
- **static/styles.css** — 可换肤的设计系统（`:root` +
  `[data-theme="light"]`）
- **static/app.js** — 示例问题列表、消息输入区、主题切换、SSE 解析、
  Markdown 渲染
- **static/markdown-it.min.js** — 本地内置的 Markdown 渲染器（MIT），
  按需加载而非随页面加载
- **static/bedtimenews.webp** — favicon / 品牌 logo
- **pyproject.toml** — 依赖元数据（`fastapi`、`uvicorn`、`httpx`）

## 端点

| 方法 | 路径            | 用途                            |
| ----- | --------------- | ------------------------------- |
| GET   | `/`             | 托管 SPA（`static/index.html`） |
| GET   | `/api/starters` | 示例问题 JSON（`categories`）   |
| POST  | `/chat`         | 将 agent 的 SSE 流代理给浏览器  |
| GET   | `/healthz`      | 存活检查                        |

## 开发流程

容器运行 `uvicorn server:app`。修改 Python 或静态文件后，重新构建并
重启：

```bash
# Frontend 发布在宿主机上（FRONTEND_PORT，默认 8080）
docker compose build web
docker compose up -d web
open http://localhost:8080
```

> 如果重新构建后似乎仍在提供旧代码，使用 `--no-cache`。

### 不使用 Docker 运行

```bash
cd frontend
pip install .
# 指向一个可达的 agent 后端：
AGENT_BACKEND_HOST=localhost AGENT_BACKEND_PORT=8000 \
  uvicorn server:app --reload --port 8080
```

### 自定义

- **示例问题 / 类别：** 编辑 `starters.py`（`CATEGORIES`）。
- **样式：** 编辑 `static/styles.css`（设计令牌位于 `:root`）。
- **文案 / 布局：** 编辑 `static/index.html`。
- **Logo / favicon：** 替换 `static/bedtimenews.webp`。它以 2.1rem 渲染，
  保持小体积即可——128px 见方足以覆盖 hi-DPI，且该文件被
  `CachedStaticFiles` 缓存一周。

## 配置

| 变量                 | 默认值  | 用途                            |
| -------------------- | ------- | ------------------------------- |
| `AGENT_BACKEND_HOST` | `agent` | Docker 网络上的 agent 服务名    |
| `AGENT_BACKEND_PORT` | `8000`  | Agent 端口                      |
| `FRONTEND_PORT`      | `8080`  | Frontend 发布到的宿主机端口     |

## 调试

```bash
# 日志
docker compose logs -f web

# 从容器内检查后端连通性（slim 镜像没有 ping/curl；
# 用自带的 Python + httpx 代替）
docker compose exec web python -c "import httpx; print(httpx.post(
    'http://agent:8000/chat', json={'question': '测试'}, timeout=120).text)"
```

## API 契约

Frontend 代理 agent 的 `/chat` 端点。

### 请求

```json
{
  "question": "string (required)",
  "history": [{"question": "…", "answer": "…", "grounded": true}],
  "stream": true
}
```

`history` 可选；浏览器最多发送最近三轮对话。

### 流式响应（SSE）

```json
{"type": "step", "step": "condense|route|rewrite|retrieve|grade|generate", "content": "…"}
{"type": "citations", "urls": {"ShuiQianXiaoXi/0501-0600/0588.md": {"title": "睡前消息588", "url": "https://bedtimenewsstudio.github.io/BedtimeNews-Transcripts/contents/ShuiQianXiaoXi/0501-0600/0588.html"}}}
{"type": "answer_chunk", "content": "…"}
{"type": "answer_final", "content": "…", "grounded": true}
{"type": "answer_meta", "grounded": true}
{"type": "followups", "items": ["…"]}
{"type": "error", "content": "…"}
```

服务器可能在事件之间发送 `: ping` SSE 注释，并以 `data: [DONE]` 结束每条
流。成功的一轮恰好发送 `answer_final` 或 `answer_meta` 之一。

## 限制（MVP）

- **无鉴权** — 仅匿名使用
- **无持久化** — 刷新即清空会话
- **会话仅限单标签页** — 没有跨标签页或服务端历史

## 故障排查

**8080 端口被占用：** 在 `.env` 中把 `FRONTEND_PORT` 设为其它宿主机端口，
并重建服务（`docker compose up -d web`）。

**无法连接后端：**

- `docker compose ps agent` 与 `docker compose logs agent`
- 从容器内检查连通性（见[调试](#调试)）

**修改未生效：** 重新构建（`--no-cache`）并强制刷新浏览器
（Cmd/Ctrl+Shift+R）。
