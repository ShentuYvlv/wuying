# 跨请求常驻 Worker 多手机并发方案

## 目标

把当前执行模式升级为常驻调度服务：

- 多台云手机可以并发执行。
- 同一台云手机同一时间只执行一个 App 任务。
- 同一次任务内按平台顺序执行，例如先跑所有手机的 `doubao`，再跑所有手机的 `kimi`。
- 跨 API 请求复用同一台手机的 ADB 连接和 `uiautomator2` driver。
- 任务超时或卡死时可以 kill 对应手机 worker 并自动重启。

## 当前状态

当前批处理已经支持：

- 同一平台下多手机并发。
- 同一台手机内串行执行。
- 平台顺序执行，例如 `deepseek,kimi,yuanbao` 会按顺序跑。

当前不足：

- 每个平台/每条记录会启动独立子进程。
- 每个平台都会重新 `adb connect` 检查。
- 每个平台都会重新初始化 `U2Driver(serial)`。
- API 多请求之间不会复用设备会话。
- App 切换前后存在重复初始化成本。

## 最终架构

```text
FastAPI 主进程
  -> WorkerManager
      -> 杭州 DeviceWorker 进程
      -> 上海 DeviceWorker 进程
      -> 北京 DeviceWorker 进程
      -> ...
  -> TaskScheduler
      -> 接收 CLI/API 任务
      -> 按平台顺序调度
      -> 同一平台下多手机并发
      -> 同一手机内串行
```

每台手机一个常驻 worker：

```text
DeviceWorker
  启动：
    adb connect 一次
    U2Driver 初始化一次
    创建 DeviceSession

  收到任务：
    platform + prompt
    复用已有 driver 执行
    返回 result

  异常：
    先 reset uiautomator2
    失败则退出，由 WorkerManager 重启
```

## 执行规则

请求示例：

```text
platforms = deepseek,kimi,yuanbao
devices = 杭州,上海,北京
prompt = 上海天气
```

执行顺序：

```text
1. 杭州、上海、北京同时执行 deepseek
2. 等所有 deepseek 结束或超时
3. 写 deepseek 的 batch json
4. 杭州、上海、北京同时执行 kimi
5. 等所有 kimi 结束或超时
6. 写 kimi 的 batch json
7. 杭州、上海、北京同时执行 yuanbao
8. 写 yuanbao 的 batch json
```

约束：

- 同一台手机不能并发执行两个平台。
- 不同手机可以并发执行同一个平台。
- 如果某台手机超时，只 kill 这台手机的 worker，不影响其他手机。
- 超时手机当前记录标记为 `timeout`，worker 重启后可以继续后续平台。

## 新增模块

### `DeviceSession`

路径：

```text
src/wuying/application/device_session.py
```

职责：

- 持有单台手机的长期连接状态。
- 管理 `AdbClient`、`serial`、`U2Driver`。
- 提供 driver 重置能力。

核心字段：

```text
device_id
instance_id
adb_endpoint
adb_client
serial
driver
```

核心方法：

```text
connect()
ensure_driver()
reset_driver()
restart_app(package)
close()
```

### `DeviceWorker`

路径：

```text
src/wuying/application/device_worker.py
```

职责：

- 每台手机一个独立进程。
- 持有一个 `DeviceSession`。
- 从主进程接收任务。
- 执行平台 workflow。
- 返回成功/失败/超时前的错误信息。

通信建议：

```text
multiprocessing.Queue
```

任务消息：

```json
{
  "type": "run",
  "task_id": "xxx",
  "record_id": "xxx",
  "platform": "kimi",
  "prompt": "上海天气",
  "save_result": false
}
```

返回消息：

```json
{
  "type": "result",
  "record_id": "xxx",
  "status": "succeeded",
  "result": {}
}
```

### `WorkerManager`

路径：

```text
src/wuying/application/worker_manager.py
```

职责：

- 服务启动时为所有 enabled 设备启动 worker。
- 维护 worker 状态。
- 分发任务到指定设备。
- 处理超时、kill、重启。
- 提供健康状态。

状态：

```text
idle
running
failed
restarting
dead
```

核心方法：

```text
start_all()
stop_all()
ensure_worker(device_id)
run_on_device(device_id, platform, prompt, timeout)
restart_worker(device_id)
get_status()
```

### `TaskScheduler`

路径：

```text
src/wuying/application/task_scheduler.py
```

职责：

- 接收一个批量任务。
- 按平台、repeat、prompt 顺序调度。
- 每个平台下并发调用多个设备 worker。
- 聚合结果并写 batch json。

调度伪代码：

```python
for platform in platforms:
    for repeat_index in repeat:
        for prompt_index, prompt in prompts:
            results = run_selected_devices_concurrently(platform, prompt)
            write_platform_batch_json(platform, prompt, results)
```

## Workflow 改造

当前 `ChatAppWorkflow.run_once()` 自己做连接：

```text
resolve endpoint
adb connect
U2Driver(serial)
执行平台
```

需要拆成：

```text
run_once()
  兼容旧 CLI/API 单次模式
  内部创建临时 DeviceSession

run_once_with_session(session)
  常驻 worker 使用
  复用 session.driver

_run_with_driver(driver, serial, ...)
  真正的平台执行逻辑
```

改造目标：

- 平台文件尽量不改。
- `doubao/kimi/deepseek/qianwen/yuanbao` 继续使用原来的 `_ensure_new_chat_session()`、`_set_prompt_text()`、`_send_prompt()`。
- 只把连接和 driver 生命周期从 workflow 中抽出去。

## API 改造

API 服务启动时：

```text
加载 device_pool
启动 WorkerManager
为 enabled=true 的设备启动 worker
```

任务接口：

```text
POST /api/v1/tasks/wuying-doubao
POST /api/v1/tasks/wuying-kimi
POST /api/v1/tasks/wuying-batch
```

单平台请求内部也走 scheduler：

```json
{
  "platforms": ["doubao"],
  "devices": ["杭州", "上海"],
  "prompts": ["上海天气"]
}
```

多平台请求：

```json
{
  "platforms": ["deepseek", "kimi", "yuanbao"],
  "devices": ["杭州", "上海"],
  "prompts": ["上海天气", "北京天气"]
}
```

状态接口：

```text
GET /api/v1/workers
GET /api/v1/workers/{device_id}
POST /api/v1/workers/{device_id}/restart
```

## 超时和恢复

单记录超时：

```text
主进程等待 worker 返回
超过 record_timeout_seconds
kill 当前 device worker
标记该设备该记录 timeout
重启 worker
继续后续任务
```

worker 异常：

```text
当前任务 failed
WorkerManager 检测到 worker dead
自动重启
```

uiautomator2 异常：

```text
worker 内先 reset_driver()
当前任务可重试一次
仍失败则返回 failed
```

ADB 断线：

```text
session.connect() 重新 adb connect
重新初始化 U2Driver
```

## 设备占用策略

API 多请求同时进入时：

- 同一台设备只允许一个任务占用。
- 如果设备 busy，可以排队。
- 第一版建议全局队列，按提交顺序执行，避免多个 API 请求互相抢设备。

队列行为：

```text
request A 占用 杭州,上海
request B 也需要 杭州
request B 等待杭州释放
```

## 输出结构

继续沿用当前 batch 输出：

```text
data/batches/{task_id}/{platform}/repeat_001_prompt_001.json
data/batches/{task_id}/summary.json
```

每个平台文件仍保存多设备结果：

```json
{
  "platform": "kimi",
  "prompt": "上海天气",
  "device_ids": ["杭州", "上海"],
  "status": "succeeded",
  "results": []
}
```

## CLI 行为

`run.py app` 也走同一套 scheduler。

本地 CLI 启动时：

```text
创建临时 WorkerManager
启动本次任务需要的设备 worker
执行任务
任务结束 stop_all
```

API 服务启动时：

```text
创建全局 WorkerManager
跨请求常驻
服务退出时 stop_all
```

## 实施步骤

1. 新增 `DeviceSession`，把 ADB connect 和 U2Driver 生命周期封装进去。
2. 改 `ChatAppWorkflow`，增加 `run_once_with_session()` 和 `_run_with_driver()`。
3. 新增 `DeviceWorker`，实现单设备常驻进程和队列通信。
4. 新增 `WorkerManager`，实现 worker 启动、状态、任务发送、超时 kill、重启。
5. 新增 `TaskScheduler`，把当前 batch runner 的平台顺序和多设备并发迁移过来。
6. 改 `batch_runner.py`，让 CLI 批处理走 `TaskScheduler`。
7. 改 API 层，服务启动时创建全局 `WorkerManager`。
8. 增加 worker 状态接口和手动重启接口。
9. 保留旧 `run_platform_once_with_timeout()` 一段时间作为 fallback。
10. 稳定后删除旧的“每记录一个子进程”执行路径。

## 验收标准

- 单设备多平台执行时，只初始化一次 `U2Driver`。
- 多设备同平台执行时，设备并发。
- 同设备不会并发执行两个平台。
- 一个设备任务超时后，只影响该设备，不影响其他设备。
- API 连续请求之间，worker 不退出，设备连接复用。
- worker 异常退出后可以自动重启。
- 输出 JSON 结构不变。
