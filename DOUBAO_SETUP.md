# 豆包专用实施说明

这个项目不再按“通用批量平台”设计，而是按“豆包固定工作流”设计。

## 适用范围

- 先跑 1 台无影云手机
- 后续扩到 5 台
- 每台实例都执行同一套豆包操作

## 当前代码做了什么

- 读取 `.env`
- 用无影 SDK 检查实例状态
- 必要时自动开机
- 可选自动绑定 ADB 密钥
- 启动实例 ADB
- 获取 ADB 地址并 `adb connect`
- 用 `uiautomator2` 打开豆包并执行一次问答
- 批量/设备池模式输出结果到 `data/batches/<task_id>/doubao/repeat_xxx_prompt_xxx.json`

## 你现在最该做的事

1. 安装依赖
2. 填好 `.env`
3. 先确认 `adb connect` 能通
4. 用真实豆包界面调整选择器

## 选择器建议

第一次不要猜，直接在设备上导出界面层级：

```bash
adb shell uiautomator dump /sdcard/window_dump.xml
adb pull /sdcard/window_dump.xml
```

然后把输入框、发送按钮、回答区域写回 `.env` 的 JSON 选择器配置。
