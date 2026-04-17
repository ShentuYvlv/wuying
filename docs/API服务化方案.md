# API 服务化方案

## 目标

把当前无影云手机爬虫项目作为 GEO-watcher 的“项目 B crawler”接入。

最终部署形态：

```text
GEO-watcher backend
  -> Docker external network
  -> wuying-crawler:8000
  -> POST /api/v1/tasks/{platform_id}
  -> 爬虫异步执行
  -> 回调 GEO-watcher 上传 JSON 文件
```

这份方案按 `crawler项目接入完整步骤-项目B-2026-04-14.md` 调整，重点是：不要走公网域名，不要用 `127.0.0.1`，不要复用项目 A 的 `crawler-shared` 网络。

## 固定命名

本项目作为项目 B 时，使用下面这些真实值。

| 项 | 值 |
| --- | --- |
| crawler 服务名 | `wuying-crawler` |
| crawler 容器内监听端口 | `8000` |
| crawler Docker 网络别名 | `wuying-crawler` |
| crawler 专用 external network | `wuying-crawler-shared` |
| GEO-watcher backend 网络别名 | `geo-watcher-backend` |
| GEO-watcher backend 回调端口 | `3005` |
| GEO-watcher 调 crawler 鉴权头 | `x-api-key` |
| crawler 入站鉴权环境变量 | `SCRAPER_API_KEY` |
| crawler 回调 GEO-watcher 鉴权环境变量 | `CRAWLER_CALLBACK_API_KEY` |

注意：

- GEO-watcher 当前对所有 crawler endpoint 使用同一个 `CRAWLER_API_KEY`
- 本项目的 `SCRAPER_API_KEY` 必须等于 GEO-watcher 的 `CRAWLER_API_KEY`
- 如果以后本项目要单独 API Key，需要先改 GEO-watcher，支持按平台配置 API Key；当前不是这个模式

## 平台 ID

GEO-watcher 里的 `platform_id` 建议使用 `wuying-*` 前缀，避免和项目 A 已经存在的 `doubao`、`deepseek`、`yuanbao` 等平台 ID 冲突。

| GEO-watcher platform_id | crawler 内部平台 | 接口路径 |
| --- | --- | --- |
| `wuying-doubao` | `doubao` | `/api/v1/tasks/wuying-doubao` |
| `wuying-deepseek` | `deepseek` | `/api/v1/tasks/wuying-deepseek` |
| `wuying-kimi` | `kimi` | `/api/v1/tasks/wuying-kimi` |
| `wuying-qianwen` | `qianwen` | `/api/v1/tasks/wuying-qianwen` |
| `wuying-yuanbao` | `yuanbao` | `/api/v1/tasks/wuying-yuanbao` |

如果以后确定要替换项目 A 的同名平台，才把 GEO-watcher 的平台 ID 改成 `doubao`、`deepseek`、`yuanbao` 这类原始 ID。当前不建议这样做。

## GEO-watcher 配置

### Docker network

服务器先创建本项目专用共享网络：

```bash
docker network create wuying-crawler-shared
```

GEO-watcher 的 `backend` 服务需要额外加入该网络：

```yaml
services:
  backend:
    networks:
      geo-watcher: {}
      crawler-shared:
        aliases:
          - ${CRAWLER_BACKEND_ALIAS:-geo-watcher-backend}
      wuying-crawler-shared:
        aliases:
          - ${WUYING_BACKEND_ALIAS:-geo-watcher-backend}

networks:
  geo-watcher:
    driver: bridge

  crawler-shared:
    external: true
    name: ${CRAWLER_SHARED_NETWORK:-crawler-shared}

  wuying-crawler-shared:
    external: true
    name: ${WUYING_SHARED_NETWORK:-wuying-crawler-shared}
```

只需要 GEO-watcher 的 `backend` 加入该网络，不要把 nginx、frontend、admin-frontend 加进去。

这个 compose 改动必须真实落到 GEO-watcher 的 `docker-compose.prod.yml`，否则 GEO-watcher 容器内访问不到：

```text
http://wuying-crawler:8000
```

修改后需要重建或重启 GEO-watcher backend。

### backend/.env

保留项目 A 的已有配置。新增本项目网络变量：

```env
WUYING_SHARED_NETWORK=wuying-crawler-shared
WUYING_BACKEND_ALIAS=geo-watcher-backend
```

`CRAWLER_PLATFORM_ENDPOINTS` 需要为本项目平台配置完整 URL，不能写相对路径。相对路径会拼到项目 A 的 `CRAWLER_BASE_URL`。

推荐值：

```env
CRAWLER_PLATFORM_ENDPOINTS={"wuying-doubao":"http://wuying-crawler:8000/api/v1/tasks/wuying-doubao","wuying-deepseek":"http://wuying-crawler:8000/api/v1/tasks/wuying-deepseek","wuying-kimi":"http://wuying-crawler:8000/api/v1/tasks/wuying-kimi","wuying-qianwen":"http://wuying-crawler:8000/api/v1/tasks/wuying-qianwen","wuying-yuanbao":"http://wuying-crawler:8000/api/v1/tasks/wuying-yuanbao"}
```

如果 `.env` 里已有项目 A 的配置，需要合并，不要覆盖。例如：

```env
CRAWLER_PLATFORM_ENDPOINTS={"deepseek":"/api/v1/tasks/deepseek","doubao":"/api/v1/tasks/doubao-stand","yuanbao":"/api/v1/tasks/yuanbao","doubao-ark":"/api/v1/tasks/doubao-ark","wuying-doubao":"http://wuying-crawler:8000/api/v1/tasks/wuying-doubao","wuying-deepseek":"http://wuying-crawler:8000/api/v1/tasks/wuying-deepseek","wuying-kimi":"http://wuying-crawler:8000/api/v1/tasks/wuying-kimi","wuying-qianwen":"http://wuying-crawler:8000/api/v1/tasks/wuying-qianwen","wuying-yuanbao":"http://wuying-crawler:8000/api/v1/tasks/wuying-yuanbao"}
```

GEO-watcher 后台还要创建对应平台 ID，例如：

```text
wuying-doubao
wuying-kimi
wuying-deepseek
wuying-qianwen
wuying-yuanbao
```

并把用户、关键词、任务绑定到这些平台，否则任务不会派发到本项目。

## 本项目配置

### docker-compose

本项目 API 服务加入自己的 internal network 和专用 external network。

```yaml
services:
  wuying-crawler:
    networks:
      wuying-internal: {}
      wuying-crawler-shared:
        aliases:
          - ${WUYING_CRAWLER_ALIAS:-wuying-crawler}
    expose:
      - "8000"
    environment:
      - SCRAPER_API_KEY=${SCRAPER_API_KEY}
      - CRAWLER_CALLBACK_URL=${CRAWLER_CALLBACK_URL}
      - CRAWLER_CALLBACK_API_KEY=${CRAWLER_CALLBACK_API_KEY}
      - WUYING_MANUAL_ADB_ENDPOINT=${WUYING_MANUAL_ADB_ENDPOINT}
      - WUYING_INSTANCE_IDS=${WUYING_INSTANCE_IDS}
      - ADB_PATH=/app/platform-tools/adb
    volumes:
      - ./data:/app/data
      - ./platform-tools:/app/platform-tools

networks:
  wuying-internal:
    driver: bridge

  wuying-crawler-shared:
    external: true
    name: ${WUYING_SHARED_NETWORK:-wuying-crawler-shared}
```

不需要暴露宿主机端口：

```yaml
ports:
  - "8000:8000"
```

如果本地调试需要临时暴露端口，可以只在开发 compose 里加，生产环境不要加。

### .env

本项目至少需要：

```env
WUYING_SHARED_NETWORK=wuying-crawler-shared
WUYING_CRAWLER_ALIAS=wuying-crawler
SCRAPER_API_KEY=<GEO-watcher 调用本 crawler 的 key>
CRAWLER_CALLBACK_URL=http://geo-watcher-backend:3005/api/integrations/crawler/uploads
CRAWLER_CALLBACK_API_KEY=<双方约定的 callback key>
CRAWLER_RECORD_TIMEOUT_SECONDS=300
WUYING_MANUAL_ADB_ENDPOINT=106.14.114.146:100
WUYING_INSTANCE_IDS=acp-xxxxxxxxxxxxxxxx
ADB_PATH=/app/platform-tools/adb
```

说明：

- `SCRAPER_API_KEY` 必须和 GEO-watcher 请求本 crawler 时使用的 `CRAWLER_API_KEY` 对得上
- `CRAWLER_CALLBACK_API_KEY` 必须和 GEO-watcher backend 的 `CRAWLER_CALLBACK_API_KEY` 一致
- `CRAWLER_CALLBACK_URL` 必须用 Docker 网络别名 `geo-watcher-backend`
- `CRAWLER_RECORD_TIMEOUT_SECONDS` 是单条 prompt 的硬超时，默认 `300` 秒
- 当前不要给本项目单独设置另一套入站 API Key，除非 GEO-watcher 已经支持按平台配置 API Key

## 入站接口

GEO-watcher 调用本项目时使用下面的接口。

```http
POST /api/v1/tasks/{platform_id}
Content-Type: application/json
x-api-key: <SCRAPER_API_KEY>
```

支持的 `platform_id`：

```text
wuying-doubao
wuying-deepseek
wuying-kimi
wuying-qianwen
wuying-yuanbao
```

请求体：

```json
{
  "prompts": ["问句1", "问句2"],
  "repeat": 1,
  "save_name": "2026-04-14_wuying-doubao_user_product_keyword_07_r1",
  "env": {
    "task_id": "GEO-watcher task id",
    "monitor_date": "2026-04-14",
    "user_id": "user id",
    "product_id": "product id",
    "keyword_id": "keyword id",
    "platform_id": "wuying-doubao",
    "is_negative": "false",
    "run_id": "task_id:2026-04-14:7:1",
    "callback_url": "http://geo-watcher-backend:3005/api/integrations/crawler/uploads",
    "callback_api_key": "<双方约定的 callback key>"
  }
}
```

接口行为：

- 校验 `x-api-key`
- 校验 `platform_id`
- 把 `wuying-doubao` 映射到内部 `doubao` workflow
- 立即创建本项目侧任务
- 后台异步执行 prompts
- 单条 prompt 超过 `CRAWLER_RECORD_TIMEOUT_SECONDS` 会强制终止并标记失败
- 执行完成后回调 GEO-watcher

立即响应：

```json
{
  "task_id": "wuying-20260414-abcdef",
  "trace_id": "wuying-20260414-abcdef",
  "type": "wuying-doubao",
  "status": "pending",
  "expected_records": 2,
  "output_file": "/app/data/tasks/wuying-20260414-abcdef.json"
}
```

## 执行顺序

GEO-watcher 每次请求只对应一个 `platform_id`。

本项目内部执行顺序：

```text
for repeat in repeat:
  for prompt in prompts:
    run internal workflow once
```

示例：

```text
POST /api/v1/tasks/wuying-kimi
prompts = ["问题1", "问题2"]
repeat = 2

执行顺序：
1. kimi -> 问题1 -> repeat 1
2. kimi -> 问题2 -> repeat 1
3. kimi -> 问题1 -> repeat 2
4. kimi -> 问题2 -> repeat 2
```

第一版同一 crawler 服务只跑一个任务。以后有 5 台云手机后，再按实例 ID 做并发锁。

## 超时和错误返回

`POST /api/v1/tasks/{platform_id}` 是异步入队接口，正常只返回任务已接收，不等待真实爬取完成。

错误检测分两层：

- 入队前错误：鉴权失败、平台不存在、请求体非法，直接返回 HTTP `401/403/404/422`
- 执行中错误：ADB 连接失败、App 卡住、UI 找不到、单条 prompt 超时，写入任务状态文件

执行中错误通过下面接口查询：

```http
GET /api/v1/tasks/{crawler_task_id}
GET /api/v1/tasks/{crawler_task_id}/results
```

返回里看这些字段：

```json
{
  "status": "failed",
  "failed_records": 1,
  "error": "Crawler record timed out after 300s: platform=doubao, instance_id=acp-xxx"
}
```

这样 GEO-watcher 不应该等 POST 阻塞完成，而是按 `task_id` 轮询或等待 callback。

## 回调 GEO-watcher

任务完成后，本项目必须回调：

```http
POST http://geo-watcher-backend:3005/api/integrations/crawler/uploads
x-api-key: <CRAWLER_CALLBACK_API_KEY>
Content-Type: multipart/form-data
```

表单字段必须包含：

```text
run_id
task_id
user_id
platform_id
product_id
keyword_id
monitor_date
files
```

字段来源：

- 全部优先使用 GEO-watcher 派单请求里的 `env`
- 不要重新生成 `run_id`
- 不要改 `monitor_date`
- 不要丢 `task_id`

上传文件要求：

- `files` 可以上传一个或多个 JSON 文件
- 每个文件必须是合法 JSON
- JSON 顶层必须是数组

本项目建议生成的上传 JSON：

```json
[
  {
    "query": "用户问句",
    "response": "AI 回答正文",
    "提及率": 100,
    "前三率": 100,
    "置顶率": 0,
    "负面提及率": 0,
    "platform_id": "wuying-doubao",
    "platform": "doubao",
    "references": {
      "summary": null,
      "keywords": [],
      "items": []
    },
    "raw_output_path": ""
  },
  {
    "attitude": 92
  }
]
```

当前指标字段先用 mock 数据对齐 GEO-watcher 入库结构：

- `提及率`: 100
- `前三率`: 100
- `置顶率`: 0
- `负面提及率`: 0
- `attitude`: 92

后续再把这些 mock 值替换成真实品牌识别、排名识别、负面判断和态度分逻辑。

GEO-watcher 成功响应：

```json
{
  "queued": 1,
  "ids": ["review id"]
}
```

## 本地调试接口

下面接口只给本项目自查用，不作为 GEO-watcher 的主接入路径。

```http
GET /health
GET /api/v1/tasks/{crawler_task_id}
GET /api/v1/tasks/{crawler_task_id}/results
```

GEO-watcher 主接入路径永远是：

```text
POST /api/v1/tasks/{platform_id}
```

## 结果 JSON 保存改造

当前批量模式的完整结果保存在 `data/batches/<task_id>/<platform>/repeat_xxx_prompt_xxx.json`。

callback 上传仍然单独生成 GEO-watcher 需要的 JSON 数组：

```text
data/batches/<task_id>/<platform>/repeat_xxx_prompt_xxx.json
  -> 从任务内存结果转成 GEO-watcher callback JSON 数组
  -> 保存到 data/callback_payloads/*.json
  -> multipart/form-data 上传到 callback_url
```

建议新增文件：

```text
src/wuying/application/geo_watcher_payload.py
```

建议代码结构：

```python
from __future__ import annotations

from typing import Any


MOCK_METRICS = {
    "提及率": 100,
    "前三率": 100,
    "置顶率": 0,
    "负面提及率": 0,
}


def build_geo_watcher_records(*, raw_result: dict[str, Any], platform_id: str) -> list[dict[str, Any]]:
    return [
        {
            "query": raw_result.get("prompt", ""),
            "response": raw_result.get("response", ""),
            **MOCK_METRICS,
            "platform_id": platform_id,
            "platform": raw_result.get("platform", ""),
            "references": raw_result.get("references", {}),
            "raw_output_path": raw_result.get("output_path", ""),
        },
        {
            "attitude": 92,
        },
    ]
```

说明：

- `raw_result` 直接来自当前统一结果 JSON
- `platform_id` 使用 GEO-watcher 下发的 `env.platform_id`，不要自己改
- `query` 对应当前结果里的 `prompt`
- `response` 对应当前结果里的 `response`
- 指标字段第一版先 mock，后续替换真实计算逻辑
- 上传给 GEO-watcher 的 JSON 顶层必须是数组
- 内部原始 JSON 继续保留，方便排错

## 连通性验证

### GEO-watcher backend 访问本 crawler

进入 GEO-watcher backend 容器：

```bash
docker exec -it geo-watcher-backend sh
```

执行：

```bash
python - <<'PY'
from urllib.request import urlopen
print(urlopen("http://wuying-crawler:8000/health", timeout=5).read().decode())
PY
```

### 本 crawler 访问 GEO-watcher backend

进入本项目 crawler 容器：

```bash
docker exec -it <wuying-crawler-container> sh
```

执行：

```bash
python - <<'PY'
from urllib.request import urlopen
print(urlopen("http://geo-watcher-backend:3005/health", timeout=5).read().decode())
PY
```

### 检查共享网络

```bash
docker network inspect wuying-crawler-shared
```

应该看到：

- `geo-watcher-backend`
- `wuying-crawler`

## 实施顺序

1. 新增 FastAPI 服务入口，监听 `0.0.0.0:8000`
2. 新增 `SCRAPER_API_KEY` 入站鉴权
3. 新增平台 ID 映射：`wuying-*` -> 内部平台名
4. 新增 `POST /api/v1/tasks/{platform_id}`
5. 新增本地任务状态文件：`data/tasks/*.json`
6. 后台异步调用现有 workflow
7. 单条 prompt 使用子进程执行，按 `CRAWLER_RECORD_TIMEOUT_SECONDS` 做硬超时
8. 新增 `geo_watcher_payload.py`，把内部结果转成 GEO-watcher callback JSON 数组
9. 指标字段第一版先写 mock 值，后续替换真实计算逻辑
10. 按 `env.callback_url` 和 `env.callback_api_key` 回调上传
11. 新增 `/health`
12. 写 Dockerfile / docker-compose
13. GEO-watcher `backend` 实际加入 `wuying-crawler-shared`
14. GEO-watcher `CRAWLER_PLATFORM_ENDPOINTS` 合并本项目完整 URL，不要覆盖项目 A 配置
15. GEO-watcher 后台创建 `wuying-*` 平台 ID，并绑定用户、关键词和任务

## 关键原则

- 本项目不要复用项目 A 的 `crawler-shared`
- GEO-watcher 对本项目必须配置完整 URL
- `SCRAPER_API_KEY` 必须等于 GEO-watcher 的 `CRAWLER_API_KEY`
- `platform_id` 必须和 GEO-watcher 数据库里的平台 ID 一致
- 本项目 API 路径按 `platform_id` 区分平台
- 本项目内部再把 `wuying-*` 映射到 `doubao/kimi/deepseek/qianwen/yuanbao`
- 回调字段必须原样使用 GEO-watcher 下发的 `env`
- callback 上传 JSON 顶层必须是数组，且第一版要带 mock 指标字段
- 生产环境不要暴露宿主机端口
- 不要用公网域名或 `127.0.0.1` 做容器间通信
