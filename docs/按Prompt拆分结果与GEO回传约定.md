# 按平台和 Prompt 拆分结果与 GEO 回传约定

日期：2026-04-21

## 改动结论

新任务不再生成 `records.json` 文件。

最终结果按平台和 prompt 拆分：

```text
data/tasks/{task_id}/status.json
data/tasks/{task_id}/raw/*.json
data/tasks/{task_id}/prompts/{年-月-日-时}-{platform}-p{prompt_index}-{prompt}.json
```

## 文件职责

### status.json

任务状态文件。

包含：

- 任务状态
- 进度计数
- 当前执行位置
- `prompt_files`
- callback 状态

不保存完整 records。

### raw/*.json

单设备、单平台、单 prompt 的记录文件。

成功、失败、超时都会写入这里。
如果启用了失败回补，每一次 attempt 都会单独写入，文件名包含 `_a001/_a002` 等 attempt 序号。

用途：

- `/api/v2/batches/{task_id}/results` 聚合读取
- 本地排查
- `raw_output_path` 指向这里
- `attempt_index` 和 `is_final_attempt` 用于区分原始失败尝试与最终采用的尝试

### prompts/*.json

最终业务回传文件。

每个文件对应一个平台和一个 prompt，内容是该平台、该 prompt 下所有设备 records。
如果启用了失败回补，这里只包含每台设备最终采用的 attempt；完整 attempt 历史仍以 `raw/*.json` 为准。

如果一个任务包含：

- 3 个平台
- 3 台设备
- 3 个 prompt

则最多生成 9 个结果文件。

每个结果文件最多包含：

```text
3 台设备 = 3 条 record
```

成功、失败、超时 record 都会进入对应平台和 prompt 文件；同时每条记录也会保留在 `raw/*.json`。

## 文件名规则

```text
{YYYYMMDDTHHMMSSZ}-{platform}-p{prompt_index}-{prompt}.json
```

示例：

```text
2026-04-21-15-doubao-p001-牛奶推荐.json
2026-04-21-15-kimi-p001-牛奶推荐.json
2026-04-21-15-deepseek-p002-洗地机推荐.json
```

`platform` 和 `prompt` 会做文件名安全清理，`prompt` 超长会截断。

## Wuying API 变化

任务提交接口不变：

```text
POST /api/v2/batches
```

主状态接口不变：

```text
GET /api/v2/batches/{task_id}
```

结果接口不变：

```text
GET /api/v2/batches/{task_id}/results
```

状态和结果返回中：

- `records_path` 固定为 `null`
- `output_file` 指向 `data/tasks/{task_id}/prompts`
- 新增或保留 `prompt_files`

示例：

```json
{
  "records_path": null,
  "output_file": "data/tasks/wuying-xxx/prompts",
  "prompt_files": [
    {
      "platform": "doubao",
      "platform_id": "wuying-doubao",
      "prompt_index": 1,
      "prompt": "牛奶推荐",
      "path": "data/tasks/wuying-xxx/prompts/2026-04-21-15-doubao-p001-牛奶推荐.json",
      "record_count": 3
    }
  ]
}
```

## Callback 变化

Callback URL 不变：

```text
POST {callback_url}
```

鉴权不变：

```text
x-api-key: {callback_api_key}
```

表单字段新增：

```text
file_count
```

文件上传为 multipart 多文件：

```text
files=2026-04-21-15-doubao-p001-牛奶推荐.json
files=2026-04-21-15-kimi-p001-牛奶推荐.json
files=2026-04-21-15-deepseek-p002-洗地机推荐.json
```

多个文件使用同一个字段名 `files`。

## Callback 文件内容

每个文件内容是一个对象。

对象顶层表示：

- 当前平台
- 当前 prompt
- 当前平台 + prompt 下的所有设备 records
- 当前平台 + prompt 汇总后的唯一一组指标

`raw/*.json` 是原始设备结果，不包含 `提及率 / 前三率 / 置顶率 / 负面提及率 / attitude`。

`prompts/*.json` 是最终回传结果，写入前在文件顶层统一补充上述指标字段。

Callback 文件结构：

```json
{
  "platform_id": "wuying-doubao",
  "platform": "doubao",
  "query": "牛奶推荐",
  "prompt": "牛奶推荐",
  "prompt_index": 1,
  "repeat_indexes": [1],
  "record_count": 3,
  "records": [
    {
      "platform_id": "wuying-doubao",
      "platform": "doubao",
      "device_id": "杭州",
      "instance_id": "acp-xxx",
      "adb_endpoint": "1.2.3.4:100",
      "query": "牛奶推荐",
      "prompt": "牛奶推荐",
      "prompt_index": 1,
      "repeat_index": 1,
      "response": "AI 回答正文",
      "references": {
        "summary": null,
        "keywords": [],
        "items": []
      },
      "raw_output_path": "data/tasks/wuying-xxx/raw/doubao_杭州_p001_r001.json",
      "status": "succeeded",
      "error": null,
      "started_at": "2026-04-21T06:15:30+00:00",
      "finished_at": "2026-04-21T06:16:20+00:00",
      "platform_extra": {}
    }
  ],
  "提及率": 100,
  "前三率": 100,
  "置顶率": 0,
  "负面提及率": 0,
  "attitude": 92
}
```

`records[]` 内不写入 `提及率 / 前三率 / 置顶率 / 负面提及率 / attitude`。

## GEO 需要修改的点

1. callback 接口支持同名字段 `files` 下的多个 JSON 文件。
2. 不再读取或依赖 `records.json`。
3. 不再依赖 `records_path`，该字段新任务为 `null`。
4. 每个 JSON 文件单独解析，文件内容是对象，设备结果在 `records[]`。
5. 指标字段只读取文件顶层的 `提及率`、`前三率`、`置顶率`、`负面提及率`、`attitude`。
6. 每条 record 仍然使用自己的 `platform_id`、`platform`、`device_id`、`query` 入库。
7. 审核队列建议按文件维度展示，一个文件对应一个平台和一个 prompt。
8. 如果某个平台的某个 prompt 全部失败，Wuying 仍会上传该文件，文件内 records 的 `status` 为 `failed` 或 `timeout`。

## 兼容说明

旧任务如果已经存在 `records.json`，`/results` 可以兼容读取。

新任务不会创建 `records.json`。
