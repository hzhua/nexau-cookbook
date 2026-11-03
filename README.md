# NexAU Cookbook · 政务 Agent 实战教程

> 12 个来自真实政务与企业场景的 Agent 样例，从最简单的政策问答、API/数据库接入，到 MCP、文档生成、完整业务审核流水线、联网检索和内容安全护栏，循序渐进，每一篇都配有详细的 TUTORIAL。读完这些样例，你就掌握了用 **North Agent Cloud（NAC）** 构建业务智能体的完整方法论。

> 💡 **NexAU 是一个开源项目**。本 Cookbook 聚焦「**怎么用**」——面向政务业务开发者；如果你想深入了解 NexAU 框架**本身的源代码、架构与设计文档**，请访问官方仓库：
>
> **https://github.com/nex-agi/NexAU**
>
> 仓库包含：框架源码、内置工具实现、完整官方文档（`docs/core-concepts/`、`docs/rfcs/`）、更多示例与模板。

---

## 目录

- [这份教程适合谁](#这份教程适合谁)
- [学习路径（推荐顺序）](#学习路径推荐顺序)
- [按场景快速定位](#按场景快速定位)
- [按技术模式快速定位](#按技术模式快速定位)
- [核心概念速览](#核心概念速览)
- [样例总览表](#样例总览表)
- [如何使用本 Cookbook](#如何使用本-cookbook)

---

## 这份教程适合谁

- 需要为政府部门/事业单位构建 AI 应用的开发者
- 已经会写后端代码，但第一次做 Agent / LLM 应用
- 想把已有的政策文档、业务系统、审核流程「Agent 化」

> 不需要你懂 RAG、向量数据库、Prompt Engineering —— 跟着走就行。

**准备工作**：读一遍 [**开始之前：样例项目的通用约定**](./GETTING_STARTED.md)，了解通用目录结构、上传到 NAC 的流程、以及环境变量配置。

---

## 学习路径（推荐顺序）

每篇 tutorial 都是独立的，但**强烈建议按顺序读**——后面的样例会复用前面的概念，跳着看容易漏掉关键心智模型。

### 🟢 Level 1 · 入门：Agent 的两种基础能力

Agent 做事只有两个信息来源：**读知识**（Skill）和 **调工具**（Tool）。Level 1 的两个样例正好对应这两种最基础的能力。

#### 1️⃣ [劳动法问答](./劳动法问答/) — *从知识中找答案：Agentic RAG*

> 📄 [tutorial-agentic-rag-agent.md](./劳动法问答/tutorial-agentic-rag-agent.md) · ⏱ 约 30 分钟

从一份《劳动法》知识库开始，理解什么是 Agentic RAG、为什么层级化 `SKILL.md` 比向量数据库更可控。

**学到**：
- 传统 RAG vs Agentic RAG 的区别
- 多跳路由：Agent 如何像查百科全书一样层层下钻
- `SKILL.md` 的编写规范与 BFS 构建流程

#### 2️⃣ [金融数据智能体](./金融数据智能体/) — *从外部 API 拿数据：自定义工具*

> 📄 [tutorial-finance-agent.md](./金融数据智能体/tutorial-finance-agent.md) · ⏱ 约 45 分钟

接入 Finnhub 财经数据 API，让 Agent 能查股价、财报、公司新闻、IPO 日历等**实时数据**。这一篇让你学会如何把**任何一个 REST API** 封装成 Agent 可以调用的工具。

**学到**：
- `.tool.yaml` 工具声明的编写方法
- `custom_tools/` 下用 Python 实现 API 调用
- API Key、超时、错误处理怎么对 Agent 友好
- 多工具并存时如何写 `description`，让模型选对工具

---

### 🟡 Level 2 · 进阶：换范式

切换到数据库、MCP 和外部知识库平台等更工程化的接入方式。

#### 3️⃣ [企业数据库问数](./企业数据库问数/) — *Text-to-SQL：让 Agent 答对数据库问题*

> 📄 [tutorial-text-to-sql.md](./企业数据库问数/tutorial-text-to-sql.md) · ⏱ 约 1 小时

7 张表、50 家企业，用户自然语言提问，Agent 答对 SQL。这一篇是**数据库场景下如何写 SKILL.md 的手把手教学**。

**学到**：
- 审计数据库：列含义、类型陷阱、业务规则
- 把业务语义、枚举、跨表 JOIN 范式写进 SKILL.md
- `description` 怎么写才能让模型主动调用
- 测试与迭代的闭环：让 Agent 越用越准

#### 4️⃣ [数据库 MCP 接入](./数据库%20MCP%20接入/) — *连接外部 HTTP MCP 服务接入业务数据库*

> 📄 [tutorial-database-mcp.md](./数据库%20MCP%20接入/tutorial-database-mcp.md) · ⏱ 约 1 小时

把企业经营 DM8 数据库接到外部 HTTP MCP 服务，让 Agent 通过 `mcp_servers` 连接已部署的数据库工具，并通过 Skill 理解表结构和业务口径。

**学到**：
- HTTP MCP 服务在 `agent.yaml` 中的配置方式
- 生产环境如何连接实际 MCP 服务
- 只读 SQL、单语句限制、行数限制为什么必须写在 server 侧
- MCP 与 Skill 如何分工：MCP 负责连接，Skill 负责语义

#### 5️⃣ [达梦数据库](./达梦数据库/) — *custom_tools + dmPython 一个 zip 自包含*

> 📄 [tutorial-dameng-sql-agent.md](./达梦数据库/tutorial-dameng-sql-agent.md) · ⏱ 约 45 分钟

跟上一篇 MCP 接入做同一个企业问数场景，但把数据库访问做成 NexAU `custom_tools` 直连达梦（agent runtime 进程持有 dmPython），**一个 zip 自包含、无外挂服务**。中途顺手解 dmPython `[CODE:-70089]` 加密模块加载失败这个老 bug。

**学到**：
- `custom_tools/` + native 驱动 vs 外部 MCP 服务的取舍
- `nexau.json` `setup` 在 runtime 启动时**离线**装额外依赖（同一套技巧适用于任何 NAC 不自带的库：clickhouse / 内网 SDK / 锁版本依赖）
- airgap whl 打包，多架构 wheel（x86_64 + aarch64）共存自动挑选
- `${env.XXX}` 连接串注入 + 工具 schema 隔离敏感凭证
- dmPython `[-70089]` 真实根因 + `dmpython.libs/` 软链 workaround

#### 6️⃣ [MaaS 平台对接](./MaaS%20平台对接/) — *检索 MaaS 知识库 + 调用 MaaS 智能体*

> 📄 [tutorial-maas-platform.md](./MaaS%20平台对接/tutorial-maas-platform.md) · ⏱ 约 1 小时

对接 MaaS 平台的两类能力，不依赖 MCP：一是复用平台上的既有向量知识库做检索（不重建索引），二是调用平台上已配置好的智能体。样例封装了鉴权、批检索、并发、结果裁剪和 fallback，以及智能体的会话管理与流式聚合，开发者只需要替换自己的 KB 映射表并配置环境变量。

**学到**：
- 如何把第三方检索 API 封装成 Agent 工具
- 多 KB 并行检索和同义词扩召回
- 鉴权、token 缓存、重试和结果裁剪的工程化处理
- 如何调用外部平台上的智能体：会话管理、流式 SSE 聚合、日志脱敏

---

### 🔴 Level 3 · 实战：完整审核流

从对话式 Agent 转向文档流水线和完整政务审核 Agent。

#### 7️⃣ [公文写作](./公文写作/) — *多技能协作生成 Word 公文*

> 📄 [tutorial-govdoc-writer.md](./公文写作/tutorial-govdoc-writer.md) · ⏱ 约 1.5 小时

输入 PDF 参考材料和写作要求，输出符合国家标准的 Word 公文。样例组合了 PDF 解析、公文写作知识和 docx 渲染引擎。

**学到**：
- 多 Skill 协作的文档生成流水线
- Schema 内联如何减少 JSON 构造错误
- 需要编译的输出引擎如何封装成稳定脚本

#### 8️⃣ [住房公积金审核](./住房公积金审核/) — *集大成的业务审核 Agent*

> 📄 [tutorial-house-fund-review.md](./住房公积金审核/tutorial-house-fund-review.md) · ⏱ 约 1.5 小时

上海市住房公积金提取审核 Agent。把前面样例的套路——三层分离、Skill 化知识、PDF 处理、边界防御、结构化输出——全部组合起来，交付一个真实可用的政务审核智能体。

**学到**：
- 8 大设计原则构建专业 Agent 的完整方法论
- System Prompt 当「指挥官」：只讲流程，不掺知识
- Skill 作为可插拔的「领域大脑」
- 用表格代替散文、输出格式即质量控制
- 动手实践：把这套方法应用到你自己的业务

---

### 🔗 架构模式 · 一个制品放多个 Agent

#### ⑫ [法律公文双助手](./法律公文双助手/) — *MultiAgent 制品：平级的多个 Agent 共享一次部署*

> 📄 [tutorial-multi-agent-artifact.md](./法律公文双助手/tutorial-multi-agent-artifact.md) · ⏱ 约 45 分钟

把 ① 劳动法问答 和 ⑦ 公文写作**原样装进一个制品**：两个 Agent 平级、互不知道对方存在，共享一次部署与一套环境变量，调用时用 `agent` 字段选。

**学到**：
- `nexau.json` 注册多个 Agent，以及为什么每个 Agent 的目录**几乎不用改**就能搬进来
- 平级 MultiAgent 与 `sub_agents` 委派的区别：**谁来决定用哪个 Agent**
- 运行时隔离的边界：上下文 / skills / 工具各自独立，制品与环境变量共享
- chat 请求**必须传 `agent` 字段**（漏了直接 400，而控制台生成的示例不含它）
- skill 目录 basename 跨 Agent 共用命名空间，重名会撞车

### 🛡️ 横切能力 · 给任何 Agent 加一道护栏

业务做对了，还要「不能说错话」。横切能力不绑定具体业务场景，写一次、所有样例通用。

#### ⑨ [敏感词过滤](./敏感词过滤/) — *内容安全护栏：输入/输出/工具结果三路拦截*

> 📄 [tutorial-sensitive-word-guardrail.md](./敏感词过滤/tutorial-sensitive-word-guardrail.md) · ⏱ 约 30 分钟 · ⚠️ 需包含 RFC-0027 的 NexAU 版本（暂未合入 main）

不改一行业务代码，在 `agent.yaml` 里加一段 `middlewares` 声明，给任意已有 Agent 套上确定性的敏感词拦截：违规输入不进模型、违规输出不返回用户、工具读到的敏感材料不进上下文。

**学到**：
- 为什么内容安全要用 Middleware 兜底，而不是靠 prompt 约束
- 输入 / 输出 / 工具结果三路拦截的时机与事件序列
- 词库「目录 = 类别」的组织方式，以及为什么必须用绝对路径
- 命中后的可观测性：拒绝文案、`ContentBlockedEvent`、审计日志

#### ⑩ [聚合搜索](./聚合搜索/) — *一个工具接口，背后可换四家搜索服务商*

> 📄 [tutorial-aggregated-web-search.md](./聚合搜索/tutorial-aggregated-web-search.md) · ⏱ 约 45 分钟

给 Agent 接上**公网实时信息**。把 Serper / 豆包搜索 / 百度 AI 搜索 / 北坡聚合搜索封装成同一个工具接口，19 个检索参数（时效、站点、权威度、行业、正文粒度……）全部开放给模型；换服务商只改一个环境变量，不动代码、不重新打包。

**学到**：
- Provider（调哪家）与 Engine（结果来自哪个引擎）为什么必须分成两层
- 服务商能力不齐时的三条原则：不报错、能降级就降级、**但必须留痕**
- 参数三级优先级（调用传参 > 环境变量 > 内置默认）与 `None` 哨兵的必要性
- 密钥为什么走 **Runtime** 环境变量而不是沙箱环境变量
- 检索型 Agent 必须在 prompt 里立「检索预算」，否则会得到 `status=completed` 但**空回复**

#### ⑪ [主动提问](./主动提问/) — *信息不足时问清楚，而不是猜*

> 📄 [tutorial-ask-user-interaction.md](./主动提问/tutorial-ask-user-interaction.md) · ⏱ 约 30 分钟

用户第一句话往往是不完整的（「帮我写份周报」——写哪周？给谁看？）。这一篇让 Agent 学会在信息不足时用 `ask_user` 问清楚再动手，并讲透一个静默的坑：**`ask_user` 必须配 `stop_tools`，否则 Agent 提完问不会停，会自己编一个答案继续跑**。

**学到**：
- 停止型工具（stop tool）的语义，以及漏写 `stop_tools` 为什么不报错却会出错
- 三种问题类型（`choice` / `text` / `yesno`）怎么选，空 `options` 为什么会被拒
- 约束「什么时候**不该**问」比「什么时候该问」更影响交互质量
- 通过 API 完成两轮 chat 的完整对接（含从 SSE 拼接问题）

---

## 按场景快速定位

> 「我只想抄一个最接近我业务的例子」的开发者走这里：

| 你要做…… | 直接看 |
|---------|--------|
| **政策/法规咨询问答** | ① 劳动法问答 |
| **接入外部业务 API（天气、数据、第三方服务等）** | ② 金融数据智能体 |
| **连接业务数据库做自然语言问数** | ③ 企业数据库问数 |
| **把数据库封装成可复用 MCP 服务** | ④ 数据库 MCP 接入 |
| **runtime 需要装 NexAU 自带不带的依赖（native 驱动 / 专有 SDK）** | ⑤ 达梦数据库 |
| **复用已有向量库 / 知识库平台** | ⑥ MaaS 平台对接 |
| **调用已有平台（MaaS）上的智能体** | ⑥ MaaS 平台对接 |
| **根据材料生成 Word 公文** | ⑦ 公文写作 |
| **材料审核 / 合规核查 / 表单校验** | ⑧ 住房公积金审核 |
| **内容安全 / 敏感词拦截 / 上线合规评审** | ⑨ 敏感词过滤 |
| **联网检索实时信息 / 政策情报 / 多搜索服务商切换** | ⑩ 聚合搜索 |
| **需求澄清 / 交互式确认 / 让 Agent 别瞎猜** | ⑪ 主动提问 |
| **一个制品提供多种并列服务 / 多 Agent 共享部署** | ⑫ 法律公文双助手 |

---

## 按技术模式快速定位

> 从**实现套路**而不是业务场景出发：

| 技术模式 | 对应样例 |
|---------|---------|
| **Agentic RAG** + 层级化知识库 | ① 劳动法、⑧ 住房公积金 |
| **自定义工具封装外部 API** | ② 金融数据智能体 |
| **Text-to-SQL** via SKILL.md | ③ 企业数据库问数 |
| **自建 MCP server 接入数据库** | ④ 数据库 MCP 接入 |
| **`nexau.json` `setup` 安装离线 whl / 额外依赖** | ⑤ 达梦数据库 |
| **第三方向量库 / 检索 API 接入** | ⑥ MaaS 平台对接 |
| **调用外部平台智能体（会话管理 + 流式聚合）** | ⑥ MaaS 平台对接 |
| **多技能协作 + 文档生成** | ⑦ 公文写作 |
| **多工具协同 + 中间件** | ⑧ 住房公积金（多 Skill） |
| **严格输出契约**（Markdown 报告） | ⑧ 住房公积金 |
| **Middleware 确定性拦截**（内容安全护栏） | ⑨ 敏感词过滤 |
| **多服务商适配器**（一个接口封装多家 API） | ⑩ 聚合搜索 |
| **环境变量驱动的部署级配置**（参数默认值可覆盖） | ⑩ 聚合搜索 |
| **交互式提问 + 停止型工具**（`ask_user` / `stop_tools`） | ⑪ 主动提问 |
| **MultiAgent 制品**（`nexau.json` 注册多个平级 Agent） | ⑫ 法律公文双助手 |

---

## 核心概念速览

读 tutorial 前先认识几个高频术语：

| 术语 | 一句话解释 | 对应文件 |
|------|---------|---------|
| **Agent** | 一个能接任务、自主调用工具、给出结果的 AI 角色 | `agent.yaml` |
| **System Prompt** | 告诉 Agent 它是谁、要做什么、怎么输出 | `systemprompt.md` |
| **Skill** | 一块**可插拔的领域知识**，由 `SKILL.md` + 相关文件组成 | `skills/<name>/SKILL.md` |
| **Tool** | Agent 可以调用的**函数/能力**，内置（读文件、shell）或自定义（调 API） | `tools/*.tool.yaml` |
| **MCP server** | 通过 Model Context Protocol 暴露的一组外部工具，Agent 启动时自动发现 | `agent.yaml` 里的 `mcp_servers` |
| **Agentic RAG** | 不用向量库，让 Agent 通过读层级化 `SKILL.md` **自己翻目录**找知识 | `skills/**/SKILL.md` |
| **nexau.json** | 项目清单：声明哪些 Agent 要发布、哪些文件要排除 | `nexau.json` |
| **Middleware** | 挂在 Agent 执行前后的**拦截器**，如压缩上下文、切分超长输出、敏感词拦截 | `agent.yaml` 里声明 |
| **NAC** | North Agent Cloud，部署与托管 Agent 的平台 | 网页控制台 |

想要完整的运行时机制、文件布局、上传流程，看 [GETTING_STARTED](./GETTING_STARTED.md)。

---

## 样例总览表

| # | 样例 | 场景 | 核心模式 | 难度 | 预计时长 |
|---|------|------|---------|------|---------|
| 1 | [劳动法问答](./劳动法问答/) | 劳动法咨询 | Agentic RAG | 🟢 入门 | 30 min |
| 2 | [金融数据智能体](./金融数据智能体/) | 财经数据查询（股价/财报/新闻） | 自定义工具封装 REST API | 🟢 入门 | 45 min |
| 3 | [企业数据库问数](./企业数据库问数/) | 自然语言查企业库 | Text-to-SQL via SKILL.md | 🟡 进阶 | 1 h |
| 4 | [数据库 MCP 接入](./数据库%20MCP%20接入/) | 自建 MCP 连接数据库 | MCP server + 只读 SQL | 🟡 进阶 | 1 h |
| 5 | [达梦数据库](./达梦数据库/) | custom_tools + dmPython 直连达梦 | airgap whl + `nexau.json` setup | 🟡 进阶 | 45 min |
| 6 | [MaaS 平台对接](./MaaS%20平台对接/) | 检索 MaaS 知识库 + 调用 MaaS 智能体 | 检索 / 智能体 API 工具封装 | 🟡 进阶 | 1 h |
| 7 | [公文写作](./公文写作/) | 根据材料生成 Word 公文 | 多 Skill + docx 渲染 | 🔴 实战 | 1.5 h |
| 8 | [住房公积金审核](./住房公积金审核/) | 公积金提取材料审核 | 多工具 + 多 Skill + 严格输出 | 🔴 实战 | 1.5 h |
| 9 | [敏感词过滤](./敏感词过滤/) | 内容安全护栏（合规上线） | SensitiveWordMiddleware 三路拦截 | 🟢 入门 | 30 min |
| 10 | [聚合搜索](./聚合搜索/) | 联网检索政策情报 | 多服务商适配器 + 环境变量配置 | 🟡 进阶 | 45 min |
| 11 | [主动提问](./主动提问/) | 需求澄清 / 交互式确认 | `ask_user` + `stop_tools` 停止语义 | 🟢 入门 | 30 min |
| 12 | [法律公文双助手](./法律公文双助手/) | 一个制品同时提供咨询与公文两种服务 | MultiAgent 制品（多 Agent 平级注册） | 🟡 进阶 | 45 min |

---

## 如何使用本 Cookbook

1. **第一次来**：读 [GETTING_STARTED](./GETTING_STARTED.md)，了解通用结构和上传 NAC 的流程
2. **想系统学**：从 ① 劳动法问答 开始，按编号往后走
3. **赶时间**：查 [按场景快速定位](#按场景快速定位)，找最接近的例子复制改造
4. **遇到问题**：先翻 [GETTING_STARTED 的常见问题](./GETTING_STARTED.md#常见问题)，再看对应样例 TUTORIAL 结尾的「注意事项」

---

## 反馈与交流

这些样例都经过真实业务场景打磨，但你的业务一定有它的特殊性。如果遇到：

- Tutorial 里没说清楚的点
- 你的场景在本 cookbook 里找不到类似的套路
- 想贡献新的样例

欢迎联系北坡团队。祝学习顺利 🚀

---

## 附录

- **内置工具完整参考**: 了解 NexAU Agent 可利用的全部内置工具（文件操作、Shell、Web 搜索、会话管理等），详见 [内置工具附录](附录/内置工具附录.md)。

---

## 参阅更多资料

NexAU 是开源项目，关于**框架本身**的一切——源码、内置工具实现、核心概念文档、RFC、更多示例——都在这里：

**GitHub 仓库**：<https://github.com/nex-agi/NexAU>

本 Cookbook 只是 NexAU 的「政务业务应用」切片；需要了解底层机制（Agent 执行、中间件、Skill 加载等）时，请查阅仓库内 `docs/core-concepts/` 与 `docs/rfcs/`。
