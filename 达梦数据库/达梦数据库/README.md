# 达梦数据库 · agent 源码

NexAU `custom_tools` + dmPython 直连达梦的样例 agent。**详细教程见上一级目录的 `tutorial-dameng-sql-agent.md`**。

## 文件

| 文件 | 作用 |
|---|---|
| `agent.yaml` | Agent 配置，绑定 `run_dameng_sql` |
| `nexau.json` | 项目清单 + `setup` 离线装 dmPython |
| `tools/run_dameng_sql.tool.yaml` | 工具 schema（模型可见） |
| `custom_tools/dameng_sql.py` | dmPython 调用 + SQL 校验 + `[CODE:-70089]` workaround |
| `dmpython-airgap/` | 离线 wheel 目录（whl 需手动跑 `download-whls.sh` 拉） |

## setup 路径说明

`nexau.json` 里的 `/agent/dmpython-airgap` 是 NAC runtime 中的平台 artifact root 下路径。也就是说，本地这个目录：

```text
达梦数据库/达梦数据库/dmpython-airgap/
```

打包上传后会在 runtime 里对应：

```text
/agent/dmpython-airgap/
```

它不是本地机器上的绝对路径。

## 所需 Runtime Vars

`DAMENG_DB_CONN_STR` / `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` —— 详见 tutorial 第 2 步。
