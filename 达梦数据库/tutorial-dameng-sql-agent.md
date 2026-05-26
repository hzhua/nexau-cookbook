# 教程：达梦数据库 — custom_tools + dmPython 一个 zip 自包含

> Level 2 · 进阶 ｜ 约 45 分钟 ｜ `custom_tools` + dmPython 原生驱动 + airgap whl

把数据库访问做成 NexAU `custom_tools` 直连达梦：agent runtime 进程持有 dmPython，**一个 zip 自包含，无外挂服务**。同样是企业经营问数场景，跟前一篇 [数据库 MCP 接入](../数据库%20MCP%20接入/tutorial-database-mcp.md) 是同一个目标的两种实现路径（详见下方对比表）。

中途用到一个通用技巧：**`nexau.json` 的 `setup` 数组在 agent runtime 启动时离线装额外依赖**（airgap 场景，runtime pod 通常没出公网权限）。这个技巧对 clickhouse-driver / mysqlclient / 内网 SDK 等所有 NAC 默认不带的库都通用 —— dmPython 只是个完整案例。

---

## 前置条件

| 工具 / 资源 | 要求 | 验证 |
|---|---|---|
| Python | ≥ 3.8（仅 `download-whls.sh` 用） | `python3 --version` |
| 达梦数据库实例 | 一个可达的 DM8 + 一个能 SELECT 的账号（连接串后续填 NAC Runtime Var） | 用任意 DM 客户端工具 ping 通 |
| NAC 账号 + 项目 | 已注册 [North Agent Cloud](https://nac.xiaobei.top) 并建了项目 | 浏览器登录 → 项目列表能看到 |
| LLM 凭证（OpenAI 兼容） | model id、base url、api key 三件套 | `curl -H "Authorization: Bearer $key" $base/models` |

> **没有达梦数据库？** 本篇 cookbook 的运行时验证依赖真实 DM8（dmPython 原生连接），没有的话先看 cookbook 4️⃣「数据库 MCP 接入」的"路径 B · SQLite"做完整理解，再回来读本篇看 dmPython + airgap whl 这套技术细节。

---

## 场景与目标

延续 [数据库 MCP 接入](../数据库%20MCP%20接入/tutorial-database-mcp.md) 的场景：

- 业务方有企业经营 DM8 数据库（主表 `ENTERPRISES` / 年度指标 `FINANCIAL_METRICS` / 政策扶持 `POLICY_SUPPORT`）。
- 用户自然语言提问：「2024 年营收最高 5 家企业是谁」「各区县纳税额排名」「补贴拿了但营收下滑的企业」…
- Agent 要把自然语言翻成 DM8 SQL，执行，整理结果。

**和 MCP 版的核心区别**：

| | custom_tools 直连（本篇） | MCP 接入（上一篇） |
|---|---|---|
| 连接路径 | Agent runtime → dmPython → DM8 | Agent runtime → MCP client → HTTP MCP server → DM8 |
| 部署粒度 | **一个 zip 自包含**，无外挂服务 | Agent zip + 独立 MCP server，两套生命周期 |
| 数据库凭证位置 | 在 Agent 的 Runtime Var 里 | 在 MCP server 配置里，Agent 看不到 |
| 网络拓扑 | Agent runtime pod 要能直连 DB | Agent runtime pod 只要能到 MCP server |
| 适合 | 团队自己拥有数据库 + Agent，部署链路想极简 | 数据库托管在另一个团队 / 跨边界审计 / 多个 Agent 共用同一套 SQL 白名单 |

---

## 项目结构

```
达梦数据库/
└── 达梦数据库/                    ← 要上传的 NAC artifact(zip 后名字一致)
    ├── agent.yaml                    Agent 主配置
    ├── nexau.json                    项目清单 + setup 命令
    ├── README.md                     一句话说明
    ├── custom_tools/
    │   ├── __init__.py
    │   └── dameng_sql.py             dmPython 调用 + SQL 校验 + 加密 lib workaround
    ├── tools/
    │   └── run_dameng_sql.tool.yaml  工具 schema(模型可见)
    └── dmpython-airgap/              离线安装包目录
        ├── download-whls.sh          拉 whl 的脚本(whl 不入库)
        ├── README.md
        └── *.whl                     运行 download-whls.sh 后产生
```

`dmpython-airgap/*.whl` 单文件 ~11 MB × 2，体量大不入库，跟着仓库走的是 `download-whls.sh` —— clone 仓库后跑一次脚本就能补齐。

---

## 设计要点 1：dmPython 是 native 驱动，必须用 airgap whl 离线装

dmPython 不是纯 Python 包，是 Cython 编译出来的 native binding，PyPI 只有 Linux x86_64 / aarch64 manylinux 两种平台 wheel（**没有 macOS 版**）。这给两个工程约束：

1. **Agent runtime 内必须有 dmPython** —— 不能在沙盒里跑（沙盒是另一套进程），custom_tools 是 agent runtime 自身的代码。
2. **NAC 部署是 airgap 的** —— pod 启动后没出公网权限，pip 没法连 PyPI，必须把 whl 打进 zip 里走 `--no-index --find-links`。

所以打包前要先把两份 wheel 拉到 `dmpython-airgap/`：

```bash
cd "达梦数据库/达梦数据库/dmpython-airgap"
bash download-whls.sh
# 产物:
#   dmpython-2.5.32-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
#   dmpython-2.5.32-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.whl
```

`nexau.json` 的 setup 命令会按 pod 架构自动挑：

```json
"setup": [
  "/usr/local/bin/uv pip install --python /opt/venv/bin/python --no-index --find-links /agent/dmpython-airgap dmpython==2.5.32"
]
```

这里的 `/agent/dmpython-airgap` 是 **NAC runtime 中的平台 artifact root 路径**：上传 zip 后，包含 `nexau.json` 的这一层会作为 artifact root 出现在 `/agent` 下，所以本地目录 `达梦数据库/达梦数据库/dmpython-airgap/` 在 runtime 里对应 `/agent/dmpython-airgap/`。不要把它理解成本地机器上的绝对路径。

生产 NAC（x86_64）和 ARM Linux（aarch64）共用同一份 zip，零分叉。

---

## 设计要点 2：连接串走 Runtime Var，不暴露给模型

`agent.yaml` 用 `${env.XXX}` 占位符把连接串注入到 tool 的 `extra_kwargs`：

```yaml
tools:
  - name: run_dameng_sql
    yaml_path: ./tools/run_dameng_sql.tool.yaml
    binding: custom_tools.dameng_sql:run_dameng_sql
    extra_kwargs:
      db_conn_str: ${env.DAMENG_DB_CONN_STR}
```

NAC 在 agent runtime 启动时把 `${env.DAMENG_DB_CONN_STR}` 替换成真实值，**只在 Python 进程内可见**。模型那边看到的 `run_dameng_sql` 工具 schema 只有 `sql` / `max_rows` / `timeout` 三个参数，连接串完全不在 prompt / 对话历史里出现：

```yaml
# tools/run_dameng_sql.tool.yaml
parameters:
  type: object
  properties:
    sql:        { type: string, description: "Read-only Dameng SQL query." }
    max_rows:   { type: integer, default: 20, minimum: 1, maximum: 1000 }
    timeout:    { type: integer, default: 30, minimum: 1, maximum: 300 }
  required: [sql]
```

这是 custom_tools 模式比直接 prompt 注入更稳的关键 —— **凭证从来不进上下文**。

---

## 设计要点 3：SQL 只读校验在 tool 里硬卡

`dameng_sql.py` 在执行 SQL 前做三层过滤，全部失败就直接拒绝调用：

```python
_DANGEROUS_KEYWORDS = (
    "DROP", "TRUNCATE", "DELETE", "ALTER", "CREATE",
    "INSERT", "UPDATE", "REPLACE", "MERGE",
    "CALL", "EXEC", "EXECUTE", "GRANT", "REVOKE",
    "COMMIT", "ROLLBACK",
)
```

| 层 | 规则 |
|---|---|
| 1 | SQL 必须以 `SELECT` 或 `WITH` 开头（**剥注释后**判断，防止 `/* */ DROP TABLE` 绕过） |
| 2 | 单语句限制 —— 不允许多个 `;` 分号串接 |
| 3 | 关键字黑名单（剥注释和字符串字面值后） |

剥注释 + 剥字符串 + 大写化在 `_validation_view()` 里完成，避免 `'DROP'` 字符串字面值或 `-- INSERT` 注释被误杀。

> 即使在 prompt 里写「只允许 SELECT」，模型也可能在工具调用里塞 DDL。所以**安全必须写代码**，不能只靠 prompt。

---

## 设计要点 4：dmPython `[CODE:-70089]` 加密模块加载失败的运行时修法

dmPython 2.5.x connect 时大概率报：

```
dmPython.DatabaseError: [CODE:-70089] Encryption module failed to load
```

社区帖（[1](https://eco.dameng.com/community/question/ec52c8c5b36d5445db1ed8399728fb97)、[2](https://eco.dameng.com/community/question/1e6c4271e7617f05b15e4e6d2b2e83eb)）给的 `LD_LIBRARY_PATH=site-packages/dmssl` 修法**在进程启动后改无效**（glibc 启动时已缓存搜索路径）。

`LD_DEBUG=files,libs` 抓出来的真因：dmPython connect 时 dlopen 搜 `site-packages/dmpython.libs/` 但**不搜 `site-packages/dmssl/`**，所以 dmssl/ 里的 `libssl.so` / `libcrypto.so` 找不到。

**修法**：在 `dmpython.libs/` 下造软链指向 `dmssl/` 的同名文件，文件系统层面解决。

代码在 `custom_tools/dameng_sql.py` 的 `_preload_dmssl_libs()`：

```python
link_specs = [
    ("libssl.so",    "libssl.so"),
    ("libssl.so",    "libssl-3.so"),    # libdmdpi 还会找带 -3 后缀的
    ("libcrypto.so", "libcrypto.so"),
    ("libcrypto.so", "libcrypto-3.so"),
]
for src_name, link_name in link_specs:
    src = os.path.join(dmssl_dir, src_name)
    link = os.path.join(libs_dir, link_name)
    if not os.path.exists(src) or os.path.exists(link):
        continue
    with contextlib.suppress(OSError):
        os.symlink(src, link)
```

特点：
- 在 `run_dameng_sql` 第一次被调时跑（不在 import 阶段，避免 agent 启动慢）
- 幂等：模块级 `_DMSSL_LOADED` 标志位 + `os.path.exists(link)` 双重保险
- 静默 OSError：如果 dmpython.libs/ 只读，软链建不上时跳过；运行环境如果跑得通的话不会管
- 跨架构通用：ARM64 和 x86_64 wheel 都有相同的 dmssl/ 布局

部署后想验证修法是否生效，进 agent runtime pod 看：

```bash
ls -la /opt/venv/lib/python3.12/site-packages/dmpython.libs/libssl*
# 应该看到 libssl.so 和 libssl-3.so 都是软链
```

---

## 准备 DM8 数据

两个 cookbook 共用 `数据库 MCP 接入/_shared_schema/schema_seed.sql` 这份种子（3 张教学表 + 数据）。怎么往你的 DM8 灌进去，见 [cookbook 4️⃣ · 可选：往你的 DM8 添加 cookbook 演示数据](../数据库%20MCP%20接入/tutorial-database-mcp.md#可选往你的-dm8-添加-cookbook-演示数据)。

如果你已经有自己的业务表（不用 demo 数据），那就跳过这一步，把 `达梦数据库/达梦数据库/agent.yaml` 里的 `system_prompt` 改成你的真实表结构和业务口径，或者新增 Skill 承载更完整的表说明。

---

## 打包 + 部署到 NAC

### 第 1 步：拉 whl + 打 zip

```bash
cd "达梦数据库/达梦数据库/dmpython-airgap"
bash download-whls.sh                  # 拉两份 whl

cd ..                                  # 回到 达梦数据库/(内层)
zip -rq ../达梦数据库.zip . \
  -x '*__pycache__*' -x '*.DS_Store'
ls -lh ../达梦数据库.zip            # 约 19 MB
```

### 第 2 步：上传 + 配 Runtime Var

1. Playground → 新建项目（或选已有的）→ Artifact Editor → 上传 `达梦数据库.zip`
2. 进 Config / 配置 → **Runtime Vars** → 加 4 条：

| Key | Value | Secret? |
|---|---|---|
| `DAMENG_DB_CONN_STR` | `SYSDBA/<你的密码>@<你的 DM8 host>:5236` | ☑️ |
| `LLM_MODEL` | （你的模型 ID） | ☐ |
| `LLM_BASE_URL` | （OpenAI 兼容 endpoint） | ☐ |
| `LLM_API_KEY` | （对应 key） | ☑️ |

> ⚠️ DM8 地址必须填 **NAC 沙箱可达的地址**（同集群 K8s Service / 内网 DNS / 公网 IP 等），见下一章 "迁移到真实数据库"。

### 第 3 步：Deploy + 试问

回到 Deployments → 点 Deploy → 等几十秒。进 Playground 问：

```
2024 年营收最高的 5 家企业是哪些？
```

预期看到 `run_dameng_sql` 被调用，返回 Demo_Green_Materials / Demo_Rail_Control / Demo_Mfg_Alpha / Demo_Cloud_Intelligence / Demo_Starlink_Sensor 排名。

---

## 迁移到真实数据库

部署到生产时改 4 处：

1. **`DAMENG_DB_CONN_STR`** → 改成生产 DM8 地址
   - 同集群 K8s Service：`SYSDBA/<密码>@dm8-svc.<ns>.svc.cluster.local:5236`
   - 公司内网 DNS：`SYSDBA/<密码>@dm.intra.example.com:5236`
   - 直接 IP：`SYSDBA/<密码>@10.0.x.x:5236`

2. **替表名 / 库名** —— `custom_tools/dameng_sql.py` 不需要改，但 `agent.yaml` 里的 `system_prompt` 或者通过 Skill 让模型知道你真实的表结构。

3. **生产用专用账号** —— 不要用 SYSDBA，建个只读账号，DM 端 `GRANT SELECT ON ... TO readonly_user`。

4. **审计**：DM8 自身有审计日志，开启后能看到所有 SELECT 的来源、用户、SQL 文本，配合 NAC 的 Agent 调用日志做交叉对账。

---

## 自查清单

- [ ] `dmpython-airgap/` 下两份 whl 都在（x86_64 + aarch64）
- [ ] `nexau.json` 的 setup 命令没改过，写的是 `--no-index --find-links /agent/dmpython-airgap`（`/agent` 是 NAC artifact root）
- [ ] zip 里没多余 `__pycache__/` / `.DS_Store` / `归档.zip`
- [ ] NAC Runtime Vars 4 个全配，`DAMENG_DB_CONN_STR` / `LLM_API_KEY` 勾了 Secret
- [ ] Deploy 后看 agent-runtime pod 日志，应该看到 setup 命令成功 + LLM 调用正常
- [ ] 进 Playground 调用一次工具，看返回了结构化 `data` 而不是 `[CODE:-70089]`
- [ ] 如果遇到 -70089，进 pod 验证 `dmpython.libs/` 下有没有 `libssl*` / `libcrypto*` 软链（`_preload_dmssl_libs()` 是否生效）

---

## 下一步

| 你想做 | 看这里 |
|---|---|
| 把这个 agent 改成接 PostgreSQL / MySQL | 改 `custom_tools/dameng_sql.py` 的驱动从 dmPython 换成 psycopg2 / pymysql，连接串结构基本不变 |
| 想给数据库工具加权限边界 / 集中审计 | 切到 [数据库 MCP 接入](../数据库%20MCP%20接入/tutorial-database-mcp.md) 模式 |
| 想让模型「更懂业务」（表语义、口径） | 看 [企业数据库问数](../企业数据库问数/tutorial-text-to-sql.md) 里 SKILL.md 怎么写 |
| 想做多工具协作（SQL 工具 + 图表工具 + 报告生成） | 参考 [金融数据智能体](../金融数据智能体/tutorial-finance-agent.md) 的多工具组合模式 |
