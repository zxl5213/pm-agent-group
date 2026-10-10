# AI项目经理 Agent — 建群归档版

基于飞书开放平台的项目管理机器人，覆盖项目群全生命周期管理。

## 功能概览

### F-01 建群

单入口 `/建群`，两条路径灵活适配不同场景：

**快速建群**：填写项目名+简称 → 直接建群，PM 和机器人即时入群。

**收集成员**：PM 建群后，按部门搜索负责人 → 向负责人发送成员收集卡 → 负责人提交成员 → 群内 PM 确认卡（认可/拒绝，30分钟超时自动认可）→ 拉入成员 → 实时更新进度看板。

核心交互特性：
- 负责人搜索：输入框实时匹配（姓名·部门·职务），选中即上榜，无需回车
- 成员确认卡：PM 认可后移除按钮变只读，拒绝后恢复可编辑
- 飞书任务集成：收集卡发送时自动为负责人创建任务（T+24h 截止，提前1小时提醒），提交成员后自动标记完成
- 逾期推送：每日扫描未提交的负责人，机器人文本提醒

### F-02 退群归档

项目结束时，两种归档模式：

- **解散群聊**：直接解散，群聊不可恢复
- **保留群聊**：群名改为 `[已归档]` 前缀，机器人退出，群聊保留可查看历史记录

归档流程：通知 → 记录索引 → 解散/退群 → 更新状态，全程卡片可视化（进行中→完成/失败），聊天记录获取失败不阻塞流程。

## 技术栈

- **Python 3.11+** / Flask（Webhook 监听 8080）
- **lark-oapi SDK**（WebSocket 长连接 + 事件分发）
- **飞书多维表格**（项目配置、成员管理、归档日志）
- **飞书卡片**（v1 普通卡 + v2 表单卡，支持 form_value 回调）

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/zxl5213/pm-agent-group.git
cd pm-agent-group

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置
cp config/settings.example.yaml config/settings.yaml
# 编辑 settings.yaml，填入你的飞书应用 app_id / app_secret / 多维表格 ID

# 4. 设置通讯录搜索所需的 user token（可选，仅收集成员路径需要）
$env:LARKSUITE_CLI_USER_ACCESS_TOKEN = "u-em.xxxxx"

# 5. 启动
python main.py
```

启动后在飞书中与机器人私聊，发送 `/建群` 开始。

## 飞书应用配置

1. 在[飞书开放平台](https://open.feishu.cn)创建企业自建应用，获取 App ID 和 App Secret
2. 开通权限：`bitable:app`、`base:record:*`、`contact:user:search`、`im:chat`、`im:message`、`bot`
3. 应用可用范围设为「全员可用」
4. 订阅事件：`im.message.receive_v1`、`card.action.trigger`、`im.chat.member.user.added`
5. 多维表格在「分享」中将应用设为**可编辑**

## 项目结构

```
pm-agent-group/
├── main.py                      # 入口：Flask + WebSocket 双线程
├── config/
│   ├── settings.yaml            # 配置（本地，不入库）
│   └── settings.example.yaml    # 配置模板
├── src/
│   ├── feishu/
│   │   ├── client.py            # 认证/请求/重试/编码
│   │   ├── im_client.py         # 消息发送 + 通讯录搜索
│   │   ├── chat_client.py       # 群聊管理（建群/入群/退群/解散）
│   │   ├── base_client.py       # 多维表格读写
│   │   ├── card_builder.py      # 卡片构建（v1 + v2）
│   │   └── ws_client.py         # WebSocket 长连接 + 事件分发
│   ├── modules/
│   │   ├── group_manager.py     # 群聊生命周期（建群/解散/归档）
│   │   ├── command_handler.py   # 文本指令处理
│   │   ├── card_action_handler.py # 卡片回调（核心业务逻辑）
│   │   ├── session_manager.py   # 用户会话状态
│   │   ├── state_engine.py      # 项目状态流转
│   │   ├── scheduler.py         # 超时定时器
│   │   └── overdue_pusher.py    # 逾期推送
│   └── models/
│       ├── project.py           # 数据模型
│       └── exceptions.py        # 异常定义
└── tests/                       # 测试脚本
```

## 关键设计决策

- **认证分流**：消息发送/建群/多维表格用 `tenant_access_token`（机器人身份），通讯录搜索必须 `user_access_token`
- **卡片回调异步**：飞书要求 3 秒内响应，业务逻辑在后台线程执行，立即返回 toast
- **PM 自动入群**：建群时 PM 的 open_id 自动插入成员列表头部
- **状态机驱动**：项目状态流转 `待建群 → 收集中 → 建群中 → 活跃 → 归档中 → 已归档`

## 关于拆分

本仓库从 [pm-agent](https://github.com/zxl5213/pm-agent) 主仓库拆分而来，仅包含 F-01 建群和 F-02 退群归档功能。F-03 信息提取（会议纪要/决策/待办/风险/进度提取）在主仓库中开发。

## License

MIT
