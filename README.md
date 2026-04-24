# 无影云手机多平台自动化

基于无影云手机、ADB 和 `uiautomator2` 的聊天类 App 自动化项目。

当前已接入：

- `doubao`
- `deepseek`
- `kimi`
- `qianwen`
- `yuanbao`

## 命令行运行

先确认 ADB 能手工连通：

```powershell
.\platform-tools\adb.exe connect 106.14.114.146:100
.\platform-tools\adb.exe devices -l
```

前提：

- 不管是单机模式还是设备池模式，云手机实例都要先绑定密钥对
- 本地要有该密钥对对应的 `adbkey` 文件
- 如果 5 台手机绑定的是同一个密钥对，直接共用同一个 `ADB_VENDOR_KEYS`/`adbkey` 即可，不需要每台单独配
- 按官方文档，密钥或 `adbkey` 刚变更后要先执行一次 `adb kill-server` 再 `adb start-server`
- 手工执行 `.\platform-tools\adb.exe connect ...` 时，ADB 默认只读 `%USERPROFILE%\.android\adbkey`
- 如果你的 `adbkey` 放在项目目录里，手工测试前要先在当前 shell 设置：

```powershell
$env:ADB_VENDOR_KEYS="E:\all code\C一念\wuying\platform-tools\adbkey"
.\platform-tools\adb.exe kill-server
.\platform-tools\adb.exe start-server
```

再用统一脚本入口：

```powershell
.\venv\Scripts\python.exe .\run.py app --platform doubao --prompt "你好，介绍一下你自己"
```

多平台和文件批量：

```powershell
.\venv\Scripts\python.exe .\run.py app --platform doubao,kimi --file .\data\prompts.txt
```

多手机池执行：

```powershell
.\venv\Scripts\python.exe .\run.py app --platform doubao,deepseek,kimi --devices A,B,C --file .\data\prompts.txt
```

设备池批量装 APK：

```powershell
.\venv\Scripts\python.exe .\run.py install-apks --devices A,B,C
```

默认会把 [apk](E:/all code/C一念/wuying/apk) 目录下所有 `.apk` 依次安装到选中的设备上。
如果你显式传目录也可以：

```powershell
.\venv\Scripts\python.exe .\run.py install-apks --apk .\apk\
```

只装指定 APK：

```powershell
.\venv\Scripts\python.exe .\run.py install-apks --devices A,B --apk deepseek.apk,com.aliyun.tongyi.apk
```

说明：

- 设备池默认配置文件是 [device_pool.json](E:/all code/C一念/wuying/config/device_pool.json)
- 设备池只区分 `device_id / instance_id / adb_endpoint`，不单独配置密钥对；密钥对和 `adbkey` 继续走全局 `.env`
- 执行顺序固定是：`平台 -> prompt -> 设备并发`
- 新任务不再生成 `records.json`
- 最终业务结果按 `平台 + prompt` 拆分到 `data/tasks/<task_id>/prompts/*.json`
- 单条原始结果和失败记录保存到 `data/tasks/<task_id>/raw/*.json`
- `data/runs` 已废弃，不再作为任何模式的输出目录
- 如果配置了 `PIPELINE_LLM_API_KEY + PIPELINE_METRIC_KEYWORD`，每个 `prompts/*.json` 在生成时会自动计算并写入指标字段

## API 运行

本项目现在也可以作为 GEO-watcher 的 crawler API 服务运行。

先配置 `.env`：

```env
SCRAPER_API_KEY=your-crawler-api-key
CRAWLER_CALLBACK_URL=http://geo-watcher-backend:3005/api/integrations/crawler/uploads
CRAWLER_CALLBACK_API_KEY=your-callback-api-key
# 可不填；默认由 uploads 地址推导到 /api/integrations/crawler/progress
CRAWLER_PROGRESS_URL=http://geo-watcher-backend:3005/api/integrations/crawler/progress
CRAWLER_PROGRESS_API_KEY=your-callback-api-key
CRAWLER_RECORD_TIMEOUT_SECONDS=300
CRAWLER_BATCH_TIMEOUT_SECONDS=3600
CRAWLER_BATCH_MAX_WORKERS=5
PIPELINE_LLM_API_KEY=your-llm-api-key
PIPELINE_METRIC_KEYWORD=你的品牌名
PIPELINE_METRIC_DETECT_TYPE=rank
WUYING_MANUAL_ADB_ENDPOINT=106.14.114.146:100
WUYING_INSTANCE_IDS=acp-xxxxxxxxxxxxxxxx
WUYING_REGION_ID=cn-shanghai
WUYING_ENDPOINT=
WUYING_KEY_PAIR_ID=kp-xxxxxxxxxxxxxxxx
WUYING_AUTO_ATTACH_KEY_PAIR=false
ADB_PATH=E:\all code\C一念\wuying\platform-tools\adb.exe
ADB_VENDOR_KEYS=E:\all code\C一念\wuying\platform-tools\adbkey
WUYING_START_ADB_VIA_API=false
DEVICE_POOL_FILE=config/device_pool.json
```

启动 API：

```powershell
.\venv\Scripts\python.exe .\run.py api
```

健康检查：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
```

创建任务：

```powershell
$headers = @{ "x-api-key" = "your-crawler-api-key" }
$body = @{
  prompts = @("你好，介绍一下你自己")
  repeat = 1
  save_name = "local_test_wuying_doubao"
  env = @{
    task_id = "local-task-id"
    monitor_date = "2026-04-14"
    user_id = "mock-user-id"
    product_id = "mock-product-id"
    keyword_id = "mock-keyword-id"
    platform_id = "wuying-doubao"
    is_negative = "false"
    run_id = "local-task-id:2026-04-14:1:1"
    callback_url = "http://geo-watcher-backend:3005/api/integrations/crawler/uploads"
    callback_api_key = "your-callback-api-key"
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/tasks/wuying-doubao" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

查询任务：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/tasks/<task_id>" `
  -Headers @{ "x-api-key" = "your-crawler-api-key" }
```

查询结果：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/tasks/<task_id>/results" `
  -Headers @{ "x-api-key" = "your-crawler-api-key" }
```

批任务接口：

```powershell
$headers = @{ "x-api-key" = "your-crawler-api-key" }
$body = @{
  platforms = @("doubao", "deepseek", "kimi")
  prompts = @("你好，介绍一下你自己", "清明应该吃什么")
  repeat = 1
  device_ids = @("A", "B", "C")
  save_name = "local_multi_device_batch"
  env = @{}
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/api/v2/batches" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

批任务查询：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v2/batches/<task_id>" `
  -Headers @{ "x-api-key" = "your-crawler-api-key" }
```

批任务结果：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v2/batches/<task_id>/results" `
  -Headers @{ "x-api-key" = "your-crawler-api-key" }
```

结果文件：

- 主状态接口只看进度和错误：`GET /api/v2/batches/<task_id>`
- 结果接口从 `raw/*.json` 聚合返回扁平 `records`：`GET /api/v2/batches/<task_id>/results`
- 新任务不再生成 `data/tasks/<task_id>/records.json`
- 最终业务结果按平台和 prompt 保存到 `data/tasks/<task_id>/prompts/{年-月-日-时}-{platform}-p{prompt_index}-{prompt}.json`
- 单条原始结果和失败记录保存到 `data/tasks/<task_id>/raw/*.json`
- 不再生成 `summary.json` 和按平台拆分的批次结果文件
- callback 会按平台和 prompt 上传多个 JSON 文件，多个文件使用同一个 multipart 字段名 `files`

任务超时：

- `POST /api/v1/tasks/{platform_id}` 是异步入队，只表示任务已接收。
- 单条 prompt 的硬超时由 `CRAWLER_RECORD_TIMEOUT_SECONDS` 控制，默认 `300` 秒。
- 整个批任务的总超时由 `CRAWLER_BATCH_TIMEOUT_SECONDS` 控制，默认 `3600` 秒。
- 超时后该条会标记为失败，`GET /api/v1/tasks/<task_id>` 和 `/results` 会返回 `status/error/failed_records`。

支持的 API 平台 ID：

- `wuying-doubao`
- `wuying-deepseek`
- `wuying-kimi`
- `wuying-qianwen`
- `wuying-yuanbao`

这些平台 ID 会映射到内部平台：

- `wuying-doubao` -> `doubao`
- `wuying-deepseek` -> `deepseek`
- `wuying-kimi` -> `kimi`
- `wuying-qianwen` -> `qianwen`
- `wuying-yuanbao` -> `yuanbao`

## Docker 调用

生产环境建议不要暴露 `8000` 到公网。GEO-watcher backend 和本项目通过 Docker external network 通信。

服务器启动前必须创建 `.env`：

```bash
cp .env.example .env
```

至少填写：

```env
SCRAPER_API_KEY=<必须等于 GEO-watcher 的 CRAWLER_API_KEY>
CRAWLER_CALLBACK_URL=http://geo-watcher-backend:3005/api/integrations/crawler/uploads
CRAWLER_CALLBACK_API_KEY=<必须等于 GEO-watcher backend 的 CRAWLER_CALLBACK_API_KEY>
# 可不填；默认由 CRAWLER_CALLBACK_URL 推导为 http://geo-watcher-backend:3005/api/integrations/crawler/progress
CRAWLER_PROGRESS_URL=http://geo-watcher-backend:3005/api/integrations/crawler/progress
CRAWLER_PROGRESS_API_KEY=<默认可与 CRAWLER_CALLBACK_API_KEY 一致>
WUYING_MANUAL_ADB_ENDPOINT=106.14.114.146:100
WUYING_INSTANCE_IDS=acp-xxxxxxxxxxxxxxxx
WUYING_REGION_ID=cn-shanghai
WUYING_KEY_PAIR_ID=kp-xxxxxxxxxxxxxxxx
ADB_VENDOR_KEYS=/app/platform-tools/adbkey
WUYING_START_ADB_VIA_API=false
WUYING_SHARED_NETWORK=wuying-crawler-shared
WUYING_CRAWLER_ALIAS=wuying-crawler
```

如果启动时看到下面这种 warning，说明 `.env` 没创建或没填：

```text
The "SCRAPER_API_KEY" variable is not set. Defaulting to a blank string.
The "WUYING_MANUAL_ADB_ENDPOINT" variable is not set. Defaulting to a blank string.
```

这不是 Docker build 失败，是容器运行配置缺失。

GEO-watcher 调用地址示例：

```env
CRAWLER_PLATFORM_ENDPOINTS={"wuying-doubao":"http://wuying-crawler:8000/api/v1/tasks/wuying-doubao","wuying-deepseek":"http://wuying-crawler:8000/api/v1/tasks/wuying-deepseek","wuying-kimi":"http://wuying-crawler:8000/api/v1/tasks/wuying-kimi","wuying-qianwen":"http://wuying-crawler:8000/api/v1/tasks/wuying-qianwen","wuying-yuanbao":"http://wuying-crawler:8000/api/v1/tasks/wuying-yuanbao"}
```

鉴权关系：

- GEO-watcher 请求本项目时使用请求头 `x-api-key`
- 本项目校验 `.env` 里的 `SCRAPER_API_KEY`
- 当前 `SCRAPER_API_KEY` 必须等于 GEO-watcher 的 `CRAWLER_API_KEY`
- 自动指标计算默认读取：
  - `PIPELINE_LLM_API_KEY`
  - `PIPELINE_METRIC_KEYWORD`
  - `PIPELINE_METRIC_DETECT_TYPE`
- 也可以在单次任务请求的 `env` 里传：
  - `metric_keyword`
  - `metric_detect_type`
  - `metric_api_key`

回调地址：

```env
CRAWLER_CALLBACK_URL=http://geo-watcher-backend:3005/api/integrations/crawler/uploads
```

完整接入方案见：
[API服务化方案.md](E:/all code/C一念/wuying/docs/API服务化方案.md)

## 当前架构

- 接口层：`scripts/` + `src/wuying/interfaces/`
  - 命令行入口、参数解析、输出格式
- 应用层：`src/wuying/application/`
  - 平台注册、运行编排、工作流
- 调用层：`src/wuying/invokers/`
  - ADB、`uiautomator2`、阿里云接口等外部调用

兼容层仍保留：

- `src/wuying/workflows/`
- `src/wuying/platforms.py`
- `src/wuying/runner.py`

这些文件现在只做转发，主路径已经切到三层结构。

## 扩展新平台

新增 `deepseek` / `kimi` / `千问` / `元宝` 时，按这个顺序接：

1. 在 `config.py` 增加对应平台配置
2. 在 `src/wuying/application/workflows/` 新建平台工作流，继承 `ChatAppWorkflow`
3. 只实现平台差异部分
   - 包名 / 启动页
   - 页面选择器
   - 回答附加信息提取
4. 在 `src/wuying/application/platform_registry.py` 注册平台名
5. 直接用 `run.py app --platform xxx` 运行

## 关键配置

```env
ADB_PATH=E:\all code\C一念\wuying\platform-tools\adb.exe
ADB_VENDOR_KEYS=E:\all code\C一念\wuying\platform-tools\adbkey
WUYING_START_ADB_VIA_API=false
WUYING_MANUAL_ADB_ENDPOINT=106.14.114.146:100
WUYING_INSTANCE_IDS=acp-xxxxxxxxxxxxxxxx
DOUBAO_PACKAGE_NAME=com.larus.nova
```

联调坑记录见：
[联调踩坑.md](E:/all code/C一念/wuying/docs/联调踩坑.md)
