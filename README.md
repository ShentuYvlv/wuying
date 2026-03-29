# 无影云手机豆包自动化

基于阿里云无影云手机、ADB 和 `uiautomator2` 的豆包专用自动化脚本。

当前目标：

- 连接无影云手机
- 启动豆包 App
- 输入问题
- 等待回答
- 读取结果

## 当前模式

当前仓库优先使用手动 ADB 模式：

- 在无影控制台手动创建 ADB 连接
- 本地手工确认 `adb connect` 可用
- 脚本直接使用 `WUYING_MANUAL_ADB_ENDPOINT`
- 不依赖阿里云 OpenAPI

联调过程中确认过的坑见：
[docs/联调踩坑.md](E:/all code/C一念/wuying/docs/联调踩坑.md)

## 项目结构

```text
wuying/
├─ .env.example
├─ README.md
├─ requirements.txt
├─ docs/
├─ scripts/
│  ├─ check_wuying_access.py
│  ├─ run_doubao_many.py
│  └─ run_doubao_once.py
└─ src/
   └─ wuying/
      ├─ aliyun_api/
      ├─ device/
      ├─ workflows/
      ├─ config.py
      ├─ logging_utils.py
      └─ models.py
```

## 依赖

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 关键配置

手动 ADB 模式至少需要：

```env
ADB_PATH=E:\all code\C一念\wuying\platform-tools\adb.exe
ADB_VENDOR_KEYS=E:\all code\C一念\wuying\platform-tools\adbkey
WUYING_START_ADB_VIA_API=false
WUYING_MANUAL_ADB_ENDPOINT=106.14.114.146:100
WUYING_INSTANCE_IDS=acp-xxxxxxxxxxxxxxxx
DOUBAO_PACKAGE_NAME=com.larus.nova
```

说明：

- `ADB_PATH` 指向本地可用的 `adb.exe`
- `ADB_VENDOR_KEYS` 指向下载的 `adbkey`
- `WUYING_MANUAL_ADB_ENDPOINT` 填控制台给出的 ADB 地址
- `WUYING_INSTANCE_IDS` 现在只用于结果标识
- `DOUBAO_PACKAGE_NAME` 必须改成真实包名

## 启动

先手工确认 ADB 已连接：

```powershell
.\platform-tools\adb.exe connect 106.14.114.146:100
.\platform-tools\adb.exe devices -l
```

再运行脚本：


python .\scripts\run_doubao_once.py --prompt "你好，介绍一下你自己"


多实例同 prompt：


python .\scripts\run_doubao_many.py --prompt "你好，介绍一下你自己" --max-workers 3


## 当前已知限制

- 手动 ADB 模式下，不会通过 OpenAPI 自动开机、开 ADB、查实例
- 如果豆包没有安装，或包名写错，脚本会停在启动器页面
- 默认选择器只是兜底值，真实运行前通常需要按当前豆包版本调整
