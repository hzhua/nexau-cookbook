# 教程：Text-to-SQL via SKILL.md — 企业数据库问数

> 🟡 **Level 2 · 进阶** ｜ ⏱ 约 1 h ｜ 🔑 Text-to-SQL + 每表一 Skill 的知识编码
>
> Agent 答错不是因为模型不会写 SQL，而是不了解你的数据库。Skill 就是把你的领域知识传递给模型的方式。本文以一个 7 表、50 家企业的真实数据库为例，手把手教你一步步写出让 Agent 答对的 SKILL.md。
>
> 💡 本篇讲 Skill 的**另一种用法**——不是装政策原文，而是装数据库的"使用说明书"。如果你还不熟悉 Skill 基本形态，建议先看 [**劳动法问答**](../劳动法问答/tutorial-agentic-rag-agent.md)。

## 目录

- [我们要解决什么问题](#我们要解决什么问题)
- [生产环境的 SQL 执行方式](#生产环境的-sql-执行方式)
- [认识 SKILL.md](#认识-skillmd)
- [第一步：审计你的数据库](#第一步审计你的数据库)
- [第二步：写 Schema 表](#第二步写-schema-表)
- [第三步：处理类型陷阱](#第三步处理类型陷阱)
- [第四步：写出业务规则](#第四步写出业务规则)
- [第五步：写示例查询](#第五步写示例查询)
- [第六步：引导跨表查询](#第六步引导跨表查询)
- [第七步：写好 description](#第七步写好-description)
- [第八步：测试与迭代](#第八步测试与迭代)
- [完整示例](#完整示例)
- [自查清单](#自查清单)

---

## 我们要解决什么问题

以一个企业数据库为例，7 张表、50 家企业：

| 表名 | 说明 | 关联 |
|------|------|------|
| `enterprise_basic` | 企业基本信息（注册、行业、专精特新等，37 列） | 主表 |
| `enterprise_contact` | 联系人（法人、总经理） | `credit_code` → 主表 |
| `enterprise_financing` | 融资与上市（贷款、估值、上市状态） | `credit_code` → 主表 |
| `enterprise_product` | 产品与知识产权 | `credit_code` → 主表 |
| `industry` | 行业链节点（树结构） | 被映射表引用 |
| `industry_enterprise` | 企业 ↔ 行业链映射 | 连接 industry 和主表 |
| `users` | 平台用户账号 | **独立表，与企业无关** |

没有 Skill 的 Agent 会犯这些错：

- 问"注册资本最高的企业"→ 按字母序排序，"8000" 排在 "50000" 前面（因为 `register_capital` 是 TEXT）
- 问"专精特新小巨人企业"→ 写出 `WHERE zhuanjingtexin_level = '小巨人'`，查不到任何结果
- 问"AI 上游有哪些企业"→ 不知道要三表 join，也不知道 `chain_position='up'` 只在 depth=1 节点上
- 问"平台有多少用户"→ 去查 `enterprise_basic` 而不是 `users`

**这些错误不是模型的问题，是知识缺失的问题。** 下面一步步教你写 Skill 来解决。

---

## 生产环境的 SQL 执行方式

> ⚠️ **关于本样例**：为了让这份 Cookbook 可以开箱即运行，我们把企业数据打包成了一个 **SQLite 文件 (`enterprise.sqlite`)**，通过一个简单的 `execute_sql` 自定义工具以 `mode=ro` 只读方式访问。**这是教学用的模拟环境，不是生产方案。**

真实政务场景里，Agent 通常不会直连一个文件数据库。常见有两种做法：

| 方案 | 用法 | 适用情形 |
|------|------|---------|
| **① SQL 执行 API** | 由业务后端暴露一个 HTTP 接口（如 `POST /query`），接受 SQL 字符串、返回结果集。Agent 端写一个 custom tool 调用这个 API。 | DB 不能直连、需要走网关 / 鉴权体系、或多租户场景下按用户切数据权限 |
| **② 直连数据库（custom tool + URI）** | Agent 侧写一个 custom tool，拿 PostgreSQL / MySQL / DuckDB 等的连接 URI（`postgresql://...`、`mysql://...`）直接连库。 | Agent 与 DB 处于同一可信网络、且能静态绑定只读账号 |

### 两种方案都必须满足的底线

无论选哪条路径，**都要从工具层把权限收紧到最小面**，不能指望 LLM 自觉：

- **只读权限**：给 Agent 用的账号（或 API 服务端使用的账号）在数据库层面就是只读。任何 `INSERT / UPDATE / DELETE / DROP / ALTER / CREATE` 在 DB 侧直接被拒，而不是靠 prompt 约束。SQLite 用 `?mode=ro` URI 参数，PostgreSQL 用 `GRANT SELECT ONLY` 或专门的 `readonly` role，MySQL 同理。
- **范围限制**：
  - 方案①：API 服务端对 SQL 做白名单/黑名单校验（禁写操作关键字、限制可访问的表/schema、限制 JOIN 深度和返回行数）。
  - 方案②：连接串绑到**特定 schema / database**（如 PostgreSQL 的 `search_path`、MySQL 的 `USE db`），账号只对业务需要的若干张表有 SELECT 权限，其他表既读不到也列不出来。
- **语句与资源保护**：强制 `statement_timeout`（超时）、`LIMIT` 默认值、最大返回字节数。防止模型不小心写出 `SELECT *` 全表扫描把数据库打挂。
- **审计**：打开 DB 的查询日志或 API 的访问日志，所有 Agent 发出的 SQL 可追溯。线上事故排查、合规审计都靠这个。
- **敏感字段脱敏**：身份证号、手机号、联系人等列要么用视图只暴露脱敏版本，要么直接不给 Agent 账号 SELECT 权限。

一句话：把 Agent 当成一个**不受信任的外部调用者**来授权——它能看到的、能做的，仅限于业务真正需要它看到 / 做的那一小块。本教程聚焦「**怎么让 Agent 把 SQL 写对**」，生产接入时请按上面这套原则改造执行层。

---

## 认识 SKILL.md

在开始写之前，先了解 Skill 的结构和工作原理。

### 一表一 Skill

每张表写一个 SKILL.md，放在独立文件夹中：

```
skills/
├── enterprise_basic/
│   └── SKILL.md          ← 企业基本信息
├── enterprise_financing/
│   └── SKILL.md          ← 融资与上市
├── enterprise_contact/
│   └── SKILL.md          ← 联系人
├── enterprise_product/
│   └── SKILL.md          ← 产品与知识产权
├── industry/
│   └── SKILL.md          ← 行业链节点
├── industry_enterprise/
│   └── SKILL.md          ← 企业-行业映射
└── users/
    └── SKILL.md          ← 平台用户
```

为什么不把所有表写在一个文件里？因为模型不需要每次都读全部表的知识。用户问"注册资本最高的企业"，只需要加载 `enterprise_basic` 的 Skill；问"谁的估值最高"，只需要加载 `enterprise_financing` 的 Skill。**按需加载，节省 token，减少干扰。**

### 文件结构：frontmatter + body

每个 SKILL.md 分两层：

```markdown
---
name: enterprise_basic                    ← frontmatter
description: >-                           ← 始终可见，用于路由
  当用户询问企业的注册信息、地址、规模、
  行业分类或专精特新等级时使用此 Skill...
---

# enterprise_basic — 企业基本信息         ← body
                                          ← 按需加载，包含详细知识
## 何时使用
...
## Schema
...
## 常见取值
...
## 示例查询
...
## 注意事项
...
```

**两层的设计是关键：**

| 层 | 内容 | 何时可见 | 作用 |
|---|---|---|---|
| frontmatter | `name` + `description` | **始终可见**——每次对话都在模型上下文中 | 路由：让模型决定"要不要读这个 Skill" |
| body | Schema、枚举、示例 SQL、注意事项 | **按需加载**——模型决定读取时才注入 | 知识：告诉模型怎么写正确的 SQL |

类比一下：`description` 是书的封面和目录，模型看封面就知道要不要翻开这本书；body 是书的正文，翻开后才看到。

### body 各章节的作用

| 章节 | 写什么 | 为什么需要 |
|------|-------|-----------|
| **何时使用** | 3-5 个典型用户问题 | 帮模型确认"我读对了 Skill" |
| **Schema** | 列名、类型、业务描述 | 模型不一定能看到数据库的 `PRAGMA table_info`，Schema 是它写 SQL 的依据 |
| **常见取值** | 枚举值、取值范围 | 防止模型猜错 `WHERE` 条件 |
| **示例查询** | 完整可执行的 SQL | Few-shot 示例——模型模仿这些 SQL 来写新查询 |
| **注意事项** | 容易出错的点 | 最后一道防线，把"坑"说清楚 |

你不一定每张表都需要全部章节。简单的表（如 `users`）可能只需要 Schema 和"何时使用"；复杂的表（如 `enterprise_basic`）则需要写全。

---

## 第一步：审计你的数据库

编写 Skill 之前，逐表回答以下问题：

| 问题 | 目的 |
|------|------|
| 这张表存的是什么？一行代表什么？ | 让模型理解业务含义 |
| 哪些列的存储类型和业务含义不匹配？ | 发现类型陷阱（TEXT 存数字） |
| 哪些列只有几个固定取值？ | 列出枚举值，减少猜测 |
| 哪些字段是预计算的？ | 防止模型重复计算 |
| 表之间怎么 join？ | 标注主键和外键 |
| 用户最常问什么？正确的 SQL 怎么写？ | 准备示例查询 |

**每回答一个问题，就往 SKILL.md 里写一条。**

以 `enterprise_basic` 为例：
- 一行 = 一家企业，37 列涵盖注册信息、行业分类、专精特新等级
- `register_capital` 是 TEXT 存数字 → 需要 CAST 提示
- `enterprise_scale` 只有 4 个值（微型/小型/中型/大型）→ 列出枚举
- `zhuanjingtexin_level` 有 3 个等级 + NULL → 列出并标注高低顺序
- 通过 `credit_code` 与其他 enterprise_* 表 join

---

## 第二步：写 Schema 表

Schema 是 Skill 的核心——模型依赖它来写 SQL。写好 Schema，模型就知道用哪些列、什么类型、怎么过滤。

### 基本格式

每列一行，包含列名、类型和业务描述：

```
| 列名 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 内部行 ID |
| `credit_code` | TEXT | 统一社会信用代码 |
| `enterprise_name` | TEXT | 企业名称 |
```

### 不只写列名——写"给模型的说明书"

Schema 不是数据库文档的搬运，而是写给模型看的说明。对比两种写法：

❌ **照搬数据库注释**：

```
| `register_capital` | TEXT | 注册资本 |
```

模型看到 TEXT，不知道里面存的是数字，排序时会出错。

✅ **标注陷阱和正确用法**：

```
| `register_capital` | TEXT | 注册资本（万元）— **TEXT not numeric**, use `CAST(register_capital AS REAL)` |
```

模型看到这行就知道：这列虽然是 TEXT，但要当数字处理。

### 标注关键角色

在 Schema 中标注每列的特殊角色：

```
| `credit_code` | TEXT | **关联键。** 统一社会信用代码——所有 enterprise_* 表共享 |
```

```
| `id` | INTEGER PK | 内部节点 ID——被 `industry_enterprise.industry_id` 引用 |
```

**关联键** 和 **PK** 这些标注帮模型理解表间关系，写 join 时不会用错列。

### 不需要写全部列

37 列的表不需要每列都写进 Schema。优先写这些列：

1. **主键和外键**——模型 join 的依据
2. **用户常查询的列**——出现在 WHERE、ORDER BY、SELECT 中的
3. **有陷阱的列**——类型不匹配、枚举值隐晦的
4. **容易混淆的列**——名字相近但含义不同的

不常用、含义显而易见的列可以省略（如 `created_at`、`updated_at`）。

---

## 第三步：处理类型陷阱

`register_capital`（注册资本）是 TEXT 类型。直接 `ORDER BY register_capital DESC`，"8000" 排在 "50000" 前面——字母序 `"8" > "5"`。

**Skill 中需要在三个地方同时提示**，形成"三点联动"：

**① Schema 表——紧邻列名标注**：

```
| `register_capital` | TEXT | 注册资本（万元）— TEXT not numeric, use CAST(register_capital AS REAL) |
```

**② 示例查询——展示正确写法**：

```sql
SELECT enterprise_name, CAST(register_capital AS REAL) AS capital_wan
FROM enterprise_basic
WHERE register_district = '海淀区'
ORDER BY capital_wan DESC LIMIT 10;
```

**③ 注意事项——解释为什么错**：

```
- register_capital is TEXT — always CAST(register_capital AS REAL).
  Direct ORDER BY gives wrong results (string sort: "8000" > "50000").
```

**为什么要三处同时写？** 只写一处，模型可能忽略。三处同时提示，遗漏概率大幅降低——Schema 让它写对类型，示例查询让它模仿正确写法，注意事项解释为什么不能偷懒。

同理，`enterprise_product.daily_capacity` 也是 TEXT 存数字，需要同样处理。

---

## 第四步：写出业务规则

类型陷阱看 Schema 就能发现，但**业务规则只有了解业务的人才知道**。这是 Skill 最有价值的部分。

### 枚举值——写进"常见取值"

用户问"专精特新小巨人企业有哪些"，模型不知道数据库中"小巨人"的精确值是什么。如果你不告诉它，它就会猜：

❌ 模型猜测的写法：

```sql
WHERE zhuanjingtexin_level = '小巨人'
-- 或者
WHERE zhuanjingtexin_level LIKE '%小巨人%'
```

第一种查不到结果（精确值不匹配），第二种可能匹配到多个等级。

✅ Skill 中列出精确值和层级：

```
## 常见取值

- zhuanjingtexin_level 层级：
  专精特新中小企业 < 专精特新潜在"小巨人"企业 < 专精特新"小巨人"企业
  NULL = 无专精特新认定
  "小巨人企业": WHERE zhuanjingtexin_level = '专精特新"小巨人"企业'
```

有了这条，模型能准确写出 `WHERE zhuanjingtexin_level = '专精特新"小巨人"企业'`。

### 同义词映射——用户说的和数据库存的不一样

用户会用各种说法，但数据库中只有固定的几个值。在"常见取值"或"注意事项"中标注映射关系：

```
- listing_status values: 未上市, 新三板, 已上市, 拟上市
  "已上市的企业" = WHERE listing_status = '已上市'
  "有上市计划的" = WHERE listing_status IN ('拟上市', '已上市')
  "在新三板挂牌的" = WHERE listing_status = '新三板'
```

```
- loan_purpose values: 流动资金, 设备采购, 研发投入, 扩大产能
  "贷款用于研发" = WHERE loan_purpose = '研发投入'
```

**原则：用户可能用的说法 → 对应的 WHERE 条件。** 你预判得越全，模型写对的概率越高。

### 树结构——不写清楚层级关系，模型无从下手

`industry` 表不是扁平表，不写清楚层级关系，模型不知道怎么查"AI 上游企业"：

```
## 注意事项

- depth=0：产业链根节点，depth=1：上游/中游/下游，depth=2：叶子节点
- chain_position（'up'/'mid'/'down'）仅在 depth=1 的节点上有值
- "AI 上游企业" = join industry (chain_position='up') → industry_enterprise → enterprise_basic
```

### 容易混淆的表——主动区分

`users` 是平台账号，不是企业。用户说"用户"时，99% 的情况是指企业：

```
## 注意事项

- "用户"在本数据库中通常指企业（enterprise_basic），不是平台用户（users）
- 只有明确问"平台管理员"、"登录账号"时才查 users 表
```

### 预计算字段——防止重复计算

如果数据库中有预计算好的字段，告诉模型直接用，不要重新算：

```
| `credit_satisfaction_rate` | REAL | 信贷满足率 (0.0 – 1.0)，已预计算，直接使用 |
```

不标注的话，模型可能试图用其他字段重新计算，既浪费又容易算错。

---

## 第五步：写示例查询

示例查询是 few-shot 示例——模型会模仿这些 SQL 来写新查询。好的示例胜过大段描述。

### 每个示例展示一个"正确处理"

不要写无聊的示例。每个示例应至少示范一个关键技巧：

❌ **展示不了任何技巧的示例**：

```sql
-- 查询所有企业
SELECT * FROM enterprise_basic;
```

✅ **展示 CAST 处理的示例**：

```sql
-- 海淀区注册资本 Top 10
SELECT enterprise_name,
       CAST(register_capital AS REAL) AS capital_wan,
       enterprise_scale
FROM enterprise_basic
WHERE register_district = '海淀区'
ORDER BY capital_wan DESC LIMIT 10;
```

这个示例同时展示了：CAST 用法、WHERE 过滤、别名、排序、LIMIT。

### 覆盖常见查询模式

为每张表准备 2-4 个示例，覆盖这些模式：

| 模式 | 示例 |
|------|------|
| **过滤 + 排序** | 按条件筛选 + ORDER BY |
| **聚合统计** | GROUP BY + COUNT/AVG/SUM |
| **跨表 join** | 两表或多表 join |
| **枚举值精确匹配** | 使用"常见取值"中的精确值 |

以 `enterprise_financing` 为例：

```sql
-- 模式 1：过滤 + 排序 — 估值最高的企业
SELECT b.enterprise_name, f.recent_valuation, f.listing_status
FROM enterprise_financing f
JOIN enterprise_basic b ON b.credit_code = f.credit_code
ORDER BY f.recent_valuation DESC
LIMIT 10;

-- 模式 2：聚合统计 — 各行业平均融资需求
SELECT b.industry_level1, AVG(f.next_financing_demand) AS avg_demand
FROM enterprise_financing f
JOIN enterprise_basic b ON b.credit_code = f.credit_code
WHERE f.next_financing_plan != '暂无'
GROUP BY b.industry_level1
ORDER BY avg_demand DESC;

-- 模式 3：枚举值精确匹配 — 已上市企业按交易所分布
SELECT planned_listing_location, COUNT(*) AS n
FROM enterprise_financing
WHERE listing_status = '已上市'
GROUP BY planned_listing_location;
```

### 加注释说明意图

在 SQL 前加一行注释，说明这个查询回答什么问题。模型看到注释就能将用户问题映射到查询模式：

```sql
-- 专精特新小巨人中已上市的企业
SELECT b.enterprise_name, f.listing_status, f.stock_code
FROM enterprise_basic b
JOIN enterprise_financing f ON b.credit_code = f.credit_code
WHERE b.zhuanjingtexin_level = '专精特新"小巨人"企业'
  AND f.listing_status = '已上市';
```

---

## 第六步：引导跨表查询

用户问"专精特新小巨人企业中有哪些已上市？"需要 join 两张表。模型需要知道三件事：**用什么 key join**、**join 哪张表**、**完整的 SQL 长什么样**。

### 在 Schema 中标注 join key

```
| `credit_code` | TEXT | **关联键。** 统一社会信用代码——所有 enterprise_* 表共享 |
```

### 在 description 中提示 join 关系

```yaml
description: >-
  当用户询问企业的身份、注册信息、地址、规模或行业分类时使用此 Skill。
  通过 credit_code 关联其他 enterprise_* 表。
```

### 在示例查询中提供完整 join SQL

**两表 join**——`enterprise_basic` + `enterprise_financing`：

```sql
SELECT b.enterprise_name, f.listing_status, f.stock_code
FROM enterprise_basic b
JOIN enterprise_financing f ON b.credit_code = f.credit_code
WHERE b.zhuanjingtexin_level = '专精特新"小巨人"企业'
  AND f.listing_status = '已上市';
```

**三表 join**——"AI 上游有哪些企业"涉及 industry → industry_enterprise → enterprise_basic：

```sql
SELECT b.enterprise_name, i.name AS industry_node
FROM industry i
JOIN industry_enterprise ie ON ie.industry_id = i.id
JOIN enterprise_basic b ON b.credit_code = ie.credit_code
WHERE i.chain_id = 45 AND i.chain_position = 'up';
```

**没有这些示例，模型很可能用错 join key 或漏掉中间表。** 三表 join 尤其需要手把手引导——模型不知道 `industry_enterprise` 是桥接表，可能试图直接 join `industry` 和 `enterprise_basic`。

### "何时使用"中写明跨表指引

在每张表的"何时使用"中，标注相关表在哪：

```
## 何时使用

- "某某企业注册在哪个区？"
- "海淀区有多少小型企业？"
- "列出所有专精特新小巨人企业"

如需联系方式请查 `enterprise_contact`，融资/上市信息请查 `enterprise_financing`，产品请查 `enterprise_product`。
```

这帮助模型在加载了一个 Skill 后，知道还需要哪些 Skill 来完成跨表查询。

---

## 第七步：写好 description

`description` 决定模型"是否读取这个 Skill"。它始终可见，所以每个字都要有路由价值。

### 正面路由——覆盖用户的不同说法

用户问同一件事有很多种表达方式。`description` 要把这些说法都覆盖到：

```yaml
description: >-
  当用户询问企业融资情况时使用此 Skill——包括银行贷款、股权融资、估值、上市状态、
  拟上市地点、未来融资需求等。通过 credit_code 关联 enterprise_basic。
```

这个 description 覆盖了：银行贷款、股权融资、估值、上市状态、融资需求。用户不管怎么问，模型都能匹配上。

### 负面路由——主动"劝退"

有些表容易被混淆，需要在 description 中明确"什么时候不该用"：

```yaml
description: >-
  仅当用户明确询问平台用户（登录账号、SSO ID、角色）时使用此 Skill。
  此表与企业表无关。大多数关于"用户"的问题实际上指的是企业而非平台用户。
```

### 常见反模式

| 反模式 | 问题 | 改进 |
|--------|------|------|
| `"企业基本信息"` | 太笼统，模型不知道什么问题该来这里 | `"当用户询问企业注册信息、地址、规模、行业分类或专精特新等级时使用"` |
| `"融资表"` | 纯标签，没有路由信息 | `"当用户询问银行贷款、股权融资、估值、上市状态时使用"` |
| 把 Schema 塞进 description | 浪费 token，description 是路由不是知识 | description 只放路由提示，Schema 放 body |
| 没写 join 关系 | 模型不知道这张表和谁关联 | 末尾加 `"通过 credit_code 关联 enterprise_basic"` |

---

## 第八步：测试与迭代

用边界问题验证 Skill 的效果：

- "注册资本最高的企业" → 是否正确 CAST？
- "专精特新小巨人企业有哪些" → 枚举值是否精确匹配？
- "已上市的小巨人企业" → 跨表 join 是否正确？
- "AI 产业链上游有哪些企业" → 三表 join 路径对吗？
- "平台有多少管理员" → 是否走了 users 而不是 enterprise_basic？

**每次 Agent 答错，分析根因并更新 Skill**：

| Agent 的错误 | 根因 | Skill 如何修改 |
|-------------|------|---------------|
| 没有读取该 Skill | description 没覆盖用户的关键词 | description 中加入用户使用的关键词 |
| 类型处理错误 | 只在一处提了 CAST | Schema + 注意事项 + 示例查询三处同时加提示 |
| 枚举值写错 | 模型在猜值 | "常见取值"中加入精确值 |
| Join 写错 | 模型不知道表间关系 | Schema 标注 FK，Example 加 join SQL |
| 误用了错误的表 | 表名或业务含义相近 | description 加强负面路由 |
| SQL 能跑但结果不对 | 缺少过滤条件（如未排除已取消订单） | "注意事项"中加入过滤规则 |

**Skill 是活文档——每修复一个错误就加一条提示，准确率逐步提升。**

---

## 完整示例

### enterprise_basic——主表的 SKILL.md

注意每个章节如何对应前面的步骤：

````markdown
---
name: enterprise_basic
description: >-
  当用户询问企业的身份、注册信息、地址、规模、行业分类或"专精特新"认定等级时使用此 Skill。
  这是最核心的主表——几乎所有涉及企业的查询都从这里开始。通过 credit_code 关联其他 enterprise_* 表。
---

# enterprise_basic — 企业基本信息

企业数据库的核心注册表。每家企业一行，以 `credit_code` 为唯一标识。

## 何时使用

- "某某企业注册在哪个区？"
- "海淀区有多少小型企业？"
- "列出所有专精特新小巨人企业"
- "某某企业的注册资本是多少？"

如需联系方式请查 `enterprise_contact`，融资/上市信息请查 `enterprise_financing`，产品与知识产权请查 `enterprise_product`。

## Schema

| 列名 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 内部行 ID |
| `credit_code` | TEXT | **关联键。** 统一社会信用代码——所有 enterprise_* 表共享 |
| `enterprise_name` | TEXT | 企业名称 |
| `register_district` | TEXT | 注册地所在区（如 `海淀区`、`黄浦区`） |
| `register_capital` | TEXT | 注册资本（万元）— **注意是 TEXT 类型**，数值比较时需用 `CAST(register_capital AS REAL)` |
| `enterprise_scale` | TEXT | 取值：`微型`、`小型`、`中型`、`大型` |
| `enterprise_type` | TEXT | 取值：`民营`、`国有`、`合资`、`外资` |
| `industry_level1` | TEXT | 行业一级分类（如 `制造业`、`金融业`） |
| `zhuanjingtexin_level` | TEXT | 专精特新等级——详见"常见取值" |

## 常见取值

- `enterprise_scale`：`微型`、`小型`、`中型`、`大型`
- `enterprise_type`：`民营`、`国有`、`合资`、`外资`
- `zhuanjingtexin_level`：`专精特新中小企业` < `专精特新潜在"小巨人"企业` < `专精特新"小巨人"企业`
  - NULL = 无专精特新认定
  - "小巨人企业"：`WHERE zhuanjingtexin_level = '专精特新"小巨人"企业'`

## 示例查询

**海淀区注册资本 Top 10：**
```sql
SELECT enterprise_name,
       CAST(register_capital AS REAL) AS capital_wan,
       enterprise_scale
FROM enterprise_basic
WHERE register_district = '海淀区'
ORDER BY capital_wan DESC LIMIT 10;
```

**按等级统计专精特新企业数量：**
```sql
SELECT zhuanjingtexin_level, COUNT(*) AS n
FROM enterprise_basic
WHERE zhuanjingtexin_level IS NOT NULL
GROUP BY zhuanjingtexin_level ORDER BY n DESC;
```

**关联融资表查找已上市的小巨人企业：**
```sql
SELECT b.enterprise_name, f.listing_status, f.stock_code
FROM enterprise_basic b
JOIN enterprise_financing f ON b.credit_code = f.credit_code
WHERE b.zhuanjingtexin_level = '专精特新"小巨人"企业'
  AND f.listing_status = '已上市';
```

## 注意事项

- `register_capital` 是 **TEXT 类型**——排序或比较时必须用 `CAST(register_capital AS REAL)`。
- `zhuanjingtexin_level` 含中文引号——必须精确匹配：`'专精特新"小巨人"企业'`
````

### users——负面路由的示例

```markdown
---
name: users
description: >-
  仅当用户明确询问平台用户（登录账号、SSO ID、角色）时使用此 Skill。
  此表与企业表无关。大多数关于"用户"的问题实际上指的是企业而非平台用户——如有歧义请先确认。
---

# users — 平台用户账号

数据平台自身的系统用户——不是企业。

## 何时使用

- "平台有多少管理员？"
- "列出所有 admin 角色的用户"

**不要在以下场景使用：** 用户询问企业、客户、联系人时，请查 `enterprise_basic` 或 `enterprise_contact`。
```

### enterprise_financing——枚举值丰富的表

````markdown
---
name: enterprise_financing
description: >-
  当用户询问企业融资情况时使用此 Skill——包括银行贷款、股权融资、估值、上市状态、
  拟上市地点、未来融资需求等。通过 credit_code 关联 enterprise_basic。
---

# enterprise_financing — 企业融资与上市

每家企业一行，汇总银行贷款历史、股权融资、估值、上市状态。通过 `credit_code` 关联 `enterprise_basic`。

## 何时使用

- "某某企业有没有申请过银行贷款？"
- "某某企业最近的估值是多少？"
- "列出所有在北交所已上市的企业"
- "哪些企业计划在未来 12 个月内融资超过 10000 万？"

## Schema

| 列名 | 类型 | 说明 |
|---|---|---|
| `credit_code` | TEXT | 关联键 |
| `applied_bank_loan` | INTEGER | 是否申请过银行贷款（0/1） |
| `credit_satisfaction_rate` | REAL | 信贷满足率（0.0 – 1.0），已预计算 |
| `loan_purpose` | TEXT | 取值：`流动资金`、`设备采购`、`研发投入`、`扩大产能` |
| `listing_status` | TEXT | 取值：`未上市`、`新三板`、`已上市`、`拟上市` |
| `planned_listing_location` | TEXT | 取值：`无`、`上交所`、`深交所`、`北交所`、`港交所` |
| `recent_valuation` | REAL | 最近估值（万元） |
| `next_financing_demand` | REAL | 下一轮融资需求金额（万元） |

## 常见取值

- `listing_status`：`未上市`、`新三板`、`已上市`、`拟上市`
  - "已上市的企业" = `WHERE listing_status = '已上市'`
  - "有上市计划的" = `WHERE listing_status IN ('拟上市', '已上市')`
- `loan_purpose`：`流动资金`、`设备采购`、`研发投入`、`扩大产能`
- `planned_listing_location`：`无`、`上交所`、`深交所`、`北交所`、`港交所`

## 示例查询

**估值最高的企业：**
```sql
SELECT b.enterprise_name, f.recent_valuation, f.listing_status
FROM enterprise_financing f
JOIN enterprise_basic b ON b.credit_code = f.credit_code
ORDER BY f.recent_valuation DESC
LIMIT 10;
```

**各行业平均融资需求：**
```sql
SELECT b.industry_level1, AVG(f.next_financing_demand) AS avg_demand
FROM enterprise_financing f
JOIN enterprise_basic b ON b.credit_code = f.credit_code
WHERE f.next_financing_plan != '暂无'
GROUP BY b.industry_level1
ORDER BY avg_demand DESC;
```
````

---

## 自查清单

写完 SKILL.md 后，逐项检查：

- [ ] **Schema** — 主键、外键、常查询列都写了？类型陷阱紧邻列名标注了？
- [ ] **类型陷阱** — TEXT 存数字的列，Schema + 示例查询 + 注意事项三处都标了？
- [ ] **枚举值** — 固定取值的列都列了？有隐式高低的标了顺序？用户的说法映射到精确值了？
- [ ] **Join 路径** — Schema 标了 join key？description 提了 join 关系？Example 有完整 join SQL？
- [ ] **示例查询** — 每个示例至少展示一个"坑"的正确处理？覆盖了过滤、聚合、join 等常见模式？
- [ ] **description** — 正面路由覆盖了用户的不同说法？容易混淆的表加了负面路由？
- [ ] **何时使用** — 列了 3-5 个典型问题？标注了相关表的指引？

---

> Skill 写得准，Agent 才答得对。把 Schema 写清楚、类型陷阱标出来、业务规则列完整、Join 路径写明白——这就是数据库 Skill 工程的全部。

---

## 下一步

Level 2 结束。你已经掌握了**数据库 Skill 编码**形态——让 Agent 通过读 SKILL.md 写出准确的 SQL。接下来进入 Level 3，看完整的政务审核 Agent 长什么样：[**住房公积金审核**](../住房公积金审核/tutorial-house-fund-review.md)，把前面几篇的套路全部组合起来。

← 返回 [Cookbook 主页](../README.md) ｜ [通用约定](../GETTING_STARTED.md)
