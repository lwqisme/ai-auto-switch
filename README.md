# ai-auto-switch

[English README](README.en.md)

探测多个兼容 Gemini 的 `{GOOGLE_GEMINI_BASE_URL, GEMINI_API_KEY}` 配置，
选择当前最快且健康的提供方并自动切换。

默认情况下，运行成功后会把选中的配置写入 `~/.gemini/.env`。

默认模型拆分如下：
- 延迟探测模型：`gemini-3-flash-preview`
- 写入环境变量的会话模型：`gemini-3-pro-preview`

## 功能说明

- 运行一个本地 Gemini 兼容代理，并在后台持续做健康探测。
- 分两阶段探测提供方：
  - 先探测便宜阶段的提供方（`expensive_only != true`）
  - 仅当便宜阶段全部失败或超时时，才探测昂贵阶段提供方
- 使用归一化后的 `input_price` 和延迟分数对健康提供方排序。
- 实际请求会路由到当前选中的健康提供方，并在可重试失败时自动切换重试。
- 默认会把本地代理地址、选中的提供方凭证和模型写入 `~/.gemini/.env`。

## 配置

先复制示例文件：

```bash
cp providers.example.json providers.json
```

然后在 `providers.json` 中填写你的提供方配置：

```json
{
  "providers": [
    {
      "name": "yunwu-main",
      "input_price": 1.2,
      "base_url": "https://yunwu.ai",
      "api_key_env": "YUNWU_MAIN_API_KEY",
      "model": "gemini-3-flash-preview",
      "session_model": "gemini-3-pro-preview",
      "cheap_only": true,
      "expensive_only": false,
      "use_query_key": true,
      "use_header_key": true,
      "header_key_name": "x-goog-api-key",
      "headers": {
        "x-provider-group": "gemini-cli"
      }
    },
    {
      "name": "relay-backup",
      "input_price": 3.8,
      "base_url": "https://example-relay.ai",
      "api_key": "your-direct-key-here",
      "model": "gemini-3-flash-preview",
      "session_model": "gemini-3-pro-preview",
      "cheap_only": false,
      "expensive_only": true,
      "test_path": "/health",
      "test_method": "GET",
      "use_query_key": false,
      "use_header_key": true,
      "header_key_name": "Authorization",
      "headers": {
        "Authorization": "Bearer your-direct-key-here",
        "x-relay-route": "backup"
      }
    }
  ]
}
```

建议优先使用 `api_key_env`，而不是明文 `api_key`。
对于 `proxy_app.py` 的评分路由逻辑，每个提供方都必须配置 `input_price`。
`cheap_only` 和 `expensive_only` 不能同时为 `true`。

## proxy_app.py（本地代理）

先安装运行依赖：

```bash
python3 -m pip install --user -r requirements.txt
```

默认启动方式（后台分离进程，默认开启 macOS 菜单栏模式）：

```bash
python3 proxy_app.py
```

- 默认日志文件：`/tmp/ai-auto-switch-proxy.log`
- 默认会把以下代理环境变量写入 `~/.gemini/.env`：
  - `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:18080`（或你通过 `--host/--port` 指定的地址）
  - 选中的提供方 API Key 与会话模型环境变量
- 禁用自动写入：

```bash
python3 proxy_app.py --no-auto-write
```

- 把代理环境变量写入自定义文件：

```bash
python3 proxy_app.py --write-env ~/.gemini/.env
```

- 前台运行（保持附着在终端中）：

```bash
python3 proxy_app.py --foreground
```

- 菜单栏模式下，下拉菜单会显示所有提供方的实时状态：
  - 按评分排序（评分越低越优，未知评分排在后面）
  - 提供 `Probe Interval` 子菜单，可快速切换 `1m`、`5m`、`10m`、`30m`
  - 探测进行中时，仅在下拉菜单里显示 `Probing...`
  - `⭐ 🟢 provider score=0.184 123ms`（当前激活且健康的提供方）
  - `  🔴 provider DOWN`（当前不健康）
  - `  🟡 provider INIT`（尚未探测）
  - 包含 `Quit` 用于退出菜单栏应用

- 代理模式下的探测策略：
  - 默认每 `60s`（1 分钟）执行一次后台探测，可通过 `--probe-interval` 调整
  - 优先探测便宜阶段提供方（`expensive_only != true`）
  - 仅在便宜阶段全部失败或超时时探测昂贵阶段提供方
  - 请求超时、HTTP 5xx 或连续失败会把提供方标记为不健康
  - 使用最近 5 次成功探测计算移动平均延迟，可通过 `--latency-window` 调整
  - 使用归一化后的 `input_price` 和延迟计算评分：
    - `Balance_Score = (alpha * normalized_price) + ((1-alpha) * normalized_latency)`
    - 分数越低越好
  - 默认总是选择评分最低的健康提供方（`--sticky-improvement-threshold 0.0`）
  - 可选粘性策略：只有当挑战者评分显著更低时才切换，例如通过 `--sticky-improvement-threshold 0.2` 要求至少低 `20%`
- 常用调参示例：

```bash
python3 proxy_app.py --probe-interval 60 --alpha 0.5 --sticky-improvement-threshold 0.2
python3 proxy_app.py --latency-window 5 --failure-threshold 2
```

- 实际请求失败时的切换策略：
  - 如果当前活跃提供方在真实流量中失败（超时、限流、5xx），会立即标记为不健康
  - 代理会重新选择下一个最佳健康提供方，并自动重试同一个请求
- 默认日志较为精简，示例如下：
  - `[2026-03-07 14:20:00+0800] [probe-status] WORKING healthy=5/8 selected=foo score=0.1842 latency=132.1ms reason=sticky_keep`
  - `[2026-03-07 14:20:00+0800] [probe-provider] foo=WORKING avg_latency=132.1ms score=0.1842 fails=0`
  - `[2026-03-07 14:20:01+0800] [live] provider=foo failure=HTTP 500: ...`
  - `[2026-03-07 14:20:01+0800] [route] failover from=foo to=bar reason=select_best`
- 打印更详细的逐提供方探测信息（探测结果、输入价格、标记、错误）：

```bash
python3 proxy_app.py --probe-detail
```

如果系统缺少 `rumps`，进程会记录提示并自动退回到 headless 模式。安装方式：

```bash
python3 -m pip install --user rumps
```

仅 headless 运行：

```bash
python3 proxy_app.py --headless
```

健康检查：

```bash
curl -sS http://127.0.0.1:18080/_health
```

## 提供方字段

- `name`（必填，提供方名称）
- `base_url`（必填，提供方基础地址）
- `input_price`（必填，`proxy_app.py` 评分使用）
- `api_key`（与 `api_key_env` 二选一）
- `api_key_env`（与 `api_key` 二选一；从环境变量读取密钥）
- `model`（可选，探测模型，默认：`gemini-3-flash-preview`）
- `session_model`（可选，导出到运行环境中的模型，默认：`gemini-3-pro-preview`）
- `cheap_only`（可选布尔值；从昂贵回退阶段中排除）
- `expensive_only`（可选布尔值；从便宜阶段中排除，仅在回退时使用）
- `cheap_only` 与 `expensive_only` 不能同时为 `true`
- `test_path`（可选，覆盖默认的模型探测路径）
- `test_method`（可选；若未设置，存在 `test_path` 时默认 `GET`，否则默认 `POST`）
- `test_body`（可选；仅用于 `POST`/`PUT`/`PATCH`）
- `use_query_key`（可选，默认：`true`）
- `use_header_key`（可选，默认：`true`）
- `header_key_name`（可选，默认：`x-goog-api-key`）
- `headers`（可选，额外请求头对象）

## 说明

- 脚本在激活提供方时会设置以下环境变量：
  - `GOOGLE_GEMINI_BASE_URL`
  - `GEMINI_API_KEY`
  - `GOOGLE_GEMINI_API_KEY`
  - `GEMINI_MODEL`
  - `GOOGLE_GEMINI_MODEL`
- 如果所有提供方都失败，脚本会以非零状态退出。
- 如果 Python 默认 CA 证书库缺失，脚本会自动回退到常见 CA bundle 路径。
