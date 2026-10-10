# PM Agent — AI 协作指南 (Agents.md)

> 本文档为 AI 编码代理提供项目上下文、约定和操作规范，确保协作一致性。
> 最后更新：2026-10-10

---

## 一、项目概述

**项目名称**：AI项目经理 Agent (PM Agent) — 建群归档版

**目标**：基于飞书开放平台的项目管理机器人，覆盖项目群生命周期管理：
- **F-01 建群**：单入口 `/建群`，支持快速建群 / 收集成员两条路径
- **F-02 退群归档**：项目结束时优雅归档（解散群聊 / 保留群聊两种模式）

> 本仓库为 F-01/F-02 独立项目，从主仓库（pm-agent）拆分而来；F-03 信息提取功能在主仓库中开发。

**技术栈**：Python 3.11+ / Flask / lark-oapi SDK / 飞书多维表格

---

## 二、应用与环境信息

| 项目 | 值 |
|------|------|
| 飞书应用名称 | AI项目经理 |
| App ID | `cli_aaf7a4a6e5f99cb0` |
| App Secret | 见 `config/settings.yaml` |
| 测试账号（张晓丽）open_id | `ou_855b3d803ce05cd265ac285263e86996` |
| API Base | `https://open.feishu.cn/open-apis` |
| Python 环境 | `C:\ProgramData\miniconda3\envs\agent\python.exe` |
| 启动命令 | `python main.py`（Flask 监听 8080 + WebSocket 长连接） |

### 权限要求
- `bitable:app` — 多维表格读写
- `base:record:create / retrieve / update` — 记录操作
- `contact:user:search` — 通讯录搜索（需 **user_access_token**）
- `im:chat / im:message / bot` — 消息与群聊

> 多维表格必须在飞书「分享」中将「AI项目经理」应用设为**可编辑**，否则写入返回 91403。

---

## 三、目录结构

```
pm-agent-group/
├── main.py                      # 入口：Flask + WebSocket 双线程
├── config/
│   └── settings.yaml            # 配置（app_id/secret/bitable表ID/超时）
├── src/
│   ├── feishu/
│   │   ├── client.py            # FeishuClient 基类（认证/请求/重试/编码）
│   │   ├── im_client.py         # 消息发送 + 通讯录搜索 + 部门查询
│   │   ├── chat_client.py       # 群聊管理（建群/入群/退群/解散）
│   │   ├── base_client.py       # 多维表格读写
│   │   ├── card_builder.py      # 卡片构建（v1普通卡 + v2表单卡）
│   │   └── ws_client.py         # WebSocket 长连接 + 事件分发
│   ├── modules/
│   │   ├── group_manager.py     # 群聊生命周期（注册/建群/解散）
│   │   ├── command_handler.py   # 文本指令处理（/建群 等）
│   │   ├── card_action_handler.py # 卡片按钮回调（核心业务逻辑）
│   │   ├── session_manager.py   # 用户会话状态跟踪
│   │   ├── state_engine.py      # 项目状态流转
│   │   └── scheduler.py         # 超时定时器（成员确认自动认可）
│   ├── models/
│   │   ├── project.py           # 数据模型
│   │   └── exceptions.py        # 异常定义
│   └── utils/
│       └── logger.py            # 日志配置
└── tests/                       # 测试脚本
```

---

## 四、核心流程

### F-01 建群流程（单入口 `/建群`）

```
PM 发送 /建群
  ↓
卡片1：项目信息表单（项目名 + 简称）
  ↓ 提交
  ├── 「快速建群」→ 直接建群（PM+Agent入群）
  └── 「收集成员」→
       ├── 立即建群（PM+Agent入群）
       ├── 搜索负责人 → 发送收集卡
       ├── 群内进度看板（实时更新）
       ├── 负责人提交成员 → 群内确认卡
       ├── PM认可 / 拒绝（30分钟超时自动认可）
       └── 拉入成员 → 更新进度
```

### 关键设计决策

1. **认证方式**
   - 消息发送 / 建群 / 多维表格：`tenant_access_token`（机器人身份）
   - 通讯录搜索 `/contact/v3/users/search`：**必须** `user_access_token`
   - 用户详情 / 部门查询：`tenant_access_token` 即可

2. **卡片回调超时**
   - 飞书要求回调 3 秒内响应
   - `_on_card_action` 中用 `threading.Thread` 异步执行业务逻辑
   - 立即返回 `{"toast": {"content": "处理中..."}}` 响应

3. **open_id 跨应用不互通**
   - 测试账号张晓丽在「AI项目经理」应用下的 open_id = `ou_855b3d803ce05cd265ac285263e86996`
   - lark-cli 应用的 open_id 不能直接用于本应用

4. **PM 自动入群**
   - `create_project_group()` 中 PM（指令发起人）的 open_id 自动插入成员列表头部

---

## 五、编码约定

### 5.1 Token 管理
- `FeishuClient._get_token()` 自动检测 user token 是否有效（ASCII 检查），无效则 fallback 到 tenant token
- `_cached_user_token` 在 `__init__` 中一次性检测并缓存，避免每次请求重复检测
- tenant token 刷新加锁 + double-check，避免并发重复刷新

### 5.2 HTTP 请求编码
- JSON body 必须用 `json.dumps(...).encode('utf-8')` 通过 `data=` 参数发送
- **不要**直接用 `json=` 参数（Windows 下 latin-1 编码会报错）

### 5.3 多维表格
- 过滤查询用 `POST /records/search`，不能用 GET+body
- 文本字段返回富文本数组 `[{"text":"xxx"}]`，用 `_extract_text()` 提取

### 5.4 飞书卡片格式
- **v1**：`{"config":..., "header":..., "elements":[...]}`，支持 action/note
- **v2**：`{"schema":"2.0", "config":..., "header":..., "body":{"elements":[...]}}`，支持 form/input
- v2 表单提交按钮需 `form_action_type="submit"`，提交数据在 `action.form_value`
- v2 卡片中 form 外的普通按钮**不携带**表单值

---

## 六、Git 协作规范

### 6.1 提交要求
- **每次改动完毕必须创建对应的 git commit**，方便追踪和回滚
- Commit 消息格式：`<type>(<scope>): <description>`
  - type: `feat` / `fix` / `refactor` / `docs` / `test` / `chore`
  - 示例：`feat(card): 新增成员确认卡` / `fix(client): 修复latin-1编码错误`

### 6.2 测试要求
- **每次改动后必须编写/更新相关测试**
- 交付前确保所有测试通过：`python tests/<test_file>.py`
- 核心测试：`tests/test_e2e_create_group.py`（建群流程端到端）

### 6.3 分支策略
- `main`：稳定分支，仅合并已验证的代码
- 功能开发在新分支进行，测试通过后合并

---

## 七、环境变量

| 变量 | 用途 |
|------|------|
| `LARKSUITE_CLI_USER_ACCESS_TOKEN` | 通讯录搜索 API 所需的 user token（2小时过期） |

> user token 通过飞书 OAuth 授权获取：`https://open.feishu.cn/open-apis/authen/v1/index?app_id=cli_aaf7a4a6e5f99cb0&redirect_uri=https://open.feishu.cn&state=pm_agent`

---

## 八、已知问题与注意事项

1. **user token 过期**：有效期 2 小时，过期后通讯录搜索会报 99991663，需重新授权
2. **根部门权限**：部门 ID "0"（根部门）无查看权限，部门名称可能为空
3. **回调超时**：所有卡片回调必须异步处理，立即返回 toast 响应
4. **临时文件**：`exchange_token.py`、`test_*.py`（根目录下的）为临时脚本，已加入 .gitignore

---

## 九、快速启动

```bash
# 1. 设置 user token（如需要通讯录搜索功能）
$env:LARKSUITE_CLI_USER_ACCESS_TOKEN = "u-em.xxxxx"

# 2. 启动服务
python main.py

# 3. 在飞书中与「AI项目经理」机器人私聊，发送 /建群
```
