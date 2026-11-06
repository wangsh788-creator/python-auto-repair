# Python 自动修复

适用于 **Windows 10 / 11（64 位）**。现成程序请从 [Releases 下载 Windows 分享包](https://github.com/wangsh788-creator/python-auto-repair/releases/latest)，解压后运行 `PythonAutoRepair.exe`。

v2.1.0 启动后常驻后台，每 2 秒检查新增或变化的文件；修复结果用 VS Code 打开。

扫描已选择文件夹中新增或内容变化的 Python 文件，判断代码是否实现开头注释或模块说明中的功能目标。有明确错误时保存修复副本，并用 VS Code 打开。这里的“检索”指本地文件扫描，不是互联网搜索。

## 发给别人使用

把整个“Python自动修复分享版.zip”发送给对方，让对方先解压，再双击 `PythonAutoRepair.exe`。首次启动进入设置，选择自己的检查文件夹和连接方式，保存后测试连接。

分享版包含 Python 和 Codex CLI 运行环境，无需另外安装 Python、Codex 或 ChatGPT 桌面应用。接收者仍需有可用网络、自己的有效登录或 API 密钥；需要自动打开结果时，还需安装 VS Code。只安装 ChatGPT 或在网页中登录过，并不代表本程序已经完成授权。

之后双击 `PythonAutoRepair.exe` 会启动后台监测。关闭设置窗口只会隐藏界面，文件检查仍在后台继续。再次双击程序或同目录的“打开设置.cmd”，会唤起已有的设置窗口。原来桌面上的“Python自动修复.exe”和设置快捷方式也可以继续使用。

需要停止监测时，在设置窗口点击“退出后台”；若正在分析，会完成当前轮检查后退出。

## 三种连接方式

| 连接方式 | 需要填写或完成的内容 | 分析代码的方式 |
| --- | --- | --- |
| ChatGPT 登录 | 在设置中发起登录，在 Chrome 中完成自己的 ChatGPT 授权；账号需有可用 Codex 权限和额度 | 后台调用随程序提供的 Codex CLI |
| 官方 API | 自己的 OpenAI API 密钥、可用模型名 | 直接调用 OpenAI API |
| 自定义 API | 服务商提供的 API 地址、密钥、模型名，以及对应的接口格式 | 直接调用该兼容服务的 API |

官方 API 和自定义 API 模式无需 ChatGPT Plus/Pro 会员，使用对应服务的密钥和额度。ChatGPT 登录模式需要账号具有可用的 Codex CLI 权限和额度，不能仅凭能使用免费版 ChatGPT 桌面功能判断此模式可用。

API 模式支持 Responses 和 Chat Completions 两种接口格式。按照服务商文档选择；中转服务需要兼容其中一种接口，并支持所选模型和本程序要求的代码输出格式。不能仅凭“支持 ChatGPT”或能在网页聊天，就判断某个中转服务可用。代码会提交给当前选择的服务分析，切换服务后不会自动转交给其他服务。

ChatGPT 模式的“ChatGPT 登录”是 Codex 的认证方式。程序不会操作 ChatGPT Work 或 ChatGPT Web 的聊天窗口；使用 Chrome 只是为了完成登录授权。官方及自定义 API 模式不依赖 Codex CLI，也不控制浏览器聊天界面。

每个人的文件夹、连接配置和登录状态都保存在自己的 Windows 用户目录中。分享包不包含制作者的账号、密钥、所选文件夹或待检查代码。

## 检查和保存规则

选择文件夹时会记录当时已有文件，之后检查新增或内容变化的 `.py` 文件，包含子文件夹。同名源文件只要内容变化就会重新分析，未变化则跳过；新选择文件夹时已有的文件不会立即全部送去分析。

默认每 2 秒检查一次所选文件夹，之后加入或修改文件即可自动触发分析。程序不会自动配置 Windows 开机启动。需要手动立即检查时，可在设置窗口点击检查按钮。

修复结果保留原文件名和开头的功能目标，不添加修改过程、说明或新注释。原文件保持不变。桌面没有同名文件时直接保存到桌面；已有同名文件时保存到桌面“Python修复结果”下的独立子文件夹，仍保留原文件名，不覆盖已有文件。没有发现需要修改的问题时不生成副本。

保存后用 VS Code 打开查看或编辑，复用已有窗口并定位到文件首行，不自动运行代码。未找到 VS Code 或打开失败时，已保存结果会保留并等待重试打开。可通过环境变量 `PY_AUTO_REPAIR_VSCODE` 指定 VS Code 原生可执行文件 `Code.exe` 的绝对路径。

## 进度和失败处理

设置窗口显示当前文件、分析阶段、耗时和具体错误。后台监测保持静默，有修复结果才打开 VS Code。单文件分析总超时为 180 秒；明确的认证失败会提前结束。失败文件会保留并自动重试，两次自动重试至少间隔 60 秒。

程序检查语法并保留目标说明，通过静态分析和模型判断发现错误，不会执行待检查文件进行测试，也无法保证发现所有运行时和逻辑错误。

状态和错误记录位于 `%LOCALAPPDATA%\PythonAutoRepair`。ChatGPT 专用登录目录为其下的 `codex-home`。程序忽略 `.git`、`.hg`、`.svn`、`__pycache__`、`.venv`、`venv`、`node_modules` 及符号链接或目录联接；单个源文件超过 512 KB 时不自动分析。

## 从源码运行

源码使用 Python 3.11 或以上版本及标准库。下载仓库源码后，在仓库目录中运行：

```powershell
python app.pyw --settings
```

默认启动会持续后台监测。需要只检查一轮并退出时，可使用：

```powershell
python app.pyw --once
```

`--headless` 保留原有单轮模式，同样只检查一轮并退出，且不显示设置窗口。

API 模式无需 Codex CLI。ChatGPT 登录模式会查找已安装的 Codex 原生可执行文件，也可通过环境变量 `PY_AUTO_REPAIR_CODEX` 指定其完整路径。

本地验证使用模拟 API 和临时文件，不需要真实账号、API 密钥或模型调用：

```powershell
python -m unittest discover -s tests -v
```

## 从源码打包

Git 仓库保存源码和许可文本，不提交 Codex 的大型二进制文件或编译产物。直接使用程序时下载 Releases 分享包即可。

自行打包时，先安装 PyInstaller，将本机已安装的 Codex 原生 `codex.exe` 复制到 `vendor/codex/codex.exe`，保留该目录的 `LICENSE`、`NOTICE`、`PROVENANCE.txt` 并核对运行时版本与来源，然后运行 `build.ps1`。安装了 ChatGPT/Codex 桌面应用时，其原生运行时通常位于 `%LOCALAPPDATA%\OpenAI\Codex\bin\<版本目录>\codex.exe`。脚本会将运行时和许可文本打包到单个 EXE，不会打包用户登录和设置。

分享目录使用 `PythonAutoRepair.exe` 作为程序文件名，配合同目录的“打开设置.cmd”。Codex 的许可和来源文本应随分享包一并提供。

本项目源码使用 [MIT 许可证](LICENSE)；分享包内的 Codex 运行时使用其独立许可，见 `vendor/codex/LICENSE` 和 `vendor/codex/NOTICE`。

官方参考：[Codex 认证](https://learn.chatgpt.com/docs/auth?surface=cli)、[非交互调用](https://learn.chatgpt.com/docs/non-interactive-mode)、[模型服务配置](https://learn.chatgpt.com/docs/config-file/config-advanced)。
