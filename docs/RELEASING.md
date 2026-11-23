# 源码发布指南

建议版本为 `v0.1.0-beta.1`，定位为 Windows 本地微信角色机器人管理台与 AstrBot 定制扩展源码预览版。需要已有可工作的 AstrBot + WeChat Native 部署。

## 发布范围

只提交源码、测试、文档、示例配置和许可证。`.runtime/`、`data/`、`native-state/`、虚拟环境、密钥、二维码、日志、数据库、下载媒体与本机 `local.config.json` 必须留在本地。不要将完整运行目录压缩为 Release 附件。

保留随附第三方代码的原许可证和作者说明，见 `THIRD_PARTY_NOTICES.md`。根目录 MIT 不表示外部依赖均采用 MIT。

## 本地检查

在具备依赖的 Python 3.12 环境中运行：

```powershell
python -m pip install astrbot==4.29.4 -r requirements.txt -r plugins/astrbot_plugin_wechat_native/requirements.txt -r plugins/astrbot_plugin_personal_companion/requirements.txt
python -m unittest discover -s tests -v
node --check manager_app/static/app.js
python tools/check_source.py
git diff --cached --stat
git diff --cached --name-only
git ls-files
```

源码检查只覆盖其明确扫描的文件和模式，不替代对 Git 索引、历史、图片和凭据的审查。`.gitignore` 不会移除已经跟踪的文件。有效凭据若已公开，应先撤销或轮换，再处理历史。

## 提交与发布

1. 核对仓库地址、分支和提交文件；内容替换使用普通提交保留原历史，不使用强制推送。
2. 推送后确认 GitHub Actions 通过，并在干净目录克隆检查 README、资源路径和示例配置。
3. 为通过检查的提交创建 `v0.1.0-beta.1` 标签，草拟 Release，标记 Pre-release。
4. Release 说明写清已有部署要求、模型 API 费用与接口限制，源码包不包含登录态或安装好的 EXE。
5. 后续修改重复执行检查并发布新标签，运行数据不随版本提交。

GitHub 提供标签对应的源码归档；仓库与 Release 不会自动托管机器人。

官方说明：[上传本地项目](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)、[管理 Release](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)。
