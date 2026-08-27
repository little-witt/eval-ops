# FORGE Desktop

FORGE 是 `aceval` Kernel 的桌面产品壳，不是静态报告页。它通过 Electron
主进程与本地 Python 子进程的 JSON-RPC 通信，不开放 HTTP 端口。

## 最终用户运行要求

- macOS arm64 构建已内置 Python Kernel 与 Node 22，不要求用户安装 Python/Node。
- Git 用于仓库检出与候选发布。
- D2C 需要 Google Chrome/Chromium；客户端会在“配置与环境”中显示指纹与缺项。
- 本地分析使用已安装的 Codex CLI。客户端分别导入 `config.toml` 和 `auth.json` 的私有副本并展示账户实际可用的 GPT 模型，不修改原 Codex/Claude 配置。

## 开发启动

```bash
cd desktop
npm install
npm start
```

源码开发要求 Node.js >= 20.19、Python >= 3.9。生成独立 `.app` 前先安装桌面构建依赖：

```bash
python3 -m pip install -e '.[desktop-build]'
cd desktop
npm run dist:mac
```

构建脚本会要求 Python 与 Electron 目标架构一致。需要指定独立构建环境时使用
`ACEVAL_BUILD_PYTHON=/absolute/path/to/python npm run dist:mac`。

可用环境变量：

- `ACEVAL_PYTHON`：仅开发模式使用的 Python 可执行文件；打包版使用应用内置 Kernel
- `ACEVAL_TASK_ROOT`：任务目录，默认复用项目 `.aceval/tasks`
- `ACEVAL_MODEL_PROFILE_ROOT`：客户端隔离 Codex Profile 目录
- `ACEVAL_CODEX_EXECUTABLE`：Codex CLI 绝对路径；未设置时会检查 PATH、Homebrew 与 NVM 常见位置

Codex Profile 文件由主进程文件选择器导入，Renderer 看不到源路径或内容；配置只保留模型连接字段，认证副本以 0600 保存。API Key 认证优先使用低 Token Responses 通道，ChatGPT OAuth 认证回退官方 Codex App Server。Agent/API/PAT 等其他密钥由 Electron `safeStorage` 保存，Kernel 配置只持久化环境变量名。
安装的 Chrome 插件仅进入 `persist:forge-preview` 预览 Profile；D2C 判定 Worker 使用
临时 Profile 并强制禁用插件。
