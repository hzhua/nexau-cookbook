# 教程：接入第三方向量库 — MAP 知识库问答

> 🟢 **Level 1 · 入门** ｜ ⏱ 约 30 min ｜ 🔑 基于现成 Agent 微调到自己的 KB 列表
>
> 本目录已经提供了一套**完整可运行**的 MAP 向量库问答 Agent：HTTP client、鉴权 / 重试、批检索、结果裁剪、服务端 bug fallback——均已实现。本教程不讲"从零实现"，而是说明**如何将其接入你自己的 KB 列表**：哪些位置必须替换、哪些为可选微调、哪些机制无需关心。
>
> 💡 适合的读者：
> - 已有 MAP 平台账号 + 一组业务相关的向量库
> - 希望 Agent 直接复用这些库，无需从零编写鉴权 / 检索代码
> - 需要根据自身领域微调 prompt 或工具暴露范围

## 目录

- [场景与目标](#场景与目标)
- [项目结构](#项目结构)
- [必需的改动](#必需的改动)
  - [替换精选 KB 映射表](#替换精选-kb-映射表)
  - [配置 MAP 鉴权与环境变量](#配置-map-鉴权与环境变量)
- [可选的微调](#可选的微调)
  - [按业务领域调整同义词表](#按业务领域调整同义词表)
  - [隐藏不想暴露的检索工具](#隐藏不想暴露的检索工具)
  - [检索调优参数](#检索调优参数)
- [已封装的内置机制](#已封装的内置机制)
- [注意事项](#注意事项)

---

## 场景与目标

并非每个用户都需要从零搭建一套 RAG。许多政务 / 企业客户的实际情况是：

- 已有内部知识库平台（本文以 MAP 为例）
- 平台上已维护数十甚至数百个向量库（KB），切片与嵌入均已完成
- 业务方希望 Agent 直接复用这些既有资产，**不重建索引**、**不改动目标平台**

本目录提供的方案：将目标平台的检索 API 封装为 Agent 工具，让 Agent 通过这些工具发起检索。**所有复杂细节已处理完毕**——鉴权、批检索、并发、结果裁剪、服务端 bug 兜底。你只需：

1. 将 systemprompt 中的精选 KB 列表替换为你自己平台上的 KB
2. 配置几个鉴权环境变量
3. （可选）根据业务调整同义词表 / 工具暴露范围

其余部分开箱即用。

---

## 项目结构

```
MAP 知识库问答/
├── nexau.json                          ← Agent 注册表
├── agent.yaml                          ← 骨架：模型 + 工具 + 中间件
├── systemprompt_vb.md                  ← 指挥官：角色 + KB 映射表 + 检索流程
├── custom_tools/
│   ├── _map_client.py                  ← MAP HTTP client（鉴权 + 重试）
│   └── map_api.py                      ← 所有 map_vec_* 工具的 Python 实现
└── tools/
    ├── read_file.tool.yaml             ← 三个标配工具
    ├── search_file_content.tool.yaml
    ├── run_shell_command.tool.yaml
    ├── map_vec_batch_search.tool.yaml          ← 主力检索工具（cross-product）
    ├── map_vec_semantic_search.tool.yaml       ← 语义检索备用
    ├── map_vec_fulltext_search.tool.yaml       ← 全文检索备用
    └── map_vec_get_chunks_by_file.tool.yaml    ← 取同源文档其他切片
```

**三个关注点分离**：

| 层 | 位置 | 职责 | 是否需要修改？ |
|----|------|------|---------------|
| 鉴权 / 重试层 | `_map_client.py` | token 缓存、过期重取、transient 重试 | ❌ 否 |
| 业务 / 加工层 | `map_api.py` | 将每个 MAP API 封装为对 LLM 友好的函数 | ❌ 否 |
| 编排层 | `agent.yaml` + `systemprompt_vb.md` | 注册工具、KB 映射表、检索流程 | ✅ **需要修改** |

---

## 必需的改动

### 替换精选 KB 映射表

`systemprompt_vb.md` 中有一段"精选 KB 映射表"，列出了示例所用的 35 个 MAP 法律法规库。**这部分必须替换为你自己平台上的 KB**。

打开 `systemprompt_vb.md`，定位到类似如下段落：

```markdown
# 精选 KB 映射表（可搜的全部库）

KB id                                | 名字（主题）
=====================================|=====================================
3b6efa50137911f0b7dbbc24111a57ca     | 知识管理产品_法律法规库
33fdab720eec11f08b85bc24111a57ca     | 统计制度
9281a60c507a11ef802bb5c7b550ea53     | 法律
... (35 条)
```

> ✏️ **替换为你自己的 KB id + 主题名**。强烈建议：
> - **人工筛选**，仅保留业务确实需要、内容质量达标的 KB（10–50 个量级即可）
> - 名称使用"用户可理解的主题"，避免直接使用平台原始库名（很多平台库名带 `-pg / -es / -v2` 等内部后缀，对 Agent 选库无帮助甚至会产生误导）

紧接其后的"主题大类速查"与"选库流程"也应按你的领域调整：

```markdown
**主题大类速查**：
- 法律 / 法规 / 司法解释 → 「法律法规库」「法律」「行政法规」等
- 政策文件 → 多个"政策…库"
- 上海市制度 → 上海地方/行业相关
- 统计制度 → 统计专题
```

将"用户可能查询的关键词"映射到"应当检索哪几个库"。这一步决定了 Agent 选库准确率的上限——白名单越精挑细选，命中越准。

为什么要直接写入 prompt，而不提供一个"按主题搜 KB"的工具？

- Agent 每次会话都需先调用一次 list/search 才能进入检索阶段 → **额外开销，效率较低**
- 平台上诸多库的标题相似度较高（"政策库 / 政策库 2 / 政策库-pg"），关键词搜索难以区分哪个可用 → **易选错库**
- 平台上数千个库中真正可供 Agent 使用的通常仅数十个 → **大量无效检索**

**通用原则**：能在编辑期枚举的内容，不应交由 Agent 在运行时去发现。**Prompt 是成本最低的索引**。

### 配置 MAP 鉴权与环境变量

`agent.yaml` 中每个 MAP 工具都通过 `extra_kwargs` 注入了鉴权三要素：

```yaml
- name: map_vec_batch_search
  yaml_path: tools/map_vec_batch_search.tool.yaml
  binding: custom_tools.map_api:map_vec_batch_search
  extra_kwargs:
    app_key: ${env.MAP_APP_KEY}
    app_secret: ${env.MAP_APP_SECRET}
    user_id: ${env.MAP_USER_ID}
```

部署到 NAC 时**通过 Playground 的"沙箱环境变量"设置**，或者在调 `/agent-api/chat` 时通过 `variables.sandbox_env` 注入：

| 环境变量 | 含义 | 必填 |
|----------|------|------|
| `MAP_APP_KEY` | MAP 平台分配的 app key | ✅ |
| `MAP_APP_SECRET` | MAP 平台分配的 app secret | ✅ |
| `MAP_USER_ID` | 调用账号的 user id | ✅ |
| `MAP_HOST` | MAP 平台地址，默认指向测试环境 | 可选 |
| `MAP_VERIFY_SSL` | 是否校验 SSL，默认 false（自签名） | 可选 |
| `MAP_VECTOR_KB_ID` | 默认 KB id；配置后，用户未指定 KB 时即使用此值 | 可选 |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | 模型配置 | ✅ |

**切勿**将密钥写入 `agent.yaml` 或 `systemprompt_vb.md`，否则会被纳入版本库。

---

## 可选的微调

### 按业务领域调整同义词表

`systemprompt_vb.md` 的"铁律 2 — 同义词扩召回"中有一张对照表：

```markdown
| 用户查询词 | 建议同时尝试的相关词 |
|---|---|
| 政务      | 政府、公共事务、行政 |
| 外商投资  | 外资、对外开放、涉外投资 |
| 党建      | 党的建设、党组织、党务 |
| 保密      | 信息安全、机密、涉密 |
```

> ✏️ **按你自己的业务术语替换这张表**。其作用是引导 LLM：用户查询"政务"时不要只搜"政务"，应同时检索"政府/行政"——因为入库语料与查询用词之间常存在偏差。

向量检索的命中质量主要由 query 质量决定。在 Prompt 中专门用一节指导 Agent 构造 query，效果优于调整任何工具参数。

"铁律 1 — 关键词只保留核心名词"一节为通用规则，不依赖领域，保留即可：

```markdown
- ✅ 1-3 个汉字 / 1 个名词短语
- ❌ 不应包含疑问词（"是什么"/"如何"/"为何"）、连词、句号问号
- ❌ 不应将整句作为 query
```

### 隐藏不想暴露的检索工具

默认 `agent.yaml` 暴露了 4 个 MAP 检索工具：

- `map_vec_batch_search` —— 主力，cross-product 批检索
- `map_vec_semantic_search` —— 备用：纯语义
- `map_vec_fulltext_search` —— 备用：BM25
- `map_vec_get_chunks_by_file` —— 取同源文档其他切片

**经验值**：Agent 可见的工具越多，"选错工具"的概率越大。如果你的业务场景中某个工具不希望 Agent 调用（例如不希望它走纯 BM25），将 `agent.yaml` 中对应段落**注释掉**即可：

```yaml
tools:
  - name: map_vec_batch_search
    yaml_path: tools/map_vec_batch_search.tool.yaml
    binding: custom_tools.map_api:map_vec_batch_search
    extra_kwargs: { ... }

  - name: map_vec_get_chunks_by_file
    yaml_path: tools/map_vec_get_chunks_by_file.tool.yaml
    binding: custom_tools.map_api:map_vec_get_chunks_by_file
    extra_kwargs: { ... }

  # # 经验上 batch_search 已能覆盖大部分场景，下面两个备用默认可隐藏。
  # # 若某些 KB hybrid 不可用、或希望 Agent 显式选择检索方式，再行恢复。
  # - name: map_vec_semantic_search
  #   yaml_path: tools/map_vec_semantic_search.tool.yaml
  #   binding: custom_tools.map_api:map_vec_semantic_search
  #   extra_kwargs: { ... }
  #
  # - name: map_vec_fulltext_search
  #   yaml_path: tools/map_vec_fulltext_search.tool.yaml
  #   binding: custom_tools.map_api:map_vec_fulltext_search
  #   extra_kwargs: { ... }
```

采用注释而非删除，是为了将来重新启用时可直接取消注释，无需从 git history 中查找。

> ⚠️ **请勿注释 `map_vec_batch_search`**——它是主力工具，且兼容 N=1 的单查询场景（`queries=["x"]`, `knowledge_base_ids=["y"]`），单查询与批量查询均通过它完成。

**通用原则**：当工具同时存在"易用但低效"与"稍复杂但高效"两种调用方式时，**应隐藏低效的那一个**。Agent 不可见的工具便不会调用。

### 检索调优参数

`systemprompt_vb.md` 中"调优参数"一节给出了 `top_k / score_threshold / weights` 的默认值。一般场景**使用默认值即可**，需要调整时仅改对应数值：

| 参数 | 默认 | 何时调整 |
|------|------|----------|
| `top_k` | 5 | KB 内容稀疏、希望多返回几条 → 调至 8-10 |
| `score_threshold` | 0 | 噪声较多、需过滤低相关结果 → 调至 0.3-0.5 |
| `weights` | `[0.5, 0.5]` | 业务更看重精确关键词 → BM25 权重提升至 0.7 |

`agent.yaml` 顶级的几项配置一般也无需调整：

| 配置 | 取值 | 说明 |
|------|------|------|
| `temperature` | 0.2 | 法规/政策类回答需要精确引用，不宜调高 |
| `max_iterations` | 50 | 多轮搜索 + fallback + 取同源切片可能需要 10+ 轮 |
| `tool_call_mode` | structured | OpenAI 风格 function calling |
| `ContextCompactionMiddleware.threshold` | 0.6 | 上下文使用超过 60% 时自动压缩 |

---

## 已封装的内置机制

以下是 `custom_tools/map_api.py` 已经实现的机制——**无需编写任何代码即可使用**。本节旨在让你了解后台正在发生的行为（看到 fallback 标记、结果字段不完整时便于理解），而非要求你修改代码。

### 批检索 + 并发

`map_vec_batch_search` 接受 `queries[] × knowledge_base_ids[]`，内部对 cross-product 执行 8 路并发。20 个 (query, kb) pair 在数秒内完成，Agent 不会陷入"逐个串行查询"的退化模式。

工具的 description 中也已明确说明：即使是单查询、单 KB 的场景，也通过该工具完成（`queries=["x"]`, `knowledge_base_ids=["y"]`）。

### 搜索结果信噪比优化

平台原始 hit 包含十余个字段（`id / score / document_id / segment_id / metadata` 等），其中许多对 LLM 不具备决策价值——id 类字段没有"按 id 取 chunk"的对应端点可用，`score` 在不同 query 之间量纲不一致，反而会误导 LLM。

工具内部已完成字段裁剪，返回给 Agent 的 hit 仅保留 4-5 个有效字段：`page_content / title / file_name / file_id / segment_position`。20 个 hit 可节省数千 token。

### 服务端 bug 自动 fallback

实测发现 MAP 平台部分早期 KB 的 hybrid 端点会报 `'Document' object has no attribute 'id'` 或 `不支持全文检索`——属服务端 bug，不在我们的修复范围内。

工具层已精准识别这两类错误并自动降级至 semantic 端点，降级后的 hit 会附带 `fallback: true` 标记。systemprompt 也已指导 Agent：遇到 `fallback: true` 时在回答中简短说明"该结果为降级返回，质量可能略差"。

> 💡 若回答中偶尔出现"…来自向量库 X 的结果是降级后的…"等说明，属于正常机制行为，并非缺陷。

### Hybrid body 完整性

MAP 的 `/retriever_search_docs` 端点存在一个隐藏问题：若 body 简化（仅传 `query` + `knowledge_base_id`），服务端**不会报错**，但会静默退化为 semantic（并未真正执行 hybrid）。工具层已显式携带 `retriever_type=hybrid` + 完整 `config`，确保 hybrid 真正生效。

### 鉴权 / token 缓存 / transient 重试

`MapClient` 使用进程级 token 缓存，本地计时到期后自动重取。MAP 有时会用 `HTTP 500 + state=28201`（或“token无效”）表示鉴权失败；客户端会清除被拒绝的旧 token、现场获取最新 token，并重试原请求一次。按 token 值做条件失效，可避免并发请求误删另一个请求刚刷新的 token。`state=20001` 的 transient 错误则最多退避重试两次。你仅需配置 `MAP_APP_KEY/SECRET/USER_ID` 三个 env，其余无需关心。

---

## 注意事项

### 鉴权密钥安全

`MAP_APP_KEY` / `MAP_APP_SECRET` / `MAP_USER_ID` 均通过 `extra_kwargs: ${env.XXX}` 注入。**切勿**将其写入 `tool.yaml` 或 `systemprompt.md`。

### 速度上限

主要瓶颈在于上游 MAP 服务器的响应时间，而非 Agent 本身。可采取的优化措施：

1. **在 systemprompt 中压缩 query 数量**——5 个同义词的边际收益已较低，3 个即可
2. **过滤历史上持续 fallback 的 KB**——已知会失败的 hybrid 调用直接跳过
3. **新增会话级 LRU cache**（若同一 session 内会重复查询相似问题）

不建议修改 NAC runtime pod 的资源——agent-runtime 容器无显式 CPU 限制，瓶颈仅在外网 RTT 与并发上限。

### Score 量纲不一致

`map_vec_batch_search` 内部按 raw score 排序。但 hybrid hit 的 score（bm25 + 向量加权）与 fallback 后的 semantic hit 的 score（纯余弦）**量纲不同**，混合排序时 fallback hit 可能不公平地排在前列。本案例选择不做 normalize（业务可接受）；若你的场景对排序公平性敏感，需要在 `map_api.py` 中引入加权 penalty 或强制将非 fallback 结果排在前列——这已超出本教程的微调范围。

### MAP 平台 SSL 自签名

测试环境使用自签名证书。`_map_client.py` 默认 `verify=False`，可通过设置 `MAP_VERIFY_SSL=true` 选择性启用校验。生产环境的 MAP 部署若使用正式证书，应启用 `MAP_VERIFY_SSL`。

### ContextCompactionMiddleware import 路径

`agent.yaml` 中 middleware 的 import 路径为 `nexau.archs.main_sub.execution.middleware.context_compaction:ContextCompactionMiddleware`——注意包含 `execution` 这一层，缺失会引发 import 错误。参数名是 `threshold`，而非 `threshold_ratio`。

---

## 下一步

替换 KB 映射表并配置好鉴权环境变量后，前往 NAC Playground 用真实问题进行验证：

- 询问一个白名单内的主题 → 检查 Agent 是否正确选择了 KB
- 故意询问表外主题 → 检查 Agent 是否如实告知"不在可用 KB 列表内"
- 使用一个会触发 fallback 的旧 KB → 检查回答中是否包含降级说明
- 询问一个用词不一致的问题（如以"政务"查询"政府"相关内容） → 检查 Agent 是否启用了同义词扩召回

← 返回 [Cookbook 主页](../README.md) ｜ 相关阅读：[金融数据智能体（封装 REST API）](../金融数据智能体/tutorial-finance-agent.md) ｜ [劳动法问答（自建 SKILL）](../劳动法问答/tutorial-agentic-rag-agent.md)
