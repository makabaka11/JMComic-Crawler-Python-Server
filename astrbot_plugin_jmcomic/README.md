# JMComic AstrBot 插件

对接 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)，为 AstrBot 提供禁漫下载能力。

## 架构

```
用户 → AstrBot → 插件(HTTP Client) → JMComic Server(FastAPI) → jmcomic 库
```

- **插件**：轻量 AstrBot 插件，只做 HTTP 调用 + 消息发送
- **Server**：独立运行的 FastAPI 后端，封装 jmcomic 下载逻辑

## 部署

### 1. 启动 JMComic Server

```bash
cd JMComic-Crawler-Python

# 安装依赖
pip install fastapi uvicorn
pip install commonx curl-cffi pillow pycryptodome pyyaml

# 启动（默认 127.0.0.1:8899）
python jmcomic_server.py

# 或自定义端口/地址
JMCOMIC_HOST=0.0.0.0 JMCOMIC_PORT=8899 python jmcomic_server.py

# 配置代理（可选）
JMCOMIC_PROXY=http://127.0.0.1:7890 python jmcomic_server.py
```

### 2. 安装插件

将 `astrbot_plugin_jmcomic` 文件夹放入 AstrBot 的插件目录。

### 3. 配置插件

在 AstrBot 管理面板配置 Server 地址（默认 `http://127.0.0.1:8899`）。

## 功能

### 命令

| 命令 | 说明 |
|------|------|
| `/jm <ID>` | 下载本子 |
| `/jm photo <ID>` | 下载章节 |
| `/jm <链接>` | 从链接下载 |

### LLM 工具

AI 可自主调用以下工具（无需用户输入命令）：

- `jm_download_album` — 下载本子
- `jm_download_photo` — 下载章节

### 自动识别

开启后自动识别消息中的禁漫链接并下载。

## Server 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `JMCOMIC_HOST` | `127.0.0.1` | 监听地址 |
| `JMCOMIC_PORT` | `8899` | 监听端口 |
| `JMCOMIC_DOWNLOAD_DIR` | `./data/jmcomic/downloads` | 下载目录 |
| `JMCOMIC_MAX_CONCURRENT` | `3` | 最大并发数 |
| `JMCOMIC_DOWNLOAD_TIMEOUT` | `600` | 下载超时(秒) |
| `JMCOMIC_MAX_PAGES` | `200` | 最大页数限制 |
| `JMCOMIC_PROXY` | (空) | HTTP 代理 |
| `JMCOMIC_API_KEY` | (空) | API 鉴权密钥 |

## Server API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| POST | `/api/download/album` | 下载本子 |
| POST | `/api/download/photo` | 下载章节 |
| POST | `/api/export/pdf` | 导出 PDF |
| POST | `/api/export/zip` | 导出 ZIP |
| GET | `/api/task/{id}` | 查询任务 |
| DELETE | `/api/task/{id}` | 删除任务 |
| GET | `/api/file/{id}/{name}` | 获取文件 |

## 注意事项

- 本插件仅供学习和研究使用，请遵守当地法律法规
- Server 和插件可部署在不同机器上
- 建议设置 `JMCOMIC_API_KEY` 并在插件侧通过 `Authorization: Bearer <key>` 鉴权（TODO）
