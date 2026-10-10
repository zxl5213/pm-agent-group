import hashlib
import json


class CardBuilder:
    """飞书交互卡片构建器 - 按原型设计生成卡片

    v1格式: 用于普通卡片(带action按钮)
    v2格式: 用于表单卡片(带input输入框)
    """

    def _card_v1(self, title, template, elements):
        """v1卡片 - 支持 action/note/div/hr"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "elements": elements,
        }

    def _card_v2(self, title, template, elements):
        """v2卡片 - 支持 form/input，不支持 action/note"""
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": template,
            },
            "body": {"elements": elements},
        }

    def _divider(self):
        return {"tag": "hr"}

    def _md(self, content):
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    def _note(self, content):
        return {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": content}],
        }

    def _actions(self, *buttons):
        return {"tag": "action", "actions": list(buttons)}

    def _btn(self, text, action, btn_type="default", **extra):
        val = {"action": action}
        val.update(extra)
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": btn_type,
            "value": val,
        }

    def _form_submit_btn(self, text, action, btn_type="primary", name="submit", **extra):
        """v2表单提交按钮 - 必须有 form_action_type=submit 和 behaviors
        name: 按钮唯一标识，同一form内不能重复
        """
        val = {"action": action}
        val.update(extra)
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": btn_type,
            "form_action_type": "submit",
            "name": name,
            "behaviors": [{"type": "callback", "value": val}],
            "value": val,
        }

    def _v2_btn(self, text, action, btn_type="default", **extra):
        """v2普通按钮(非表单提交) - 用 behaviors 替代 value"""
        val = {"action": action}
        val.update(extra)
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": btn_type,
            "behaviors": [{"type": "callback", "value": val}],
            "value": val,
        }

    def _row_id(self, open_id):
        """花名册行的组件ID（确定性、卡内唯一、≤20字符）。

        open_id 过长不能直接做 element_id，取其 md5 前8位。
        """
        digest = hashlib.md5(open_id.encode("utf-8")).hexdigest()[:8]
        return f"row_{digest}"

    def _roster_row_readonly(self, info):
        """只读花名册行（无移除按钮），用于提交后锁定成员行。"""
        title = f"（{info['job_title']}）" if info.get("job_title") else ""
        dept = info.get("dept") or "未知部门"
        return {
            "tag": "column_set",
            "element_id": self._row_id(info.get("open_id", "")),
            "horizontal_spacing": "8px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{info['name']}**{title}　·　{dept}"}},
                    ],
                },
            ],
        }

    def _roster_row(self, info, remove_action):
        """花名册单行：左侧「姓名（职务）· 部门」，右侧「移除」按钮

        Args:
            info: {"name", "dept", "job_title", "open_id"}
            remove_action: 移除按钮的action名（remove_lead / remove_member）
        """
        title = f"（{info['job_title']}）" if info.get("job_title") else ""
        dept = info.get("dept") or "未知部门"
        val = {"action": remove_action, "open_id": info.get("open_id", "")}
        digest = hashlib.md5(info.get("open_id", "").encode("utf-8")).hexdigest()[:8]
        return {
            "tag": "column_set",
            "element_id": self._row_id(info.get("open_id", "")),
            "horizontal_spacing": "8px",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [
                        {"tag": "div", "text": {"tag": "lark_md",
                         "content": f"**{info['name']}**{title}　·　{dept}"}},
                    ],
                },
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [
                        {
                            "tag": "button",
                            "element_id": f"rm_{digest}",
                            "text": {"tag": "plain_text", "content": "移除"},
                            "type": "default",
                            "behaviors": [{"type": "callback", "value": val}],
                            "value": val,
                        },
                    ],
                },
            ],
        }

    def _person_picker(self, element_id, picker_name, action,
                       scope_users, exclude_ids=None):
        """下拉选择器 select_static（form 外）：预载范围内全部人员为选项。

        用户点开后在顶部搜索框输入姓名，客户端本地实时过滤（无需回车）：
        - 命中选项按「姓名 · 部门 · 职务」逐行显示；
        - 选中即回调（option=open_id），下拉自动收起、同批其余候选消失；
        - 未点开时只有占位文字，不显示任何人；无匹配时下拉内无选项。
        initial_index=0 表示不预置选中项。
        """
        exclude = set(exclude_ids or [])
        options = [self._person_option(u) for u in (scope_users or [])
                   if u.get("open_id") not in exclude]
        return {
            "tag": "select_static",
            "element_id": element_id,
            "name": picker_name,
            "initial_index": 0,
            "placeholder": {"tag": "plain_text", "content": "输入姓名搜索"},
            "options": options,
            "behaviors": [
                {"type": "callback", "value": {"action": action}},
            ],
        }

    def _person_option(self, user):
        """select_static 单选项：文本「姓名 · 部门 · 职务」，值为 open_id。"""
        parts = [user.get("name", "")]
        if user.get("department"):
            parts.append(user["department"])
        if user.get("job_title"):
            parts.append(user["job_title"])
        return {
            "text": {"tag": "plain_text", "content": " · ".join(parts)},
            "value": user.get("open_id", ""),
        }

    def _info_block(self, rows):
        """构建信息块"""
        lines = []
        for label, value in rows:
            lines.append(f"{label}　{value}")
        content = "\n".join(lines)
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    def _card_entry_row(self, num, num_color, title, subtitle, right_text, right_style,
                        action=None, btn_type="primary"):
        """入口能力行 - 左侧编号徽章+标题+描述，右侧状态文本，整行可点击(action 非空时)"""
        # 编号徽章 (用圆角矩形markdown模拟，飞书支持font_color+bgcolor不支持，用**加粗+颜色**)
        badge_md = f"**{num}.**"
        title_md = f"**{title}**"
        desc_md = subtitle
        right_md = right_text
        if right_style == "primary":
            right_md = f'<font color="blue">{right_text}</font>'
        elif right_style == "warning":
            right_md = f'<font color="orange">{right_text}</font>'
        elif right_style == "disabled":
            right_md = f'<font color="grey">{right_text}</font>'

        # 用 columns 实现：左 75% (徽章 标题 + 描述)，右 25% (右对齐状态文字)
        elements = [
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "grey" if action is None else "default",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 3,
                        "elements": [
                            {"tag": "div", "text": {"tag": "lark_md",
                                "content": f"{badge_md} {title_md}\n{desc_md}"}}
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {"tag": "div", "text": {
                                "tag": "lark_md",
                                "content": right_md,
                                "text_align": "right",
                            }}
                        ],
                    },
                ],
            }
        ]
        # 可点击：追加一个同名按钮，实现跳转
        if action is not None:
            elements.append(self._actions(self._btn(f"进入「{title}」", action, btn_type)))
        return elements

    def welcome_card(self):
        """欢迎能力卡 - 按原型对齐：3 条带编号入口 + 前2条可跳转 + 底部蓝色提示条"""
        elements = [
            self._md("**我能做什么**"),
        ]
        # 行1：创建项目群（蓝主色）
        elements.extend(self._card_entry_row(
            num=1, num_color="blue",
            title="创建项目群",
            subtitle="设置项目信息 → 指定部门负责人 → 收集成员 → 一键建群",
            right_text="点击开始 →", right_style="primary",
            action="start_create", btn_type="primary",
        ))
        elements.append(self._divider())
        # 行2：关闭并归档项目群（橙色警告色）
        elements.extend(self._card_entry_row(
            num=2, num_color="orange",
            title="关闭并归档项目群",
            subtitle="整理归档文档 → 记录归档清单 → 移出成员 → Agent退群",
            right_text="点击开始 →", right_style="warning",
            action="start_archive", btn_type="danger",
        ))
        elements.append(self._divider())
        # 行3：项目进度追踪（灰态，敬请期待）
        elements.extend(self._card_entry_row(
            num=3, num_color="grey",
            title="项目进度追踪",
            subtitle="查看 WBS 进度、风险预警、每周生成项目周报",
            right_text="敬请期待", right_style="disabled",
            action=None,
        ))
        elements.append(self._divider())
        # 底部蓝底提示条 (用 note 无法改蓝底，飞书card v1 没有背景色div，就用 md + 蓝字体模拟)
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": '<font color="blue">💡 点击上方功能卡片或在下方输入指令</font>',
            },
        })
        return self._card_v1("项目管理Agent", "blue", elements)

    def project_info_form_card(self):
        """项目信息表单卡片（入口卡）- 填项目名+简称，可选快速建群或收集成员"""
        return self._card_v2("创建项目群", "blue", [
            {"tag": "div", "element_id": "info_intro",
             "text": {"tag": "lark_md",
                "content": "请填写项目信息，系统将自动生成群名："}},
            {
                "tag": "form",
                "element_id": "info_form",
                "name": "project_info",
                "elements": [
                    {
                        "tag": "input",
                        "name": "project_name",
                        "required": True,
                        "label": {"tag": "plain_text", "content": "项目名称"},
                        "placeholder": {"tag": "plain_text", "content": "请输入项目全称"},
                        "max_length": 50,
                    },
                    {
                        "tag": "input",
                        "name": "project_abbr",
                        "label": {"tag": "plain_text", "content": "项目简称（可选，默认取名称前10字）"},
                        "placeholder": {"tag": "plain_text", "content": "留空则自动使用项目名称前10字"},
                        "max_length": 10,
                    },
                    {"tag": "div", "text": {"tag": "lark_md",
                        "content": "**群名规则**：项目简称 + 「项目管理Agent」\n建群后管理员可手动修改群名。"}},
                    self._form_submit_btn("快速建群", "quick_create_from_info", btn_type="primary", name="quick_create"),
                    self._form_submit_btn("收集成员", "confirm_project_info", name="collect_members"),
                ],
            },
            {**self._v2_btn("取消", "cancel"), "element_id": "info_cancel"},
        ])

    def project_info_locked_elements(self, project_name, project_abbr, group_name, status):
        """信息卡提交后的只读内容：无任何可操作组件。

        status: 当前进度提示，如「已进入成员收集流程」。
        """
        return [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": "✅ **项目信息已提交**（本卡片已锁定，不可再编辑）"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": (f"**项目名称**　{project_name}\n"
                            f"**项目简称**　{project_abbr}\n"
                            f"**群名**　`{group_name}`")}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f'<font color="grey">{status}</font>'}},
        ]

    def project_confirm_card(self, project_name, project_abbr):
        """项目信息确认卡 - 展示摘要，提供「快速建群」和「收集成员」两个路径"""
        group_name = f"{project_abbr}项目管理Agent"
        return self._card_v1("项目信息确认", "blue", [
            self._md("✅ 已收到项目信息，请选择后续操作："),
            self._divider(),
            self._info_block([
                ("项目名称", project_name),
                ("项目简称", project_abbr),
                ("群名", group_name),
            ]),
            self._divider(),
            self._actions(
                self._btn("快速建群", "quick_create_from_info", "primary"),
                self._btn("收集成员", "go_collect_members"),
            ),
        ])

    def dept_lead_collect_card(self, project_name, project_abbr,
                               lead_infos=None, scope_users=None):
        """部门负责人收集卡片 - v2格式

        布局自上而下：项目信息 → 已选花名册 → 搜索下拉 → 按钮（用户指定样式）。

        交互方式「下拉搜索 → 选中上榜」循环：
        1. 点开下拉，在搜索框输入姓名，无需回车，客户端实时过滤；
        2. 匹配人员逐行显示「姓名 · 部门 · 职务」，选中即上榜（出现在上方名单）；
        3. 选中后下拉自动收起、同批其余候选消失，下拉重置、已选者不再出现；
        4. 未点开时不显示任何人；也可点花名册「移除」，移除后重新可选。

        Args:
            project_name: 项目名称
            project_abbr: 项目简称
            lead_infos: 已选负责人信息列表 [{"name", "dept", "job_title", "open_id"}]
            scope_users: 可见范围全部用户（list_scope_users），用于预载下拉选项
        """
        group_name = f"{project_abbr}项目管理Agent"
        lead_infos = lead_infos or []

        elements = [
            {
                "tag": "div",
                "element_id": "lead_info",
                "text": {
                    "tag": "lark_md",
                    "content": f"**项目名称**　{project_name}\n**项目简称**　{project_abbr}\n**群名**　`{group_name}`",
                },
            },
            {"tag": "hr", "element_id": "lead_info_hr"},
            {"tag": "div", "element_id": "lead_count",
             "text": {"tag": "lark_md",
             "content": f"**已选负责人（{len(lead_infos)}人）**"}},
        ]

        # 花名册（每人一行带移除按钮）；为空时给提示
        if lead_infos:
            for info in lead_infos:
                elements.append(self._roster_row(info, "remove_lead"))
        else:
            elements.append({"tag": "div", "element_id": "lead_empty",
             "text": {"tag": "lark_md",
             "content": '<font color="grey">暂未选择，请在下方搜索姓名添加</font>'}})

        elements.extend([
            # lead_hr 分隔花名册与搜索区：新增行 insert_before 此锚点
            {"tag": "hr", "element_id": "lead_hr"},
            {"tag": "div", "element_id": "lead_search_title",
             "text": {"tag": "lark_md", "content": "**搜索添加负责人**"}},
            self._person_picker(
                "lead_picker", "lead_pick", "pick_lead",
                scope_users, [i.get("open_id") for i in lead_infos]),
            # lead_pick_tail：新下拉的插入锚点（选中后删旧下拉、在此前插入全新下拉）
            {"tag": "hr", "element_id": "lead_pick_tail"},
            {**self._v2_btn("发送收集表", "send_dept_leads", "primary",
                         project_name=project_name, project_abbr=project_abbr),
             "element_id": "lead_send_btn"},
            {
                "tag": "column_set",
                "element_id": "lead_skip_cancel",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            self._v2_btn("跳过收集", "skip_collection",
                                         project_name=project_name, project_abbr=project_abbr),
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            self._v2_btn("取消", "cancel"),
                        ],
                    },
                ],
            },
        ])

        return self._card_v2("部门负责人收集", "purple", elements)

    def dept_lead_collect_locked_elements(self, project_name, project_abbr,
                                          lead_infos, status):
        """负责人收集卡发送成功后的只读摘要（无任何可交互组件）。

        与 project_info_locked_elements 对应，用于锁定负责人收集卡。
        展示项目信息 + 只读花名册 + 状态提示。

        Args:
            lead_infos: 已选负责人列表 [{"name", "dept", "job_title", "open_id"}]
            status: 当前进度提示，如「已向 3/3 位负责人发送收集表」。
        """
        group_name = f"{project_abbr}项目管理Agent"
        elements = [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": "✅ **收集表已发送**（本卡片已锁定，不可再编辑）"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": (f"**项目名称**　{project_name}\n"
                            f"**项目简称**　{project_abbr}\n"
                            f"**群名**　`{group_name}`")}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**已选负责人（{len(lead_infos)}人）**"}},
        ]
        for info in lead_infos:
            title = f"（{info['job_title']}）" if info.get("job_title") else ""
            dept = info.get("dept") or "未知部门"
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**{info['name']}**{title}　·　{dept}"}})
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f'<font color="grey">{status}</font>'}})
        return elements

    def lead_member_collect_card(self, project_name, project_abbr, pm_user_id="",
                                 pm_chat_id="", lead_name="",
                                 member_infos=None, scope_users=None,
                                 deadline_str=""):
        """发给部门负责人的成员收集卡片 - v2格式

        布局自上而下：项目信息 → 截止时间 → 已选花名册 → 搜索下拉 → 按钮。

        Args:
            deadline_str: 格式化截止时间字符串，如「09-29 15:00」，为空则不显示。
        """
        member_infos = member_infos or []

        elements = [
            {"tag": "div", "element_id": "mem_intro",
             "text": {"tag": "lark_md",
                "content": "PM邀请您为以下项目收集部门成员："}},
            {"tag": "div", "element_id": "mem_info",
             "text": {"tag": "lark_md",
                "content": f"**项目名称**　{project_name}\n**项目简称**　{project_abbr}"}},
        ]
        if deadline_str:
            elements.append({"tag": "div", "element_id": "mem_deadline",
                "text": {"tag": "lark_md",
                    "content": f'<font color="blue">⏰ 截止时间：{deadline_str}</font>'}})
        elements.extend([
            {"tag": "hr", "element_id": "mem_info_hr"},
            {"tag": "div", "element_id": "mem_count",
             "text": {"tag": "lark_md",
             "content": f"**已选成员（{len(member_infos)}人）**"}},
        ])

        # 花名册
        if member_infos:
            for info in member_infos:
                elements.append(self._roster_row(info, "remove_member"))
        else:
            elements.append({"tag": "div", "element_id": "mem_empty",
             "text": {"tag": "lark_md",
             "content": '<font color="grey">暂未选择，请在下方搜索姓名添加</font>'}})

        elements.extend([
            # mem_hr 分隔花名册与搜索区：新增行 insert_before 此锚点
            {"tag": "hr", "element_id": "mem_hr"},
            {"tag": "div", "element_id": "mem_search_title",
             "text": {"tag": "lark_md", "content": "**搜索添加成员**"}},
            self._person_picker(
                "mem_picker", "member_pick", "pick_member",
                scope_users, [i.get("open_id") for i in member_infos]),
            # mem_pick_tail：新下拉的插入锚点（选中后删旧下拉、在此前插入全新下拉）
            {"tag": "hr", "element_id": "mem_pick_tail"},
            {**self._v2_btn("✅ 提交成员名单", "lead_submit_members", "primary",
                         project_name=project_name, project_abbr=project_abbr,
                         pm_user_id=pm_user_id, pm_chat_id=pm_chat_id, lead_name=lead_name),
             "element_id": "mem_submit_btn"},
            {
                "tag": "column_set",
                "element_id": "mem_skip_cancel",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            self._v2_btn("本部门不参与", "decline_collection",
                                 project_name=project_name, project_abbr=project_abbr,
                                 pm_user_id=pm_user_id, pm_chat_id=pm_chat_id, lead_name=lead_name),
                        ],
                    },
                ],
            },
        ])

        return self._card_v2("成员收集请求", "turquoise", elements)

    def member_collect_locked_elements(self, project_name, project_abbr,
                                       member_infos, status_text=None):
        """成员收集卡锁定后的只读摘要（无任何可交互组件）。

        Args:
            member_infos: 已提交成员列表 [{"name", "dept", "job_title", "open_id"}]
            status_text: 自定义锁定状态文案；None 时用默认「已截止」文案
        """
        if status_text is None:
            status_text = (f"✅ **已截止（共提交 {len(member_infos)} 人）**"
                           "（本卡片已锁定，不可再编辑）")
        elements = [
            {"tag": "div", "text": {"tag": "lark_md",
                "content": status_text}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**项目名称**　{project_name}\n"
                           f"**项目简称**　{project_abbr}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                "content": f"**已提交成员（{len(member_infos)}人）**"}},
        ]
        for info in member_infos:
            title = f"（{info['job_title']}）" if info.get("job_title") else ""
            dept = info.get("dept") or "未知部门"
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": f"**{info['name']}**{title}　·　{dept}"}})
        return elements

    def member_collect_overdue_text(self):
        """逾期提示文案（用于 patch mem_deadline 元素）"""
        return '<font color="red">⚠ 已逾期（未提交，仍可提交）</font>'

    def progress_dashboard_card(self, depts, project_name="", project_abbr="", bitable_url=""):
        """收集进度看板卡片 - v1格式，支持多维表格链接"""
        total = len(depts)
        filled = sum(1 for d in depts if d.get("status") in ("filled", "approved"))
        pending = sum(1 for d in depts if d.get("status") == "pending")
        timeout = sum(1 for d in depts if d.get("status") == "timeout")
        all_filled = filled == total

        status_icons = {"filled": "✅", "pending": "⏳", "timeout": "⚠️",
                        "not_found": "❓", "send_failed": "❌", "declined": "🚫",
                        "approved": "✅", "rejected": "⏳"}
        status_texts = {"filled": "已填待确认", "pending": "待填写", "timeout": "已超时",
                        "not_found": "未找到用户", "send_failed": "发送失败", "declined": "不参与",
                        "approved": "已入群", "rejected": "已拒绝"}

        lines = [f"**收集进度**：{filled}/{total} 已完成\n"]
        for d in depts:
            icon = status_icons.get(d.get("status", "pending"), "⏳")
            status_text = status_texts.get(d.get("status", "pending"), "待填写")
            count = d.get("member_count", 0)
            members = d.get("members", [])
            if d.get("status") in ("filled", "approved"):
                status_text = f"{count}人已填"
                if members:
                    names = [m.get("name", "") if isinstance(m, dict) else str(m) for m in members[:3]]
                    status_text += f"（{', '.join(names)}{'...' if len(members) > 3 else ''}）"
            lead_display = d.get("lead", "未知")
            job_title = d.get("job_title", "")
            if job_title:
                lead_display = f"{lead_display}（{job_title}）"
            lines.append(f"{icon} {d.get('dept', '未知')} · {lead_display} — {status_text}")

        elements = [
            self._md("\n".join(lines)),
            self._divider(),
            self._note(f"✅ {filled}已填  ⏳ {pending}待填  ⚠️ {timeout}超时"),
        ]

        if bitable_url:
            elements.append(self._actions(
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "📊 在多维表格中查看详情"},
                 "type": "default",
                 "url": bitable_url},
            ))

        # 群已创建，只保留催填提醒按钮
        if not all_filled:
            elements.append(self._actions(
                self._btn("催填提醒", "remind_pending"),
            ))

        template = "green" if all_filled else "purple"
        return self._card_v1("成员收集进度", template, elements)

    def member_confirm_card(self, lead_name, dept_name, members, lead_open_id="",
                            project_name="", timeout_minutes=30):
        """成员确认卡片 - 推送到群聊，PM可认可或拒绝负责人提交的成员

        Args:
            lead_name: 负责人姓名
            dept_name: 部门名称
            members: list of dict, each has name, dept, position, employee_id, open_id
            lead_open_id: 负责人open_id（用于回调定位）
            project_name: 项目名称
            timeout_minutes: 超时自动认可时间（分钟）
        """
        member_lines = []
        for m in members:
            info_parts = []
            if m.get("dept"):
                info_parts.append(m["dept"])
            if m.get("position"):
                info_parts.append(m["position"])
            if m.get("employee_id"):
                info_parts.append(m["employee_id"])
            info = f"（{' · '.join(info_parts)}）" if info_parts else ""
            member_lines.append(f"👤 {m.get('name', '未知')}{info}")

        content = (
            f"🔔 **成员提交待确认**\n\n"
            f"**{dept_name} · {lead_name}** 提交了 {len(members)} 名成员：\n\n"
            + "\n".join(member_lines)
            + f"\n\n⏰ {timeout_minutes}分钟内未操作将自动认可并拉入群。"
        )

        members_json = json.dumps(members, ensure_ascii=False)
        return self._card_v1("成员确认", "orange", [
            self._md(content),
            self._actions(
                self._btn("✅ 认可并拉入群", "approve_members", "primary",
                          lead_name=lead_name, dept_name=dept_name,
                          lead_open_id=lead_open_id, members=members_json),
                self._btn("❌ 拒绝", "reject_members", "danger",
                          lead_name=lead_name, dept_name=dept_name,
                          lead_open_id=lead_open_id, members=members_json),
            ),
        ])

    def member_confirm_locked_card(self, lead_name, dept_name, members,
                                   status_text="✅ 已认可", added_count=0):
        """PM 群内确认卡锁定后的只读 v1 卡：仅显示状态摘要，无任何按钮。

        Args:
            status_text: 顶部状态文案（已认可 / 超时自动认可）
            added_count: 实际拉入群的人数
        """
        member_lines = []
        for m in members:
            info_parts = []
            if m.get("dept"):
                info_parts.append(m["dept"])
            if m.get("position"):
                info_parts.append(m["position"])
            if m.get("employee_id"):
                info_parts.append(m["employee_id"])
            info = f"（{' · '.join(info_parts)}）" if info_parts else ""
            member_lines.append(f"👤 {m.get('name', '未知')}{info}")

        # 拒绝场景不显示"已入群"信息
        if "拒绝" in status_text:
            suffix = "（未拉入群）"
        else:
            suffix = f"（{added_count}人已入群）"

        content = (
            f"{status_text}：**{dept_name} · {lead_name}** 提交的 "
            f"{len(members)} 名成员{suffix}。\n\n"
            + "\n".join(member_lines)
            + "\n\n_本卡片已锁定_"
        )
        return self._card_v1("成员确认", "grey", [
            self._md(content),
        ])

    def build_progress_card(self, steps, current_step=0, error_msg=""):
        """建群进度卡片"""
        lines = []
        for i, step in enumerate(steps):
            done = i < current_step
            active = i == current_step
            if done:
                icon = "✅"
            elif active:
                icon = "🔄" if not error_msg else "❌"
            else:
                icon = "⬜"
            label = step if isinstance(step, str) else step.get("label", "")
            desc = step.get("desc", "") if isinstance(step, dict) else ""
            line = f"{icon} **{label}**"
            if desc:
                line += f"\n　　{desc}"
            lines.append(line)

        content = "\n\n".join(lines)
        if error_msg:
            content += f"\n\n❌ **错误**：{error_msg}"

        template = "blue"
        if current_step >= len(steps):
            template = "green"
        elif error_msg:
            template = "red"

        return self._card_v1("建群进度", template, [self._md(content)])

    def success_card(self, project_name, group_name, member_count, chat_id="", uncollected_depts=None):
        """建群成功卡片 - 对齐原型，支持未收集部门警告"""
        rows = [
            ("群名", group_name),
            ("群人数", f"{member_count} 人"),
            ("Agent状态", "✅ 已入群"),
        ]
        if chat_id:
            rows.append(("群聊ID", f"`{chat_id}`"))

        elements = [
            self._md(f"🎉 **项目群创建成功！**\n\n**项目名称**：{project_name}"),
            self._divider(),
            self._info_block(rows),
        ]

        if uncollected_depts:
            dept_list = ", ".join(uncollected_depts)
            elements.extend([
                self._divider(),
                self._md(f"⚠️ **以下部门暂未填写成员**：{dept_list}\n可在群内手动邀请，或通过多维表格更新。"),
            ])

        elements.extend([
            self._divider(),
            self._md(
                "Agent已入群，可以开始使用以下功能：\n"
                "• 发送消息通知全员\n"
                "• 自动提取会议纪要、决策、待办\n"
                "• 每日/每周生成项目摘要"
            ),
        ])

        return self._card_v1("建群成功", "green", elements)

    def agent_welcome_card(self, project_name, group_name):
        """Agent入群后在群内发送的欢迎卡片 - 按PRD 1.3.1的5项能力"""
        return self._card_v1(f"欢迎来到{group_name}", "blue", [
            self._md(
                f"👋 **大家好，我是项目管理Agent**\n\n"
                f"我已加入本群，为项目「{project_name}」提供服务。\n"
                f"可以随时 @我 提问，我会："
            ),
            self._divider(),
            self._md(
                "📝 **自动提取会议纪要和群聊信息**\n\n"
                "📅 **每周五自动生成项目周报**\n\n"
                "🔍 **答疑规格查询、预算编码查询**\n\n"
                "🚠 **监控项目进度，风险预警提醒**\n\n"
                "📋 **创建和追踪项目问题**"
            ),
            self._divider(),
            self._md(
                "💡 试试：\"@项目管理Agent 本周有哪些待办？\""
            ),
        ])

    def error_card(self, error_type, message, suggestion=""):
        """错误通知卡片"""
        content = f"❌ **{error_type}**\n\n{message}"
        if suggestion:
            content += f"\n\n💡 **建议**：{suggestion}"

        return self._card_v1("操作失败", "red", [
            self._md(content),
            self._actions(
                self._btn("重试", "retry", "primary"),
                self._btn("取消", "cancel"),
            ),
        ])

    def disband_project_select_card(self, projects):
        """项目选择卡片 - 列出PM的活跃项目供选择解散"""
        if not projects:
            return self._card_v1("解散项目", "grey", [
                self._md("当前没有活跃的项目可解散。"),
                self._actions(self._btn("关闭", "cancel")),
            ])
        elements = [self._md("请选择要关闭归档的项目：")]
        buttons = []
        for p in projects:
            name = p.get("project_name", "")
            abbr = p.get("project_abbr", "")
            pid = p.get("project_id", "")
            label = f"{name}（{abbr}）" if abbr else name
            buttons.append(self._btn(
                label, "select_disband_project", "default",
                project_id=pid,
            ))
        elements.append(self._actions(*buttons))
        elements.append(self._actions(self._btn("取消", "cancel")))
        return self._card_v1("解散项目", "red", elements)

    def disband_confirm_card(self, project_name, group_name, member_count):
        """解散确认卡片"""
        archived_name = f"[已归档] {group_name}"
        return self._card_v1("确认关闭项目", "red", [
            self._md("⚠️ **关闭项目将执行以下操作**："),
            self._info_block([
                ("项目", project_name),
                ("当前群名", group_name),
                ("群人数", f"{member_count}人"),
                ("关闭后群名", archived_name),
            ]),
            self._divider(),
            self._md(
                "📋 **归档内容**：\n"
                "• 聊天记录归档\n"
                "• 结构化索引（决策/待办/问题/风险）"
            ),
            self._divider(),
            self._md(
                "**请选择归档方式**：\n"
                "🔴 **解散群聊**：归档后解散群，群聊不复存在\n"
                "🟢 **保留群聊**：群名改为[已归档]，Agent退出，群聊保留可查看历史记录"
            ),
            self._actions(
                self._btn("确认归档并解散群", "confirm_disband", "danger", dissolve=True),
                self._btn("确认归档保留群", "confirm_disband", "default", dissolve=False),
                self._btn("取消", "cancel"),
            ),
        ])

    def disband_processing_card(self, mode_text):
        """归档进行中只读卡片（无按钮）"""
        return self._card_v1("⏳ 归档进行中", "grey", [
            self._md(f"正在执行归档（{mode_text}），请稍候..."),
        ])

    def disband_completed_card(self, project_name, dissolve, results):
        """归档完成只读卡片（冻结状态，无按钮）"""
        msg_count = results.get("messages_archived", 0)
        lines = [
            f"**项目**：{project_name}",
            "",
            "📋 **归档结果**：",
            f"• 归档消息：{msg_count} 条",
        ]
        if dissolve:
            lines.append("• 群聊已解散")
        else:
            lines.append("• 群名已改为 [已归档] 前缀")
            lines.append("• Agent已退出群聊（群聊保留）")
        lines.append("• 项目状态：已归档")
        lines.append("")
        lines.append("🔒 本卡片已锁定")
        return self._card_v1("✅ 归档完成", "grey", [self._md("\n".join(lines))])

    def disband_progress_card(self, steps, current_step=0, error_msg=""):
        """解散归档进度卡片"""
        return self.build_progress_card(steps, current_step, error_msg)

    def help_card(self):
        """帮助卡片 - 单入口说明"""
        return self._card_v1("指令帮助", "grey", [
            self._md(
                "**可用指令**：\n\n"
                "`/建群` — 创建项目群（唯一入口）\n"
                "`/解散` — 关闭项目并归档\n"
                "`/帮助` — 查看所有指令\n\n"
                "**建群流程**：\n"
                "1. 填写项目名称和简称\n"
                "2. 选择「快速建群」或「收集成员」\n"
                "   • 快速建群：立即创建群聊，PM + Agent 入群\n"
                "   • 收集成员：指定部门负责人，负责人填写成员后 PM 确认拉入群\n"
                "3. 群内可实时查看成员收集进度\n"
            ),
        ])
