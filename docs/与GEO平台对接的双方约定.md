# 与 GEO 平台对接的双方约定

更新时间：2026-04-18

## 目的

本文只约定一件事：

- GEO-watcher 负责什么
- wuying crawler 负责什么
- 双方接口、状态、超时、callback 的最终约定是什么

这份文档以当前双方代码实现为准，不再讨论旧设计草案。

## 当前结论

双方主链路已经明确：

- GEO-watcher 负责任务创建、任务调度、409 重试、状态轮询、callback 接收、审核入库、后台日志展示。
- wuying 负责设备池、设备租约、batch 执行、平台串行、设备并发、任务状态查询、结果回传。

也就是说：

- GEO 不再自己做“单手机串行执行”。
- wuying 不负责“409 后排队重试 10 分钟”，这是 GEO 的职责。

## 责任边界

### GEO-watcher 负责的内容

1. 后台创建任务时约束任务类型：
   - 非 `wuying-*` 平台一次只能选一个
   - `wuying-*` 平台可多选
   - `wuying-*` 和非 `wuying-*` 不能混选
2. 保存任务的：
   - `platform_ids`
   - `device_ids`
   - `crawler_mode`
3. Wuying 任务调用：
   - `POST /api/v2/batches`
4. 轮询任务进度：
   - `GET /api/v2/batches/{task_id}/results`
5. 如果提交 batch 返回 `409 Conflict`：
   - run 保持 `queued`
   - 自动重试
   - 超过 10 分钟标记 `timeout`
6. 如果 batch 已提交但长时间没 callback：
   - 按 GEO 自己的 callback timeout 规则标记超时
7. 接收 callback 文件并进入审核队列
8. 审核通过后正式入库
9. 入库时以每条 record 的 `platform_id` 为准
10. 多设备同 query 指标先取平均，再写正式指标
11. 在后台日志页面展示：
   - run
   - step
   - device_result

### wuying 负责的内容

1. 提供 batch 接口：
   - `POST /api/v2/batches`
   - `GET /api/v2/batches/{task_id}`
   - `GET /api/v2/batches/{task_id}/results`
2. 校验并解析：
   - `platforms`
   - `prompts`
   - `repeat`
   - `device_ids`
   - `env`
3. 设备池选择与设备租约
4. 如果目标设备被占用：
   - 立即返回 `409 Conflict`
   - 不做内部排队
5. 执行批任务时保证：
   - 平台串行
   - 同一步骤下设备并发
   - 单设备内部串行
6. 提供任务状态与结果查询
7. 在任务结束后尝试 callback GEO
8. callback payload 中每条 record 保留自己的平台信息
9. callback record 中补充设备信息，便于 GEO 做设备级展示和排查

## 最终执行顺序约定

以当前 wuying 真实代码为准，双方统一采用：

```text
平台 -> repeat -> prompt -> 设备并发
```

说明：

- 如果 `repeat=1`，它和 `平台 -> prompt -> 设备并发` 的差异不明显。
- 但双方文档和日志展示都必须按真实顺序写，避免后续 `repeat>1` 时解释错误。

## GEO -> wuying 请求约定

接口：

```text
POST /api/v2/batches
```

请求头：

```text
x-api-key: {SCRAPER_API_KEY}
Content-Type: application/json
```

请求体约定：

```json
{
  "platforms": ["wuying-doubao", "wuying-kimi"],
  "prompts": ["问题1", "问题2"],
  "repeat": 1,
  "save_name": "2026-04-18_xxx",
  "device_ids": ["上海", "杭州", "深圳"],
  "env": {
    "run_id": "task_id:2026-04-18:15:1",
    "task_id": "geo-task-id",
    "user_id": "user-id",
    "product_id": "product-id",
    "keyword_id": "keyword-id",
    "monitor_date": "2026-04-18",
    "is_negative": "false",
    "callback_url": "http://geo-watcher-backend:3005/api/integrations/crawler/uploads",
    "callback_api_key": "xxx"
  }
}
```

补充约定：

- GEO 对 Wuying batch 不再在 `env` 里传单个 `platform_id`。
- `device_ids` 由 GEO 明确传入。
- GEO 后台默认全选设备池，取消勾选表示本次不使用该设备。

## wuying -> GEO 返回约定

### 1. 创建 batch 成功返回

至少包含：

```json
{
  "task_id": "20260418_xxx",
  "trace_id": "20260418_xxx",
  "type": "wuying-batch",
  "status": "pending",
  "expected_records": 6,
  "expected_batches": 2,
  "output_file": "data/tasks/20260418_xxx/prompts",
  "records_path": null,
  "prompt_files": []
}
```

### 2. 设备占用返回

当任意目标设备被租用时：

```text
HTTP 409 Conflict
```

语义约定：

- 这是“设备当前不可用”
- 不是永久失败
- GEO 负责后续重试和最终 timeout

## 状态查询约定

### 主状态接口

接口：

```text
GET /api/v2/batches/{task_id}
```

这是双方约定的**主进度接口**。

应至少保证返回这些字段：

- `task_id`
- `trace_id`
- `type`
- `status`
- `expected_records`
- `expected_batches`
- `finished_records`
- `failed_records`
- `finished_batches`
- `failed_batches`
- `platforms`
- `platform_ids`
- `device_ids`
- `selected_devices`
- `current_platform`
- `current_repeat_index`
- `current_prompt_index`
- `current_prompt`
- `records_path`，新任务为 `null`
- `prompt_files`
- `callback`
- `error`

不再返回 `summary_path`，也不再把 `platform_batches` 作为主数据结构。

### 结果接口

接口：

```text
GET /api/v2/batches/{task_id}/results
```

用途约定：

- 主要用于 GEO 的日志展示和排错
- GEO 用它同步扁平 records 明细
- 但任务是否完成，不能只靠它推断，仍要结合 callback 与 GEO 自己的状态机

返回结构：

```json
{
  "task_id": "wuying-xxx",
  "status": "succeeded",
  "records_path": null,
  "prompt_files": [],
  "records": []
}
```

兼容字段：

- `results` 可以存在，但内容必须和 `records` 一致，不能再是嵌套批次结构。

## callback 约定

接口：

```text
POST {env.callback_url}
```

请求头：

```text
x-api-key: {env.callback_api_key}
```

表单层字段约定：

- `run_id`
- `task_id`
- `user_id`
- `platform_id`
- `product_id`
- `keyword_id`
- `monitor_date`
- `files`

其中：

- 表单层 `platform_id` 只是兼容字段
- GEO 入库时不会再把它当唯一平台依据

### callback record 最终约定

每条 record 至少应包含：

```json
{
  "query": "用户问句",
  "response": "AI 回答正文",
  "提及率": 100,
  "前三率": 100,
  "置顶率": 0,
  "负面提及率": 0,
  "attitude": 92,
  "platform_id": "wuying-doubao",
  "platform": "doubao",
  "device_id": "上海",
  "references": {},
  "raw_output_path": "..."
}
```

最终强约定：

1. `platform_id` 必须保留在每条 record 上
2. `device_id` 必须补上
3. `raw_output_path` 应尽量提供真实可追溯路径；如果当前实现做不到，可先允许为空，但不应伪造
4. `raw/*.json` 是原始设备结果，不包含 `提及率 / 前三率 / 置顶率 / 负面提及率 / attitude`
5. `prompts/*.json` 是最终回传结果，Wuying 在写入该文件前统一补充指标字段
6. 同一个 callback 文件只包含一个平台和一个 query 的多设备结果

## 超时与收尾约定

### GEO 负责的超时

#### 1. 409 冲突排队超时

- GEO 收到 409 后保持 `queued`
- 按 GEO 自己的重试周期重试
- 超过 10 分钟标记 `timeout`

#### 2. callback 超时

- run 已 `submitted`
- 超过 GEO 配置的 callback timeout 仍未收到 callback
- GEO 标记 `timeout`

### wuying 不保证的事情

当前真实行为必须写清楚：

- 如果整批任务没有任何 record，wuying 可以跳过 callback
- 正常只要产生了 raw record，即使全部失败，也会按平台和 prompt 文件 callback

## GEO 当前已实现的部分

当前 GEO-watcher 已完成：

1. 多平台 `platform_ids` 任务模型
2. `device_ids` 任务模型
3. `wuying_batch` / `single` 双模型调度
4. Wuying `/api/v2/batches` 提交
5. `409` 自动重试
6. 10 分钟冲突排队超时
7. Wuying results 轮询
8. run / step / device_result 三层日志
9. callback 多平台 payload 接收
10. 审核入库按 `record.platform_id`
11. 多设备同 query 指标平均聚合

## wuying 当前需要补齐或保持的部分

### 必须保持

1. `POST /api/v2/batches`
2. `GET /api/v2/batches/{task_id}`
3. `GET /api/v2/batches/{task_id}/results`
4. 设备占用返回 `409`
5. record 级 `platform_id`
6. callback 使用 `env.callback_url` 和 `env.callback_api_key`

### 建议补齐

当前已补齐：

1. callback record 已包含 `device_id`
2. batch 模式下 callback record 的 `raw_output_path` 已写入真实结果文件路径

## 一句话约定

最终双方边界是：

- GEO 负责“任务调度、排队重试、状态收口、审核入库、后台展示”
- wuying 负责“设备池、设备锁、batch 执行、状态查询、结果回传”

只要双方都遵守这份约定，主链路就是稳定可联调的。

## 追加修改

### 1. 进度轮询接口统一

前文曾同时出现两种写法：

- `GET /api/v2/batches/{task_id}/results`
- `GET /api/v2/batches/{task_id}`

这里统一最终约定如下：

- 主进度轮询接口：`GET /api/v2/batches/{task_id}`
- 结果查看/排错补充接口：`GET /api/v2/batches/{task_id}/results`
- Wuying 新任务不再生成 `data/tasks/{task_id}/records.json`
- Wuying 最终业务结果保存到 `data/tasks/{task_id}/prompts/*.json`
- Wuying 单条记录和失败记录保存到 `data/tasks/{task_id}/raw/*.json`
- Wuying 不再生成 `data/batches/{task_id}/summary.json`
- Wuying 不再生成每个平台/每个 prompt 的嵌套 batch 结果文件
- callback 上传的 JSON 文件来自 `prompts/*.json`，结构仍是 record 数组

原因：

- 当前 `wuying` 实现里，`/api/v2/batches/{task_id}` 才是主状态接口
- 它包含：
  - `status`
  - `expected_batches`
  - `finished_batches`
  - `failed_batches`
  - `current_platform`
  - `current_repeat_index`
  - `current_prompt_index`
  - `current_prompt`
  - `records_path`，新任务为 `null`
  - `prompt_files`
- `/results` 更适合 GEO 做日志展示和排错，不适合作为唯一进度判断依据

因此 GEO 最终应按以下方式实现：

1. 轮询进度时调用 `GET /api/v2/batches/{task_id}`
2. 需要同步 step / device_result 细节时，可额外调用 `GET /api/v2/batches/{task_id}/results`
3. 任务最终完成判断不能只看 `/results`，仍要结合 callback 和 GEO 自己的状态机

### 2. 按平台和 Prompt 拆分结果文件和 Callback

Wuying 最终业务结果改为按平台和 prompt 拆分。

本地保存结构：

```text
data/tasks/{task_id}/status.json
data/tasks/{task_id}/raw/*.json
data/tasks/{task_id}/prompts/{年-月-日-时}-{platform}-p{prompt_index}-{prompt}.json
```

最终约定：

1. 新任务不再生成 `records.json`。
2. `raw/*.json` 是单设备单平台单 prompt 的记录，成功、失败、超时都会写入。
3. `prompts/*.json` 是最终业务结果，一个平台加一个 prompt 生成一个文件，包含该平台、该 prompt 下所有设备 records。
4. `/api/v2/batches/{task_id}` 和 `/api/v2/batches/{task_id}/results` 会返回 `prompt_files`。
5. callback 不再只上传一个 `records.json`。
6. callback 改为 multipart 多文件上传，多个文件都使用字段名 `files`。
7. 每个 callback 文件内容仍然是 record 数组，单条 record 结构不变。
8. 如果某个平台的某个 prompt 全部失败，Wuying 仍会上传该文件，文件内 records 的 `status` 为 `failed` 或 `timeout`。

GEO 需要改：

1. callback 接口支持同名字段 `files` 的多个 JSON 文件。
2. 不要依赖文件名 `records.json`，新任务不会生成它。
3. 每个 JSON 文件单独解析，文件内容按 record 数组处理。
4. 入库仍以单条 record 的 `platform_id`、`platform`、`device_id`、`query` 为准。
5. 审核队列建议按文件维度展示，一个文件对应一个平台和一个 prompt。

详细说明见：

```text
docs/按Prompt拆分结果与GEO回传约定.md
```

## 追加修改：实时进度推送与执行耗时

更新时间：2026-04-21

### 目标

1. GEO 后台任务列表和执行日志需要展示每次 run 的执行耗时。
2. GEO 后台需要尽量实时看到 Wuying 任务进行到哪个平台、哪个 prompt、哪个设备。
3. Wuying 不再只等最终 callback，而是在执行过程中主动向 GEO 推送进度事件。

### GEO 已做或需要保持的更改

1. 任务列表展示最近一次 run 的执行耗时。
2. 执行日志的 run 元信息展示执行耗时。
3. 打开执行日志时，前端每 3 秒刷新 run 明细。
4. 任务列表和统计卡片每 5 秒刷新一次。
5. 新增进度接收接口：

```text
POST /api/integrations/crawler/progress
```

请求头：

```text
x-api-key: {CRAWLER_CALLBACK_API_KEY}
Content-Type: application/json
```

6. GEO 收到 progress 后：
   - 根据 `run_id` 定位 `monitoring_task_runs`
   - 更新 run 的 `crawler_status / current_platform / current_prompt_index / current_repeat_index / expected_batches / finished_batches / failed_batches`
   - 同步 `steps`
   - 同步 `device_results`
   - 写入 run timeline event
   - 不直接入库业务指标，业务指标仍只在审核通过后入库

### Wuying 需要新增的更改

Wuying 在以下节点主动调用 GEO progress 接口：

1. 整个 batch 开始执行时。
2. 每个平台开始执行时。
3. 每个平台执行完成时。
4. 每个 prompt 开始执行时。
5. 每个 prompt 执行完成时。
6. 每个设备开始执行时。
7. 每个设备执行完成、失败、超时时。
8. 整个 batch 执行完成、失败、部分失败时。

Wuying progress 地址解析规则：

1. 优先读取请求 `env.progress_url / env.progressUrl / env.callback_progress_url / env.callbackProgressUrl`。
2. 其次读取服务端环境变量 `CRAWLER_PROGRESS_URL`。
3. 如果以上都没有，但存在 `CRAWLER_CALLBACK_URL=http://.../api/integrations/crawler/uploads`，则自动推导为 `http://.../api/integrations/crawler/progress`。
4. progress 鉴权优先读取 `env.progress_api_key / env.progressApiKey`，其次读取 `env.callback_api_key / env.callbackApiKey`，再读取 `CRAWLER_PROGRESS_API_KEY / CRAWLER_CALLBACK_API_KEY`。
5. progress 推送失败只记录 warning，不阻塞主爬虫任务。

### progress 请求体约定

最小字段：

```json
{
  "run_id": "geo-task-id:2026-04-21:13:1",
  "task_id": "geo-task-id",
  "crawler_task_id": "wuying-20260421132244-e4d26995",
  "trace_id": "wuying-20260421132244-e4d26995",
  "event_type": "device_finished",
  "message": "设备执行完成",
  "status": "running",
  "platform_ids": ["wuying-doubao"],
  "device_ids": ["北京", "上海", "杭州", "深圳", "杭州2"],
  "current_platform": "wuying-doubao",
  "current_repeat_index": 1,
  "current_prompt_index": 2,
  "expected_batches": 10,
  "finished_batches": 3,
  "failed_batches": 0
}
```

如果是设备级进度，应额外带 `record` 或 `records`：

```json
{
  "run_id": "geo-task-id:2026-04-21:13:1",
  "task_id": "geo-task-id",
  "event_type": "device_finished",
  "message": "上海设备执行完成",
  "status": "running",
  "record": {
    "platform_id": "wuying-doubao",
    "platform": "doubao",
    "device_id": "上海",
    "prompt_index": 2,
    "repeat_index": 1,
    "query": "用户问句",
    "status": "succeeded",
    "started_at": "2026-04-21T13:22:44+08:00",
    "finished_at": "2026-04-21T13:24:10+08:00",
    "result_path": "data/tasks/.../raw/xxx.json",
    "error": null
  }
}
```

如果是平台/prompt 级进度，可带 `platform_batches`：

```json
{
  "run_id": "geo-task-id:2026-04-21:13:1",
  "task_id": "geo-task-id",
  "event_type": "prompt_finished",
  "message": "Prompt 2 执行完成",
  "status": "running",
  "platform_batches": [
    {
      "platform_id": "wuying-doubao",
      "platform_name": "豆包（手机版）",
      "prompt_index": 2,
      "repeat_index": 1,
      "prompt": "用户问句",
      "device_ids": ["北京", "上海", "杭州", "深圳", "杭州2"],
      "status": "succeeded",
      "started_at": "2026-04-21T13:22:44+08:00",
      "finished_at": "2026-04-21T13:24:10+08:00",
      "output_path": "data/tasks/.../prompts/xxx.json"
    }
  ]
}
```

### 状态语义

`status` 建议使用：

- `pending`
- `running`
- `succeeded`
- `partial_failed`
- `failed`
- `timeout`

GEO 处理原则：

1. `running/pending` 只更新进度，不入库业务指标。
2. `failed/timeout` 可以把 run 标记为失败或超时。
3. `succeeded/partial_failed` 不代表业务已入库；仍要等最终 callback 文件进入审核队列。
4. 最终业务数据仍以 callback 文件和审核通过为准。

### 执行耗时口径

GEO 前端展示耗时的口径：

```text
finished_at - started_at
```

如果 `finished_at` 为空，但任务仍在执行中：

```text
当前时间 - started_at
```

因此 Wuying progress 中应尽量提供：

- `started_at`
- `finished_at`

设备级 record 也应提供各自的 `started_at / finished_at`，便于 GEO 展示每个设备的耗时。

## 追加修改：指标计算关键词由 GEO 按任务传入

更新时间：2026-04-24

### 背景

Wuying 在生成 `prompts/*.json` 时会计算并写入以下指标字段：

```json
{
  "提及率": 100,
  "前三率": 100,
  "置顶率": 0,
  "负面提及率": 0,
  "attitude": 92
}
```

这些指标不能只靠 prompt 自动推断，必须明确知道本次任务要检测的品牌词或关键词。

因此 GEO 调用 Wuying 时，需要在 batch 请求的 `env` 里传指标关键词。

### GEO 请求约定

GEO 调用：

```text
POST /api/v2/batches
```

请求体示例：

```json
{
  "platforms": ["doubao", "kimi"],
  "prompts": ["进口乳铁蛋白粉推荐"],
  "device_ids": ["北京", "上海", "杭州", "深圳", "杭州2"],
  "repeat": 1,
  "env": {
    "task_id": "geo-task-id",
    "run_id": "geo-task-id:2026-04-24:1:1",
    "callback_url": "http://geo-watcher-backend:3005/api/integrations/crawler/uploads",
    "callback_api_key": "xxx",
    "progress_url": "http://geo-watcher-backend:3005/api/integrations/crawler/progress",
    "progress_api_key": "xxx",
    "metric_keyword": "诺崔特",
    "metric_detect_type": "rank"
  }
}
```

### 字段含义

`metric_keyword`：

本次任务要检测的品牌词、产品词或核心关键词。Wuying 会用它判断每条回答是否提及目标，以及目标在回答中的推荐排序。

`metric_detect_type`：

检测模式。当前建议固定传：

```text
rank
```

`rank` 会计算：

- `提及率`
- `前三率`
- `置顶率`

如果 GEO 传入 `is_negative=true`，Wuying 会按负面任务口径计算：

- `提及率`：目标品牌被正常识别、正常提及且没有混淆的比例
- `负面提及率`：任一负面词被语义判定为“用于评价目标品牌本身”的比例
- `前三率 / 置顶率`：负面任务不适用，写为 `null`
- `attitude`：当前仍写为 `null`

负面词来自请求 `env.negative_words / env.negativeWords / env.metric_negative_words / env.metricNegativeWords / env.negative_keywords / env.negativeKeywords`，也可由服务端环境变量 `PIPELINE_NEGATIVE_WORDS / METRIC_NEGATIVE_WORDS` 提供。支持 JSON 数组或逗号/换行分隔字符串。

### Wuying 读取优先级

Wuying 当前读取指标关键词的优先级如下：

1. 请求 `env.metric_keyword`
2. 请求 `env.metricKeyword`
3. 请求 `env.keyword`
4. 请求 `env.target_keyword`
5. 请求 `env.targetKeyword`
6. 请求 `env.brand_keyword`
7. 请求 `env.brandKeyword`
8. 请求 `env.product_name`
9. 请求 `env.productName`
10. 服务端环境变量 `PIPELINE_METRIC_KEYWORD`
11. 服务端环境变量 `METRIC_KEYWORD`

最终建议 GEO 固定使用：

```json
{
  "env": {
    "metric_keyword": "诺崔特",
    "metric_detect_type": "rank",
    "is_negative": "false",
    "negative_words": ["难吃", "变质", "不新鲜", "口感差", "溢价高"]
  }
}
```

### LLM 配置

指标计算依赖 Wuying 服务端自己的 LLM 配置。GEO 默认不需要传 API Key。

Wuying 服务端 `.env` 需要配置：

```env
PIPELINE_LLM_API_KEY=xxx
PIPELINE_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
PIPELINE_LLM_MODEL=doubao-seed-1-6-lite-251015
```

如果某次任务确实要覆盖 Wuying 默认 LLM 配置，也可以在 `env` 中传：

```json
{
  "metric_api_key": "xxx",
  "metric_base_url": "https://ark.cn-beijing.volces.com/api/v3",
  "metric_model": "doubao-seed-1-6-lite-251015"
}
```

但默认不建议 GEO 传这些字段，避免把 Wuying 的模型配置耦合到 GEO。

### 未传关键词时的行为

如果 GEO 没传 `metric_keyword`，且 Wuying 服务端也没有配置 `PIPELINE_METRIC_KEYWORD`：

1. Wuying 仍正常执行爬虫。
2. Wuying 不会调用 LLM 指标计算。
3. `prompts/*.json` 中指标字段会写为 `null`。
4. 任务不会因为缺少指标关键词而失败。

### Callback 文件中的结果

最终 callback 上传的每个 `prompts/*.json` 文件内，只在文件顶层写入一组综合指标值。

原因是一个 `prompts/*.json` 文件对应：

```text
一个平台 + 一个 prompt + 多台设备结果
```

所以指标计算口径也是这个文件整体的一组聚合指标，而不是每台设备单独一组指标。

文件结构示例：

```json
{
  "platform_id": "wuying-doubao",
  "platform": "doubao",
  "query": "进口乳铁蛋白粉推荐",
  "prompt": "进口乳铁蛋白粉推荐",
  "prompt_index": 1,
  "repeat_indexes": [1],
  "record_count": 5,
  "records": [
    {
      "platform_id": "wuying-doubao",
      "platform": "doubao",
      "device_id": "杭州",
      "query": "进口乳铁蛋白粉推荐",
      "response": "单台设备回答正文",
      "references": {},
      "raw_output_path": "data/tasks/xxx/raw/doubao_杭州_p001_r001.json",
      "status": "succeeded"
    }
  ],
  "提及率": 100,
  "前三率": 100,
  "置顶率": 0,
  "负面提及率": 0,
  "attitude": 92
}
```

`records[]` 内不再写入 `提及率 / 前三率 / 置顶率 / 负面提及率 / attitude`。

## 追加修改：执行调度与失败回补口径

更新时间：2026-04-25

当前 Wuying 执行约定补充如下：

1. API 服务不再使用单个全局串行任务队列；设备集合不重叠的 batch 可以并发执行。
2. 同一台设备同一时刻仍只能被一个任务预约或租用；如果目标设备已预约或已租用，提交时返回 `409 Conflict`。
3. 设备租约只在任务执行线程开始时落盘，避免任务长时间 pending 时制造“设备正在执行”的假象。
4. `CRAWLER_BATCH_MAX_WORKERS` 控制同一平台 + prompt 下的设备并发上限。
5. `CRAWLER_BATCH_TIMEOUT_SECONDS` 会参与单设备执行和失败回补的剩余时间计算，不再只在 prompt 开始前检查。
6. 默认 `CRAWLER_FAILED_RECORD_RETRY_COUNT=0`，失败、超时、空响应不会被静默重跑；如显式启用回补，每次 attempt 都会保存到 `raw/*.json`。
7. `raw/*.json` 文件名包含 attempt 序号，例如 `_a001/_a002`；`prompts/*.json` 只聚合每台设备最终 attempt。
8. worker 状态中的 `idle` 只表示 worker 进程空闲，是否已完成 driver 初始化应看 `driver_ready` / `ready_for_task`。

## 追加修改：停止执行与 Wuying 队列取消

更新时间：2026-04-29

### 背景

GEO 后台已经新增 Wuying 停止执行能力：

1. 任务管理页面可以停止正在排队、已提交、执行中的 Wuying 后台任务。
2. 售前诊断页面可以停止正在排队、已提交、执行中的 Wuying 诊断任务。
3. 后台新增 `Wuying 队列` 菜单，可以统一查看后台任务和售前诊断的 Wuying 队列，并支持置顶、上调、下调、取消。

但 GEO 只能管理自己的本地队列状态。对于已经提交到 Wuying 的 batch，真正停止手机执行、释放设备、终止 worker，必须由 Wuying 提供 cancel 接口并在 Wuying 进程内处理。

### GEO 已实现的调用约定

GEO 新增配置：

```env
WUYING_BATCH_CANCEL_URL={WUYING_CRAWLER_BASE_URL}/api/v2/batches/{task_id}/cancel
WUYING_BATCH_CANCEL_TIMEOUT_SECONDS=15
```

当 GEO 取消 Wuying run 时：

1. 如果 run 还只是 GEO 本地 `queued`，没有 `crawler_task_id`：
   - GEO 只在本地标记 `cancelled`
   - 不调用 Wuying
2. 如果 run 已经 `submitted` 或 `running`，并且有 `crawler_task_id`：
   - GEO 调用：

```text
POST /api/v2/batches/{task_id}/cancel
```

请求头：

```text
x-api-key: {SCRAPER_API_KEY}
Content-Type: application/json
```

请求体：

```json
{
  "task_id": "wuying-20260429071208-c656077b"
}
```

3. 只有 Wuying cancel 返回 `2xx` 时，GEO 才会：
   - 标记 run 为 `cancelled`
   - 写入取消日志
   - 本地释放 Wuying 全局资源锁
   - 忽略之后可能迟到的 progress/upload callback
4. 如果 Wuying cancel 返回非 `2xx` 或请求失败：
   - GEO 不会本地标记取消成功
   - GEO 不会释放 Wuying 全局资源锁
   - 前端提示取消失败
   - 目的是避免 Wuying 实际仍在操作手机时，GEO 又启动下一个手机任务

### Wuying 必须新增的接口

Wuying 需要实现：

```text
POST /api/v2/batches/{task_id}/cancel
```

鉴权方式和现有 batch 接口一致：

```text
x-api-key: {SCRAPER_API_KEY}
```

请求体可以为空，也可以接收：

```json
{
  "task_id": "wuying-20260429071208-c656077b"
}
```

Wuying 返回示例：

```json
{
  "task_id": "wuying-20260429071208-c656077b",
  "status": "cancelled",
  "cancelled": true,
  "message": "batch cancelled"
}
```

### Wuying cancel 语义

Wuying 收到 cancel 后必须做到：

1. 如果 batch 还在 pending/排队：
   - 从 Wuying 内部待执行队列移除
   - 状态改为 `cancelled`
   - 不再执行设备任务
2. 如果 batch 正在 running：
   - 设置 batch 级取消标记
   - 正在等待的后续平台、prompt、设备任务不再启动
   - 已经启动的设备任务应尽快中断或在当前安全点退出
   - 释放设备租约
   - 状态改为 `cancelled` 或 `cancelling -> cancelled`
3. 如果 batch 已经 succeeded/failed/timeout/cancelled：
   - cancel 接口应幂等
   - 可以返回 `200 OK`
   - 返回当前最终状态，不应报 500
4. 如果 `task_id` 不存在：
   - 返回 `404 Not Found`
5. 如果 API Key 不正确：
   - 返回 `401 Unauthorized`

### Wuying 状态接口需要同步支持 cancelled

`GET /api/v2/batches/{task_id}` 返回的 `status` 需要支持：

```text
cancelled
```

建议返回：

```json
{
  "task_id": "wuying-20260429071208-c656077b",
  "status": "cancelled",
  "finished_batches": 1,
  "failed_batches": 0,
  "expected_batches": 5,
  "error": "cancelled by GEO"
}
```

`GET /api/v2/batches/{task_id}/results` 对 cancelled 的处理：

1. 如果已有部分 raw/prompts 结果，可以正常返回已有结果。
2. 如果没有任何结果，返回空 records，不要 500。
3. `status` 返回 `cancelled`。

### progress 推送约定

Wuying 收到 cancel 后建议向 GEO progress 接口推送一次取消事件：

```text
POST /api/integrations/crawler/progress
```

请求体：

```json
{
  "run_id": "GEO传入的env.run_id",
  "task_id": "GEO任务ID",
  "crawler_task_id": "wuying-20260429071208-c656077b",
  "trace_id": "wuying-20260429071208-c656077b",
  "event_type": "batch_cancelled",
  "status": "cancelled",
  "message": "Wuying batch cancelled by GEO"
}
```

注意：GEO 当前已经会忽略本地状态为 `cancelled` 的迟到 progress/upload，因此 Wuying 即使在取消后仍有旧进度到达，也不会把 GEO 状态改回执行中。

### callback 约定

取消后的 callback 规则：

1. 如果取消时没有生成可审核业务结果：
   - Wuying 不需要 callback upload
2. 如果取消时已经生成了部分结果：
   - 默认不建议再 callback 到审核队列，避免用户以为这是完整结果
   - 如确实要回传部分结果，必须在 payload 顶层或 record 中明确：

```json
{
  "status": "cancelled",
  "partial": true,
  "cancelled": true
}
```

GEO 当前主要按 run 状态处理：本地已取消的 run 收到 upload 会被忽略。

### 双方最终责任边界

GEO 负责：

1. 展示停止按钮。
2. 管理本地 Wuying 队列。
3. 对本地 queued 任务直接取消。
4. 对 submitted/running 任务调用 Wuying cancel。
5. Wuying cancel 成功后本地标记 cancelled 并释放 GEO 侧锁。
6. Wuying cancel 失败时保留当前状态和锁，避免误启动下一个任务。

Wuying 负责：

1. 实现 `POST /api/v2/batches/{task_id}/cancel`。
2. 维护 batch 的 cancelled 状态。
3. 停止未开始的后续平台、prompt、设备执行。
4. 尽快中断或安全退出已启动设备任务。
5. 释放 Wuying 内部设备租约。
6. status/results 接口正确返回 cancelled 状态。
7. cancel 接口幂等，已完成或已取消任务不应报 500。

### 一句话约定

停止执行不是 GEO 单方面能完成的能力：

- GEO 负责发起取消、更新本地队列和锁。
- Wuying 必须负责真正停止 batch、释放设备、返回 cancelled 状态。

## 追加修改：售前诊断回调与正式监控任务回调分离

更新时间：2026-04-30

### 背景

售前诊断调用 Wuying 执行后，结果只是给售前诊断页面查看和生成统计，不进入 GEO 正式监控任务的审核队列。

之前售前诊断仍把 `callback_url` 传成：

```text
/api/integrations/crawler/uploads
```

这个入口是正式监控任务入口，会要求 `user_id/product_id/keyword_id/monitor_date` 等任务字段齐全，并会把结果写入审核队列。售前诊断回传不应该走这个入口，否则会出现 `400 Bad Request`。

### GEO 已调整

GEO 售前诊断提交 Wuying batch 时，`env` 中会传入：

```json
{
  "source_type": "presales_diagnostic",
  "business_id": "GEO售前诊断ID",
  "diagnostic_id": "GEO售前诊断ID",
  "input_id": "GEO售前输入行ID",
  "run_id": "GEO售前诊断run_id",
  "task_id": "GEO售前诊断run_id",
  "callback_url": "http://geo-watcher-backend:3005/api/integrations/presales/uploads",
  "callback_api_key": "xxx",
  "progress_url": "http://geo-watcher-backend:3005/api/integrations/presales/progress",
  "progress_api_key": "xxx"
}
```

说明：

1. `callback_url` 从 `CRAWLER_CALLBACK_URL` 的同一 host 自动推导，不需要新增 `PRESALES_CALLBACK_URL`。
2. `progress_url` 同理自动推导，不需要新增 `PRESALES_PROGRESS_URL`。
3. 售前诊断结果不会进入 `crawler_upload_reviews` 审核队列。
4. GEO 仍兼容旧入口：如果 Wuying 误发到 `/api/integrations/crawler/uploads`，只要能通过 `run_id/crawler_task_id/trace_id` 识别为售前诊断，也会分流到售前诊断处理。但这只是兜底，不是推荐做法。

### Wuying 必须配合

Wuying 对售前诊断 batch 必须严格使用 GEO 请求 `env` 中的地址：

1. 上传最终结果时使用：

```text
POST {env.callback_url}
```

也就是：

```text
POST /api/integrations/presales/uploads
```

2. 推送执行进度时使用：

```text
POST {env.progress_url}
```

也就是：

```text
POST /api/integrations/presales/progress
```

3. 不要把售前诊断结果强制发到全局 `CRAWLER_CALLBACK_URL` 或 `/api/integrations/crawler/uploads`。
4. multipart form 建议字段：

```text
run_id={env.run_id}
source_type=presales_diagnostic
diagnostic_id={env.diagnostic_id}
input_id={env.input_id}
files=<json files>
```

5. 请求头：

```text
x-api-key: {env.callback_api_key}
```

6. 如果 Wuying 内部只能拿到自己的 `task_id`，GEO 也会尝试用 `crawler_task_id/trace_id` 查找售前 run；但标准做法仍然是回传 `run_id={env.run_id}`。

### 正式监控任务与售前诊断的区别

| 场景 | callback_url | progress_url | GEO处理 |
| --- | --- | --- | --- |
| 正式监控任务 | `/api/integrations/crawler/uploads` | `/api/integrations/crawler/progress` | 进入审核队列，审核通过后写指标 |
| 售前诊断 | `/api/integrations/presales/uploads` | `/api/integrations/presales/progress` | 写入售前诊断 run，只给诊断页面/统计使用 |

### 排错规则

如果售前诊断执行完成后 Wuying 日志出现：

```text
POST http://geo-watcher-backend:3005/api/integrations/crawler/uploads HTTP/1.1 400 Bad Request
```

说明 Wuying 仍在把售前诊断发到正式监控任务入口，需要检查：

1. 是否读取并使用了 `env.callback_url`。
2. 是否错误使用了全局 `CRAWLER_CALLBACK_URL`。
3. multipart form 是否带了 `source_type=presales_diagnostic`。
4. `run_id` 是否优先使用 `env.run_id`。
