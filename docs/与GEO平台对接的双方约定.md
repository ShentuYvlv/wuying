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
  "output_file": "data/tasks/20260418_xxx/records.json",
  "records_path": "data/tasks/20260418_xxx/records.json"
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
- `records_path`
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
  "records_path": "data/tasks/wuying-xxx/records.json",
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
4. 同一个 callback 文件允许包含：
   - 多个平台
   - 同平台同 query 的多设备结果

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

- 如果整批任务没有任何成功 record，wuying 可以跳过 callback
- 因此 GEO 不能假设“所有 submitted 任务最终一定会收到 callback”

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
- Wuying 本地任务结果统一保存到 `data/tasks/{task_id}/records.json`
- Wuying 不再生成 `data/batches/{task_id}/summary.json`
- Wuying 不再生成每个平台/每个 prompt 的嵌套 batch 结果文件
- callback 上传的 JSON 文件内容来自成功 records，结构与 `records.json` 的单条 record 保持一致

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
  - `records_path`
- `/results` 更适合 GEO 做日志展示和排错，不适合作为唯一进度判断依据

因此 GEO 最终应按以下方式实现：

1. 轮询进度时调用 `GET /api/v2/batches/{task_id}`
2. 需要同步 step / device_result 细节时，可额外调用 `GET /api/v2/batches/{task_id}/results`
3. 任务最终完成判断不能只看 `/results`，仍要结合 callback 和 GEO 自己的状态机
