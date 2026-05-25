# 教程：Agentic RAG 智能体 — 劳动法问答

> 🟢 **Level 1 · 入门** ｜ ⏱ 约 30 min ｜ 🔑 Agentic RAG + 层级化 Skill 知识库
>
> 以「劳动法 QA」为实际案例，讲解如何用层级化 Skill 知识库构建 Agentic RAG Agent。这是整个 Cookbook 的起点——理解了这一篇，后面的样例只是在此基础上「扩规模」或「换产出形态」。

## 目录

- [什么是 Agentic RAG](#什么是-agentic-rag)
- [总体架构](#总体架构)
- [设计要点 1：三层分离——Prompt 管流程，Skill 管知识，References 管原文](#设计要点-1三层分离prompt-管流程skill-管知识references-管原文)
- [设计要点 2：让 Agent 查资料而不是背资料](#设计要点-2让-agent-查资料而不是背资料)
- [设计要点 3：层级化知识库——多跳路由取代全量索引](#设计要点-3层级化知识库多跳路由取代全量索引)
- [设计要点 4：路径导航是最重要的 Prompt 工程](#设计要点-4路径导航是最重要的-prompt-工程)
- [设计要点 5：防御性设计——Agent 会犯的错](#设计要点-5防御性设计agent-会犯的错)
- [设计要点 6：输出格式即质量控制](#设计要点-6输出格式即质量控制)
- [完整配置文件](#完整配置文件)
- [如何构建层级化知识库](#如何构建层级化知识库)
- [实际案例：劳动法 QA 的改造过程](#实际案例劳动法-qa-的改造过程)
- [注意事项](#注意事项)

---

## 什么是 Agentic RAG

传统 RAG（检索增强生成）依赖向量数据库：用户提问 → 向量检索 → 取回文本片段 → 拼入上下文。

**Agentic RAG 不需要向量库**。它让 Agent 自己"翻目录、查资料"——就像一个人查百科全书：先看目录，翻到对应章节，再读具体内容。

```
读 references/SKILL.md（根索引）
  → 判断进入哪个子目录
    → 读 子目录/SKILL.md（子索引）
      → 判断继续下沉还是直接读文件
        → read_file 具体知识文件
```

每个目录有一个 `SKILL.md` 充当索引，描述"这个目录下有什么"。Agent 逐层读索引、缩小范围，最终精准定位到需要的文件。

---

## 总体架构

一个 Agentic RAG Agent 制品的结构：

```
my-agent/
├── nexau.json                      ← Agent 注册表
├── agent.yaml                      ← 骨架：模型、工具、中间件
├── systemprompt.md                 ← 指挥官：角色 + 流程 + 导航规则
├── tools/                          ← 手脚：标配三件套
│   ├── read_file.tool.yaml
│   ├── search_file_content.tool.yaml
│   └── run_shell_command.tool.yaml
└── skills/                         ← 领域大脑：层级化知识库
    └── my-knowledge/
        ├── SKILL.md                ←   触发条件 + 知识框架 + 导航指引
        └── references/             ←   知识原文（层级化组织）
            ├── SKILL.md
            ├── 主题A/
            └── 主题B/
```

核心理念：**每一层只做一件事，层与层之间通过文件路径松耦合**。

---

## 设计要点 1：三层分离——Prompt 管流程，Skill 管知识，References 管原文

大多数人写 Agent 会把所有内容塞进一个巨大的 System Prompt。这个方案把内容分成三层：

| 层级 | 文件 | 职责 | 变更频率 |
|------|------|------|---------|
| 指挥层 | `systemprompt.md` | 角色定义、处理流程、导航规则、输出规范 | 低 |
| 知识层 | `skills/.../SKILL.md` | 知识框架、主题索引、检索指引 | 中 |
| 原文层 | `skills/.../references/**` | 知识原文（法规、手册、数据等） | 几乎不变 |

**为什么这样做？**

```
❌ 反面教材：把所有知识塞进 Prompt
   → 知识量大时 context 直接爆
   → 知识更新要重新测试整个 Agent
   → Agent 可能"记混"不同来源的内容

✅ 本方案的做法：按需加载
   → System Prompt 只有几十行，告诉 Agent "怎么干活"
   → Skill 几百行，告诉 Agent "去哪找什么"
   → 知识原文只在需要时才 read_file 读取
```

**关键收益**：知识更新时，只需替换 `references/` 下的文件，不需要改动 Agent 的任何逻辑。

---

## 设计要点 2：让 Agent 查资料而不是背资料

这是最关键的架构决策。

```
方案 A：把知识塞进 Prompt 让 Agent "背"
  → Agent 回答的是"记忆中的大意"
  → 数字、条款号、公式等细节极易出错
  → 知识量一大就超出上下文限制

方案 B（本方案）：知识存文件，Agent 按需查阅
  → Agent 引用的是原文，不是"印象"
  → 精确到条款号、金额、公式
  → 知识库可以无限扩展（Agent 只加载需要的部分）
  → 代价：多几次 read_file 调用（完全可接受）
```

Skill 中的**知识索引**是连接 Agent 和知识文件的桥梁：

```markdown
## 子目录

- `法律条文/` — 7 部核心法律原文（按合同用工/保障保险/争议解决分类）
- `赔偿计算/` — N/N+1/2N、加班费、双倍工资、年假工资计算方法
- `纠纷处理/` — 8 类常见纠纷处理指南（按合同/待遇/特殊分类）
```

这段索引同时教了 Agent 三件事：
1. **文件在哪** — 5 个主题目录
2. **优先级是什么** — 描述中的关键词帮助 Agent 匹配用户问题
3. **规模多大** — 文件数量帮助 Agent 评估搜索范围

---

## 设计要点 3：层级化知识库——多跳路由取代全量索引

知识库小的时候（几个文件），扁平组织没问题。但当文件数量增长到几十上百个时，扁平索引的问题就暴露了：

| | 扁平知识库 | 层级化知识库 |
|---|---|---|
| **Agent 行为** | 一次加载全量索引（可能数千条） | 每跳只读当前层索引（几十条） |
| **Token 消耗** | 高（索引本身占大量上下文） | 低（按需加载） |
| **搜索精度** | 关键词匹配，容易搜到无关内容 | 按主题逐层缩小，精准定位 |
| **可维护性** | 新增文件只能扔进扁平列表 | 新增文件归入对应层级，结构清晰 |

层级化的思路很简单：**让 Agent 像翻百科全书一样，先看总目录，再翻到对应章，最后读具体页**。3-4 跳就能从几千个文件中精准定位到目标。

---

## 设计要点 4：路径导航是最重要的 Prompt 工程

Agentic RAG 中，**systemprompt 最重要的部分不是角色定义，而是路径导航**。如果 Agent 不知道怎么找到文件，再好的角色定义也没用。

### Agent 必须先加载技能

Agent 加载技能后会获得技能文件夹路径（`{path_to_skill_folder}`），这是读取所有知识文件的前提。systemprompt 中的工作流程第一步应该就是加载技能。

### 用具体示例教 Agent 导航

不要抽象地说"逐层读取"，直接给具体的跳数示例：

```markdown
例如加载技能后获得路径 `{path_to_skill_folder}`，则：

第1跳: read_file("{path_to_skill_folder}/references/SKILL.md")        → 看主题目录
第2跳: read_file("{path_to_skill_folder}/references/赔偿计算/SKILL.md") → 看文件列表
第3跳: read_file("{path_to_skill_folder}/references/赔偿计算/经济补偿金计算.md") → 读取内容
```

### 结构概览表必不可少

在 systemprompt 中列出知识库的顶层结构概览，让 Agent 第一时间就知道该进哪个目录：

```markdown
| 主题目录 | 内容 |
|---------|------|
| `法律条文/` | 7 部核心法律原文 |
| `赔偿计算/` | 各类赔偿金计算方法 |
| `纠纷处理/` | 8 类纠纷处理指南 |
```

没有这张表，Agent 每次都要先读根 SKILL.md 才能开始——多一跳、多消耗 token、多花时间。

---

## 设计要点 5：防御性设计——Agent 会犯的错

实际部署后，我们发现 Agent 有几个典型的"坏习惯"。必须在 systemprompt 和 SKILL.md 中做防御。

### 5.1 跳过 SKILL.md 直接猜文件名

**最常见的错误**。Agent 看到父级 SKILL.md 列出了子目录 `合同纠纷/`，就直接猜了一个文件名 `违法解除劳动合同纠纷处理.md`（实际叫 `违法解除劳动合同.md`）——文件不存在，报错。

**防御**：在 systemprompt 和 SKILL.md 中都写：

```markdown
🚫 禁止跳步：看到子目录名后必须先读该子目录的 SKILL.md，
   禁止凭猜测构造文件名——只读取 SKILL.md 中明确列出的文件。
```

### 5.2 不加载技能就开始读文件

Agent 不知道技能文件夹的实际路径，会猜 `/agent/skills/...` 或 `skills/...`，全部报错。

**防御**：在 systemprompt 的工作流程中明确"第一步：加载技能获取路径"。

### 5.3 在大目录上全局搜索

当 references 有几千个文件时，Agent 对根目录执行 `search_file_content` 会卡死。

**防御**：

```markdown
🚫 禁止操作
- 禁止对 references/ 根目录搜索（文件太多会超时）
- 搜索时指定到分类子目录
```

### 5.4 不查资料直接回答

即使有知识库，Agent 有时还是会跳过检索，直接凭"记忆"回答——而 LLM 的"记忆"经常不精确。

**防御**：

```markdown
⚠️ 禁止凭记忆回答。必须先查阅知识库，基于原文回答。
```

---

## 设计要点 6：输出格式即质量控制

定义输出格式不只是"好看"——它是质量控制机制。

**咨询类输出**：
```
## 结论                ← 结论先行
## 依据                ← 引用具体条款/章节
## 注意事项            ← 例外、限制、时效
```

**为什么这个格式有效**：
1. **"结论在前"** — 强制 Agent 先给答案再解释，避免长篇废话
2. **"引用条款号/出处"** — 可验证性，用户可以自己去查原文核实
3. **"注意事项"** — 强制 Agent 思考边界情况，而不是给出一个过于自信的回答

没有格式约束的 Agent 回答往往是"散文式的"——看起来很完整，但很难验证是否正确。

---

## 完整配置文件

以下是每个配置文件的完整内容，**可直接复制后按注释修改**。

### nexau.json

```json
{
  "agents": {
    "my_agent": "agent.yaml"
  }
}
```

- `my_agent`：Agent 名称（下划线命名），改成你的 Agent 名
- `agent.yaml`：指向 Agent 配置文件

### agent.yaml

```yaml
type: agent
name: my_agent
description: Agent 描述

system_prompt: ./systemprompt.md
system_prompt_type: jinja                    # ← 必须用 jinja，否则不会读取文件

llm_config:
  api_type: openai_chat_completion           # ← 必须显式指定
  model: ${env.LLM_MODEL}                   # ← 平台自动注入，不要硬编码
  base_url: ${env.LLM_BASE_URL}
  api_key: ${env.LLM_API_KEY}
  max_tokens: 8192
  temperature: 0.2                           # ← 知识问答建议 0.1-0.3
  stream: True

tools:                                       # ← Agentic RAG 标配三件套
  - name: read_file                          #    读取知识库文件
    yaml_path: tools/read_file.tool.yaml
    binding: nexau.archs.tool.builtin.file_tools:read_file
  - name: search_file_content                #    按关键词搜索文件内容
    yaml_path: tools/search_file_content.tool.yaml
    binding: nexau.archs.tool.builtin.file_tools:search_file_content
  - name: run_shell_command                  #    执行 shell 命令（ls/find 等辅助操作）
    yaml_path: tools/run_shell_command.tool.yaml
    binding: nexau.archs.tool.builtin.shell_tools:run_shell_command

skills:
  - ./skills/my-knowledge                    # ← 你的知识库 skill 目录

max_iterations: 50
max_context_tokens: 128000
tool_call_mode: structured

middlewares:                                 # ← 防止搜索结果撑爆上下文
  - import: nexau.archs.main_sub.execution.middleware.context_compaction:ContextCompactionMiddleware
    params:
      threshold: 0.6                         #    上下文使用超过 60% 时自动压缩
```

**与基本 Agent 的关键区别**：

| 配置项 | 基本 Agent | Agentic RAG Agent | 为什么 |
|--------|-----------|-------------------|--------|
| `tools` | 可以为空 | 标配三件套 | Agent 需要工具来读取和搜索知识库文件 |
| `skills` | 可以没有 | 指向知识库 skill 目录 | 知识库以 skill 形式组织 |
| `middlewares` | 通常不需要 | ContextCompactionMiddleware | 多次文件读取会膨胀上下文，需要自动压缩 |
| `temperature` | 按场景 | 建议 0.1-0.3 | 知识问答需要精确引用，不能"创造" |

**禁止配置**（由平台自动注入）：
- `sandbox_config` —— 平台按部署环境注入
- `tracers` —— 平台自动注入 Langfuse

### systemprompt.md

```markdown
你是一位[领域]专家，基于专业知识库为用户提供准确的解答。

## 工作流程

1. 阅读用户的问题，判断涉及哪个知识领域
2. 加载技能获取知识库路径
3. 通过 `read_file` 逐层导航知识库查找相关资料
4. 基于资料原文给出专业回答

## 知识库导航方法

你的知识库是层级化组织的。加载技能后，你会获得技能文件夹路径，
知识库位于其下的 `references/` 目录中。通过逐层读取 SKILL.md 来缩小范围。

例如加载技能后获得路径 `{path_to_skill_folder}`，则：

第1跳: read_file("{path_to_skill_folder}/references/SKILL.md")       → 看主题目录
第2跳: read_file("{path_to_skill_folder}/references/主题A/SKILL.md") → 看文件列表
第3跳: read_file("{path_to_skill_folder}/references/主题A/具体文件.md") → 读取内容

🚫 **禁止跳步**：
- 看到子目录名后，**必须先读该子目录的 SKILL.md**，确认有哪些文件
- **禁止凭猜测构造文件名**——只读取 SKILL.md 中明确列出的文件
- 如果读取报错"文件不存在"，回到上一级 SKILL.md 核对正确文件名

**知识库结构概览**：

| 主题目录 | 内容 |
|---------|------|
| `主题A/` | 简要描述 |
| `主题B/` | 简要描述 |

## 回复准则

- [根据你的领域填写具体准则]
- 引用资料时标明出处
- 如果知识库中没有找到相关内容，诚实告知用户

今天的日期是 {{ date }}。
```

**systemprompt 编写要点**：

1. **工作流程第一步加载技能**：Agent 必须先获得技能路径才能读文件
2. **禁止跳步必须写**：Agent 倾向于看到目录名就猜文件名，必须强制它先读 SKILL.md
3. **结构概览必须写**：让 Agent 一眼判断该进哪个目录，减少不必要的跳数
4. **`{{ date }}` 放末尾**：Jinja2 模板变量，平台自动注入当天日期

### 工具文件

`tools/` 目录下的三个 `.tool.yaml` 文件从平台内置工具库复制，无需手写。

---

## 如何构建层级化知识库

这是 Agentic RAG 的核心工作。目标是把扁平的知识文件组织成多层树形结构。

### 构建流程

#### 第一步：初始化

把知识文件放入 `references/` 目录，生成一个 SKILL.md 索引，列出所有文件的概要描述。

#### 第二步：BFS 逐层优化

从根目录开始，逐层处理。对当前层每个文件，做出决策：

```
对每个文件：
  阅读 → 决策：
    ├── 拆分？→ 一个文件包含多个独立主题 → 拆成独立文件
    ├── 下沉？→ 文件属于某个主题分类 → 移入对应子目录
    └── 保留？→ 文件就属于当前层 → 留下
  更新所有受影响的 SKILL.md（删旧、增新、介绍新文件夹）
```

**决策依据**：

| 情况 | 决策 |
|------|------|
| 一个文件包含多个独立主题 | **拆分**为独立文件 |
| 当前文件夹 > 20 个文件 | **下沉**，按主题创建子目录 |
| 文件明确属于某个已有分类 | **下沉**到该子目录 |
| 文件夹 ≤ 20 个文件且主题一致 | **停止下沉** |
| 已达到深度上限（建议 3-5 层） | **停止下沉** |

处理完当前层后，进入下一层，重复以上过程。

#### 第三步：一致性检查

遍历整棵树，确认：

- ✅ 每个文件夹都有 SKILL.md
- ✅ 每个 SKILL.md 准确描述当前目录的所有文件和子文件夹
- ✅ 无残留引用（已删除的文件仍被提及）
- ✅ 无孤立文件（磁盘上存在但未被 SKILL.md 记录）

### SKILL.md 编写规范

每层 SKILL.md 遵循以下结构：

```markdown
# 主题名称

一段话描述本目录覆盖的知识范围。

## 子目录

- `劳动争议/` — 劳动仲裁、诉讼程序、举证规则（35 件）
- `工资福利/` — 最低工资、加班费、社保公积金（22 件）

## 本级文件

- `劳动合同法.md` — 劳动合同法全文，涵盖合同订立、履行、解除、终止
- `工伤赔偿计算.md` — 工伤赔偿计算方法、公式、示例

## 交叉引用

- 工伤认定标准 → 见 `../工伤保险/工伤认定标准.md`
```

**编写要点**：

1. **子目录描述要足够判断相关性**：写清主题范围和文件数量，Agent 不进入就能判断是否相关
2. **文件描述写内容摘要**：不只是文件名重复，包含关键词方便 Agent 匹配用户问题
3. **交叉引用**：文件只放在最相关的目录，其他相关目录的 SKILL.md 加引用指向
4. **文件名要可读**：`劳动合同法.md` ✅ / `ff8081818c91.md` ❌

---

## 实际案例：劳动法 QA 的改造过程

### 改造前（扁平）

```
references/
├── core_laws.md              ← 7 部法律挤在一个文件
├── calculation_methods.md    ← 8 种计算方法挤在一个文件
├── case_types.md
├── evidence_checklist.md
├── local_variations.md
└── update_sources.md
```

6 个文件，Agent 需要搜索全部内容才能找到答案。

### 改造后（层级化）

```
references/
├── SKILL.md                              ← "这里有 5 大主题"
├── 法律条文/
│   ├── SKILL.md                          ← "分为合同用工/保障保险/争议解决"
│   ├── 合同与用工/
│   │   ├── SKILL.md                      ← "有劳动法、劳动合同法、实施条例"
│   │   ├── 劳动法.md
│   │   ├── 劳动合同法.md
│   │   └── 劳动合同法实施条例.md
│   ├── 保障与保险/ ...
│   └── 争议解决/ ...
├── 赔偿计算/ ...
├── 纠纷处理/ ...
├── 证据与维权/ ...
└── 地方差异/ ...
```

47 个文件，3 层深度。Agent 用 3 跳就能精准定位到《劳动合同法》第 47 条。

### 改造收益

| 指标 | 改造前 | 改造后 |
|------|--------|--------|
| 文件数 | 6 | 47 |
| 层级深度 | 1 层 | 3 层 |
| Agent 定位一条法规 | 搜索全部 6 个文件 | 3 跳精准到达 |
| 新增法规 | 扔进扁平列表 | 归入对应分类 |
| 文件名 | `core_laws.md`（混合） | `劳动合同法.md`（独立、可读） |

---

## 注意事项

### 知识库路径

SKILL.md 中引用的文件路径是**相对于技能文件夹**的。Agent 加载技能时会自动获得技能文件夹的完整路径，并据此解析所有相对路径。

开发者只需要关心技能内部的相对路径结构，不需要知道运行时的部署路径。

### 禁止跳过 SKILL.md

Agent 倾向于看到子目录名后直接猜文件名。必须在 systemprompt 和 SKILL.md 中都强调：

> 每进入一个目录，必须先读该目录的 SKILL.md。禁止凭猜测构造文件名。

这是我们在实际部署中反复遇到的问题——Agent 看到 `合同纠纷/` 就猜 `违法解除劳动合同纠纷处理.md`（实际叫 `违法解除劳动合同.md`），然后报错。

### ContextCompactionMiddleware

多次 read_file 会逐渐膨胀上下文。`ContextCompactionMiddleware` 在上下文使用超过阈值时自动压缩历史工具输出，防止 Agent 因上下文溢出而丢失信息。

配置注意：
- import 路径中有 `execution` 层：`nexau.archs.main_sub.execution.middleware.context_compaction:ContextCompactionMiddleware`
- 参数名是 `threshold`（不是 `threshold_ratio`）
- 这两个细节都是部署时踩出来的坑，写错会直接启动失败

---

> 好的 Agentic RAG Agent 不是 prompt 写得多，而是**结构设计得好**。把知识原文、检索逻辑、输出格式分清楚，让 LLM 在正确的框架下精准引用——这比任何 prompt 技巧都重要。
