# 教程：用自定义工具封装 REST API — 金融数据分析师 Agent

> 🟢 **Level 1 · 入门** ｜ ⏱ 约 45 min ｜ 🔑 自定义工具封装 REST API
>
> 以「Finnhub 金融数据分析师 Agent」为例，展示如何把一个**外部 REST API**（18 个端点）包装成 Agent 可以调用的工具，让 LLM 从「只会聊天」变成「能拿到真实数据」。本教程不涉及 RAG，专注讲**工具驱动**的 Agent 该怎么写。
>
> 💡 不熟悉 Agent / Skill / Tool 等术语？先看 [Cookbook 主页 · 核心概念速览](../README.md#核心概念速览)。

## 目录

- [场景与目标](#场景与目标)
- [总体架构](#总体架构)
- [设计要点 1：工具的三件套结构](#设计要点-1工具的三件套结构)
- [设计要点 2：用 extra_kwargs 注入 API Key](#设计要点-2用-extra_kwargs-注入-api-key)
- [设计要点 3：统一的 HTTP helper，18 个工具只写业务逻辑](#设计要点-3统一的-http-helper18-个工具只写业务逻辑)
- [设计要点 4：返回给 LLM 的是「窄数据」，不是原始响应](#设计要点-4返回给-llm-的是窄数据不是原始响应)
- [设计要点 5：错误即数据，不要抛异常](#设计要点-5错误即数据不要抛异常)
- [设计要点 6：System Prompt 的三段式](#设计要点-6system-prompt-的三段式)
- [设计要点 7：Jinja 让上下文动态注入](#设计要点-7jinja-让上下文动态注入)
- [设计要点 8：ContextCompactionMiddleware 应对多工具长会话](#设计要点-8contextcompactionmiddleware-应对多工具长会话)
- [完整配置文件](#完整配置文件)
- [动手实践：封装你自己的 REST API](#动手实践封装你自己的-rest-api)
- [注意事项](#注意事项)

---

## 场景与目标

假设你要让一个 AI 助手能够回答这类问题：

- 「苹果现在股价多少？」
- 「特斯拉最近的 P/E、市值、ROE 是什么？」
- 「英伟达上一季度 EPS 超预期了吗？」
- 「微软最近一周有什么新闻？」
- 「下周有哪些公司要发财报？有哪些新股要 IPO？」
- 「过去 3 个月苹果内部人卖了多少股？」

这些问题的共性是：**答案不在文档里，而在一个外部 API 里**——股票数据每分钟都在变，RAG 帮不上忙。

**目标**：把 [Finnhub](https://finnhub.io) 的 18 个 REST 端点包装成 Agent 可以直接调用的工具，让模型自己选工具、填参数、拿数据、组织回答。

**核心要点**：这是一个**工具驱动**的 Agent，不是知识驱动。它和 Agentic RAG 代表了 Agent 的两种基础形态——
- **RAG 型**：答案来自离线知识（政策文件、法规原文）
- **工具型**：答案来自实时调用（数据库、API、计算）

两种形态可以组合，但这个样例专注讲工具型，讲透一件事。

---

## 总体架构

```
finnhub-agent/
├── nexau.json                       ← Agent 注册表
├── agent.yaml                       ← 骨架：模型、工具、中间件
├── systemprompt.md                  ← 指挥官：角色 + 工作流 + 工具地图 + 能力边界
├── custom_tools/
│   └── finnhub_api.py               ← 18 个工具函数的 Python 实现（stdlib only）
└── tools/
    ├── read_file.tool.yaml          ← 3 个内置工具的 schema
    ├── search_file_content.tool.yaml
    ├── run_shell_command.tool.yaml
    ├── finnhub_quote.tool.yaml      ← 18 个 Finnhub 工具的 schema
    ├── finnhub_company_profile.tool.yaml
    ├── finnhub_basic_financials.tool.yaml
    ├── finnhub_earnings_surprises.tool.yaml
    ├── finnhub_recommendation.tool.yaml
    ├── finnhub_insider_transactions.tool.yaml
    ├── finnhub_company_news.tool.yaml
    ├── finnhub_market_news.tool.yaml
    ├── finnhub_sec_filings.tool.yaml
    ├── finnhub_earnings_calendar.tool.yaml
    ├── finnhub_ipo_calendar.tool.yaml
    ├── finnhub_market_status.tool.yaml
    └── ... (共 18 个)
```

**三个关注点分离**：

| 层 | 位置 | 职责 |
|----|------|------|
| Schema 层 | `tools/*.tool.yaml` | 告诉模型「工具叫什么、有什么参数、要用来干嘛」 |
| 实现层 | `custom_tools/finnhub_api.py` | 真正发 HTTP 请求、处理响应 |
| 编排层 | `agent.yaml` + `systemprompt.md` | 把工具注册到 Agent、告诉模型什么时候用哪个 |

---

## 设计要点 1：工具的三件套结构

每一个自定义工具都由**三样东西**组成，缺一不可：

### ① 工具函数（Python）

`custom_tools/finnhub_api.py` 里一个普通的 Python 函数：

```python
def finnhub_quote(symbol: str, api_token: str = "") -> str:
    """Get real-time quote for a stock symbol."""
    data = _finnhub_get("/quote", {"symbol": symbol}, api_token)
    return _dumps(data)
```

注意三件事：

- 参数名 = 工具 schema 里的参数名
- 返回值是**字符串**（通常是 JSON 字符串），方便塞进模型上下文
- `api_token` 是平台注入的，业务代码不关心它从哪来

### ② 工具 Schema（YAML）

`tools/finnhub_quote.tool.yaml`：

```yaml
type: tool
name: finnhub_quote
description: >-
  Get real-time stock quote including current price, change, percent change,
  high, low, open, previous close, and timestamp.
input_schema:
  type: object
  properties:
    symbol:
      type: string
      description: Stock ticker symbol (e.g. 'AAPL', 'MSFT').
  required:
    - symbol
  additionalProperties: false
```

`description` 是**模型决定是否调用这个工具的唯一依据**——写得越清楚，模型选工具越准。`input_schema` 用 JSON Schema 约束参数形态，模型生成的 tool call 会按这里的格式填参。

### ③ 在 `agent.yaml` 里注册

```yaml
tools:
  - name: finnhub_quote
    yaml_path: tools/finnhub_quote.tool.yaml
    binding: custom_tools.finnhub_api:finnhub_quote
    extra_kwargs:
      api_token: ${env.X_FINNHUB_SECRET}
```

- `yaml_path` → schema 文件
- `binding` → Python 函数（模块路径 `:` 函数名）
- `extra_kwargs` → 注入到函数调用时的额外参数（下一节重点讲）

**记住这个三件套**，封装任何 REST API 都是这三步。18 个 Finnhub 端点就是这一模式重复 18 次。

---

## 设计要点 2：用 extra_kwargs 注入 API Key

### 问题：API Key 不能让模型看到

如果你把 `api_token` 写进工具 schema 让模型填，会出两个大问题：

1. **泄露风险**：模型会把 token 写进 tool_call JSON，再写进对话历史，再写进日志
2. **幻觉风险**：模型可能自己编一个 token 字符串塞进去

### 解法：extra_kwargs 在**运行时**注入

看 `agent.yaml`：

```yaml
- name: finnhub_quote
  yaml_path: tools/finnhub_quote.tool.yaml
  binding: custom_tools.finnhub_api:finnhub_quote
  extra_kwargs:
    api_token: ${env.X_FINNHUB_SECRET}   # ← 从环境变量取
```

对应 Python 函数：

```python
def finnhub_quote(symbol: str, api_token: str = "") -> str:
    #                         ↑
    #              模型不会填这个，平台运行时注入
    ...
```

注意 `finnhub_quote.tool.yaml` 里**只有 `symbol` 一个参数**，没有 `api_token`——模型根本不知道它的存在。运行时平台把 `extra_kwargs` merge 到模型生成的参数里，再调用 Python 函数。

### 在 NAC 上怎么配 `X_FINNHUB_SECRET`

本地调试写 `.env`（记得在 `nexau.json` 的 `excluded` 里排除）：

```bash
X_FINNHUB_SECRET=your_finnhub_api_key_here
```

部署到 NAC 时，两种方式任选其一把 `X_FINNHUB_SECRET` 注入沙盒：

1. **Playground 控制台**：右上角 **运行配置 → 沙箱环境** 标签页点 **+ 添加变量**。
2. **Chat API**：调用 `/agent-api/chat` 时在 `variables.sandbox_env` 中传入，仅对本次请求生效。

详见 [GETTING_STARTED 的环境变量配置](../GETTING_STARTED.md#环境变量配置)。

**通用原则**：工具需要的任何密钥、账号、环境信息，都走 `extra_kwargs + ${env.XXX}`，不要放进 schema。

---

## 设计要点 3：统一的 HTTP helper，18 个工具只写业务逻辑

18 个工具都要发 HTTP GET、处理超时、处理错误、拼接 token——如果每个函数都自己写一遍，代码爆炸且不一致。

### 抽出一个 helper

```python
_BASE_URL = "https://finnhub.io/api/v1"

def _finnhub_get(path: str, params: dict, api_token: str) -> dict:
    # 1. 注入 token，过滤空值
    filtered: dict[str, str] = {"token": api_token}
    for k, v in params.items():
        if v != "" and v is not None:
            filtered[k] = str(v)

    # 2. 拼 URL、发请求
    url = f"{_BASE_URL}{path}?{urllib.parse.urlencode(filtered)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})

    # 3. 统一错误处理：任何异常都转成 {"error": "..."} 返回
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"error": f"URL error: {exc.reason}"}
    except TimeoutError:
        return {"error": "Request timed out (10 s)"}
    except Exception as exc:
        return {"error": str(exc)}
```

然后 18 个工具函数都是**薄薄一层**：

```python
def finnhub_quote(symbol: str, api_token: str = "") -> str:
    data = _finnhub_get("/quote", {"symbol": symbol}, api_token)
    return _dumps(data)

def finnhub_company_profile(symbol: str, api_token: str = "") -> str:
    data = _finnhub_get("/stock/profile2", {"symbol": symbol}, api_token)
    return _dumps(data)

def finnhub_peers(symbol: str, api_token: str = "") -> str:
    data = _finnhub_get("/stock/peers", {"symbol": symbol}, api_token)
    return _dumps(data)
```

**收益**：

- 超时、错误格式、URL 拼接全部一致
- 改 HTTP 客户端（比如换成 httpx 加重试）只改一处
- 每个工具函数都聚焦在「我要调哪个端点、传什么参」

### 为什么只用 stdlib？

`urllib` 而不是 `requests`——因为 NAC sandbox **默认只有 Python 标准库**。装第三方依赖要另外声明 `pyproject.toml`，能不装就不装。如果你的 API 很复杂、需要 `httpx` / `requests`，再声明依赖。

---

## 设计要点 4：返回给 LLM 的是「窄数据」，不是原始响应

Finnhub 很多端点返回的是**大礼包**：几百条新闻、多年季度财报、几百笔内部人交易。如果原封不动塞给模型：

- Token 瞬间爆炸（128k context 很快用光）
- 噪声淹没信号（模型抓不到重点）
- 响应又慢又贵

### 在工具里「提前加工」

看 `finnhub_company_news` 的实现：

```python
def finnhub_company_news(symbol, from_date, to_date, api_token=""):
    data = _finnhub_get("/company-news", {...}, api_token)
    if isinstance(data, dict) and "error" in data:
        return _dumps(data)

    articles = []
    for item in (data if isinstance(data, list) else [])[:10]:     # ← 只取前 10 条
        summary = item.get("summary", "") or ""
        articles.append({
            "headline": item.get("headline"),
            "source": item.get("source"),
            "datetime": item.get("datetime"),
            "summary": summary[:200] + ("…" if len(summary) > 200 else ""),  # ← 摘要截到 200 字
            "url": item.get("url"),
        })

    return _dumps({
        "total_available": len(data) if isinstance(data, list) else 0,
        "showing": len(articles),
        "articles": articles,
    })
```

**三个处理**：

1. **限条数**：只保留前 10 条
2. **限字段**：原始返回的二十几个字段里只挑 5 个对模型有用的
3. **限字长**：summary 超过 200 字就截断
4. **给元数据**：返回 `total_available` 和 `showing`，让模型知道「还有更多」

### 同款处理见于

| 工具 | 处理策略 |
|------|---------|
| `finnhub_symbol_lookup` | 最多返回 10 条匹配 |
| `finnhub_insider_transactions` | 只展示最近 20 笔 |
| `finnhub_market_news` | 只展示前 15 条 |
| `finnhub_sec_filings` | 只展示前 20 份文件 |
| `finnhub_financials_reported` | 只展示前 2 份报告，且用 `report_sections` 摘要掉巨大的 `report` 对象 |
| `finnhub_basic_financials` | 保留 `metric` 全量，`series` 只返回 keys 摘要 |

**通用原则**：工具的责任不是「把原始数据原样倒给模型」，而是「把有价值的部分喂给模型，让模型用最少 token 回答问题」。必要时告诉模型「还有更多，按需再查」。

---

## 设计要点 5：错误即数据，不要抛异常

观察 `_finnhub_get`：任何异常都**不向外抛**，而是返回 `{"error": "..."}`。

```python
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())
except urllib.error.HTTPError as exc:
    return {"error": f"HTTP {exc.code}: {exc.reason}"}
except TimeoutError:
    return {"error": "Request timed out (10 s)"}
...
```

**为什么？**

- 如果抛异常，Agent 框架会把栈信息直接返回给模型——又长又难读，还可能包含内部实现细节
- 如果抛异常，模型看到「工具崩溃了」，容易跳出任务、停止重试
- 返回 `{"error": "..."}` 作为**正常数据**，模型可以：
  - 读懂错误（"HTTP 401: Unauthorized" → 知道 token 有问题）
  - 决定是否改参数重试（"404" → 尝试换个股票代码）
  - 告诉用户「这个数据查不到，因为 xxx」

System prompt 里也有一条对应的指令：

> **错误处理**：如果工具返回错误，解释发生了什么并建议替代方案。

**通用原则**：工具要对 LLM 友好——把所有「异常」都当作「一种特殊的数据」返回，让模型自己决定怎么处理。

---

## 设计要点 6：System Prompt 的三段式

`systemprompt.md` 一共做三件事：

### ① 工具地图（告诉模型有什么）

用**表格**列出全部 18 个工具，按**功能分组**：

```markdown
| 类别 | 工具 | 功能说明 |
|------|------|---------|
| **股票核心** | `finnhub_quote` | 实时价格、涨跌幅、最高/最低/开盘价 |
| | `finnhub_symbol_lookup` | 按名称/ISIN 搜索股票代码 |
| | `finnhub_company_profile` | 公司信息：行业、市值、IPO 日期 |
| | `finnhub_peers` | 同行/竞争对手代码列表 |
| **基本面** | `finnhub_basic_financials` | P/E、P/B、EPS、股息率、52 周范围等 |
...
```

虽然每个工具的 schema 里都有 `description`，模型确实能看到，但**统一的表格地图**能让它一眼看到「全景」，更容易选对。

### ② 使用指南（告诉模型什么时候用）

另一张**场景 → 工具**的映射表：

```markdown
| 用户询问内容 | 使用工具 | 关键参数 |
|------------|---------|---------|
| 当前股价 | `finnhub_quote` | symbol |
| "AAPL 是什么公司？" | `finnhub_company_profile` | symbol |
| 查找"微软"的代码 | `finnhub_symbol_lookup` | query |
| 市盈率、市值、财务指标 | `finnhub_basic_financials` | symbol |
| 盈利是否超预期？ | `finnhub_earnings_surprises` | symbol |
...
```

这张表比 `description` 更接近**用户的原问句**，模型把用户问题和表左列对齐，就知道该调哪个工具。

再加一条「多工具综合」指引，告诉模型**组合策略**：

```markdown
## 多工具综合分析

进行全面的股票分析时，组合使用多个工具：
1. finnhub_company_profile → 公司概况
2. finnhub_quote           → 当前股价
3. finnhub_basic_financials → 核心财务指标
4. finnhub_recommendation  → 分析师共识
5. finnhub_earnings_surprises → 近期盈利表现
6. finnhub_company_news    → 最新动态
```

### ③ 能力边界（告诉模型没有什么）

**最容易被忽略但最关键的一节**：

```markdown
# 功能限制

🚫 以下数据在当前套餐中**不可用**（请勿尝试获取）：
- 历史股票/外汇/加密货币 K 线数据
- 技术指标（SMA、RSI、MACD 等）
- 分析师目标价
- 股息和拆股历史
- 指数成分股（如标普 500 成分列表）
- ...

如果用户询问以上内容，请说明这些数据需要 Finnhub 高级订阅，并建议使用现有工具的替代方案。
```

没有这一节，模型会**幻觉**——用户问「给我苹果过去一年的 K 线」，模型可能会乱选一个工具（比如 `finnhub_quote`），返回一次报价冒充 K 线，用户被误导。

有了这一节，模型会直接说：「这个数据不在我的工具范围内，但我可以用 xxx 给你一个近似的替代」。

**通用原则**：别只告诉模型「它能做什么」，还要明确告诉它「它不能做什么」。能力边界是防幻觉的第一道闸。

---

## 设计要点 7：Jinja 让上下文动态注入

看 `agent.yaml`：

```yaml
system_prompt: ./systemprompt.md
system_prompt_type: jinja
```

`system_prompt_type: jinja` 意味着 `systemprompt.md` 是一个 Jinja 模板，平台在每次对话前把**运行时变量**渲染进去。

`systemprompt.md` 末尾：

```markdown
# 上下文

- 日期：{{ date }}
- 用户：{{ username }}
```

**为什么重要？**

- `{{ date }}` → 模型知道「今天」是几号，才能正确计算「最近 7 天新闻」的日期范围
- `{{ username }}` → 可以做简单个性化

对于财经 Agent，`{{ date }}` 尤其关键——涉及到 `finnhub_company_news`、`finnhub_earnings_calendar` 等带日期参数的工具，模型必须知道「今天」才能填对 `from_date` / `to_date`。如果模型用训练截止时的日期，查出来的新闻全是过期的。

**通用原则**：凡是对话时可能变化的变量（日期、用户信息、环境标识、当前项目 ID 等），都用 Jinja 注入，不要写死在 prompt 里。

---

## 设计要点 8：ContextCompactionMiddleware 应对多工具长会话

`agent.yaml` 最后挂了一个中间件：

```yaml
middlewares:
  - import: nexau.archs.main_sub.execution.middleware.context_compaction:ContextCompactionMiddleware
    params:
      threshold: 0.6
```

**为什么需要它？**

一次「综合股票分析」可能调用 6 个工具，每个工具返回几 KB 的 JSON。如果再加上用户的多轮追问，上下文很快会冲到 context 上限附近。

`ContextCompactionMiddleware` 在**上下文使用率达到 60%** 时自动触发，把早期的工具调用历史压缩成摘要，腾出空间给后续对话。

**参数选择**：

- `threshold: 0.6` — 比较激进，适合工具输出多的 Agent
- `0.7 ~ 0.8` — 比较保守，适合对话密集、工具输出少的 Agent

**通用原则**：凡是**工具调用密集**的 Agent（比如本样例、数据库问数），都建议挂上这个中间件。

---

## 完整配置文件

### nexau.json

```json
{
  "agents": {
    "finnhub_agent": "agent.yaml"
  },
  "excluded": [".nexau/", ".env", "__pycache__/", "start.py"]
}
```

- `agents` — 项目里的 Agent 入口
- `excluded` — 发布时要排除的文件（`.env` 含密钥必须排除）

### agent.yaml（关键部分）

```yaml
type: agent
name: finnhub_agent
description: >-
  Financial data analyst agent powered by Finnhub API...

system_prompt: ./systemprompt.md
system_prompt_type: jinja

llm_config:
  api_type: openai_chat_completion
  model: ${env.LLM_MODEL}
  base_url: ${env.LLM_BASE_URL}
  api_key: ${env.LLM_API_KEY}
  max_tokens: 8192
  temperature: 0.2     # ← 金融场景要稳定，低温
  stream: True

max_iterations: 50     # ← 多工具综合分析时，可能需要 10+ 轮调用
max_context_tokens: 128000
tool_call_mode: structured

tools:
  - name: read_file
    yaml_path: tools/read_file.tool.yaml
    binding: nexau.archs.tool.builtin.file_tools:read_file
  - name: search_file_content
    yaml_path: tools/search_file_content.tool.yaml
    binding: nexau.archs.tool.builtin.file_tools:search_file_content
  - name: run_shell_command
    yaml_path: tools/run_shell_command.tool.yaml
    binding: nexau.archs.tool.builtin.shell_tools:run_shell_command

  # 18 个 Finnhub 工具，每个都通过 extra_kwargs 注入 API token
  - name: finnhub_quote
    yaml_path: tools/finnhub_quote.tool.yaml
    binding: custom_tools.finnhub_api:finnhub_quote
    extra_kwargs:
      api_token: ${env.X_FINNHUB_SECRET}
  # ... 其余 17 个同构

middlewares:
  - import: nexau.archs.main_sub.execution.middleware.context_compaction:ContextCompactionMiddleware
    params:
      threshold: 0.6
```

**关键配置解读**：

- `temperature: 0.2` — 金融数据对准确度敏感，低温度减少胡说
- `max_iterations: 50` — 给多工具综合分析留足预算
- `tool_call_mode: structured` — 用 OpenAI 风格的 function calling，比纯文本解析更稳

---

## 动手实践：封装你自己的 REST API

要把任何一个 REST API 变成 Agent 工具，按下面 7 步走：

### Step 1 — 盘点端点

列出你要暴露给 Agent 的每一个端点：path、方法、参数、响应结构。本例是 Finnhub 的 18 个端点。

### Step 2 — 给端点**分组分类**

按**业务维度**分组（比如 Finnhub 分为「股票核心、基本面、分析师、内部人、新闻、SEC、日历、市场状态」）。这个分组后面要写进 system prompt 的工具地图。

### Step 3 — 为每个端点写 `.tool.yaml`

输入 schema 严格匹配你 Python 函数的参数名。`description` 以「用户问题」的视角来写，而不是「API 文档」的视角。

### Step 4 — 写一个 HTTP helper，再写业务函数

所有共性（token 注入、超时、错误处理、URL 编码）抽进一个 helper。每个端点对应一个 Python 函数，只做三件事：调 helper、做必要的精简、`json.dumps` 返回。

### Step 5 — 决定每个端点的「窄数据」策略

- 条数多的（新闻、交易）→ 截取前 N 条
- 字段多的 → 只挑 3-5 个
- 内嵌大对象的（完整财报）→ 只返回 keys 摘要 + 「要详情请按 key 再查」的提示

### Step 6 — 在 `agent.yaml` 里注册，所有密钥走 `extra_kwargs`

一个工具一个 entry。需要密钥的都用 `extra_kwargs: api_token: ${env.XXX}`，不要放进 schema。

### Step 7 — 写 System Prompt 的三段式

- **工具地图**：按分组列全表
- **使用指南**：场景 → 工具的映射表
- **能力边界**：明确列出「不支持什么」，防止幻觉

测试时故意问一些**能力边界外**的问题，看模型会不会幻觉；再问一些**需要多工具**的问题，看模型会不会组合调用。

---

## 注意事项

### API Key 安全

- `X_FINNHUB_SECRET` 只能通过 `extra_kwargs` 注入，**不要**出现在 tool.yaml 或 systemprompt.md 里
- 本地 `.env` 一定要在 `nexau.json` 的 `excluded` 里声明，否则上传时可能被一起打包

### 套餐限制

Finnhub 的免费 / 基础套餐不支持某些端点。对应的能力边界**必须写进 system prompt**，否则模型会幻觉。你迭代时如果升级了套餐、多解锁了数据，记得回头更新 `# 功能限制` 一节。

### 返回数据的大小

每个工具函数的返回值最终会变成模型上下文的一部分。定期用真实调用测一下单次返回的 token 数——如果某个端点的返回还是过大，继续在工具里做二次摘要。

### ContextCompactionMiddleware import 路径

`nexau.archs.main_sub.execution.middleware.context_compaction:ContextCompactionMiddleware` — 注意有 `execution` 这一层，漏了会报 import 错误。

### 把日期注入进 prompt

`system_prompt_type: jinja` + `{{ date }}` 务必保留。不然模型会用训练截止时的日期去查「最近一周新闻」，返回的全是过期数据，用户体验非常差。

---

## 下一步

读完本篇，你已经掌握了 Agent 的**工具驱动**形态——外部 API 可以变成 Agent 的手脚。下一站建议进入 Level 2，看 [**企业数据库问数**](../企业数据库问数/tutorial-text-to-sql.md)——把 Skill 从「装政策原文」切换到「装数据库使用说明」的新范式。

← 返回 [Cookbook 主页](../README.md) ｜ [通用约定](../GETTING_STARTED.md)
