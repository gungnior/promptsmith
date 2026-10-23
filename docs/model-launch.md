# 本地模型服务管理

PromptSmith 可以直接**启动 / 停止**本地模型服务，不用再单独开命令行窗口。

典型用法：跑词时让模型在线，去跑图（ComfyUI 等）时点一下释放显存，回来再点一下启动。

---

## 一、配置写在哪

**不要写进 `data/config.json`**（那个文件在版本库里）。写在同目录的 `data/config.local.json`，它会**深度覆盖**主配置，且已被 `.gitignore` 排除。

```json
{
  "model_profiles": [
    {
      "id": "local-llm",
      "label": "本地 27B",
      "base_url": "http://127.0.0.1:8081/v1",
      "model": "my-model",
      "n_ctx": 65536,
      "max_output_tokens": 4096,
      "launch": {
        "enabled": true,
        "command": "D:\\path\\to\\llama-server.exe",
        "args": ["-m", "D:\\path\\to\\model.gguf",
                 "-ngl", "99", "-c", "65536",
                 "--host", "127.0.0.1", "--port", "8081", "-n", "4096"],
        "cwd": "D:\\path\\to",
        "port": 8081,
        "ready_path": "/health",
        "startup_timeout_sec": 180,
        "auto_start": true,
        "auto_start_on_generate": true
      }
    }
  ]
}
```

> 注意：`model_profiles` 是数组，local 文件里会**整个替换**掉主配置的数组。所以这里要写完整的模型项（`base_url`、`model`、`n_ctx` 都要有）。

### launch 字段

| 键 | 作用 | 建议 |
|---|---|---|
| `enabled` | 是否允许从界面启停 | 配好命令后设为 `true` |
| `command` | 可执行文件绝对路径 | 必填 |
| `args` | 参数数组（**每个参数单独一项**，不要拼成一个字符串） | 照抄你原来的启动命令 |
| `cwd` | 工作目录 | 有些引擎依赖相对路径，建议填 |
| `port` | 监听端口，用于反查进程 | 与 `base_url` 里的端口一致 |
| `ready_path` | 就绪探测路径 | llama.cpp 用 `/health`；Ollama 用 `/api/tags` |
| `startup_timeout_sec` | 等多久算超时 | 27B 首次加载约 40–80s，留 180 |
| `auto_start` | 软件启动时自动拉起 | 想让「双击就可用」就设 `true` |
| `auto_start_on_generate` | 点生成时若离线则先拉起 | 推荐 `true` |

---

## 二、界面怎么用

顶栏模型下拉框右侧有一个**状态芯片**：

| 显示 | 含义 | 点击 |
|---|---|---|
| `● 模型在线 · 显存 9.1G` | 已就绪 | 停止（会二次确认，提示释放多少显存） |
| `◌ 加载中…` | 正在启动，还没就绪 | 无动作 |
| `○ 模型离线` | 没在跑，且配了启动命令 | 启动 |
| `○ 模型离线（未配启动命令）` | 没配 `launch` | 提示去哪配 |

> 「显存」显示的是**整卡占用**，不是模型独占的量。跑图程序也占显存，所以这个数字能帮你判断还塞不塞得下。

**生成前自动拉起**：点「生成」时如果模型离线且 `auto_start_on_generate` 为真，会先启动并轮询到就绪（界面提示"正在拉起，首次约 1-2 分钟"），就绪后才真正生成。启动失败会在日志里留痕。

---

## 三、接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/model/status?mid=<模型id>` | 状态：`running` / `ready` / `pid` / `owned` / `gpu_used_mb` / `launch_enabled` |
| POST | `/api/model/start` `{"mid":"..."}` | 异步启动，立即返回；用 status 轮询就绪 |
| POST | `/api/model/stop` `{"mid":"..."}` | 停止（同步，几秒内返回） |
| POST | `/api/model/stopall` | 停掉所有配了 `launch` 的模型 |

`owned` 表示进程是 PromptSmith 自己拉起的；`false` 表示是你手动启动的（或上次软件退出后残留的），这种情况会**按端口反查 PID 并校验进程名**再杀，避免误杀同端口的其他程序。

---

## 四、三个实现细节（踩过的坑）

**1. 本地地址必须绕过系统代理**

系统里设了 `http_proxy` 时，`urllib` 连 `127.0.0.1` 也会走代理。代理返回 502 会被误判成"服务活着"——结果是模型根本没起，程序却认为已就绪。

修法：用 `build_opener(ProxyHandler({}))` 发本地健康探测，并且只把 `401/403` 当作"活但需鉴权"，其余 HTTP 错误一律算失败。

**2. Windows 中文系统的命令输出是 GBK**

`netstat` / `tasklist` / `nvidia-smi` 在中文 Windows 上输出 GBK 编码。按 UTF-8 解码会抛 `UnicodeDecodeError`，导致端口反查、显存查询全部静默失效。

修法：统一走一个 `_run()` 封装，依次尝试 `utf-8` → `gbk` → `latin-1`。

**3. 端口可能被两个进程同时监听**

Windows 的 `SO_REUSEADDR` 允许新旧两个进程绑同一个端口。旧服务没退干净时，请求会被随机分给其中一个——表现为"接口时好时坏"。

排查方法：`netstat -ano | findstr :8765`，出现两个 LISTENING 就全杀再启。

---

## 五、不用这个功能也行

`launch.enabled` 为 `false` 时，程序完全不碰进程管理，只按 `base_url` 探测服务在不在。你用别的脚本、Docker、Ollama 起服务都行，只是没有界面上一键启停的便利。
