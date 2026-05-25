# 教程：多技能协作的文档生成智能体 — 公文写作

> 🔴 **Level 3 · 实战** ｜ ⏱ 约 1.5 h ｜ 🔑 PDF 解析 + 领域知识检索 + Word 生成 三技能协作
>
> 以「公文写作助手」为例，介绍如何构建一个 **PDF 进 → Word 出** 的端到端文档生成智能体。与问答型智能体不同，这里 Agent 的产出是一份符合 GB/T 9704 标准的 Word 公文。
>
> 💡 本篇是多技能协作的典型样例——三个 Skill 各司其职，通过 System Prompt 串联成流水线。同时也是「踩坑驱动优化」的工程实践记录，每个设计决策都来自真实部署中的失败教训。
>
> 前置阅读：[**开始之前：样例项目的通用约定**](../GETTING_STARTED.md)（了解 nexau.json、agent.yaml、技能加载机制）

## 目录

- [场景与目标](#场景与目标)
- [总体架构](#总体架构)
- [设计要点 1：三技能协作架构](#设计要点-1三技能协作架构)
- [设计要点 2：Schema 内联 — 让 Agent 不可能写错 JSON](#设计要点-2schema-内联--让-agent-不可能写错-json)
- [设计要点 3：LoadSkill 路径机制](#设计要点-3loadskill-路径机制)
- [设计要点 4：.NET 引擎生命周期管理](#设计要点-4net-引擎生命周期管理)
- [设计要点 5：Agentic Search 驱动的公文写作知识](#设计要点-5agentic-search-驱动的公文写作知识)
- [设计要点 6：System Prompt 的编写方法](#设计要点-6system-prompt-的编写方法)
- [五轮测试的踩坑与修复](#五轮测试的踩坑与修复)
- [完整配置文件](#完整配置文件)
- [注意事项](#注意事项)
- [动手实践](#动手实践)
- [下一步](#下一步)

---

## 场景与目标

政府机关日常需要根据各种参考材料（PDF 格式的政策文件、会议记录、工作报告等）撰写公文。人工写公文需要熟悉 GB/T 9704 格式标准、掌握公文用语规范、了解各文种结构——门槛不低，效率也有限。

**目标**：构建一个端到端的公文写作助手，输入 PDF 参考材料 + 写作要求，输出符合国家标准的 Word 公文。

**核心流程**：

```
用户上传 PDF + 提出写作要求
    ↓
① pdf_to_md Skill：OCR 解析 PDF → Markdown
    ↓
② govdoc-writing Skill：查阅公文写作规范和模板
    ↓
③ Agent 撰写公文内容，构造符合 schema 的 JSON
    ↓
④ docx-report Skill：JSON → 标准格式 Word 文档
    ↓
输出 .docx 文件
```

---

## 总体架构

```
公文写作 Agent/
├── nexau.json                          ← 智能体注册表
├── agent.yaml                          ← 智能体配置（工具 + 技能 + 中间件）
├── systemprompt.md                     ← 系统提示词（含完整 JSON schema）
├── tools/                              ← 内置工具声明
│   ├── read_file.tool.yaml
│   ├── write_file.tool.yaml
│   ├── search_file_content.tool.yaml
│   └── run_shell_command.tool.yaml
└── skills/
    ├── pdf_to_md/                      ← Skill ①：PDF → Markdown
    │   ├── SKILL.md
    │   └── scripts/pdf_to_md.py
    ├── docx-report/                    ← Skill ②：JSON → Word
    │   ├── SKILL.md
    │   ├── scripts/build              ←   渲染引擎（含 JSON 预校验）
    │   ├── schemas/                   ←   各文种的 JSON schema
    │   └── templates/                 ←   .NET 渲染模板
    └── govdoc-writing/                 ← Skill ③：公文写作知识库
        ├── SKILL.md
        └── references/
            ├── guidelines/            ←   格式标准 / 行文规则 / 常用套语
            └── templates/             ←   6 种文种的写作模板 + JSON 示例
```

三个 Skill 各司其职：

| Skill | 职责 | 输入 → 输出 |
|-------|------|------------|
| `pdf_to_md` | PDF 解析 | PDF 文件 → Markdown 文本 |
| `govdoc-writing` | 公文写作知识 | Agent 查询 → 格式规范 / 模板 / 用语 |
| `docx-report` | Word 生成 | JSON 数据 → .docx 文件 |

---

## 设计要点 1：三技能协作架构

本智能体的核心设计是**三个独立 Skill 通过 System Prompt 串联成流水线**。每个 Skill 只做一件事，通过 Agent 的多步推理能力衔接起来。

**为什么拆成三个 Skill 而非一个大脚本？**

```
❌ 一个大脚本包办一切（PDF解析 + 公文写作 + Word生成）：
   → 任何一环出问题，整体失败且难以定位
   → 无法复用——别的 Agent 也需要 PDF 解析或 Word 生成
   → 公文规范更新时，要改脚本而非知识库

✅ 三个 Skill 各司其职：
   → 每个 Skill 可以独立测试、独立更新
   → pdf_to_md 可以被任何需要 PDF 解析的 Agent 复用
   → docx-report 可以被任何需要生成 Word 的 Agent 复用
   → 公文规范更新只需修改 govdoc-writing 的 references，不动代码
```

**选型原则**：如果一个能力（PDF 解析、Word 生成、知识检索）**会被多个 Agent 复用**，就应该拆成独立的 Skill。

### agent.yaml 中的技能注册

```yaml
skills:
  - ./skills/pdf_to_md        # PDF → Markdown
  - ./skills/docx-report       # JSON → Word
  - ./skills/govdoc-writing    # 公文写作知识库
```

注册后，Agent 通过 `LoadSkill("skill-name")` 加载技能，获得技能文件夹路径，然后用该路径调用脚本或读取知识文件。

**实践建议**：如果一个能力（PDF 解析、Word 生成、知识检索）**会被多个 Agent 复用**，就应该拆成独立的 Skill。反之，只有一个 Agent 用的一次性逻辑，写在 System Prompt 或 custom_tools 里即可。

---

## 设计要点 2：Schema 内联 — 让 Agent 不可能写错 JSON

这是本项目**最重要的设计决策**——把 docx-report 的 JSON schema 直接写在 System Prompt 中。

### 问题：Agent 构造 JSON 总是出错

在早期测试中，Agent 反复在 JSON 字段名上犯错：

| 错误 | 应该写 | Agent 写了 |
|------|--------|-----------|
| section 标题 | `"heading"` | `"title"` |
| 正文段落 | `"paragraphs": [...]` | `"content": "..."` |
| 标题层级 | `"level": 1` | （漏掉了） |

这些错误导致 Word 文件要么为空（< 3KB），要么格式混乱。

### 根因：schema 定义在 base.json 中，Agent 没读到

docx-report 的 `section` 结构定义在 `schemas/base.json` 中，各文种 schema 通过 `$ref` 引用它。Agent 读了 `notice.json` 但跳过了 `base.json`，不知道 `section` 的正确字段名。

### 解决：把 schema 直接内联到 System Prompt

```
❌ 告诉 Agent "去读 schema 文件再构造 JSON"：
   → Agent 可能只读了类型 schema，漏掉 base.json 的 $ref
   → 多一次文件读取，多一次出错机会
   → 即使读了也可能理解偏差

✅ 把 schema + ❌/✅ 对照表直接写在提示词里：
   → Agent 每次生成 JSON 时都能直接看到正确结构
   → 零检索开销，零遗漏
   → ❌/✅ 对照表让常见错误一目了然
```

System Prompt 中的 schema 片段：

```json
{
  "heading": "一、总体要求",        // ✅ 字段名是 heading，不是 title
  "level": 1,                       // ✅ 必填，1/2/3
  "paragraphs": [                   // ✅ 字段名是 paragraphs，不是 content
    "第一段正文……",
    "第二段正文……"
  ]
}
```

同时在 System Prompt 中还为每种文种提供了**完整的 JSON 模板**和 `required` 字段清单。这样 Agent 构造 JSON 时有两层保护：
1. 通用的 `section` 结构规范（schema 内联）
2. 每种文种的完整示例（JSON 模板内联）

### 额外保护：build 脚本的 JSON 预校验

即使 Agent 仍然写错了 JSON，`docx-report/scripts/build` 脚本在调用 .NET 渲染引擎之前会用 Python 预校验 JSON：

```
✅ JSON 校验通过（类型：notice，字段：10 个）

或者：

❌ JSON 校验失败（3 个错误）：
   • sections[0] 缺少 "heading"（注意不是 "title"）
   • sections[0] 缺少 "level"（必须是 1/2/3）
   • sections[0] 包含无效字段 "content"（应该用 "paragraphs"）
```

这样即使 Agent 犯错，也能拿到明确的错误信息自行修正，而不是得到一个空白 Word 文件茫然无措。

**实践建议**：如果你的 Agent 需要生成结构化数据（JSON/YAML/SQL），把目标 schema 的关键结构直接内联到 System Prompt，并提供 ❌/✅ 对照表。这比"告诉 Agent 去读 schema 文件"可靠得多。

---

## 设计要点 3：LoadSkill 路径机制

### 问题：Agent 猜路径，总猜错

早期测试中，Agent 看到 `agent.yaml` 里写着 `./skills/pdf_to_md`，就猜测运行时路径是 `/agent/skills/pdf_to_md/` 或 `skills/pdf_to_md/`——全部 File Not Found。

### 正确做法：先 LoadSkill，再用路径

NexAU 的技能加载机制是**两级懒加载**：

1. **注册时**：Agent 只看到 SKILL.md 的 `name` 和 `description`（摘要）
2. **LoadSkill 时**：完整的 SKILL.md 内容加载到上下文，并**返回技能文件夹的完整路径**

System Prompt 中的工作流第一步就是加载所有技能：

```markdown
### 第一步：加载全部技能并获取路径

1. LoadSkill("pdf-to-md")      → 记为 {pdf_to_md_path}
2. LoadSkill("docx-report")    → 记为 {docx_report_path}
3. LoadSkill("govdoc-writing") → 记为 {govdoc_writing_path}
```

后续所有命令都基于 LoadSkill 返回的路径：

```bash
python3 {pdf_to_md_path}/scripts/pdf_to_md.py <pdf文件>
{docx_report_path}/scripts/build render notice /tmp/data.json /tmp/output.docx
```

```
❌ 硬编码路径（不通用，换环境就 404）：
   .skills/pdf_to_md/scripts/pdf_to_md.py
   /agent/skills/pdf_to_md/scripts/pdf_to_md.py

❌ 用 find 搜索路径（浪费时间，不可靠）：
   find . -name "pdf_to_md.py" -type f

✅ LoadSkill 返回路径（标准做法）：
   {pdf_to_md_path}/scripts/pdf_to_md.py
```

**实践建议**：在 System Prompt 和 SKILL.md 中，路径一律用 `{skill_path}/...` 占位符。Agent 的第一步永远是 LoadSkill 获取路径。

---

## 设计要点 4：.NET 引擎生命周期管理

`docx-report` 的 Word 渲染引擎基于 .NET，需要首次编译。这在部署测试中引发了一系列问题。

### 问题链：首次 render 静默失败

```
首次运行，bin/ 目录不存在
  → find "$ENGINE_DIR/bin" ... 返回 exit code 1
    → set -o pipefail 让管道整体 exit 1
      → set -e 直接杀掉脚本
        → 连编译逻辑都没执行到
          → Agent 看到 exit 1 无输出，一头雾水
```

### 修复：三层防护

**第一层**：`find` 命令加 `|| true`

```bash
# 修复前：bin/ 不存在时 find 返回 1，set -e 杀掉脚本
DLL=$(find "$ENGINE_DIR/bin" -name "GovDoc.dll" ... | head -1)

# 修复后：find 失败时 DLL 为空字符串，脚本继续执行
DLL=$(find "$ENGINE_DIR/bin" -name "GovDoc.dll" ... | head -1) || true
```

**第二层**：.NET SDK 预初始化

```bash
# 首次运行 dotnet 时需要初始化 ~/.dotnet（写工作负载、配置遥测等）
# 不做这步，后续 dotnet build 可能静默失败
dotnet --info > /dev/null 2>&1 || true
```

**第三层**：编译失败自动重试

```bash
(cd "$RENDER_DIR" && dotnet build -c Release --nologo -v q 2>&1) || {
  echo "⚠️  首次编译失败，重试..."
  sleep 1
  (cd "$RENDER_DIR" && dotnet build -c Release --nologo 2>&1) || {
    echo "❌ 引擎编译失败"
    exit 1
  }
}
```

### 另一个坑：CS8802 编译冲突

`docx-report/templates/` 目录下有多个 `.cs` 文件，每个都有 top-level statements（C# 9.0 的特性），直接 `dotnet build` 会报 CS8802。

```
❌ 直接 dotnet build 或 dotnet run：
   → 多个 top-level statements 文件冲突
   → error CS8802: Only one compilation unit can have top-level statements

✅ build render 命令（自动隔离编译）：
   → 只把 GovDocEngine.cs 复制到临时目录
   → 重命名为 Program.cs 单独编译
   → 避免冲突
```

System Prompt 中明确禁止直接调用 `dotnet`：

```markdown
# ❌ 错误：直接调用 dotnet（会报 CS8802 编译冲突）
dotnet run --project {docx_report_path}/templates/GovDoc.csproj ...

# ✅ 正确：用 build 脚本的 render 子命令
{docx_report_path}/scripts/build render <type> <input.json> <output.docx> 2>&1
```

**实践建议**：如果你的 Skill 包含需要编译的代码（.NET、Go、Rust 等），用 shell 脚本封装编译和运行逻辑，不要让 Agent 直接操作编译工具链。脚本内部处理好首次编译、环境初始化、冲突隔离等问题。

---

## 设计要点 5：Agentic Search 驱动的公文写作知识

`govdoc-writing` 技能采用 **Agentic Search** 模式：SKILL.md 提供知识框架和导航索引，详细资料放在 `references/` 中按需检索。

### 知识库结构

```
govdoc-writing/
├── SKILL.md                              ← 总纲：文种速查表 + references 导航
└── references/
    ├── guidelines/
    │   ├── format-standard.md            ← GB/T 9704 格式标准
    │   ├── writing-rules.md              ← 行文规则、语体风格
    │   └── common-phrases.md             ← 常用公文套语速查
    └── templates/
        ├── notice.md                     ← 通知模板（含 JSON 示例）
        ├── report.md                     ← 报告模板
        ├── letter.md                     ← 函模板
        ├── resolution.md                 ← 请示/批复模板
        ├── minutes.md                    ← 纪要模板
        └── decision.md                   ← 决定模板
```

### 为什么用 Agentic Search 而非全量内联？

```
❌ 把所有公文规范写在 System Prompt 里：
   → 9 个文件合计数万字，撑爆上下文
   → 每次对话都加载全量知识，浪费 tokens
   → Agent 只需要当前文种的模板，不需要全部 6 种

✅ Agentic Search：按需检索
   → SKILL.md 只有速查表（几百字），随 LoadSkill 加载
   → Agent 确定文种后，只读对应的 template 文件
   → 需要确认格式细节时，才去读 guidelines
```

### 模板文件的关键设计

每个 template 文件都包含**完整的 JSON sections 示例**——这是与普通知识库的关键区别。Agent 不仅查到了"通知的正文结构是什么"，还直接看到了 docx-report 可以渲染的 JSON 长什么样：

```json
{
  "heading": "一、检查范围",
  "level": 1,
  "paragraphs": [
    "2024年6月30日前在我市各级民政部门登记注册的社会团体……"
  ]
}
```

这样 Agent 写公文时有三层参照：
1. System Prompt 中的通用 schema（结构层面）
2. Template 文件中的文种示例（内容层面）
3. Common-phrases 中的规范用语（语言层面）

**实践建议**：当知识量适中（几十个文件）、Agent 每次只需要其中几个时，用 Agentic Search。SKILL.md 提供导航，references 按需读取。

---

## 设计要点 6：System Prompt 的编写方法

本智能体的 System Prompt 和知识库问答型的提示词完全不同——它是一份**工序指令书**，每一步都有明确的输入、输出和工具调用。

### 结构总览

```
┌─ 关键规则（3 条铁律）── 路径机制、build render、英文文件名
├─ Schema 内联 ──────── section 结构 + ❌/✅ 对照表
├─ 各文种 JSON 模板 ──── 6 种文种的 required 字段 + 完整示例
├─ 工作流程（9 步）──── LoadSkill → PDF → 写作 → JSON → Word → 验证
├─ 工具使用表 ──────── 何时用何工具
├─ 禁止操作 ──────── 9 条明确禁令
└─ 故障排除表 ──────── 10 种错误现象 → 原因 → 解决
```

### 关键技巧：禁止操作比正面引导更有效

```markdown
## 🚫 禁止操作

- 禁止在加载技能之前就使用脚本或读取技能文件
- 禁止猜测或硬编码技能路径
- 禁止直接调用 dotnet run
- 禁止不对照 schema 就构造 JSON
- 禁止在 build render 命令后加管道
```

每条禁令都来自真实的失败案例。大模型对"不能做什么"比"应该做什么"的遵从度更高——定义失败条件比定义成功条件更能约束行为。

### 关键技巧：故障排除表 = Agent 的自愈手册

```markdown
| 错误现象 | 原因 | 解决方法 |
|---------|------|---------|
| exit code 1 无输出 | 引擎首次编译 | 正常现象，重试一次 |
| CS8802 | 直接调了 dotnet | 改用 build render |
| JSON 校验失败 | 字段名不匹配 | 按错误提示修正 |
```

这张表让 Agent 在遇到错误时可以**自主诊断和修复**，不需要人工介入。

**实践建议**：部署后每发现一个 Agent 犯的错，就在 System Prompt 中新增一条禁止操作或故障排除表的一行。积累 5-10 条后，Agent 的自主成功率会显著提高。

---

## 五轮测试的踩坑与修复

本项目经历了 5 轮真实部署测试，每轮都发现了新问题。以下是问题演进和解决过程：

| 轮次 | 主要问题 | 根因 | 修复 |
|------|---------|------|------|
| 第 1 轮 | 脚本路径 404、Permission denied、JSON 错误、CS8802 | 路径猜错、缺权限、没读 schema、直接 dotnet | 加路径规则、chmod、Schema-first、禁 dotnet |
| 第 2 轮 | 封装脚本不存在、JSON 24 个错误 | 非标准目录不部署、没读 base.json | 删封装脚本、schema 内联到 prompt |
| 第 3 轮 | 首次编译静默失败、模板混用 | .NET 引擎未编译、generic≠notice | 加 warmup 命令、JSON 预校验 |
| 第 4 轮 | warmup 本身失败 | warmup 多此一举 | 删 warmup 步骤，render 自动编译 |
| 第 5 轮 | 首次 render 仍然 exit 1 | `find` 在 `bin/` 不存在时被 `set -e` 杀掉 | 7 处 `find` 加 `|| true`（**真正的根因**） |

**关键教训**：

1. **`set -euo pipefail` 是双刃剑**：它让脚本更安全（任何失败立即退出），但也让 `find` 对不存在的目录返回 exit 1 这种"合理的空结果"变成致命错误。修复：对预期可能为空的查找操作加 `|| true`

2. **不要加 `| tail` 管道**：`cmd | tail -50` 会掩盖 `cmd` 的真实退出码（tail 永远 exit 0），还会截断有用的输出。修复：直接用 `2>&1`

3. **Schema 内联比"让 Agent 去读"可靠 10 倍**：5 轮测试中，内联 schema 后 JSON 构造零错误。之前"去读 schema 文件"的方式，Agent 总会漏读 base.json

---

## 完整配置文件

### nexau.json

```json
{
  "agents": {
    "govdoc_writer": "agent.yaml"
  },
  "excluded": [".nexau/", ".env", "__pycache__/", "start.py"]
}
```

### agent.yaml

```yaml
type: agent
name: govdoc_writer
description: 公文写作 Agent — 解析 PDF，根据要求撰写公文，输出标准 Word 文档。
max_context_tokens: 200000
system_prompt: ./systemprompt.md
system_prompt_type: jinja                    # ← 使用 jinja 支持 {{ date }} 变量
tool_call_mode: structured
max_iterations: 100

llm_config:
  model: ${env.LLM_MODEL}
  base_url: ${env.LLM_BASE_URL}
  api_key: ${env.LLM_API_KEY}
  max_tokens: 32000
  stream: True
  temperature: 0.2                           # ← 低温度：公文写作需要精确，不需要创意
  api_type: openai_chat_completion

tools:
  - name: read_file
    yaml_path: ./tools/read_file.tool.yaml
    binding: nexau.archs.tool.builtin.file_tools:read_file
  - name: write_file
    yaml_path: ./tools/write_file.tool.yaml
    binding: nexau.archs.tool.builtin.file_tools:write_file
  - name: search_file_content
    yaml_path: ./tools/search_file_content.tool.yaml
    binding: nexau.archs.tool.builtin.file_tools:search_file_content
  - name: run_shell_command
    yaml_path: ./tools/run_shell_command.tool.yaml
    binding: nexau.archs.tool.builtin.shell_tools:run_shell_command

skills:
  - ./skills/pdf_to_md                       # PDF → Markdown
  - ./skills/docx-report                     # JSON → Word
  - ./skills/govdoc-writing                  # 公文写作知识库

middlewares:
  - import: nexau.archs.main_sub.execution.middleware.context_compaction:ContextCompactionMiddleware
    params:
      threshold: 0.6                         # ← PDF 内容可能很长，60% 时开始压缩
```

---

## 注意事项

### 环境变量

| 变量 | 说明 | 是否必填 |
|------|------|---------|
| `OCR_API_KEY` | SiliconFlow OCR API Key（pdf_to_md 使用） | 必填 |

两种方式任选其一：（1）**Playground 控制台**：右上角 **运行配置 → 沙箱环境** 标签页添加；（2）**Chat API**：调用 `/agent-api/chat` 时在 `variables.sandbox_env` 中传入。详见 [GETTING_STARTED 的环境变量配置](../GETTING_STARTED.md#环境变量配置)。

> ⚠️ **pdf_to_md（即 OCR_API_KEY）当前仅支持互联网版本**。政务外网版本正在开发中，后续将由平台统一提供 PDF 解析能力——上传的 PDF 经平台解析后，文本内容将**自动存入沙盒**，开发者无需自行配置 OCR_API_KEY。

### 文件限制

- PDF 最大：50MB
- PDF OCR 并发：最多 10 页并行
- Word 输出：文件 ≥ 3KB 为正常，< 3KB 说明 JSON 有误

### 支持的公文类型

| type | 文种 |
|------|------|
| `notice` | 通知 / 通报 / 公告 / 意见 |
| `report` | 报告 / 公报 / 议案 |
| `letter` | 函 |
| `resolution` | 请示 / 批复 |
| `minutes` | 会议纪要 |
| `decision` | 决定 / 命令 |
| `generic` | 通用文档 |

### 公文规范更新

需要更新写作规范时，只需修改 `skills/govdoc-writing/references/` 下的文件，不需要改动 Agent 配置或代码。

---

## 动手实践

如果你要构建一个类似的**多技能协作文档生成 Agent**，参照以下步骤：

### 1. 拆分技能

把你的流程拆成独立的 Skill：

| 步骤 | 对应 Skill | 是否可复用 |
|------|-----------|-----------|
| 输入解析（PDF/图片/表格） | pdf_to_md 或同类 | ✅ |
| 领域知识（写作规范/业务规则） | 自定义 Skill + references | ✅ |
| 输出生成（Word/Excel/PPT） | docx-report 或同类 | ✅ |

### 2. Schema 内联

如果 Agent 需要生成结构化数据：
- 把目标 schema 的**关键结构**直接写在 System Prompt
- 为每种输出类型提供**完整 JSON 示例**
- 添加 **❌/✅ 对照表**列出常见字段名错误

### 3. 封装脚本

如果 Skill 包含编译型代码：
- 用 shell 脚本封装编译和运行
- 处理好首次编译的初始化
- 加入输入校验（在编译前快速报错）

### 4. 路径用 LoadSkill

System Prompt 和 SKILL.md 中的路径一律用 `{skill_path}/...`。工作流第一步永远是 LoadSkill。

### 5. 从失败中学习

- 部署后跑几轮真实测试
- 收集 Agent 遇到的错误
- 把每个错误转化为 System Prompt 中的一条规则或故障排除表的一行
- **禁止操作 > 正面引导**，**故障排除表 = Agent 的自愈手册**

---

> 文档生成类 Agent 的核心不是"让大模型写得好"，而是**把每一步的输入输出格式定义清楚**。schema 内联、JSON 预校验、故障排除表——这些工程化手段比任何 Prompt 技巧都更可靠。五轮测试告诉我们：每个部署中的失败，都是优化 System Prompt 的素材。

---

## 下一步

本篇展示了**三技能协作 + 端到端文档生成**的完整架构。如果你想了解其他 Agent 构建模式：

- **只需要知识库问答**？看 [劳动法问答](../劳动法问答/tutorial-agentic-rag-agent.md)（Agentic RAG 入门）
- **需要接入外部 API**？看 [金融数据智能体](../金融数据智能体/tutorial-finance-agent.md)（自定义工具封装）
- **需要完整的审核/合规流程**？看 [住房公积金审核](../住房公积金审核/tutorial-house-fund-review.md)（集大成之作）

← 返回 [Cookbook 主页](../README.md) ｜ [通用约定](../GETTING_STARTED.md)
