# 绑定已有部署

本项目第一版管理一个已经安装并可工作的 Windows AstrBot + WeChat Native 部署。首次安装微信运行环境，请参照 [AstrBot](https://docs.astrbot.app/) 和 [agent-wechat](https://github.com/thisnick/agent-wechat) 的官方说明。

## 目录约定

```text
your-bot-deployment/
  .venv/Scripts/python.exe
  .venv/Scripts/pythonw.exe
  manage.py
  group_manager.py
  sticker_manager.py
  native/wx.exe
  native/ffmpeg.exe
  native-state/                 本地微信运行配置与凭据
  data/cmd_config.json
  data/data_v4.db
  data/plugins/astrbot_plugin_wechat_native/
  data/plugins/astrbot_plugin_personal_companion/
  data/plugin_data/astrbot_plugin_personal_companion/
  logs/
```

随附 `runtime_scripts/` 和 `plugins/` 是对应源码。更新已有部署时先停止机器人、备份配置与数据库，随后只复制需要更新的源码，再启动验证；不要覆盖 `data/plugin_data/` 和 `native-state/`。

管理服务使用绑定部署的 Python，不另建一个带微信账号的运行环境。该 Python 需能导入 AstrBot、WeChat Native 插件及定制插件的依赖。

## 配置约定

- AstrBot 平台实例 ID 为 `wechat-rolebot`，平台类型为 `wechat_native`。
- Native 本地 API 使用 `127.0.0.1:6174`，AstrBot 后台为 `127.0.0.1:6185`。
- DeepSeek 服务源 ID 为 `deepseek`；辅助、日常、知识问答模型 ID 分别为 `deepseek-rolechat`、`deepseek-rolechat-high`、`deepseek-rolechat-max`。
- 当前角色在 `personas` 表中的 ID 为“大肥鱼”。
- 管理台通过 Native 的 login-attempts 接口管理扫码任务，保留原任务 ID 与幂等键，二维码刷新时更新到当前版本。
- Native 普通读写令牌路径由 `cmd_config.json` 的平台配置指定，登录管理令牌位于 `native-state/api-token`。
- 语音使用 DeepSeek 官方 ASR/TTS。发送前转换为 16 kHz、单声道、16 位 PCM WAV；启动器将项目内 `native/` 加入子进程 PATH，供 Native 查找 ffmpeg。

上述文件中需要的账号 ID、密钥和登录态均由使用者在本机配置，源码不包含真实值。部署依赖并确认原终端入口可用后，运行根目录的 `setup.ps1` 即可绑定管理台。

## 管理台自身的生命周期

管理台运行在独立 Python 进程。网页中的“停止机器人”只停止 AstrBot 与 Native；再次点击启动时，管理台调用绑定目录的 `manage.py`。关闭网页不会关闭机器人或管理服务。

修改管理台源码后，可在任务管理器中结束对应 `manager_app.server` 的 Python 进程，再双击快捷方式启动；不会清除机器人数据。不要直接结束不确定归属的 Python 进程。

## 项目内运行布局

建议使用 `.runtime/bot/` 保存完整机器人环境、微信登录态、配置、数据库与日志，基础 Python 位于 `.runtime/python/`。管理台的 `local.config.json` 绑定 `.runtime/bot/`。

机器人与管理服务的临时文件、缓存分别位于 `.runtime/bot/data/temp/` 和 `.runtime/bot/data/cache/`。项目不依赖原 C 盘 Python 路径；系统和浏览器使用自己的 Windows 安装环境。
