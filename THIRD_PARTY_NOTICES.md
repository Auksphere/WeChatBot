# 第三方与本地修改

- **AstrBot 4.29.4**：独立 Python 运行依赖，本仓库不复制其框架源码或 WebUI。项目：https://github.com/AstrBotDevs/AstrBot ，许可证以该项目为准（AGPL-3.0）。
- **astrbot_plugin_wechat_native 0.3.21**：`plugins/astrbot_plugin_wechat_native/` 保留原 MIT 许可证和作者信息。来源：https://github.com/neozhu/astrbot-plugin-wechat-native 。本地修改位于 `tool_support.py`（限定可信文字唤醒事件可使用表情工具）和 `native_client.py`（容忍有效 PNG 尾部扩展数据）。
- **agent-wechat 2.9.0**：独立本地服务，本仓库不分发其可执行文件或用户 profile。项目：https://github.com/thisnick/agent-wechat 。许可证以其发布内容及第三方声明为准。
- **imageio-ffmpeg / FFmpeg、Pillow、psutil、PyCryptodome、zstandard 及文档读取依赖**：由运行环境安装，不在本仓库中捆绑二进制。各自许可证和版本约束见对应项目与插件 requirements。

`plugins/astrbot_plugin_personal_companion/` 是本项目的定制扩展源码，不包含账号资料、个人档案或真实聊天数据。

本地补丁补充：WeChat Native 卡片转换新增 subtype 51 视频号卡片，保留原群聊与私聊准入规则；真实视频读取由定制陪伴插件通过原 Native 媒体接口完成。

视频号发送前检查补丁：为定制插件验证过的文档、视频及视频号保留原附件类型，复核同账号、同会话、原消息身份、引用发送者和撤回状态，避免按默认图片类型拒绝合法回复。

私聊表情与输出修复：`adapter.py` 接收私聊直接表情并在发送前过滤内部工具标记；`event.py` 保留真实表情图像并提供自然回应提示；`sticker_service.py` 复用原消息、原资产、账号/会话及撤回校验读取直接表情；新增 `output_text.py` 仅清理可见正文及助手历史，不执行文本内工具调用。群聊直接表情不触发回复。

异常模型回复恢复补充：`output_text.py` 在请求副本中清理无效助手正文、对应损坏推理和失败占位文本，正常结构化工具调用保持原样；定制插件的 `reply_recovery.py` 负责重生成答案，底层发送拦截仍保留。

- **yt-dlp 2026.10.26**：通过 Python 运行依赖使用，不复制其源码或分发可执行文件。项目：https://github.com/yt-dlp/yt-dlp ，核心采用 Unlicense；实际许可与依赖声明以其发行包为准。用于匿名解析 Bilibili/小红书视频，FFmpeg 仍由已有独立运行环境提供。
- 平台视频补丁：`adapter.py` 在发送前调用定制插件设置的来源校验，原有身份、引用、撤回和回执检查保留。

免费表情自动获取补充：定制扩展新增后台浏览、免费资格核对、幂等添加与下载、真实预览描述及本地候选索引；WeChat Native 的 `sticker_service.py` 增加按当前事件绑定且复核原图哈希的候选选择方法。原插件许可证及作者声明保持不变。
