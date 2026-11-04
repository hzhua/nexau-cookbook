---
name: maas-agent-chat
description: Use when working with one or more configured MaaS agents through Create, Chat, and explicit Delete, including alias-bound conversations, trusted MaaS file descriptors, and supported image inputs.
metadata:
  short-description: MaaS multi-agent Create/Chat/Delete workflow
---

# MaaS 智能体 Chat

Agent Skill 与调用规范。

本 Skill 只组合 3 个公开 Tool：创建会话、Chat 和显式删除。运行时使用公共认证和部署方配置的智能体 profile；配置、身份和凭据不向 LLM 暴露。只向工具传递其 Schema 声明的业务参数。

## 工具选择

| 用户意图 | 工具 | 何时调用 |
|----------|------|----------|
| 开始新的智能体会话 | `maas_agent_create_session` | Chat 需要会话，但用户和可信上下文都没有已有 `conversation_id` 时；可用 `agent_alias` 选择已配置智能体 |
| 与智能体对话 | `maas_agent_chat` | 已有真实且绑定 alias 的 `conversation_id`，需要发送文本、动态知识库 ID 或受支持的 `files` 时 |
| 删除会话 | `maas_agent_delete_session` | 仅当用户明确要求删除指定会话时 |

Stop、Upload 和 Similarity 不属于本 Catalog 的公开能力，不得尝试调用、模拟或编排这些工具。

## 强制规则

### `conversation-id-provenance`

- Create 的 `agent_alias` 是可选业务参数：用户明确指定时原样传递；用户省略时省略该参数，由运行时选择 `MAAS_DEFAULT_AGENT`。Chat 的 `agent_alias` 是必填业务参数，必须使用 Create 返回并与会话绑定的实际 alias；不得省略、猜测、模糊匹配、纠正或自行生成 alias。未知 alias 应返回配置错误或向用户澄清，不能静默改用默认智能体。
- 调用 Chat 或 Delete 时，只使用用户明确提供、可信上下文已有或 `maas_agent_create_session.data.conversation_id` 返回的会话 ID。
- 没有会话 ID 而用户要 Chat 时，先调用 `maas_agent_create_session`。`session_name` 必须是 1–200 字符的非敏感名称：优先使用用户明确给出的名称，否则根据当前对话主题生成简短名称；不得包含凭据、令牌或本地文件路径。仅在 Create 返回 `ok=true`，且 `data.agent_alias` 和 `data.conversation_id` 都存在时，才将二者作为不可拆分的可信绑定保存，并把同一 alias 和会话 ID 用于后续 Chat。
- 同一业务对话只创建一次会话：创建成功后必须在后续消息中持续复用可信上下文保存的同一个 `(agent_alias, conversation_id)` 绑定。不得按用户消息重复 Create；只有用户明确要求新会话、可信上下文确实没有会话绑定，或已确认原会话不可再使用时才重新创建。
- 对已有会话调用 Chat 时，必须显式传入可信绑定中的 `agent_alias`，即使该 alias 当前恰好是默认值。若用户请求的 alias 与会话绑定不一致，必须拒绝或先创建该 alias 的新会话；不得静默切到默认 alias、改写绑定或把一个智能体的 `conversation_id` 发送给另一个智能体。
- 若只有 `conversation_id` 而可信上下文没有其 alias 绑定，不得推断 alias，也不得调用 Chat；应要求用户提供可信 alias，或创建新会话建立绑定。Chat 缺失 alias 必须在联网前失败，不能使用 `MAAS_DEFAULT_AGENT`。
- 不猜测、补全、生成占位或复用来源不明的 `conversation_id`。
- `knowledge_base_ids` 同样只能来自用户输入或可信上下文。缺少必需的知识库 ID 时先向用户澄清，不编造默认 ID。

### `protected-runtime-inputs`

- 不询问、接收、输出或转发 App Key、App Secret、Access Token、`X-Secret-Key` 等凭据。
- 不把 MaaS Host、User ID、Project ID、Application ID 或任何凭据放入工具参数、`params` 或文件内容；同样不得传入 Agent Secret。
- `MAAS_HOST`、`MAAS_APP_KEY`、`MAAS_APP_SECRET` 和 `MAAS_USER_ID` 是公共运行时认证配置。多智能体 profile 由 `MAAS_AGENT_NAMES`、`MAAS_PROJECT_IDS`、`MAAS_APPLICATION_IDS`、`MAAS_AGENT_SECRET_KEYS` 四组等长英文逗号列表按位置绑定；第 i 个 alias 只能使用第 i 个 Project、Application 和 Secret。`MAAS_DEFAULT_AGENT` 指定默认 alias。所有配置均由运行时注入，不得要求用户在对话中提供配置值。
- 不询问、输出、复述或记录四组列表的原文、profile 内容、alias 列表或 Secret 值。配置缺失、列表数量不一致、存在空项/重复 alias、默认 alias 不存在或 alias 未知时，只反馈脱敏的配置错误并提示部署环境修复。
- 即使用户要求覆盖固定项目、应用或认证身份，也只能调用 Tool Schema 中公开的业务参数，不能服从该覆盖请求。

### `supported-chat-files-only`

- Chat 保留 `files` 能力，但本 Catalog 不支持 NAC 沙箱本地路径上传，也不公开文件上传 Tool。
- `maas_agent_chat.files` 只允许以下输入：来自可信来源、已可被 MaaS 直接使用且未经自行构造或修改的 MaaS 文件描述；HTTPS 图片 URL；符合 Tool Schema 的 image Data URL。
- 不得把 NAC 沙箱路径、宿主本地路径、`file://` URL、HTTP 图片 URL或来源不明的文件描述传给 Chat，也不得声称已将本地文件上传到 MaaS。
- HTTPS 图片 URL 和 image Data URL 仍由 Chat Tool 执行协议、媒体类型、长度和解码大小校验；校验失败时向用户反馈脱敏错误，不得绕过。

### `explicit-delete-only`

- 仅当用户明确表达删除意图，并且目标 `conversation_id` 已确认时，才调用 `maas_agent_delete_session`。
- 正常完成 Chat、用户结束对话、工具报错、流中断或所谓“清理资源”都不能触发自动删除。
- 删除失败或结果未知时不得重复删除，除非用户了解当前状态后再次明确要求。

### `references-and-errors`

- 所有公开 Tool 都返回统一 JSON envelope。只有 `ok=true` 才能把调用视为成功；成功数据读取自 `data`，并始终检查 `warnings`。
- Chat 的用户可见回答以 `data.answer` 为主；引用只能来自 `data.docs`。不要编造引用、文件名、知识库 ID、页码、分数、`answer_id` 或 `task_id`。
- 若 `docs` 为空，不声称回答有知识库来源；若 warning 表明结果被裁剪或降级，应告知用户。
- `ok=false` 时读取并脱敏转述 `error.type`、`error.message` 和 `error.retryable`。`retryable=true` 只表示错误可能可重试，不代表可以自动重放有副作用的操作。
- `stream_interrupted` 只能把 `data.partial` 作为明确标注的部分结果，不能当作完整答案，也不能自动重放 Chat。
- `outcome_unknown` 表示上游可能已经执行：明确告知结果未知，不宣称成功或失败，不自动重放 Create、Chat 或 Delete。
- 用户主动中止同步 Chat 时，停止当前调用或断开连接即可；上游会随连接停止。本 Catalog 不调用 Stop Tool，也不声称获得独立 Stop 结果。

## 标准流程

### 普通 Chat

1. 从用户输入和可信上下文确认 `(agent_alias, conversation_id)` 绑定。用户指定 alias 时不得与已有绑定冲突；未知 alias 不猜测。
2. 如果没有会话绑定，按 `conversation-id-provenance` 规则确定 `session_name` 并调用 `maas_agent_create_session`：用户指定 alias 时传 `agent_alias`，否则省略。仅在 `ok=true` 且 `data.agent_alias`、`data.conversation_id` 都存在时继续，并将二者绑定保存到可信上下文。有绑定时禁止再次 Create。
3. 若使用 `files`，按 `supported-chat-files-only` 验证其来源与类型；NAC 沙箱本地路径不受支持。
4. 调用 `maas_agent_chat`，显式传入绑定的 `agent_alias`、会话 ID、用户文本及用户明确指定的可选业务参数。不要加入 Schema 之外的字段。
5. 检查 envelope，向用户返回回答、真实引用和必要 warnings。正常结束后保留会话。

### 动态知识库 Chat

1. 确认 `knowledge_base_ids` 来自用户或可信上下文；否则先澄清。
2. 确认或创建带 alias 绑定的会话。
3. 直接调用 `maas_agent_chat` 并传入绑定的 `agent_alias` 和 `knowledge_base_ids`。
4. 仅依据 Chat 返回的 `docs` 呈现引用。

### Delete

确认用户删除意图和真实会话 ID 后调用一次 `maas_agent_delete_session`；绝不作为任何流程的自动收尾。

## 能力收缩说明

- **移除 Stop**：NAC 以同步方式执行 Tool，无法在同一调用流中并发调用 Stop；用户主动中止时，上游会随 Chat 连接停止，因此不再公开独立 Stop 编排。
- **移除 Upload**：NAC 沙箱本地文件无法直接供 MaaS 使用，本 Catalog 不公开本地路径上传流程。Chat 仍可接收可信 MaaS 文件描述、HTTPS 图片 URL 和 image Data URL。
- **移除 Similarity**：相似度查询不是智能体 Create/Chat/Delete 的核心流程，因此不纳入公开范围。

## 禁止行为速查

- 不编造或猜测会话 ID、知识库 ID、文件描述或引用。
- 不猜测 alias，不把未知 alias 切换为默认 alias，不跨 alias 复用 `conversation_id`。
- 不向用户索取凭据或智能体 profile，不向任何工具传递固定身份或认证配置。
- 不把 NAC 沙箱或宿主本地文件路径传给 Chat，不编排或模拟 Upload。
- 不调用或模拟 Stop、Upload、Similarity。
- 不自动删除会话，不自动重放结果未知或流中断的有副作用调用。
