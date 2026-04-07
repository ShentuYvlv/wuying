# 无影云手机多平台自动化

基于无影云手机、ADB 和 `uiautomator2` 的聊天类 App 自动化项目。

当前已接入：

- `doubao`

后续预留：

- `deepseek`
- `kimi`
- `qianwen`
- `yuanbao`

## 运行

先确认 ADB 能手工连通：

```powershell
.\platform-tools\adb.exe connect 106.14.114.146:100
.\platform-tools\adb.exe devices -l
```

再用统一脚本入口：

```powershell
.\venv\Scripts\python.exe .\run_app.py --platform doubao --prompt "你好，介绍一下你自己"
```

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
5. 直接用 `run_app.py --platform xxx` 运行

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
