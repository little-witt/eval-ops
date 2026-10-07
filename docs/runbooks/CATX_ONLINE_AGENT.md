# CATX 线上 Agent 接入

本项目通过服务端 CLI 连接 CATX。密钥可从本地权限受限的凭据文件读取，也可由环境变量注入；不会进入 Profile、报告、命令输出或异常正文。

## 支持范围

```text
session execute
  -> POST /sessions
  -> POST /sessions/{sessionId}/events
  -> session_id

session status
  -> GET /sessions/{sessionId}
  -> 必要时 GET agent.message events
  -> RUNNING | COMPLETED | FAILED

session fetch
  -> GET /sessions/{sessionId}
  -> GET /sessions/{sessionId}/events?order=asc&limit=N
  -> ImportedRunBundle

session listen
  -> GET Supabase Edge Function SSE
  -> 等待 session.status_idle 或 session.error
  -> 回源 CATX 获取权威状态和所有事件类型
  -> ImportedRunBundle
```

CATX 输出按“从后向前寻找最后一条有效 `agent.message`”提取。空文本和 `(no content)` 会被跳过。`agent.tool_use`、`agent.tool_result`、消息和状态事件都会转换为 Canonical Trace。

## 配置

复制并修改 `examples/catx-api-profile.example.json`：

- `base_url`：CATX Session API 地址；
- `*_env`：环境变量名称，不是实际值；
- `credentials_file`：可选本地凭据 JSON 路径；相对路径按 Profile 所在目录解析；
- `vault_ids`：会话可访问的 Vault ID；
- `repositories[]`：一个会话可以挂载多个远程 Git 仓库；旧的单个 `repository` 字段仍兼容，但不能与 `repositories` 同时配置；
- `repositories[].url`：CATX 可以拉取的远程 Git URL，不能填写本机目录；
- `repositories[].authorization_token_env`：仓库 PAT 所在环境变量名；多个仓库可以引用同一个变量；
- `repositories[].mount_path`：仓库在 Agent 沙箱中的唯一绝对路径；
- `events_limit`：单次读取事件数，默认 1000，最大 1000；如需兼容较旧网关可显式调低。
- `stream.base_url`：Supabase 项目 URL；
- `stream.path`：Edge Function 路径；
- `stream.action`：默认 `getMessage`；
- `stream.api_key_env`：Supabase apikey 环境变量；
- `stream.bearer_env`：可选用户 access token 环境变量。未设置实际值时，Authorization 回退到 apikey。

CATX 固定请求头由连接器生成：

```text
X-Api-Key: $CATX_API_KEY
anthropic-version: 2023-06-01
anthropic-beta: files-api-2025-04-14
user-mis-id: $USER_MIS_ID
```

本地凭据文件使用 `*_env` 所指名称作为键，并在 POSIX 系统设置为 `0600`：

```json
{
  "CATX_API_KEY": "通过安全方式写入",
  "USER_MIS_ID": "your-mis",
  "CATX_AGENT_ID": "agent_xxx",
  "CATX_ENV_ID": "env_xxx",
  "CATX_REPOSITORY_AUTHORIZATION_TOKEN": "通过安全方式写入仓库 PAT"
}
```

CI/云端可不配置文件，直接注入环境变量；本地同时存在文件和同名环境变量时，环境变量优先：

```bash
export CATX_API_KEY='通过安全方式注入'
export USER_MIS_ID='your-mis'
export CATX_AGENT_ID='agent_xxx'
export CATX_ENV_ID='env_xxx'
export CATX_REPOSITORY_AUTHORIZATION_TOKEN='通过安全方式注入仓库 PAT'
export SUPABASE_ANON_KEY='通过安全方式注入'
export SUPABASE_ACCESS_TOKEN='可选的用户 access token'
```

凭据文件必须加入 Git 忽略规则，不要把值写入 Profile、受版本控制的 JSON、终端历史截图或故障报告。

代码评审会话同时挂载候选 Skill 仓库和统一 Fixture Lab 仓库。Fixture 远程仓库必须包含全部
`base/<stack>` 和 `case/<case-id>` 分支；每次评测由 Case 元数据指定固定的
base/head commit。候选 Skill 仓库固定挂载到 `/workspace/skills/<skill-name>`，Fixture
仓库固定挂载到 `/workspace/repo`；执行前必须在日志中核对两个仓库的实际 commit。

## 创建并运行会话

请求文件只允许 `title` 和 `prompt`。如果 Profile 配置了 `repositories`，创建会话时
会自动添加全部资源，`authorization_token` 只在内存中的 HTTP 请求体出现：

```json
{
  "resources": [
    {
      "type": "repository",
      "url": "ssh://git@git.sankuai.com/org/frontend-code-reviewer.git",
      "authorization_token": "$CATX_REPOSITORY_AUTHORIZATION_TOKEN",
      "mount_path": "/workspace/skills/frontend-code-reviewer"
    },
    {
      "type": "repository",
      "url": "ssh://git@git.sankuai.com/org/fixture-lab.git",
      "authorization_token": "$CATX_REPOSITORY_AUTHORIZATION_TOKEN",
      "mount_path": "/workspace/repo"
    }
  ]
}
```

请求文件示例：

```json
{
  "title": "aceval online smoke test",
  "prompt": "请执行一次最小冒烟任务，并直接返回最终结果。"
}
```

```bash
PYTHONPATH=src python3 -m aceval session execute \
  --profile examples/catx-api-profile.example.json \
  --request examples/catx-session-request.example.json
```

成功后 stdout 返回 `session_id`。若 Session 已创建但首条消息提交失败，错误会保留该 Session ID，便于人工排查，不会静默重建并造成重复运行。

## 查询和获取日志

查询映射状态：

```bash
PYTHONPATH=src python3 -m aceval session status \
  --profile examples/catx-api-profile.example.json \
  --session-id SESSION_ID
```

直接拉取当前完整事件类型：

```bash
PYTHONPATH=src python3 -m aceval session fetch \
  --profile examples/catx-api-profile.example.json \
  --session-id SESSION_ID \
  --output .aceval/imported/catx-session.json
```

SSE 等待终态并回源获取权威日志：

```bash
PYTHONPATH=src python3 -m aceval session listen \
  --profile examples/catx-api-profile.example.json \
  --session-id SESSION_ID \
  --output .aceval/imported/catx-session.json
```

输出文件可以直接交给现有离线归因：

```bash
PYTHONPATH=src python3 -m aceval session diagnose \
  --input .aceval/imported/catx-session.json \
  --output .aceval/imported/catx-session-diagnosis.json
```

## SSE 行为

监听器支持：

- 任意网络 chunk 边界；
- UTF-8 增量解码；
- CRLF/LF；
- SSE comment/keep-alive；
- 多行 `data:`；
- `session.status_running`、`agent.message.chunk`、`agent.message`、`agent.tool_use`、`agent.tool_result`；
- `session.status_idle` 正常结束；
- `session.error` 失败结束；
- 响应总字节限制和非 JSON 事件拒绝。

SSE 本身只用于等待和实时事件观测。终态结果以 CATX `/sessions/{id}` 和 `/events` 回源响应为准，避免 chunk 丢失或代理裁剪导致报告不完整。

## 状态映射

| CATX 状态 | 有效消息 | aceval 状态 |
| --- | --- | --- |
| `running` / `rescheduling` | 不读取 | `RUNNING` |
| `idle` | 有消息且无 `session.error` | `COMPLETED` |
| `idle` | 存在 `session.error` | `FAILED` |
| `idle` | 无消息且无错误 | `COMPLETED`，`round_count=0` |
| `terminated` | 有 | `COMPLETED` |
| `terminated` | 无 | `FAILED` |
| 未知状态 | 不读取 | `RUNNING` |

## 当前边界

- Events API 文档未提供翻页游标。本实现单次按 `events_limit` 获取；当事件数达到上限时，元数据会标记 `trace_may_be_truncated: true`，不能宣称日志一定无截断。
- SSE Edge Function 需要与 CATX Session ID 属于同一套线上环境。
- 多仓库 `resources` 创建字段已经接入。正式评分前，Agent 必须只读执行 `git rev-parse HEAD`
  核对候选 Skill commit 和 Fixture base/head；任何缺失或漂移都 fail closed。
- 原始 Events 可能包含 Prompt、工具参数或敏感上下文，输出目录应按敏感数据管理，不能直接上传或公开。
