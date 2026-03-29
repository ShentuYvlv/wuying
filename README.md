# 无影云手机自动化项目方案

## 目标

基于阿里云无影云手机，构建一个可批量调度的 Android 自动化系统，用于：

- 启动和管理云手机实例
- 安装和维护目标 App
- 通过 ADB 接入设备
- 使用 Android 自动化框架执行 UI 操作
- 采集执行结果并回传到服务端

当前阶段的首要目标不是做“网页里输入一句话让 AI 帮你控机”，而是做一套可编排、可批量、可恢复的程序化控机链路。

## 官方文档确认过的能力边界

根据无影云手机官方文档，目前可以确认：

- 支持 OpenAPI/SDK，用于实例组、实例、ADB、应用、文件、命令、截图等管控能力
- 支持 ADB 接入，且区分共享网络 / VPC 网络；VPC 网络支持一键 ADB
- 支持 ADB 密钥对创建、导入、绑定
- 支持应用创建 `CreateApp` 和批量安装 `InstallApp`
- 支持远程命令 `RunCommand`
- 支持文件下发 `SendFile` 与文件回传 `FetchFile`
- 支持客户端串流接入，核心入口是 `BatchGetAcpConnectionTicket` + Web SDK / Android SDK

同时也能确认：

- 官方文档没有把 Appium 或 UIAutomator 当成无影云手机的一等能力来提供
- 这意味着 UI 自动化层需要我们自己接在标准 ADB 设备之上

结论：无影提供的是“资源层 + 管控层 + 连接层 + 串流层”，不是直接替你完成 Android UI 自动化。

## 推荐技术路线

### 总体架构

1. 无影 OpenAPI 负责控制面
2. ADB 负责设备接入
3. `uiautomator2` 负责 Android UI 自动化
4. Python 服务负责任务调度、重试、日志和结果落库

推荐首版采用：

- Python 3.11+
- 阿里云无影云手机 OpenAPI SDK
- `adb`
- `uiautomator2`
- `SQLAlchemy` 或 SQLite/PostgreSQL 作为任务存储

不推荐首版直接上 Appium，原因：

- 需要额外维护 Appium Server
- 会话管理更重
- 对你当前“先跑通一台，再扩多台”的目标并不划算

## 各能力在系统中的职责

### 1. OpenAPI

用于服务端管控，不负责页面级自动化。

适合承载的动作：

- 创建/查询实例组与实例
- 启停实例
- 开启/关闭 ADB
- 查询 ADB 地址信息
- 创建/绑定 ADB 密钥
- 创建应用、批量安装应用
- 远程执行少量系统命令
- 文件上传/下载
- 截图、任务查询、失败诊断

### 2. ADB

ADB 是把云手机变成“标准 Android 设备”的关键接入层。

我们真正的自动化执行会建立在 ADB 之上，例如：

- 启动 Activity
- 安装/卸载 apk
- 读取系统属性
- 推送辅助文件
- 为 UI 自动化框架建立控制通道

### 3. UI 自动化层

这层建议优先使用 `uiautomator2`。

适合当前场景的原因：

- 直接基于 Android 设备工作
- 对控件查找、点击、输入、取文本更直接
- 不需要单独维护 Appium Server

只有在以下情况下再考虑 Appium：

- 你已经有成熟的 Appium 测试资产
- 你明确需要 WebDriver 生态
- 你需要多端统一自动化接口

## 对官方能力的工程判断

### ADB 接入

这是主入口，必须采用。

- 共享网络：仅私网 ADB
- VPC 网络：支持一键 ADB，官方推荐
- 公网 ADB：需要 NAT / DNAT 和安全组配置

所以项目落地前，第一件事不是写脚本，而是确认实例组网络类型。

### OpenAPI / SDK

这是控制面主入口，必须采用。

建议全部通过 SDK 调用，不做自签名直连。

原因：

- 官方已明确提供 SDK
- OpenAPI Explorer 可直接生成 SDK 示例
- 官方明确提示自签名复杂，建议只在必要时再做

### 批量命令

`RunCommand` 可以用，但不要把它当主自动化框架。

适合：

- 运行快速系统命令
- 拉起辅助脚本
- 做批量环境检查
- 获取轻量结果

不适合：

- 复杂页面操作
- 稳定控件定位
- 长流程 UI 编排

### Appium / UIAutomator

官方文档里没有找到无影对 Appium / UIAutomator 的专门产品级支持说明。

这里的合理推断是：

- 无影不直接提供 Appium 服务
- 也不替你托管 UIAutomator 测试框架
- 但由于官方明确支持标准 ADB 连接，所以理论上可以把云手机作为标准 Android 自动化目标设备来使用

因此首版结论是：

- `UIAutomator/uiautomator2`：推荐
- `Appium`：可选，不作为 MVP 首选

## MVP 方案

第一版只做单机链路跑通，暂不追求批量。

### MVP 功能

1. 读取配置中的实例 ID
2. 通过 OpenAPI 检查实例状态
3. 绑定 ADB 密钥并开启 ADB
4. 获取 ADB 地址并连接
5. 启动目标 App
6. 通过 `uiautomator2` 完成输入、点击、等待、读取文本
7. 把结果保存到本地数据库或 JSONL 日志

### MVP 不做

- 不做 Appium
- 不做网页端串流控制台
- 不做复杂分布式调度
- 不做全量监控平台

## 第二阶段

在单机稳定后，扩展为多机调度：

- 批量实例发现
- 队列派发
- 并发执行
- 失败重试
- 截图留档
- 文件回传
- 应用批量安装
- 镜像固化

## 第三阶段

视业务需要再增加：

- Web 控制台
- 设备池管理
- 多账号隔离
- 指标监控与告警
- 自定义镜像
- Agent 化任务编排

## 建议的项目目录

```text
wuying/
├─ README.md
├─ requirements.txt
├─ src/
│  ├─ config.py
│  ├─ aliyun_api/
│  │  ├─ client.py
│  │  ├─ instances.py
│  │  ├─ adb.py
│  │  ├─ apps.py
│  │  ├─ commands.py
│  │  └─ files.py
│  ├─ device/
│  │  ├─ adb_connect.py
│  │  ├─ u2_driver.py
│  │  └─ app_launch.py
│  ├─ workflows/
│  │  └─ ask_app_and_collect.py
│  ├─ scheduler/
│  │  ├─ queue.py
│  │  └─ runner.py
│  └─ storage/
│     ├─ models.py
│     └─ repository.py
└─ scripts/
   ├─ bootstrap_adb.py
   └─ run_single_task.py
```

## 实施顺序

### 阶段 1：环境打通

- 确认实例组网络类型
- 准备 AccessKey
- 创建/导入 ADB 密钥并绑定实例
- 本地安装 adb
- 跑通 `adb connect`

### 阶段 2：单机自动化

- 建立 Python 项目
- 封装无影 OpenAPI 客户端
- 封装 ADB 连接逻辑
- 接入 `uiautomator2`
- 完成一个端到端任务

### 阶段 3：批量化

- 实例列表拉取
- 任务队列
- 并发 worker
- 失败重试和状态回写

## 关键风险

- 网络类型不对时，ADB 路径会完全不同
- ADB 密钥放置不正确时会鉴权失败
- 云手机和应用下载源网络不通时，`CreateApp/InstallApp` 链路会失败
- 目标 App UI 变更会直接影响自动化稳定性
- 仅靠坐标点击会非常脆弱，必须优先使用控件定位

## 我给这个项目的最终建议

不要把无影当成“网页 AI 控手机产品”去集成。

应该把它当成：

- 云端 Android 设备资源池
- 带 OpenAPI 的设备管理平台
- 带 ADB 能力的远程 Android 接入层

而真正的“自动化执行逻辑”，由我们自己的 Python 服务 + `uiautomator2` 来完成。

## 关键文档

- API 概览: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-overview
- ADB 连接: https://help.aliyun.com/zh/ecp/how-to-connect-cloud-phone-via-adb
- StartInstanceAdb: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-startinstanceadb
- ListInstanceAdbAttributes: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-listinstanceadbattributes
- CreateKeyPair: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-createkeypair
- AttachKeyPair: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-attachkeypair
- CreateApp: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-createapp
- InstallApp: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-installapp
- RunCommand: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-runcommand
- DescribeTasks: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-describetasks
- SendFile: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-sendfile
- FetchFile: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-fetchfile
- BatchGetAcpConnectionTicket: https://help.aliyun.com/zh/ecp/api-eds-aic-2023-09-30-batchgetacpconnectionticket

## 下一步

下一步最合理的是直接开始搭第一版代码骨架：

1. 封装无影 OpenAPI 客户端
2. 封装 ADB 启停与连接
3. 接入 `uiautomator2`
4. 写一个单机任务脚本

如果继续，我下一步就直接把这个 Python 项目骨架搭出来。
