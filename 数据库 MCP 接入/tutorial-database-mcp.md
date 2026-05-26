# 教程：外部 MCP 接入 DM8 数据库 — 企业经营问数 Agent

> Level 2 · 进阶 ｜ 约 1 小时 ｜ MCP server + 数据库只读访问 + Text-to-SQL

本篇新增一个“外部 MCP 接入数据库”的样例：我们不把数据库查询能力写成 NexAU 自定义工具，也不让 Agent 制品包启动 MCP 代码，而是连接已经部署好的 HTTP MCP 服务。

这类模式适合生产环境：数据库、MCP 服务和 Agent 分离部署。MCP server 成为数据库边界，鉴权、只读限制、SQL 校验、审计日志都应该收敛在这一层。

---

## 前置条件

跟着这份教程走之前,你的机器需要:

| 工具 / 资源 | 要求 | 验证 |
|---|---|---|
| `uv` | 任意版本 | `brew install uv` 或 `pip install uv`，然后 `uv --version` |
| Python | ≥ 3.12 | `python3 --version` |
| 操作系统 *(仅路径 A)* | Linux (dmpython 只发 Linux wheel) —— Mac/Windows 用户在 Linux 服务器 / WSL 上跑 server | `uname -s` 输出 `Linux` |
| 达梦数据库实例 *(仅路径 A)* | 一个可达的 DM8（自有部署 / 测试集群 / 公司提供） + 一个能 SELECT 的账号 | 用任意 DM 客户端工具 ping 通 |
| NAC 账号 + 项目 | 已注册 [North Agent Cloud](https://nac.xiaobei.top) 并建了一个项目 | 浏览器登录 → 项目列表能看到 |

> **没有达梦数据库 / 是 macOS 本地调试？** 直接走"路径 B · SQLite"，零外部依赖，本机一行命令搞定。

---

## 场景与目标

假设业务方有一个企业经营数据库，包含企业主数据、年度营收利润纳税、政策补贴记录。用户会问：

- “2024 年营收最高的 5 家企业是谁？”
- “各区县 2024 年纳税额排名如何？”
- “获得补贴但营收同比下降的企业有哪些？”
- “专精特新企业的平均利润率是多少？”

目标是构建一个数据库问数 Agent：

1. 数据库能力由外部 HTTP MCP server 暴露。
2. Agent 启动时自动发现 MCP 工具。
3. MCP server 负责数据库账号、网络、SQL 安全边界。
4. 数据库 schema、SQL 模板、跨表 join key 都内联在 `systemprompt.md` 里，避免模型乱猜字段。

---

## 项目结构

```text
数据库 MCP 接入/
├── dm8_mcp_server/                        ← 主样例:DM8 MCP 服务(production-style)
│   ├── server.py                          ← FastMCP + dmPython TCP 连接串
│   ├── start.sh                           ← 一键启动(uv sync + uv run python server.py,需 Linux)
│   └── pyproject.toml
├── sqlite_mcp_server/                     ← 替代样例:SQLite MCP 服务(零外部依赖)
│   ├── server.py                          ← FastMCP + Python 标准库 sqlite3
│   ├── init_db.py                         ← 一次性建库 + 注入数据
│   ├── start.sh                           ← 一键启动
│   ├── pyproject.toml
│   └── uv.lock
├── _shared_schema/                          ← 共享演示数据 source(DM8 + SQLite 两个后端通用)
│   └── schema_seed.sql                    ← (sqlite 版会自动复用同一份)
└── db_mcp_agent/                          ← 上传到 NAC 的纯 artifact
    ├── nexau.json
    ├── agent.yaml                         ← mcp_servers.type: http
    └── systemprompt.md                    ← schema + SQL 模板内联在这里
```

**两条数据后端任选其一**：Agent artifact 完全一样，`agent.yaml` 里的 `url` 也不用动（两个 server 端口、工具契约都一致）。差异只在：

- **路径 A · DM8**：你已经有达梦库（自有部署 / 客户环境），把 `DM8_CONN_STR` 指向它即可。
- **路径 B · SQLite**：没有达梦时的零依赖路径，本地 SQLite 文件 + 一行 `start.sh` 自动建库注入数据。

关键文件：

| 文件 | 职责 |
|---|---|
| `dm8_mcp_server/server.py` | DM8 MCP 服务（`dmPython` 原生驱动 + `DM8_CONN_STR` 环境变量，跟生产 K8s 部署对接方式一致） |
| `sqlite_mcp_server/server.py` | SQLite MCP 服务（Python 标准库 sqlite3，纯本机进程，无外部 DB） |
| `sqlite_mcp_server/init_db.py` | 读 `_shared_schema/schema_seed.sql` 自动注入到 SQLite |
| `_shared_schema/schema_seed.sql` | 演示数据 source —— SQLite 自动用；DM8 用户**按需手动添加**到自己库（也可以完全不用，改 `systemprompt.md` 描述你的真实业务表） |
| `db_mcp_agent/agent.yaml` | 通过 `mcp_servers` 连接 MCP 服务，两个后端通用 |
| `db_mcp_agent/systemprompt.md` | Agent 行为规则 + 数据库表结构 + 常见 SQL 模板 |

上传到 NAC 的只有 `db_mcp_agent/`。MCP server 作为独立服务部署在 NAC 之外（本地 / 同集群 / 内网服务器都行），通过网络给 Agent 提供 MCP 接口。`_shared_schema/` 只是 SQL 文件，不是运行时组件，不需要部署。

---

## 设计要点 1：MCP server 是数据库边界

和 `custom_tools/execute_sql.py` 直接注册成工具不同，MCP 模式多了一层协议边界：

```text
Agent
  -> NexAU MCP client
    -> external HTTP MCP server
      -> DM8 database
```

这样做的收益是：

- **复用**：同一个 MCP server 可以被多个 Agent 或 MCP 客户端复用。
- **隔离**：数据库凭证、连接池、审计、SQL 白名单都在 server 侧。
- **迁移**：从 DM8 换成 PostgreSQL / MySQL / 数据仓库时，Agent 配置可以基本不动。
- **治理**：高风险操作可以在 MCP server 侧拦截，而不是只靠 prompt 约束。

---

## 设计要点 2：`agent.yaml` 连接外部 HTTP MCP 服务

本样例使用 HTTP transport。NAC Agent 不在沙箱内启动 MCP 代码，只通过 `mcp_servers.url` 连接已经部署好的 MCP 服务，并自动发现工具。

```yaml
mcp_servers:
  - name: enterprise_ops_db
    type: http
    # 改成 MCP server 在目标集群里可达的地址,例如:
    #   同集群:    http://<svc-name>.<namespace>.svc.cluster.local:8000/mcp
    #   内网:      http://10.x.x.x:8000/mcp
    #   公网:      https://mcp.your-domain.com/mcp
    #   本机调试:  http://host.docker.internal:8000/mcp
    url: http://host.docker.internal:8000/mcp
    headers:
      Accept: application/json, text/event-stream
      # 公网部署建议加鉴权,${env.X} 会按 runtime 进程环境变量展开:
      #   Authorization: Bearer ${env.MCP_TOKEN}
      #
      # 本样例 MCP server 启用了 DNS rebinding 防护,要求 Host=localhost,
      # 部署到正规域名/svc 地址后这一行可以删掉:
      Host: localhost:8000
    timeout: 30
```

> ### 🚨 部署前必须把 `url` 改成 NAC 沙箱可达的地址
>
> 默认值 `host.docker.internal:8000` 仅用于**本机 K8s 调试**（NAC agent runtime 容器通过这条特殊 DNS 反向访问宿主机端口）。上到任何真实 K8s 集群这条 DNS 都解析不到，runtime 的 MCP client 连不上 server，错误（`Name or service not known`）会被当作 tool 调用结果回灌给 LLM，最终用户看到 "数据库查询未返回结果" 之类的兜底回答。
>
> | 部署形态 | URL 写法 | 鉴权建议 |
> |---|---|---|
> | MCP server 和 Agent **同 K8s 集群** | `http://<svc-name>.<namespace>.svc.cluster.local:8000/mcp` | 集群内可信，留空 |
> | MCP server 部署在**内网另一台机器** | `http://10.x.x.x:8000/mcp` 或 `http://<intranet-host>:8000/mcp` | 看安全等级，可选 |
> | MCP server 暴露**公网** | `https://mcp.your-domain.com/mcp` | 必加 `Authorization: Bearer ${env.MCP_TOKEN}` |
>
> 💡 **`agent.yaml` 支持 `${env.X}` 插值** —— 在 agent runtime 启动时按当前进程环境变量展开（即 NAC Runtime Vars）。所以 token 别写字面值，配成 Runtime Var（勾上 Secret）然后写 `Bearer ${env.MCP_TOKEN}`，artifact 里不留任何凭证。⚠️ 注意只支持 `${env.NAME}` 这种 namespace 形式（小写 env 加点），裸的 `${ENV}` / `$ENV` 这种 shell-style 不会展开。
>
> 部署前 sanity check：在 agent-runtime pod 里跑一下 `getent hosts <mcp-host>` 和 `curl -m 5 -sI <url>`，确认 DNS 和连接都通，再发布版本。

本样例的 LLM 配置使用 NAC Cloud 注入模型、Base URL 和 API Key，只保留业务侧需要固定的参数：

```yaml
llm_config:
  stream: false
  max_tokens: ${variables.max_tokens}
  api_type: openai_chat_completion
```

这样可以避开部分运行时对 Responses API 流式返回体的兼容问题，也和 NAC 平台注入模型配置的方式保持一致。

在较新的 NexAU 版本中，MCP 工具名称会被加上 server 前缀；不同运行时版本可能直接显示原始工具名，以实际工具列表为准：

```text
mcp__enterprise_ops_db__list_tables
mcp__enterprise_ops_db__describe_table
mcp__enterprise_ops_db__query_sql
mcp__enterprise_ops_db__server_info
```

这和普通 `.tool.yaml` 的区别是：MCP 工具不需要在 `tools:` 里逐个注册，工具 schema 由 MCP server 在初始化时返回。

---

## 设计要点 3：MCP server 工具要少而稳定

本 cookbook 自带的 `dm8-mcp` server 默认暴露少量稳定工具。生产 MCP 服务可以用相同的工具契约连接 DM8、PostgreSQL、MySQL、数据仓库或内部 API。

| MCP 工具 | 作用 |
|---|---|
| `server_info` | 返回后端类型、只读状态和当前 SQL 分页语法 |
| `list_tables` | 返回当前 schema 下所有用户表的**表名列表**(字段和示例值用 `describe_table`) |
| `describe_table` | 返回单表字段、类型、是否可空 |
| `query_sql` | 执行只读 SQL 并返回表格结果 |

不要一开始就暴露太多工具。数据库 Agent 最容易出问题的地方不是“工具不够”，而是工具边界过宽、输出太散、模型不知道该用哪个。

---

## 设计要点 4：只读不是 prompt，必须写在 server 里

生产 MCP server 至少应该做几层防护：

1. 使用数据库只读账号。
2. SQL 只允许 `SELECT` / `WITH` 查询。
3. 拒绝 `INSERT`、`UPDATE`、`DELETE`、`DROP`、`ALTER`、`TRUNCATE` 等关键字。
4. 只允许单条 SQL。
5. 查询返回行数设置上限。

这些限制必须在工具层实现。Prompt 可以提醒模型“不要写入”，但不能作为安全边界。

本 cookbook 自带的 `dm8-mcp` / `sqlite-mcp` 都已在 `query_sql` 里做基础只读校验和最大返回行数限制。生产环境仍应叠加数据库只读账号、网络鉴权和审计日志。

---

## 启动 MCP server（两条数据后端任选）

跟着本节走之前，先想清楚你属于哪一类：

| 当前情况 | 选哪条路 |
|---|---|
| **已经有一套达梦数据库**（自有部署 / 测试集群 / 客户环境） | **路径 A · DM8**：直接把 `DM8_CONN_STR` 指向你的 DM8 |
| **没有达梦，只想跑通 cookbook** | **路径 B · SQLite**：零外部依赖，本地建库 + 注入数据一行命令搞定 |

两个 server 端点完全一致（默认 `http://127.0.0.1:8000/mcp`，需要改端口可设环境变量 `MCP_PORT=8001 bash start.sh -d`），工具契约一致（`server_info` / `query_sql` / `list_tables` / `describe_table`），`db_mcp_agent/agent.yaml` **不用改任何一行**。SQL 方言差异（DM8 用 `FETCH FIRST N ROWS ONLY`，SQLite 用 `LIMIT N`）通过 `server_info.limit_syntax` 明确返回，systemprompt 会要求模型先判断后端再生成 SQL。

#### 演示数据的表结构速览

无论走哪条路径，cookbook 自带的演示数据（`_shared_schema/schema_seed.sql`）都是这 3 张表，列名/单位约定不太直觉，**手工写 SQL 验证时务必看一眼**（agent 上线后由 systemprompt 兜底，模型不会踩坑）：

| 表 | 行数 | 关键列 |
|---|---|---|
| `ENTERPRISES` | 8 | `ENTERPRISE_ID` `NAME` `DISTRICT` `INDUSTRY` `REGISTER_CAPITAL_WAN` `FOUNDED_YEAR` |
| `FINANCIAL_METRICS` | 16 | `ENTERPRISE_ID` `FISCAL_YEAR` `REVENUE_WAN` `PROFIT_WAN` `TAX_WAN` `EMPLOYEE_COUNT` |
| `POLICY_SUPPORT` | 8 | `ENTERPRISE_ID` `FISCAL_YEAR` `PROGRAM` `SUBSIDY_WAN` `STATUS` |

> ⚠️ **金额列名都以 `_WAN` 结尾**（单位：万元），年份列叫 `FISCAL_YEAR` 不是 `YEAR`。完整字段类型 / 业务口径见 [`db_mcp_agent/systemprompt.md`](./db_mcp_agent/systemprompt.md)。

---

### 路径 A · DM8（你已经有达梦数据库）

`dm8_mcp_server/server.py`（~150 行 Python + FastMCP）走 dmPython 原生驱动通过 `DM8_CONN_STR` 连你的达梦库，**和生产环境跑在 K8s 里的部署方式一致**（不假设 server 和 DB 同主机）。完整源码见 [dm8_mcp_server/server.py](./dm8_mcp_server/server.py)。

`start.sh` 是 Linux native 启动（`uv sync` + `uv run python server.py`），跟生产部署完全对齐。

```bash
cd "数据库 MCP 接入/dm8_mcp_server"

# 把 DM8_CONN_STR 指向你自己的达梦实例:
DM8_CONN_STR="SYSDBA/<你的密码>@<你的 DM8 host>:5236" \
  bash start.sh -d
# 端点: http://127.0.0.1:8000/mcp
# 日志: tail -f /tmp/dm8-mcp.log
# 停止: pkill -f 'dm8[-_]mcp.*server.py'

# 端口被占 / 想并行起多份? 设 MCP_PORT 即可:
#   MCP_PORT=8001 DM8_CONN_STR="..." bash start.sh -d
```

> ⏱ **首次启动 ~5-10 秒**（`uv sync` 第一次下载 dmpython wheel + mcp 依赖）。第二次起 <1 秒。
>
> ⚠️ **macOS 用户**：dmpython 只发 Linux wheel，没有 macOS 版，`start.sh` 会**提前 exit 3 报错**并提示你走 SQLite 路径或 Linux 主机。本地教学想看效果直接走路径 B；想测真实 dmPython 链路请 SSH 到 Linux 服务器跑。
>
> ⚠️ 生产 K8s 部署时，server.py / start.sh 不变，只把 `DM8_CONN_STR` 改成集群里能解析的地址，如 `SYSDBA/<pass>@dm8.<ns>.svc.cluster.local:5236`。

#### 可选：往你的 DM8 添加 cookbook 演示数据

如果你想用 cookbook 自带的"企业经营"演示场景验证端到端（2024 营收 Top-5、补贴下滑等问句），cookbook 提供了 `_shared_schema/schema_seed.sql`（3 张教学表 + 数据）。**用你自己 DM8 客户端工具添加到库里即可**：

```bash
# disql 例子(替换主机名/密码,反引号是 disql 的脚本加载语法):
disql SYSDBA/<pass>@<your-dm-host>:5236 `_shared_schema/schema_seed.sql

# 或交互式:
# disql SYSDBA/<pass>@<your-dm-host>:5236
# SQL> `_shared_schema/schema_seed.sql
```

> `schema_seed.sql` 使用 `DROP TABLE IF EXISTS`，可以重复执行。空库首次灌入时不会因为表不存在而中断。
>
> 不同 disql 版本对脚本加载语法有差异（部分版本要求 `start file.sql` 或先 `CONN` 再 ``  ` ``），如果反引号写法报 syntax error，用你熟悉的 DM 客户端工具（dm-manager / dbeaver+JDBC / 公司内部脚手架）灌一次也行 —— 这只是教学种子数据，不是热路径。

如果你**本身就有业务表**（不想用 demo 数据），那就跳过 schema_seed，但记得把 `db_mcp_agent/systemprompt.md` 里的"数据库表结构"段改成你的真实表结构 + 业务口径，否则模型不知道你的库长啥样。

---

### 路径 B · SQLite（零外部依赖）

`sqlite_mcp_server/` 是为没有达梦实例的读者准备的：本地建一个 SQLite 文件，把 cookbook 的 schema + demo 数据注入进去，启 MCP server。**完全跑在宿主机 Python 里，不需要任何外部数据库或额外运行时。**

```bash
cd "数据库 MCP 接入/sqlite_mcp_server"
bash start.sh -d              # 首次会自动 uv sync + init_db.py + 后台启
# 端点同样 http://127.0.0.1:8000/mcp (改端口: MCP_PORT=8002 bash start.sh -d)
# 日志: /tmp/sqlite-mcp.log
# 停止: pkill -f 'sqlite[-_]mcp.*server.py'
```

`start.sh` 检测到 `.venv/` 不在就先跑 `uv sync`，再发现 SQLite 文件不存在就跑 `init_db.py`（读 `../_shared_schema/schema_seed.sql` 注入数据），最后启 server —— 一行命令端到端。

> ⏱ **首次启动 ~3-5 秒**（`uv sync` + `init_db.py`），比路径 A 的 `dmpython` 装包更快。后续启动 <1 秒。

> 路径 B 主要用于教学/演示。生产场景如果你已经有真实达梦库，请走路径 A，把凭证和访问控制收在 server 一侧。

---

### 共用工具契约

不管走哪条路径，MCP server 端点都是 `http://127.0.0.1:8000/mcp`，暴露四个工具：

| 工具 | 用途 |
|---|---|
| `server_info()` | 返回后端类型、只读状态、分页语法和默认行数上限 |
| `query_sql(sql, max_rows?)` | 执行单条只读 SQL（DM8 / SQLite 均只允许查询；DM8 默认最多返回 200 行，硬上限 1000；SQLite 最多返回 200 行） |
| `list_tables()` | 列当前 schema 用户表 |
| `describe_table(table_name)` | 看列、类型、是否可空 |

> `dm8_mcp_server/server.py` 和 `sqlite_mcp_server/server.py` 都实现了应用层只读校验：只允许 `SELECT` / `WITH` 查询，拒绝 `INSERT/UPDATE/DELETE/DROP/...` 等关键字，并限制单次返回行数。SQLite 还使用驱动层 `mode=ro` 兜底；DM8 生产环境应继续使用数据库只读账号作为最终防线。

如果你不用本 cookbook 自带的 server，**任何符合 MCP `streamable-http` / `http_sse` 协议、暴露兼容工具集的服务端都能替换**。`agent.yaml` 里只改 `mcp_servers[*].url` 即可。

### 验证 MCP server 起来了

```bash
curl -sI -m 5 http://127.0.0.1:8000/mcp
# 期望: HTTP/1.1 405 (HEAD 不允许,但 server 活着) 或 200
# 看到: Could not resolve / connection refused → server 没启,回到「启动 MCP server」节重跑 start.sh
```

想跑真实 SQL 查询验证数据库通路,直接进 Playground 问 *"2024 年营收前 5 的企业是谁?"* 即可 — 那就是最自然的端到端测试。

---

## 部署到 NAC

### 本机调试

`agent.yaml` 保持默认即可（`url: http://host.docker.internal:8000/mcp` 是本机 K8s 调试用的反向 DNS，NAC agent runtime 容器通过它访问宿主机的 MCP server 端口）。

### 部署到生产 / 测试集群 (必读)

线上 K8s 解析不到 `host.docker.internal`,必须在打 zip 之前修改 `db_mcp_agent/agent.yaml`:

```yaml
mcp_servers:
  - name: enterprise_ops_db
    type: http
    # 改成 MCP server 在目标集群里可达的地址,例如:
    url: https://mcp.your-domain.com/mcp
    headers:
      Accept: application/json, text/event-stream
      # 公网部署建议加鉴权,${env.X} 会按 runtime 进程环境变量展开(对应 NAC Runtime Vars):
      # Authorization: Bearer ${env.MCP_TOKEN}
    timeout: 30
```

部署前自检 — 一条 `curl` 就够:

```bash
curl -sI -m 5 <your-mcp-url>
# 期望: HTTP/1.1 405 或 200 — MCP server 活着
# 看到: 超时 / Could not resolve host  — 改 url 或开网络白名单
```

> 如果 URL 是 cluster-internal (`*.svc.cluster.local`),你本地 curl 不到很正常 —— 让运维 `kubectl exec` 进任一 pod 测,或者用 `kubectl port-forward` 把 svc 暂时映射到本地再 curl。

### 打包 + 上传

只打包含 `nexau.json` 的 Agent 目录，不要把 MCP server 代码或数据库文件打进 NAC 制品包：

```bash
cd "数据库 MCP 接入/db_mcp_agent"
zip -r ../database_mcp_agent.zip . -x "*.DS_Store" "*__pycache__*" "*.env"
# 产物在父目录:数据库 MCP 接入/database_mcp_agent.zip
```

上传到 North Agent Cloud 后,在 Playground 里可以测试：

- "先列出可用的数据表"
- "2024 年营收最高的 5 家企业是谁？"
- "按区县统计 2024 年纳税额"
- "获得补贴但营收同比下降的企业有哪些？"
- "专精特新企业 2024 年平均利润率是多少？"

---

## 迁移到真实数据库

生产环境不要让 NAC Agent 制品包启动 MCP server，也不要把数据库文件打进 Agent 包。应将 MCP server 作为独立服务部署，并在服务端连接真实数据库：

- DM8：使用真实业务库地址、只读账号和白名单 schema
- PostgreSQL / MySQL：同理读取连接参数
- 数据仓库：封装成只读查询账号，并限制 schema

迁移时保持 MCP 工具契约稳定：

```text
list_tables
describe_table
query_sql
server_info
```

Agent 侧只需要把 `mcp_servers.url` 改成实际服务地址；如果生产服务需要鉴权，在 `headers` 中加入 `Authorization` 等认证头。

---

## 自查清单

- [ ] 外部 MCP server 是否能独立启动并 `curl http://<url>` 返回 405/200？
- [ ] NAC 沙箱是否能访问 `mcp_servers.url`？(本机调试用 `host.docker.internal`,云上 K8s 用集群内 service DNS)
- [ ] `Host` / `Accept` / 鉴权 headers 是否配置正确？
- [ ] 数据库连接是否只读？
- [ ] SQL 校验是否在 server 侧执行？
- [ ] SQL 查询工具是否限制最大返回行数？
- [ ] System Prompt 是否写清楚表结构、单位、join key 和示例 SQL?
- [ ] System Prompt 是否要求 Agent 不得凭空回答?

---

## 下一步

如果你的场景只是单个 Agent 调数据库，直接看 [企业数据库问数](../企业数据库问数/tutorial-text-to-sql.md) 就够了。

如果你要把数据库能力沉淀成可复用服务，或未来要给多个 Agent / 多个客户端接入同一套数据库工具，就优先使用本篇的 MCP 模式。
