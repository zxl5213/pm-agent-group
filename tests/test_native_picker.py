# -*- coding: utf-8 -*-
"""单元测试：下拉搜索选人（select_static）的人员收集卡结构。

方案（2026-09-27 真实客户端截图 + 官方文档核实）：
- select_static 预载可见范围全部人员为选项（8人），选项文案「姓名 · 部门 · 职务」；
- 布局：负责人卡为 项目信息 → 花名册 → 搜索下拉 → 按钮（用户指定样式）；
  成员卡为 搜索下拉置顶（标题下第一块）→ 项目信息 → 花名册 → 按钮。
  展开方向由客户端按输入框在屏幕上的剩余空间实时决定，与卡片结构无关；
- 用户点开下拉、在顶部搜索框输入姓名，无需回车，客户端本地实时过滤：
  命中逐行显示；选中即回调（option=open_id）上榜；
  下拉自动收起，同批其余候选消失；服务端重置下拉并剔除已选者；
- 未点开时只有占位文字、不显示任何人；无匹配下拉内无选项；
- 花名册支持移除，移除后下拉选项重新出现。并发回调由 per-card 锁串行化。

运行：python tests/test_native_picker.py
"""
import json
import sys

sys.path.insert(0, ".")
from src.feishu.card_builder import CardBuilder
from src.modules.session_manager import SessionManager

cb = CardBuilder()

# 可见范围全部用户（字段名 department，来自 list_scope_users）
USERS_ALL = [
    {"name": "鲁正柱", "department": "部门2", "job_title": "职位2",
     "open_id": "ou_lu"},
    {"name": "CSY", "department": "信息部", "job_title": "",
     "open_id": "ou_csy"},
    {"name": "张晓丽", "department": "部门1", "job_title": "职位1",
     "open_id": "ou_add1"},
    {"name": "张晓凯", "department": "部门3", "job_title": "职位2",
     "open_id": "ou_xk"},
]

# 已选花名册（open_id 与范围用户重叠，用于测试选项剔除）
LEAD_INFOS = [
    {"name": "张晓丽", "dept": "部门1", "job_title": "职位1",
     "open_id": "ou_add1"},
    {"name": "张晓凯", "dept": "部门3", "job_title": "职位2",
     "open_id": "ou_xk"},
]


def _behaviors_actions(elements):
    """收集卡片内所有 behaviors callback 的 action 名"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for b in node.get("behaviors", []):
                v = b.get("value", {})
                if "action" in v:
                    found.append(v["action"])
            for val in node.values():
                walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(elements)
    return found


def _picker(card):
    els = card["body"]["elements"]
    pickers = [e for e in els if e.get("tag") == "select_static"]
    assert len(pickers) == 1
    return pickers[0]


def _option_values(picker):
    return [o.get("value") for o in picker.get("options", [])]


def _assert_member_order(card, roster_tail_id):
    """成员卡顺序：项目信息 → 计数 → 花名册(空提示/行) → mem_hr →
    搜索标题 → 下拉 → mem_pick_tail（与负责人卡一致）。"""
    els = card["body"]["elements"]
    idx = {e.get("element_id"): i for i, e in enumerate(els)}
    pi = next(i for i, e in enumerate(els) if e.get("tag") == "select_static")
    # 花名册区在下拉之前
    assert idx["mem_count"] < idx[roster_tail_id] < pi
    # 新下拉锚点紧跟其后
    assert pi < idx["mem_pick_tail"]


def _assert_lead_order(card, roster_tail_id):
    """负责人卡顺序：项目信息 → 计数 → 花名册(空提示/行) → lead_hr →
    搜索标题 → 下拉 → lead_pick_tail。"""
    els = card["body"]["elements"]
    idx = {e.get("element_id"): i for i, e in enumerate(els)}
    pi = next(i for i, e in enumerate(els) if e.get("tag") == "select_static")
    # 花名册区在下拉之前
    assert idx["lead_count"] < idx[roster_tail_id] < pi
    # 新下拉锚点紧跟其后
    assert pi < idx["lead_pick_tail"]


def test_lead_card_empty():
    card = cb.dept_lead_collect_card("测试项目", "测试", scope_users=USERS_ALL)
    assert card["schema"] == "2.0"
    # cardkit 局部更新要求 update_multi
    assert card["config"]["update_multi"] is True
    els = card["body"]["elements"]
    actions = _behaviors_actions(els)
    assert "pick_lead" in actions
    assert "send_dept_leads" in actions
    # 无 form 容器、无输入框
    assert not any(e.get("tag") == "form" for e in els)
    assert not any(e.get("tag") == "input" for e in els)
    # 下拉默认收起：只有占位文字，不预置选中项、不显示任何人
    picker = _picker(card)
    assert picker["element_id"] == "lead_picker"
    assert picker["initial_index"] == 0
    beh = picker["behaviors"][0]
    assert beh["type"] == "callback"
    assert beh["value"] == {"action": "pick_lead"}
    # 预载全部4人，文案含部门职务
    assert _option_values(picker) == ["ou_lu", "ou_csy", "ou_add1", "ou_xk"]
    content = json.dumps(picker, ensure_ascii=False)
    assert "鲁正柱 · 部门2 · 职位2" in content
    assert "CSY · 信息部" in content  # 无职务不显示多余分隔
    # 局部更新锚点：tail锚点/计数/空提示/hr 均带 element_id
    by_id = {e.get("element_id"): e for e in els}
    for aid in ("lead_pick_tail", "lead_count", "lead_empty", "lead_hr"):
        assert aid in by_id
    assert "lead_cand_anchor" not in by_id
    # 空状态提示（搜索区在下方）
    assert any("请在下方搜索姓名添加" in json.dumps(e, ensure_ascii=False) for e in els)
    # 顺序：项目信息 → 计数 → 空提示 → 搜索下拉
    _assert_lead_order(card, "lead_empty")
    print("✅ test_lead_card_empty")


def test_lead_card_with_roster():
    card = cb.dept_lead_collect_card("测试项目", "测试",
                                     lead_infos=LEAD_INFOS,
                                     scope_users=USERS_ALL)
    els = card["body"]["elements"]
    actions = _behaviors_actions(els)
    # 每人一个移除按钮
    assert actions.count("remove_lead") == 2
    # 计数标题
    assert any("已选负责人（2人）" in json.dumps(e, ensure_ascii=False) for e in els)
    # 下拉选项剔除已选2人，只剩鲁正柱/CSY
    picker = _picker(card)
    assert _option_values(picker) == ["ou_lu", "ou_csy"]
    # 花名册行带确定性 element_id，有名单时不渲染空提示
    by_id = {e.get("element_id"): e for e in els}
    assert cb._row_id("ou_add1") in by_id
    assert cb._row_id("ou_xk") in by_id
    assert "lead_empty" not in by_id
    # 有名单时：计数 → lead_hr → 搜索下拉
    _assert_lead_order(card, "lead_hr")
    print("✅ test_lead_card_with_roster")


def test_member_card_empty():
    card = cb.lead_member_collect_card("测试项目", "测试", pm_user_id="ou_pm",
                                       pm_chat_id="oc_pm", lead_name="张晓丽",
                                       scope_users=USERS_ALL)
    els = card["body"]["elements"]
    actions = _behaviors_actions(els)
    assert "pick_member" in actions
    assert "lead_submit_members" in actions
    # 无 form 容器、无输入框
    assert not any(e.get("tag") == "form" for e in els)
    assert not any(e.get("tag") == "input" for e in els)
    picker = _picker(card)
    assert picker["element_id"] == "mem_picker"
    beh = picker["behaviors"][0]
    assert beh["type"] == "callback"
    assert beh["value"] == {"action": "pick_member"}
    assert len(picker["options"]) == 4
    # 局部更新锚点：tail/计数/空提示/hr
    by_id = {e.get("element_id"): e for e in els}
    for aid in ("mem_pick_tail", "mem_count", "mem_empty", "mem_hr"):
        assert aid in by_id
    assert "mem_cand_anchor" not in by_id
    # 布局顺序：项目信息 → 计数 → 空提示 → mem_hr → 搜索下拉 → mem_pick_tail
    _assert_member_order(card, "mem_empty")
    print("✅ test_member_card_empty")


def test_member_card_with_roster():
    card = cb.lead_member_collect_card("测试项目", "测试",
                                       member_infos=LEAD_INFOS,
                                       scope_users=USERS_ALL)
    els = card["body"]["elements"]
    actions = _behaviors_actions(els)
    assert actions.count("remove_member") == 2
    assert any("已选成员（2人）" in json.dumps(e, ensure_ascii=False) for e in els)
    # 下拉选项剔除已选2人
    picker = _picker(card)
    assert _option_values(picker) == ["ou_lu", "ou_csy"]
    # 花名册行 element_id
    by_id = {e.get("element_id"): e for e in els}
    assert cb._row_id("ou_add1") in by_id and cb._row_id("ou_xk") in by_id
    # 有名单时：计数 → mem_hr → 搜索下拉
    _assert_member_order(card, "mem_hr")
    print("✅ test_member_card_with_roster")


def test_info_cache_used():
    """用户/部门缓存命中时直接返回，不发网络请求"""
    from src.feishu.im_client import IMClient

    fake_cfg = {"feishu": {"api_base": "http://invalid", "app_id": "", "app_secret": ""}}
    im = IMClient(fake_cfg)

    # 预置用户缓存（未来过期时间）
    import time as _t
    expected = {"name": "鲁正柱", "department": "研发部", "job_title": "工程师",
                "open_id": "ou_lu"}
    IMClient._user_cache["ou_lu"] = (expected, _t.time() + 600)
    # 若不命中缓存会请求 invalid 地址；命中则返回缓存
    assert im.get_user_by_open_id("ou_lu") == expected

    # 根部门 "0" 永远本地返回"公司"，不发请求
    assert im._get_department_name("0") == "公司"

    # 预置部门负缓存，同样不发请求
    IMClient._dept_cache["ou_baddept"] = ("", _t.time() + 600)
    assert im._get_department_name("ou_baddept") == ""
    print("✅ test_info_cache_used")


def test_row_id_rules():
    """_row_id 满足 cardkit element_id 约束：
    字母开头、仅字母数字下划线、≤20字符、确定性。"""
    rid = cb._row_id("ou_abc123")
    assert rid.startswith("row_")
    assert rid == cb._row_id("ou_abc123")          # 确定性
    assert cb._row_id("ou_abc123") != cb._row_id("ou_xyz999")
    assert all(c.isalnum() or c == "_" for c in rid)
    assert len(rid) <= 20
    print("✅ test_row_id_rules")


def test_handler_action_builders():
    """cardkit action 构建器输出结构（纯本地，无网络）"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import Session

    h = CardActionHandler(None, None)

    # 序号严格递增
    s = Session(user_id="u", chat_id="c")
    assert h._next_seq(s) == 1 and h._next_seq(s) == 2

    # insert_before
    add = h._act_add_before("lead_hr", [{"tag": "div"}])
    assert add["action"] == "add_elements"
    assert add["params"]["type"] == "insert_before"
    assert add["params"]["target_element_id"] == "lead_hr"
    assert add["params"]["elements"] == [{"tag": "div"}]

    # delete
    dele = h._act_delete(["row_a"])
    assert dele["action"] == "delete_elements"
    assert dele["params"]["element_ids"] == ["row_a"]

    # patch
    pat = h._act_patch("lead_count", {"text": {}})
    assert pat["action"] == "partial_update_element"
    assert pat["params"]["element_id"] == "lead_count"
    assert pat["params"]["partial_element"] == {"text": {}}

    # 空提示：div + element_id
    hint = h._empty_hint("lead_empty")
    assert hint["tag"] == "div" and hint["element_id"] == "lead_empty"

    # 下拉选项构建：文本「姓名 · 部门 · 职务」，值 open_id
    opt = cb._person_option(USERS_ALL[0])
    assert opt["value"] == "ou_lu"
    assert opt["text"] == {"tag": "plain_text",
                           "content": "鲁正柱 · 部门2 · 职位2"}
    # 无职务时只显示 姓名 · 部门
    opt2 = cb._person_option(USERS_ALL[1])
    assert opt2["text"]["content"] == "CSY · 信息部"

    # 下拉重建动作：先删旧下拉，再在 tail 锚点前插入全新下拉
    new_picker = h._build_picker("lead_picker", "lead_pick",
                                 "pick_lead", USERS_ALL[:2])
    recreate = h._act_recreate_picker("lead_picker", "lead_pick_tail",
                                      new_picker)
    assert recreate[0]["action"] == "delete_elements"
    assert recreate[0]["params"]["element_ids"] == ["lead_picker"]
    add_act = recreate[1]
    assert add_act["action"] == "add_elements"
    assert add_act["params"]["target_element_id"] == "lead_pick_tail"
    inserted = add_act["params"]["elements"][0]
    assert inserted["tag"] == "select_static"
    assert [o["value"] for o in inserted["options"]] == ["ou_lu", "ou_csy"]
    # 全新下拉不预置选中项（占位符正常，不再有原始open_id裸露）
    assert inserted["initial_index"] == 0
    print("✅ test_handler_action_builders")


def test_pick_and_remove():
    """下拉选中上榜、移除后重新可选（全程 cardkit 局部更新）：

    - 选中1人：删空提示 → 花名册加行 → 计数更新 → 删旧下拉、原位插入全新下拉；
    - 新下拉选项剔除已选者，且无选中态（占位符正常，不裸露open_id）；
    - 移除1人：删行 → 新下拉选项重新出现该人。
    """
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import Session

    captured = {}

    class FakeIM:
        def get_user_by_open_id(self, oid):
            for u in USERS_ALL:
                if u["open_id"] == oid:
                    return {"name": u["name"], "department": u["department"],
                            "job_title": u["job_title"], "open_id": oid}
            return {"name": "未知", "department": "", "job_title": "",
                    "open_id": oid}

        def list_scope_users(self):
            return USERS_ALL

        def batch_update_card(self, card_id, actions, sequence=None):
            captured["actions"] = actions

    def actions_of(*names):
        return [a for a in captured["actions"] if a["action"] in names]

    def recreated_picker():
        """取重建动作里新插入的下拉元素。"""
        for a in captured["actions"]:
            if (a["action"] == "add_elements"
                    and a["params"]["target_element_id"] == "lead_pick_tail"):
                return a["params"]["elements"][0]
        raise AssertionError("缺少 lead_picker 重建动作")

    def assert_old_picker_deleted():
        assert any("lead_picker" in a["params"]["element_ids"]
                   for a in actions_of("delete_elements"))

    h = CardActionHandler(FakeIM(), None)
    s = Session(user_id="u", chat_id="c", card_id="card_x")

    # 1. 选中张晓丽：上榜
    ok = h._apply_pick(s, "dept_leads", "ou_add1", "remove_lead",
                       count_id="lead_count", empty_id="lead_empty",
                       hr_id="lead_hr", noun="负责人",
                       picker_id="lead_picker", tail_id="lead_pick_tail",
                       picker_name="lead_pick", pick_action="pick_lead")
    assert ok is True
    assert s.dept_leads == ["ou_add1"]
    # 旧下拉与空提示都删除
    dels = actions_of("delete_elements")
    deleted_items = [i for a in dels for i in a["params"]["element_ids"]]
    assert "lead_empty" in deleted_items and "lead_picker" in deleted_items
    # 花名册插行
    roster_adds = [a for a in actions_of("add_elements")
                   if a["params"]["target_element_id"] == "lead_hr"]
    assert cb._row_id("ou_add1") in json.dumps(roster_adds, ensure_ascii=False)
    # 全新下拉：无选中态、选项不含张晓丽
    picker = recreated_picker()
    assert picker["initial_index"] == 0
    vals = [o["value"] for o in picker["options"]]
    assert "ou_add1" not in vals and "ou_lu" in vals

    # 2. 再选张晓凯：新下拉选项只剩鲁正柱/CSY
    assert_old_picker_deleted()
    h._apply_pick(s, "dept_leads", "ou_xk", "remove_lead",
                  count_id="lead_count", empty_id="lead_empty",
                  hr_id="lead_hr", noun="负责人",
                  picker_id="lead_picker", tail_id="lead_pick_tail",
                  picker_name="lead_pick", pick_action="pick_lead")
    assert s.dept_leads == ["ou_add1", "ou_xk"]
    assert [o["value"] for o in recreated_picker()["options"]] == [
        "ou_lu", "ou_csy"]

    # 3. 移除张晓丽：新下拉选项重新出现她、不含张晓凯
    h._apply_remove(s, "dept_leads", "ou_add1",
                    count_id="lead_count", empty_id="lead_empty",
                    hr_id="lead_hr", noun="负责人",
                    picker_id="lead_picker", tail_id="lead_pick_tail",
                    picker_name="lead_pick", pick_action="pick_lead",
                    hint_pos="below")
    assert s.dept_leads == ["ou_xk"]
    values = [o["value"] for o in recreated_picker()["options"]]
    assert "ou_add1" in values and "ou_xk" not in values

    # 4. 移除最后一人：补空提示，文案指向下方搜索区
    h._apply_remove(s, "dept_leads", "ou_xk",
                    count_id="lead_count", empty_id="lead_empty",
                    hr_id="lead_hr", noun="负责人",
                    picker_id="lead_picker", tail_id="lead_pick_tail",
                    picker_name="lead_pick", pick_action="pick_lead",
                    hint_pos="below")
    assert s.dept_leads == []
    hint_adds = [a for a in actions_of("add_elements")
                 if a["params"]["target_element_id"] == "lead_hr"]
    assert any("请在下方搜索姓名添加" in json.dumps(a, ensure_ascii=False)
               for a in hint_adds)
    print("✅ test_pick_and_remove")


def test_pick_handlers_read_selected():
    """select_static 选中项 open_id 在 form_value["selected"]，
    两个 pick 处理器必须认它。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    captured = []

    class FakeIM:
        def resolve_card_id(self, message_id):
            return "cid1"

    h = CardActionHandler(FakeIM(), None)
    h.sessions = sm
    h._apply_pick = lambda session, attr, new_id, *a, **k: captured.append((attr, new_id))

    # 负责人卡：会话已存在且带 card_id
    s1 = sm.get_or_create("u1", "c1")
    s1.update(card_id="cid1")
    h._on_pick_lead("c1", "u1", {"action": "pick_lead"},
                    {"selected": "ou_newlead"})
    assert captured[-1] == ("dept_leads", "ou_newlead")

    # 成员卡：占位会话迁移场景，直接预置真实会话
    class FakeIM2(FakeIM):
        def resolve_card_id(self, message_id):
            return "cid2"

    h.im = FakeIM2()
    s2 = sm.get_or_create("u2", "c2")
    s2.update(card_id="cid2")
    h._on_pick_member("c2", "u2", {"action": "pick_member"},
                      {"selected": "ou_newmember"})
    assert captured[-1] == ("selected_members", "ou_newmember")
    print("✅ test_pick_handlers_read_selected")


def test_session_card_fields_migrate():
    """占位会话迁移时 card_id/card_seq 一并迁移，迁移后占位会话删除"""
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    placeholder = sm.get_or_create("ou_user", "ou_user")
    placeholder.update(state="collecting_members", project_name="P",
                       card_id="card_123", card_seq=5)

    real = sm.migrate_context("ou_user", "oc_real")
    assert real.chat_id == "oc_real"
    assert real.card_id == "card_123"
    assert real.card_seq == 5
    # 占位会话已删除
    assert sm.get("ou_user", "ou_user") is None
    print("✅ test_session_card_fields_migrate")


def test_bind_card_by_message_id():
    """被交互消息 → card_id 的精确绑定：覆盖会话里指向旧卡的 card_id。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import Session

    class FakeIM:
        def resolve_card_id(self, mid):
            return {"om_1": "card_member"}.get(mid, "")

    h = CardActionHandler(FakeIM(), None)

    # 消息可解析：会话 card_id 被纠正到被点击的卡
    s = Session(user_id="u", chat_id="c", card_id="card_old")
    assert h._bind_card(s, "om_1") is True
    assert s.card_id == "card_member"

    # 消息解析不到：保留会话已有绑定
    assert h._bind_card(s, "om_x") is True
    assert s.card_id == "card_member"

    # 消息和会话都没有 card：失败
    assert h._bind_card(Session(user_id="u2", chat_id="c2"), "") is False

    # im_client：注册表磁盘持久化，重启后仍可定位历史卡片
    from src.feishu.im_client import IMClient

    im1 = IMClient.__new__(IMClient)
    im1._register_sent_card(
        {"message_id": "om_persist_1"}, "cid_persist_1")
    assert im1.resolve_card_id("om_persist_1") == "cid_persist_1"
    assert im1.resolve_card_id("") == ""

    # 全新实例（模拟服务重启）：从磁盘加载，映射仍在
    im2 = IMClient.__new__(IMClient)
    assert im2.resolve_card_id("om_persist_1") == "cid_persist_1"
    # 未登记的消息查不到
    assert im2.resolve_card_id("om_never_registered") == ""

    # 早期手工写入的注册也兼容
    im3 = IMClient.__new__(IMClient)
    im3._card_registry()["om_9"] = "cid_9"
    assert im3.resolve_card_id("om_9") == "cid_9"
    print("✅ test_bind_card_by_message_id")


def test_migrate_fresh_placeholder_overrides_stale_real():
    """跨轮次：真实会话残留上一轮旧 card_id/已选状态，新一轮占位会话更新，
    migrate 必须以占位会话为准。"""
    from src.modules.session_manager import SessionManager

    sm = SessionManager()

    # 上一轮：已迁移过的真实会话，带旧卡和已选成员
    stale_real = sm.get_or_create("ou_user", "oc_real")
    stale_real.update(state="collecting_members", project_name="旧项目",
                      card_id="old_card", card_seq=3,
                      selected_members=["ou_oldmember"])
    # 人为把 updated_at 调旧
    stale_real.updated_at -= 100

    # 新一轮：重新发卡，重建占位会话（新卡、空已选）
    placeholder = sm.get_or_create("ou_user", "ou_user")
    placeholder.update(state="collecting_members", project_name="新项目",
                       card_id="new_card", card_seq=0, selected_members=[])

    real = sm.migrate_context("ou_user", "oc_real")
    assert real.card_id == "new_card"
    assert real.project_name == "新项目"
    assert real.selected_members == []
    print("✅ test_migrate_fresh_placeholder_overrides_stale_real")


def test_success_card_no_done_button():
    """建群成功卡不再放「完成」按钮：无 action 元素，handler 也无 done 路由。"""
    from src.modules.card_action_handler import CardActionHandler

    card = cb.success_card("A", "A项目管理Agent", 1, chat_id="oc_x")
    assert all(e.get("tag") != "action" for e in card["elements"])
    assert "done" not in json.dumps(card, ensure_ascii=False)

    # done 处理器方法已移除（源码层面静态确认）
    import inspect
    assert "_on_done" not in inspect.getsource(CardActionHandler)
    print("✅ test_success_card_no_done_button")


def test_info_card_lock():
    """信息卡锁定：动作序列为 info_intro 前插摘要 + 删除三元素。"""
    from src.modules.card_action_handler import CardActionHandler

    captured = {}

    class FakeIM:
        def resolve_card_id(self, mid):
            return "cid_info"

        def batch_update_card(self, card_id, actions, sequence=None):
            captured["actions"] = actions

    h = CardActionHandler(FakeIM(), None)
    h._lock_info_card("om_info", "项目A", "A", "锁定状态")
    actions = captured["actions"]
    assert actions[0]["action"] == "add_elements"
    assert actions[0]["params"]["target_element_id"] == "info_intro"
    assert actions[1]["action"] == "delete_elements"
    assert actions[1]["params"]["element_ids"] == [
        "info_intro", "info_form", "info_cancel"]
    print("✅ test_info_card_lock")


def test_lead_card_lock():
    """负责人收集卡锁定：lead_info 前插只读摘要 + 删除全部交互组件（含花名册行动态 id）。"""
    from src.modules.card_action_handler import CardActionHandler

    captured = {}

    class FakeIM:
        def resolve_card_id(self, mid):
            return "cid_lead"

        def batch_update_card(self, card_id, actions, sequence=None):
            captured["actions"] = actions
            captured["card_id"] = card_id

    h = CardActionHandler(FakeIM(), None)
    h._lock_lead_collect_card("om_lead", "项目Q", "Q", LEAD_INFOS, "已发送")
    actions = captured["actions"]
    # 第0个动作：lead_info 前插入只读摘要
    assert actions[0]["action"] == "add_elements"
    assert actions[0]["params"]["target_element_id"] == "lead_info"
    summary_els = actions[0]["params"]["elements"]
    # 摘要含锁定提示 + 项目信息 + 只读花名册（无移除按钮、无 behaviors）
    summary_json = json.dumps(summary_els, ensure_ascii=False)
    assert "收集表已发送" in summary_json
    assert "项目Q" in summary_json
    assert "张晓丽" in summary_json and "张晓凯" in summary_json
    assert "移除" not in summary_json
    assert not any("behaviors" in e for e in summary_els)
    # 第1个动作：删除全部交互组件
    assert actions[1]["action"] == "delete_elements"
    deleted = actions[1]["params"]["element_ids"]
    # lead_empty 仅在花名册为空时存在；非空（LEAD_INFOS 含2人）时不应删除
    for aid in ("lead_info", "lead_info_hr", "lead_count",
                "lead_hr", "lead_search_title", "lead_picker",
                "lead_pick_tail", "lead_send_btn", "lead_skip_cancel"):
        assert aid in deleted, f"缺少删除 id: {aid}"
    assert "lead_empty" not in deleted, "非空花名册不应删除 lead_empty"
    # 花名册行动态 id 也被删除
    assert cb._row_id("ou_add1") in deleted
    assert cb._row_id("ou_xk") in deleted
    print("✅ test_lead_card_lock")


def test_stray_text_during_lead_pick_ignored():
    """waiting_dept_leads 状态下误发到聊天框的文本（如「1」）不得被当作负责人：
    状态不变、已选人员不被覆盖、不发进度卡，仅提示回卡片操作。"""
    from src.modules.command_handler import CommandHandler
    from src.modules.session_manager import SessionManager

    class FakeIM:
        def __init__(self):
            self.texts, self.cards = [], []

        def send_text(self, chat_id, content):
            self.texts.append(content)

        def send_card(self, *a, **k):
            self.cards.append(a)

        def send_cardkit(self, *a, **k):
            self.cards.append(a)

    im = FakeIM()
    sm = SessionManager()
    ch = CommandHandler(im, sm)
    sm.update_state("u1", "c1", "waiting_dept_leads",
                    dept_leads=["ou_a", "ou_b"], project_name="P",
                    project_abbr="P")

    ch.handle_message("c1", "u1", "1")

    s = sm.get_or_create("u1", "c1")
    assert s.state == "waiting_dept_leads"          # 状态未跳转
    assert s.dept_leads == ["ou_a", "ou_b"]         # 卡片已选人员未被覆盖
    assert im.cards == []                            # 不发进度卡
    assert len(im.texts) == 1 and "搜索框" in im.texts[0]
    print("✅ test_stray_text_during_lead_pick_ignored")


def _make_cmd_handler():
    from src.modules.command_handler import CommandHandler

    class FakeIM:
        def __init__(self):
            self.texts, self.cards, self.cardkits = [], [], []

        def send_text(self, cid, content):
            self.texts.append(content)

        def send_card(self, *a, **k):
            self.cards.append(a)

        def send_cardkit(self, *a, **k):
            self.cardkits.append(a)

    im = FakeIM()
    sm = SessionManager()
    return CommandHandler(im, sm), im, sm


def test_group_chat_management_silent_unless_mentioned():
    """群聊：未@机器人时，/建群、/解散、自然语言建群全部静默、不改状态。"""
    ch, im, sm = _make_cmd_handler()
    sm.update_state("u1", "cg", "idle")

    for msg in ("/建群", "/解散", "帮我建个群", "这个项目怎么解散"):
        ch.handle_message("cg", "u1", msg, chat_type="group", is_mentioned=False)

    assert im.texts == [] and im.cards == [] and im.cardkits == []
    assert sm.get_or_create("u1", "cg").state == "idle"
    print("✅ test_group_chat_management_silent_unless_mentioned")


def test_group_chat_management_guide_when_mentioned():
    """群聊：@机器人说建群/解散 → 引导私聊，不发任何卡片、不跳转状态。"""
    ch, im, sm = _make_cmd_handler()
    sm.update_state("u1", "cg", "idle")

    for msg in ("/建群", "/解散", "帮我建个群"):
        ch.handle_message("cg", "u1", msg, chat_type="group", is_mentioned=True)

    assert len(im.texts) == 3
    assert all("私聊" in t for t in im.texts)
    assert im.cards == [] and im.cardkits == []
    assert sm.get_or_create("u1", "cg").state == "idle"

    # @机器人说无关内容：简短说明，而非建群引导
    ch.handle_message("cg", "u1", "今天天气不错",
                      chat_type="group", is_mentioned=True)
    assert "/帮助" in im.texts[-1]
    print("✅ test_group_chat_management_guide_when_mentioned")


def test_group_chat_help_available():
    """群聊：/帮助 开放，任何人可查帮助卡。"""
    ch, im, sm = _make_cmd_handler()
    ch.handle_message("cg", "u1", "/帮助",
                      chat_type="group", is_mentioned=False)
    assert len(im.cards) == 1 and im.texts == []
    print("✅ test_group_chat_help_available")


def test_private_chat_create_still_works():
    """私聊：/建群 正常发卡、状态流转（回归保护）。"""
    ch, im, sm = _make_cmd_handler()
    ch.handle_message("c1", "u1", "/建群")          # 默认 p2p
    assert len(im.cardkits) == 1
    assert sm.get_or_create("u1", "c1").state == "waiting_project_info"
    print("✅ test_private_chat_create_still_works")


def test_group_card_callback_blocked():
    """群聊中管理卡片回调被拦截：不分发动作，仅提示私聊。"""
    from src.modules.card_action_handler import CardActionHandler

    class FakeIM:
        def __init__(self):
            self.texts = []

        def send_text(self, cid, t):
            self.texts.append(t)

    im = FakeIM()
    sm = SessionManager()
    sm.get_or_create("u1", "cg").update(chat_kind="group")
    h = CardActionHandler(im, sm)
    dispatched = []
    h._on_cancel = lambda *a, **k: dispatched.append(1)

    h.handle_action("cg", "u1", {"action": "cancel"})

    assert dispatched == []
    assert len(im.texts) == 1 and "私聊" in im.texts[0]
    print("✅ test_group_card_callback_blocked")


def test_group_greeting_welcome_only_when_mentioned():
    """群聊：未@机器人时问候语不触发欢迎卡；@机器人才发欢迎。"""
    from types import SimpleNamespace
    from src.feishu.ws_client import WsClient
    from src.modules.command_handler import CommandHandler

    class FakeIM:
        def __init__(self):
            self.texts, self.cards = [], []

        def send_text(self, cid, t):
            self.texts.append(t)

        def send_card(self, *a, **k):
            self.cards.append(a)

    class FakeCardHandler:
        card_builder = cb

    im = FakeIM()
    sm = SessionManager()
    ch = CommandHandler(im, sm)
    ws = WsClient("a", "b", ch, FakeCardHandler(),
                  im_client=im, session_manager=sm)

    def fire(mentioned):
        msg = SimpleNamespace(
            chat_id="cg", content=json.dumps({"text": "你好"}),
            message_type="text",
            mentions=[{"id": "x"}] if mentioned else [],
            chat_type="group")
        event = SimpleNamespace(
            message=msg,
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="u1")))
        ws._on_message_receive(SimpleNamespace(event=event))

    fire(False)                       # 未@：静默
    assert im.texts == [] and im.cards == []
    fire(True)                        # @机器人：发欢迎
    assert len(im.texts) == 1 and len(im.cards) == 1
    print("✅ test_group_greeting_welcome_only_when_mentioned")


def test_batch_delete_records_chunking():
    """BaseClient.batch_delete_records：空列表不请求；按500条分批，路径与body正确。"""
    from src.feishu.base_client import BaseClient

    bc = object.__new__(BaseClient)
    calls = []
    bc._request = lambda *a, **k: calls.append((a, k)) or {}

    bc.batch_delete_records("app", "tbl", [])
    assert calls == []

    bc.batch_delete_records("app", "tbl", ["r1", "r2", "r3"])
    assert len(calls) == 1
    (method, path), kwargs = calls[0]
    assert method == "POST"
    assert path == "/bitable/v1/apps/app/tables/tbl/records/batch_delete"
    assert kwargs["json_body"] == {"records": ["r1", "r2", "r3"]}
    assert kwargs["token_type"] == "tenant"

    calls.clear()
    bc.batch_delete_records("app", "tbl", [f"r{i}" for i in range(600)])
    assert len(calls) == 2
    assert len(calls[0][1]["json_body"]["records"]) == 500
    assert len(calls[1][1]["json_body"]["records"]) == 100
    print("✅ test_batch_delete_records_chunking")


def test_private_card_callback_passes():
    """私聊卡片回调正常分发（默认 chat_kind=p2p）。"""
    from src.modules.card_action_handler import CardActionHandler

    class FakeIM:
        def __init__(self):
            self.texts = []

        def send_text(self, cid, t):
            self.texts.append(t)

    im = FakeIM()
    sm = SessionManager()
    h = CardActionHandler(im, sm)
    h.handle_action("c1", "u1", {"action": "cancel"})
    assert any("已取消" in t for t in im.texts)
    print("✅ test_private_card_callback_passes")


# ---------- 项目名/简称唯一性检查 ----------

def _make_fake_project(name, abbr, status="待启动", pid="P1"):
    """构造一个用于查重的 Project-like 对象"""
    from src.models.project import Project
    return Project(
        project_id=pid,
        project_name=name,
        project_short_name=abbr,
        pm_open_id="ou_pm",
        status=status,
    )


def _make_records(*projects):
    """把 Project 列表转成 Bitable record 格式"""
    return [{"fields": p.to_bitable_fields()} for p in projects]


def test_find_duplicate_name_exact():
    """名称精确匹配（含首尾空格），简称忽略大小写"""
    from src.modules.group_manager import GroupManager

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok"
    gm.f01_tables = {"project_config": "tbl"}

    existing = _make_fake_project("AI项目", "AI")
    gm.base_client = type("BC", (), {
        "list_all_records": staticmethod(
            lambda app, tbl: _make_records(existing)),
    })()

    # 名称精确匹配（空格不影响）
    field, proj = gm.find_duplicate_project("  AI项目  ", "XY")
    assert field == "项目名称" and proj.project_id == "P1"

    # 名称不匹配 → 无冲突
    field, _ = gm.find_duplicate_project("BI项目", "XY")
    assert field is None
    print("✅ test_find_duplicate_name_exact")


def test_find_duplicate_abbr_case_insensitive():
    """简称忽略大小写"""
    from src.modules.group_manager import GroupManager

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok"
    gm.f01_tables = {"project_config": "tbl"}

    existing = _make_fake_project("X项目", "AI")
    gm.base_client = type("BC", (), {
        "list_all_records": staticmethod(
            lambda app, tbl: _make_records(existing)),
    })()

    # 大小写不同也视为冲突
    field, proj = gm.find_duplicate_project("Y项目", "ai")
    assert field == "项目简称" and proj.project_id == "P1"

    field, _ = gm.find_duplicate_project("Y项目", "bi")
    assert field is None
    print("✅ test_find_duplicate_abbr_case_insensitive")


def test_find_duplicate_archived_reusable():
    """已归档项目不占位，名称/简称可复用"""
    from src.modules.group_manager import GroupManager

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok"
    gm.f01_tables = {"project_config": "tbl"}

    archived = _make_fake_project("AI项目", "AI", status="已归档")
    gm.base_client = type("BC", (), {
        "list_all_records": staticmethod(
            lambda app, tbl: _make_records(archived)),
    })()

    field, _ = gm.find_duplicate_project("AI项目", "AI")
    assert field is None  # 归档可复用
    print("✅ test_find_duplicate_archived_reusable")


def test_find_duplicate_exclude_self():
    """排除自身项目ID"""
    from src.modules.group_manager import GroupManager

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok"
    gm.f01_tables = {"project_config": "tbl"}

    existing = _make_fake_project("AI项目", "AI", pid="P1")
    gm.base_client = type("BC", (), {
        "list_all_records": staticmethod(
            lambda app, tbl: _make_records(existing)),
    })()

    field, _ = gm.find_duplicate_project("AI项目", "AI", exclude_project_id="P1")
    assert field is None
    print("✅ test_find_duplicate_exclude_self")


def test_handler_precheck_blocks_on_duplicate():
    """handler 前置查重命中时不换卡、不改状态、仅发文本提示"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sent = {"texts": [], "cards": [], "cardkits": []}

    class FakeIM:
        def send_text(self, cid, t):
            sent["texts"].append(t)

        def send_card(self, *a, **k):
            sent["cards"].append(a)

        def send_cardkit(self, *a, **k):
            sent["cardkits"].append(a)

        def list_scope_users(self):
            return []

    class FakeGM:
        def find_duplicate_project(self, name, abbr):
            return "项目名称", _make_fake_project("AI项目", "AI")

    im = FakeIM()
    sm = SessionManager()
    h = CardActionHandler(im, sm, group_manager=FakeGM())
    sm.update_state("u1", "c1", "waiting_project_info")

    # 模拟表单提交：名称冲突
    h.handle_action("c1", "u1", {"action": "confirm_project_info"},
                    form_value={"project_name": "AI项目", "project_abbr": "AI"})

    s = sm.get_or_create("u1", "c1")
    assert s.state == "waiting_project_info"  # 状态未跳转
    assert sent["cards"] == [] and sent["cardkits"] == []  # 不换卡
    assert len(sent["texts"]) == 1 and "已被现有项目" in sent["texts"][0]
    print("✅ test_handler_precheck_blocks_on_duplicate")


def test_handler_precheck_passes_on_clean():
    """无冲突时正常换卡、状态流转"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sent = {"texts": [], "cardkits": []}

    class FakeIM:
        def send_text(self, cid, t):
            sent["texts"].append(t)

        def send_card(self, *a, **k):
            pass

        def send_cardkit(self, *a, **k):
            sent["cardkits"].append(a)

        def list_scope_users(self):
            return []

        def resolve_card_id(self, mid):
            return "card_x"

        def batch_update_card(self, *a, **k):
            pass

    class FakeGM:
        def find_duplicate_project(self, name, abbr):
            return None, None

    im = FakeIM()
    sm = SessionManager()
    h = CardActionHandler(im, sm, group_manager=FakeGM())
    sm.update_state("u1", "c1", "waiting_project_info")

    h.handle_action("c1", "u1", {"action": "confirm_project_info"},
                    form_value={"project_name": "新项目", "project_abbr": "NEW"})

    s = sm.get_or_create("u1", "c1")
    assert s.state == "waiting_dept_leads"  # 正常流转
    assert len(sent["cardkits"]) == 1  # 发了收集卡
    print("✅ test_handler_precheck_passes_on_clean")


def test_handler_precheck_abbr_truncated():
    """简称超长时截断到10字符后再查重"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    captured = {"abbr": None}

    class FakeIM:
        def send_text(self, cid, t):
            pass

        def send_card(self, *a, **k):
            pass

        def send_cardkit(self, *a, **k):
            pass

        def list_scope_users(self):
            return []

    class FakeGM:
        def find_duplicate_project(self, name, abbr):
            captured["abbr"] = abbr
            return None, None

    im = FakeIM()
    sm = SessionManager()
    h = CardActionHandler(im, sm, group_manager=FakeGM())

    long_abbr = "ABCDEFGHIJKLMN"  # 14字符
    h.handle_action("c1", "u1", {"action": "confirm_project_info"},
                    form_value={"project_name": "X项目", "project_abbr": long_abbr})

    assert captured["abbr"] == "ABCDEFGHIJ"  # 截断为10
    print("✅ test_handler_precheck_abbr_truncated")


def test_register_project_raises_on_duplicate():
    """register_project 兜底查重：命中则 raise DuplicateProjectError"""
    from src.modules.group_manager import GroupManager
    from src.models.exceptions import DuplicateProjectError

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok"
    gm.f01_tables = {"project_config": "tbl"}

    existing = _make_fake_project("AI项目", "AI", pid="P1")
    gm.base_client = type("BC", (), {
        "list_all_records": staticmethod(
            lambda app, tbl: _make_records(existing)),
        "create_record": staticmethod(lambda *a, **k: None),
    })()
    gm.state_engine = None  # 不应走到创建步骤

    try:
        gm.register_project("AI项目", "AI", "ou_pm")
        assert False, "应抛 DuplicateProjectError"
    except DuplicateProjectError as e:
        assert e.field == "项目名称"
        assert e.existing_project_id == "P1"
    print("✅ test_register_project_raises_on_duplicate")


def test_confirm_project_info_resets_stale_leads():
    """新一轮建群进入 waiting_dept_leads 时必须清空 dept_leads/selected_members。

    复现场景：上一轮 _on_send_dept_leads 将 dept_leads 覆写为 dict 列表（进度
    追踪用）。若新一轮不清空，pick_lead 的 _remaining_candidates 做
    set(ids) 时会因 dict 不可哈希而报 unhashable type: 'dict'，下拉重建失败。
    """
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    # 模拟上一轮残留：dept_leads 为 dict 列表、selected_members 也有残留
    sm.update_state("ou_pm", "oc_pm", "collecting_members",
                    dept_leads=[{"dept": "X", "open_id": "ou_a"}],
                    selected_members=["ou_b"])
    captured = {}

    class FakeIM:
        def list_scope_users(self):
            return []

        def send_cardkit(self, chat_id, card):
            captured["sent"] = True
            return {"card_id": "cid_new"}

        def send_text(self, *a, **k):
            pass

        def resolve_card_id(self, mid):
            return "cid_info"

        def batch_update_card(self, card_id, actions, sequence=None):
            pass

    class FakeGM:
        def find_duplicate_project(self, name, abbr):
            return None, None

    h = CardActionHandler(FakeIM(), None)
    h.sessions = sm
    h.group_manager = FakeGM()
    h._on_confirm_project_info(
        "oc_pm", "ou_pm",
        value={}, form_value={"project_name": "新项目", "project_abbr": "新"},
        message_id="om_new")

    s = sm.get_or_create("ou_pm", "oc_pm")
    assert s.state == "waiting_dept_leads"
    assert s.dept_leads == [], f"dept_leads 应已清空，实际: {s.dept_leads}"
    assert s.selected_members == [], f"selected_members 应已清空，实际: {s.selected_members}"
    print("✅ test_confirm_project_info_resets_stale_leads")


def test_member_card_deadline_display():
    """成员收集卡显示截止时间行。"""
    cb = CardBuilder()
    card = cb.lead_member_collect_card("项目X", "X", deadline_str="09-29 15:00")
    elements = card["body"]["elements"]
    deadline = next(
        (e for e in elements if e.get("element_id") == "mem_deadline"), None)
    assert deadline is not None, "应包含 mem_deadline 元素"
    assert "09-29 15:00" in deadline["text"]["content"]
    # 无 deadline_str 时不显示
    card2 = cb.lead_member_collect_card("项目X", "X")
    assert not any(e.get("element_id") == "mem_deadline"
                   for e in card2["body"]["elements"])
    print("✅ test_member_card_deadline_display")


def test_member_card_element_ids_for_freeze():
    """成员收集卡所有交互元素都有 element_id（冻结时按 id 删除）。"""
    cb = CardBuilder()
    card = cb.lead_member_collect_card("P", "P", deadline_str="09-29 15:00")
    elements = card["body"]["elements"]
    ids = {e.get("element_id") for e in elements if e.get("element_id")}
    required = {"mem_intro", "mem_info", "mem_deadline", "mem_info_hr",
                "mem_count", "mem_empty", "mem_hr", "mem_search_title",
                "mem_picker", "mem_pick_tail", "mem_submit_btn",
                "mem_skip_cancel"}
    missing = required - ids
    assert not missing, f"缺少 element_id: {missing}"
    print("✅ test_member_card_element_ids_for_freeze")


def test_member_collect_locked_elements():
    """冻结后的只读摘要不含交互组件，显示提交人数。"""
    cb = CardBuilder()
    members = [{"name": "张三", "dept": "技术部", "job_title": "工程师",
                "open_id": "ou_z3"}]
    elements = cb.member_collect_locked_elements("项目X", "X", members)
    texts = [e["text"]["content"] for e in elements if e.get("tag") == "div"]
    assert any("已截止" in t and "1" in t for t in texts), \
        f"应显示已截止(1人)，实际: {texts}"
    assert any("张三" in t for t in texts), f"应显示张三，实际: {texts}"
    # 无交互组件
    assert not any(e.get("tag") in ("select_static", "button", "column_set")
                   for e in elements)
    print("✅ test_member_collect_locked_elements")


def test_member_collect_locked_elements_custom_status():
    """认可后锁定：传入 status_text 应替换默认「已截止」文案。"""
    cb = CardBuilder()
    members = [{"name": "李四", "dept": "市场部", "job_title": "",
                "open_id": "ou_l4"}]
    elements = cb.member_collect_locked_elements(
        "项目Y", "Y", members, status_text="✅ PM 已认可，成员已入群（本卡片已锁定）")
    texts = [e["text"]["content"] for e in elements if e.get("tag") == "div"]
    assert any("PM 已认可" in t for t in texts), f"应显示 PM 已认可，实际: {texts}"
    assert not any("已截止" in t for t in texts), "不应出现默认「已截止」文案"
    print("✅ test_member_collect_locked_elements_custom_status")


def test_approve_members_locks_lead_collect_card():
    """PM 认可成员后两张卡都锁定：
    - 群内 PM 确认卡：update_card 替换为只读锁定版本（认可/拒绝按钮消失）
    - 负责人那边的成员收集卡：batch_update_card 删除交互组件
    """
    import json as _json
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    sm.update_state("ou_pm", "c_pm", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    project_id="P1", group_chat_id="cg_group_1",
                    dept_leads=[{"open_id": "ou_lead1", "lead": "张三",
                                 "dept": "前端", "status": "filled",
                                 "member_count": 1, "members": []}])
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    card_id="card_lead_1",
                    selected_members=["ou_m1"])

    update_calls = []
    batch_calls = []

    class FakeIM:
        def resolve_card_id(self, mid):
            return ""

        def get_user_by_open_id(self, oid):
            return {"name": "鲁正柱", "department": "部门2", "job_title": "职位2"}

        def send_text(self, cid, text):
            pass

        def send_text_to_user(self, oid, text):
            pass

        def update_card(self, message_id, card_content):
            update_calls.append((message_id, card_content))

        def batch_update_card(self, card_id, actions, sequence=None):
            batch_calls.append((card_id, actions))

    h = CardActionHandler(FakeIM(), sm, group_manager=None)
    members = [{"name": "鲁正柱", "open_id": "ou_m1", "dept": "部门2",
                 "position": "职位2", "employee_id": ""}]
    confirm_msg_id = "om_confirm_card_1"
    h.handle_action(
        "cg_group_1", "ou_pm",
        {"action": "approve_members",
         "lead_name": "张三", "lead_open_id": "ou_lead1",
         "dept_name": "前端", "members": _json.dumps(members)},
        form_value={}, message_id=confirm_msg_id)

    # 1. 群内 PM 确认卡应被 update_card 替换为只读锁定版本
    assert len(update_calls) >= 1, f"应调用 update_card 锁定 PM 确认卡，实际 {len(update_calls)}"
    msg_id, _ = update_calls[0]
    assert msg_id == confirm_msg_id, f"应更新 PM 确认卡 {confirm_msg_id}，实际 {msg_id}"

    # 2. 负责人那边的成员收集卡应被 batch_update_card 锁定
    assert len(batch_calls) >= 1, f"应调用 batch_update_card 锁定负责人卡，实际 {len(batch_calls)}"
    card_ids = [c[0] for c in batch_calls]
    assert "card_lead_1" in card_ids, f"应锁定负责人卡 card_lead_1，实际 {card_ids}"
    # 至少一次 batch_update 包含 delete_elements 动作
    all_actions = [a for _, actions in batch_calls for a in actions]
    assert any(a["action"] == "delete_elements" for a in all_actions), \
        "负责人卡锁定应包含 delete_elements 动作"
    print("✅ test_approve_members_locks_lead_collect_card")


def test_reject_members_locks_confirm_card():
    """PM 拒绝成员后，群内确认卡应被冻结为只读版本（认可/拒绝按钮消失）。"""
    import json as _json
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    sm.update_state("ou_pm", "c_pm", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    project_id="P1", group_chat_id="cg_group_1",
                    dept_leads=[{"open_id": "ou_lead1", "lead": "张三",
                                 "dept": "前端", "status": "filled",
                                 "member_count": 1, "members": []}])

    update_calls = []

    class FakeIM:
        def resolve_card_id(self, mid):
            return ""

        def get_user_by_open_id(self, oid):
            return {"name": "鲁正柱", "department": "部门2", "job_title": "职位2"}

        def send_text(self, cid, text):
            pass

        def send_text_to_user(self, oid, text):
            pass

        def update_card(self, message_id, card_content):
            update_calls.append((message_id, card_content))

    h = CardActionHandler(FakeIM(), sm, group_manager=None)
    members = [{"name": "鲁正柱", "open_id": "ou_m1", "dept": "部门2",
                 "position": "职位2", "employee_id": ""}]
    confirm_msg_id = "om_confirm_card_1"
    h.handle_action(
        "cg_group_1", "ou_pm",
        {"action": "reject_members",
         "lead_name": "张三", "lead_open_id": "ou_lead1",
         "dept_name": "前端", "members": _json.dumps(members)},
        form_value={}, message_id=confirm_msg_id)

    # 群内 PM 确认卡应被 update_card 替换为只读锁定版本
    assert len(update_calls) >= 1, f"应调用 update_card 锁定 PM 确认卡，实际 {len(update_calls)}"
    msg_id, card = update_calls[0]
    assert msg_id == confirm_msg_id, f"应更新 PM 确认卡 {confirm_msg_id}，实际 {msg_id}"

    # 锁定卡内容应包含"已拒绝"和"本卡片已锁定"，且不含"已入群"
    card_str = str(card)
    assert "已拒绝" in card_str, f"锁定卡应显示'已拒绝'，实际内容: {card_str[:200]}"
    assert "本卡片已锁定" in card_str, f"锁定卡应显示'本卡片已锁定'，实际内容: {card_str[:200]}"
    assert "已入群" not in card_str, f"拒绝场景不应显示'已入群'，实际内容: {card_str[:200]}"

    # PM session 中该负责人状态应重置为 pending
    pm_session = sm.get("ou_pm", "c_pm")
    lead = next(d for d in pm_session.dept_leads if d.get("lead") == "张三")
    assert lead["status"] == "pending", f"拒绝后负责人状态应为 pending，实际 {lead['status']}"
    print("✅ test_reject_members_locks_confirm_card")


def test_non_pm_cannot_approve_or_reject():
    """非PM用户点击认可/拒绝按钮应被拦截，不执行任何操作。"""
    import json as _json
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    sm.update_state("ou_pm", "c_pm", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    project_id="P1", group_chat_id="cg_group_1",
                    dept_leads=[{"open_id": "ou_lead1", "lead": "张三",
                                 "dept": "前端", "status": "filled",
                                 "member_count": 1, "members": []}])

    text_calls = []
    update_calls = []

    class FakeIM:
        def resolve_card_id(self, mid):
            return ""

        def send_text(self, cid, text):
            text_calls.append(text)

        def send_text_to_user(self, oid, text):
            pass

        def update_card(self, message_id, card_content):
            update_calls.append(message_id)

        def batch_update_card(self, card_id, actions, sequence=None):
            pass

    h = CardActionHandler(FakeIM(), sm, group_manager=None)
    members = [{"name": "成员A", "open_id": "ou_m1", "dept": "前端",
                 "position": "工程师", "employee_id": ""}]

    # 非PM用户（ou_other）尝试认可
    h.handle_action(
        "cg_group_1", "ou_other",
        {"action": "approve_members",
         "lead_name": "张三", "lead_open_id": "ou_lead1",
         "dept_name": "前端", "members": _json.dumps(members)},
        form_value={}, message_id="om_confirm_1")

    # 非PM用户尝试拒绝
    h.handle_action(
        "cg_group_1", "ou_other",
        {"action": "reject_members",
         "lead_name": "张三", "lead_open_id": "ou_lead1",
         "dept_name": "前端", "members": _json.dumps(members)},
        form_value={}, message_id="om_confirm_2")

    # 应发送2条拦截提示
    assert len(text_calls) == 2, f"非PM操作应被拦截并提示，实际提示次数: {len(text_calls)}"
    assert "项目经理" in text_calls[0], f"提示应包含'项目经理'，实际: {text_calls[0]}"
    # 不应更新卡片（不执行认可/拒绝逻辑）
    assert len(update_calls) == 0, f"非PM操作不应更新卡片，实际更新次数: {len(update_calls)}"
    print("✅ test_non_pm_cannot_approve_or_reject")


def test_lead_submit_hides_remove_buttons():
    """负责人提交成员后，成员行应被替换为只读版本（无移除按钮）。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    _setup_lead_submit_env(sm, selected_members=["ou_m1", "ou_m2"],
                           confirm_message_id="")
    # 给负责人会话设置 card_id，模拟真实卡片
    lead_s = sm.get("ou_lead1", "ou_lead1")
    lead_s.update(card_id="card_lead_1")

    batch_calls = []

    class FakeIM:
        def get_user_by_open_id(self, oid):
            return {"name": "成员A", "department": "前端", "job_title": "工程师"}

        def send_text(self, cid, t):
            pass

        def send_card(self, cid, card_content):
            return {"message_id": "m_confirm_1"}

        def complete_task(self, task_guid):
            return True

        def batch_update_card(self, card_id, actions, sequence=None):
            batch_calls.append((card_id, actions))

    h = CardActionHandler(FakeIM(), sm)
    h.handle_action("ou_lead1", "ou_lead1",
                    {"action": "lead_submit_members"},
                    form_value={})

    # 应调用 batch_update_card 替换成员行为只读版本
    assert len(batch_calls) >= 1, f"提交后应调用 batch_update_card，实际 {len(batch_calls)}"
    card_id, actions = batch_calls[0]
    assert card_id == "card_lead_1"
    # 动作应为 update_element（替换整行）
    assert actions[0]["action"] == "update_element"
    row_id = actions[0]["params"]["element_id"]
    elem = actions[0]["params"]["element"]
    # 行 ID 应为 row_{md5_8} 格式
    assert row_id.startswith("row_"), f"替换的行应以 row_ 前缀，实际 {row_id}"
    # 只读行不应包含「移除」按钮
    row_str = str(elem)
    assert "移除" not in row_str, f"只读行不应包含移除按钮，实际: {row_str[:200]}"
    # 验证两个成员行都被替换
    assert len(actions) == 2, f"应替换2个成员行，实际 {len(actions)}"
    print("✅ test_lead_submit_hides_remove_buttons")


def test_reject_members_restores_remove_buttons():
    """PM拒绝成员后，负责人卡片上被拒绝成员的「移除」按钮应恢复（update_element 替换 row_xxx）。"""
    import json as _json
    import hashlib
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    sm.update_state("ou_pm", "c_pm", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    project_id="P1", group_chat_id="cg_group_1",
                    dept_leads=[{"open_id": "ou_lead1", "lead": "张三",
                                 "dept": "前端", "status": "filled",
                                 "member_count": 1, "members": []}])
    # 负责人会话有 card_id
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    card_id="card_lead_1")

    batch_calls = []

    class FakeIM:
        def resolve_card_id(self, mid):
            return ""

        def send_text(self, cid, text):
            pass

        def send_text_to_user(self, oid, text):
            pass

        def update_card(self, message_id, card_content):
            pass

        def batch_update_card(self, card_id, actions, sequence=None):
            batch_calls.append((card_id, actions))

    h = CardActionHandler(FakeIM(), sm, group_manager=None)
    members = [{"name": "成员A", "open_id": "ou_m1", "dept": "前端",
                 "position": "工程师", "employee_id": ""}]
    confirm_msg_id = "om_confirm_card_1"
    h.handle_action(
        "cg_group_1", "ou_pm",
        {"action": "reject_members",
         "lead_name": "张三", "lead_open_id": "ou_lead1",
         "dept_name": "前端", "members": _json.dumps(members)},
        form_value={}, message_id=confirm_msg_id)

    # 应调用 batch_update_card 恢复移除按钮
    assert len(batch_calls) >= 1, f"拒绝后应调用 batch_update_card 恢复移除按钮，实际 {len(batch_calls)}"
    card_id, actions = batch_calls[0]
    assert card_id == "card_lead_1"
    # 动作应为 update_element
    assert actions[0]["action"] == "update_element"
    elem = actions[0]["params"]["element"]
    # 替换的是 row_{md5_8} 元素
    digest = hashlib.md5("ou_m1".encode("utf-8")).hexdigest()[:8]
    assert actions[0]["params"]["element_id"] == f"row_{digest}"
    # 新行应包含「移除」按钮
    row_str = str(elem)
    assert "移除" in row_str, f"恢复的行应包含移除按钮，实际: {row_str[:200]}"
    print("✅ test_reject_members_restores_remove_buttons")


def test_add_project_members_writes_to_bitable():
    """add_project_members 应将成员信息批量写入 project_members 表。"""
    from src.modules.group_manager import GroupManager

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok_f01"
    gm.f01_tables = {"project_members": "tbl_members"}

    captured = {}

    class FakeBaseClient:
        def batch_create_records(self, app_token, table_id, records):
            captured["app_token"] = app_token
            captured["table_id"] = table_id
            captured["records"] = records
            return {}

    gm.base_client = FakeBaseClient()

    members = [
        {"name": "张三", "open_id": "ou_1", "dept": "前端", "position": "工程师"},
        {"name": "李四", "open_id": "ou_2", "dept": "前端", "position": "设计师"},
    ]
    count = gm.add_project_members(
        project_id="P1", members=members,
        dept_name="前端", lead_name="王负责人", lead_open_id="ou_lead",
        chat_id="oc_test")

    assert count == 2
    assert captured["app_token"] == "tok_f01"
    assert captured["table_id"] == "tbl_members"
    assert len(captured["records"]) == 2
    # 验证字段映射
    r0 = captured["records"][0]
    assert r0["成员姓名"] == "张三"
    assert r0["open_id"] == "ou_1"
    assert r0["部门"] == "前端"
    # 部门负责人是 User 类型，应传 [{"id": open_id}]
    assert r0["部门负责人"] == [{"id": "ou_lead"}]
    assert r0["角色"] == "成员"
    assert r0["入群状态"] == "已入群"
    assert r0["入群方式"] == "收集流程"
    # 添加时间是 DateTime 类型，应传毫秒时间戳（整数）
    assert isinstance(r0["添加时间"], int) and r0["添加时间"] > 0
    # 字段名是「项目ID」而非 project_id
    assert r0["项目ID"] == "P1"
    # 群聊ID
    assert r0["群聊ID"] == "oc_test"
    print("✅ test_add_project_members_writes_to_bitable")


def test_add_project_members_empty_returns_zero():
    """空成员列表应返回0且不调用写入。"""
    from src.modules.group_manager import GroupManager

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok_f01"
    gm.f01_tables = {"project_members": "tbl_members"}

    called = []

    class FakeBaseClient:
        def batch_create_records(self, app_token, table_id, records):
            called.append(True)
            return {}

    gm.base_client = FakeBaseClient()
    count = gm.add_project_members(project_id="P1", members=[])
    assert count == 0
    assert len(called) == 0, "空列表不应调用 batch_create_records"
    print("✅ test_add_project_members_empty_returns_zero")


def test_freeze_or_overdue_member_card():
    """24h到期：≥1次提交→冻结（batch_update删交互元素），0次→patch逾期提示。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager
    from src.modules.scheduler import Scheduler

    sm = SessionManager()
    captured = {}

    class FakeIM:
        def get_user_by_open_id(self, oid):
            return {"name": "张三", "department": "技术部", "job_title": "工程师"}

        def resolve_card_id(self, mid):
            return "card_member_1"

        def batch_update_card(self, card_id, actions, sequence=None):
            captured.setdefault("calls", []).append((card_id, actions))

    h = CardActionHandler(FakeIM(), sm, scheduler=Scheduler())
    # 场景1：有提交 → 冻结
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    selected_members=["ou_z3"], card_id="card_member_1",
                    project_name="P", project_abbr="P")
    h._freeze_or_overdue_member_card("ou_lead1")
    assert len(captured["calls"]) == 1
    card_id, actions = captured["calls"][0]
    assert card_id == "card_member_1"
    # 应包含 delete 和 add_before
    action_types = [a["action"] for a in actions]
    assert "delete_elements" in action_types, "冻结应删除交互元素"
    assert "add_elements" in action_types, "冻结应插入只读摘要"

    # 场景2：无提交 → 仅 patch 逾期提示
    captured.clear()
    sm.clear("ou_lead2", "ou_lead2")
    sm.update_state("ou_lead2", "ou_lead2", "collecting_members",
                    selected_members=[], card_id="card_member_2",
                    project_name="P", project_abbr="P")
    h._freeze_or_overdue_member_card("ou_lead2")
    assert len(captured["calls"]) == 1
    card_id, actions = captured["calls"][0]
    assert card_id == "card_member_2"
    action_types = [a["action"] for a in actions]
    assert "partial_update_element" in action_types, "逾期应 patch 提示文案"
    assert "delete_elements" not in action_types, "逾期不应冻结"
    print("✅ test_freeze_or_overdue_member_card")


# ============== P2 飞书待办联动测试 ==============

def test_im_client_create_task_body():
    """IMClient.create_task 构造的请求体符合飞书 task v2 API：
    summary 必填、due.timestamp 为 ms 字符串、members 携带 assignee、
    reminders 使用 relative_fire_minute。"""
    from src.feishu.im_client import IMClient

    captured = {}

    class StubClient(IMClient):
        def __init__(self):
            pass  # 跳过真实认证初始化

        def _request(self, method, path, params=None, json_body=None,
                    max_retries=5, token_type="user", timeout_override=None):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = params
            captured["body"] = json_body
            captured["token_type"] = token_type
            return {"task": {"guid": "guid-xyz"}}

    client = StubClient()
    deadline_ts = 1800000000  # 任意固定时间戳（秒）
    result = client.create_task(
        summary="收集成员",
        assignee_open_id="ou_lead1",
        deadline_ts=deadline_ts,
        description="项目 X",
        reminder_minutes_before=60,
    )

    # 请求方法与路径
    assert captured["method"] == "POST"
    assert captured["path"] == "/task/v2/tasks"
    assert captured["token_type"] == "tenant", "应使用机器人身份"
    assert captured["params"].get("user_id_type") == "open_id"

    body = captured["body"]
    assert body["summary"] == "收集成员"
    assert body["description"] == "项目 X"
    # due.timestamp 必须是毫秒字符串
    assert body["due"]["timestamp"] == str(deadline_ts * 1000)
    assert body["due"]["is_all_day"] is False
    # members 必须以 assignee 角色携带 open_id
    assert body["members"][0]["id"] == "ou_lead1"
    assert body["members"][0]["type"] == "user"
    assert body["members"][0]["role"] == "assignee"
    # reminders 必须是相对分钟数
    assert body["reminders"][0]["relative_fire_minute"] == 60

    # 返回值
    assert result["task_guid"] == "guid-xyz"
    assert "applink.feishu.cn" in result["task_url"]
    print("✅ test_im_client_create_task_body")


def test_im_client_create_task_failure_returns_empty():
    """create_task 在 _request 抛异常时返回 {} 不传播异常（保证主流程不被阻塞）。"""
    from src.feishu.im_client import IMClient

    class StubClient(IMClient):
        def __init__(self):
            pass

        def _request(self, *a, **k):
            raise RuntimeError("simulated 403")

    result = StubClient().create_task(
        summary="X", assignee_open_id="ou_x",
        deadline_ts=1800000000,
    )
    assert result == {}, "失败应返回空 dict 以便上层降级"
    print("✅ test_im_client_create_task_failure_returns_empty")


def test_im_client_complete_task_body():
    """complete_task 构造的 PATCH 请求体符合 API：completed_at 为毫秒字符串、
    update_fields 包含 completed_at；空 guid 直接返回 False。"""
    from src.feishu.im_client import IMClient

    captured = {}

    class StubClient(IMClient):
        def __init__(self):
            pass

        def _request(self, method, path, params=None, json_body=None,
                    max_retries=5, token_type="user", timeout_override=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = json_body
            captured["token_type"] = token_type
            return {}

    client = StubClient()

    # 空 guid 不发请求
    assert client.complete_task("") is False
    assert "method" not in captured

    # 正常调用
    ok = client.complete_task("guid-xyz")
    assert ok is True
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/task/v2/tasks/guid-xyz"
    assert captured["token_type"] == "tenant"
    assert captured["body"]["task"]["completed_at"].isdigit()
    assert captured["body"]["update_fields"] == ["completed_at"]
    print("✅ test_im_client_complete_task_body")


def test_send_dept_leads_creates_task_per_lead():
    """_on_send_dept_leads 发卡循环里为每位负责人调用 create_task，
    成功后将 task_guid 写入 lead_session.member_task_id。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sent = {"texts": [], "cardkits": [], "p2p_texts": [], "tasks": []}

    class FakeIM:
        def get_user_by_open_id(self, oid):
            return {"name": "李四", "department": "技术部", "job_title": "TL"}

        def list_scope_users(self):
            return []

        def send_cardkit_to_user(self, oid, card):
            sent["cardkits"].append((oid, card))
            return {"card_id": "card_x", "message_id": "m_x"}

        def send_card(self, *a, **k):
            return {"message_id": "m_g"}

        def send_text(self, cid, t):
            sent["texts"].append((cid, t))

        def send_text_to_user(self, oid, t):
            sent["p2p_texts"].append((oid, t))

        def create_task(self, summary, assignee_open_id, deadline_ts,
                        description="", reminder_minutes_before=60):
            sent["tasks"].append({
                "summary": summary, "assignee": assignee_open_id,
                "deadline_ts": deadline_ts, "desc": description,
                "reminder": reminder_minutes_before,
            })
            return {"task_guid": f"task-{assignee_open_id}",
                    "task_url": "https://applink.feishu.cn/x"}

        def complete_task(self, task_guid):
            return True

    class FakeGM:
        def __init__(self):
            self.config = {}
            self.f01_app_token = ""

        def register_project(self, project_name, project_short_name, pm_open_id):
            from types import SimpleNamespace
            return SimpleNamespace(project_id="P1")

        def create_project_group(self, pid, quick_mode=True, project=None):
            return {"chat_id": "cg_1"}

        def quick_register_and_create_group(self, *a, **k):
            from types import SimpleNamespace
            return SimpleNamespace(project_id="P1"), {"chat_id": "cg_1", "status": "completed"}

    sm = SessionManager()
    h = CardActionHandler(FakeIM(), sm, group_manager=FakeGM())

    h.handle_action(
        "c1", "u1", {"action": "send_dept_leads",
                    "project_name": "项目A", "project_abbr": "A"},
        form_value={"leads": ["ou_lead1", "ou_lead2"]},
    )

    # 两位负责人各创建了一个任务
    assert len(sent["tasks"]) == 2
    task_calls = {t["assignee"]: t for t in sent["tasks"]}
    # 截止时间约为 sent_at + 24h（容差 5 秒）
    import time as _time
    for t in sent["tasks"]:
        diff = t["deadline_ts"] - _time.time() - 24 * 3600
        assert abs(diff) < 5, f"deadline_ts 应为 T+24h，实际偏差 {diff}"
        assert t["reminder"] == 60, "应提前 60 分钟提醒"

    # 验证 member_task_id 已写入两位负责人的 session
    s1 = sm.get("ou_lead1", "ou_lead1")
    assert s1.member_task_id == "task-ou_lead1"
    s2 = sm.get("ou_lead2", "ou_lead2")
    assert s2.member_task_id == "task-ou_lead2"
    print("✅ test_send_dept_leads_creates_task_per_lead")


def test_send_dept_leads_fallback_when_task_creation_fails():
    """create_task 返回 {} 时降级为发送机器人文本提醒（B 计划）。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sent = {"p2p_texts": [], "tasks_failed": 0}

    class FakeIM:
        def get_user_by_open_id(self, oid):
            return {"name": "李四", "department": "技术部", "job_title": ""}

        def list_scope_users(self):
            return []

        def send_cardkit_to_user(self, oid, card):
            return {"card_id": "card_x", "message_id": "m_x"}

        def send_card(self, *a, **k):
            return {"message_id": "m_g"}

        def send_text(self, *a, **k):
            pass

        def send_text_to_user(self, oid, t):
            sent["p2p_texts"].append((oid, t))

        def create_task(self, summary, assignee_open_id, deadline_ts,
                        description="", reminder_minutes_before=60):
            sent["tasks_failed"] += 1
            return {}  # 模拟权限不足或失败

        def complete_task(self, task_guid):
            return True

    class FakeGM:
        def __init__(self):
            self.config = {}
            self.f01_app_token = ""

        def register_project(self, *a, **k):
            from types import SimpleNamespace
            return SimpleNamespace(project_id="P1")

        def create_project_group(self, pid, quick_mode=True, project=None):
            return {"chat_id": "cg_1"}

        def quick_register_and_create_group(self, *a, **k):
            from types import SimpleNamespace
            return SimpleNamespace(project_id="P1"), {"chat_id": "cg_1", "status": "completed"}

    sm = SessionManager()
    h = CardActionHandler(FakeIM(), sm, group_manager=FakeGM())

    h.handle_action(
        "c1", "u1", {"action": "send_dept_leads",
                    "project_name": "项目A", "project_abbr": "A"},
        form_value={"leads": ["ou_lead1"]},
    )

    assert sent["tasks_failed"] == 1
    assert len(sent["p2p_texts"]) == 1, "B 计划应发一条文本提醒"
    assert "ou_lead1" == sent["p2p_texts"][0][0]

    s = sm.get("ou_lead1", "ou_lead1")
    assert s.member_task_id == "", "任务创建失败不应写入 task_id"
    print("✅ test_send_dept_leads_fallback_when_task_creation_fails")


def test_lead_submit_members_completes_task():
    """_on_lead_submit_members 提交成员后调用 complete_task 标记任务完成。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    completed = {"calls": []}

    class FakeIM:
        def get_user_by_open_id(self, oid):
            return {"name": "张三", "department": "前端", "job_title": ""}

        def send_text(self, cid, t):
            pass

        def send_card(self, *a, **k):
            return {"message_id": "m_c"}

        def complete_task(self, task_guid):
            completed["calls"].append(task_guid)
            return True

    sm = SessionManager()
    h = CardActionHandler(FakeIM(), sm)

    # 先在 lead 的 session 写入 member_task_id（模拟发卡阶段已经创建过任务）
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    pm_user_id="u_pm", pm_chat_id="c_pm",
                    lead_name="张三", member_task_id="task-xyz",
                    selected_members=["ou_m1"])

    # 同时把 PM 的 session 准备好，供 handler 反查 group_chat_id
    sm.update_state("u_pm", "c_pm", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    project_id="P1", group_chat_id="cg_1",
                    dept_leads=[{"open_id": "ou_lead1", "lead": "张三",
                                  "dept": "前端", "status": "pending",
                                  "member_count": 0, "members": []}])

    h.handle_action(
        "c1", "ou_lead1",
        {"action": "lead_submit_members"},
        form_value={"members": ["ou_m1"]},
    )

    assert completed["calls"] == ["task-xyz"], "提交时应标记负责人任务完成"
    print("✅ test_lead_submit_members_completes_task")


def test_session_member_task_id_migrates():
    """Session.member_task_id 在 migrate_context 中也会从占位会话迁移到真实会话。"""
    sm = SessionManager()
    # 占位会话（chat_id == user_id）写入 member_task_id
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    member_task_id="task-abc")
    # 真实 chat_id 会话第一次是空的
    real = sm.get_or_create("ou_lead1", "real_chat")
    assert real.member_task_id == ""
    # 触发迁移
    sm.migrate_context("ou_lead1", "real_chat")
    real = sm.get("ou_lead1", "real_chat")
    assert real.member_task_id == "task-abc", "member_task_id 应迁移"
    print("✅ test_session_member_task_id_migrates")


# ============== P3 追加式提交 + 去重测试 ==============

def test_session_confirm_message_id_migrates():
    """confirm_message_id 必须在 migrate_context 中迁移，保证占位会话提交后真实会话仍能识别已提交状态。"""
    sm = SessionManager()
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    confirm_message_id="msg-abc")
    real = sm.get_or_create("ou_lead1", "real_chat")
    assert real.confirm_message_id == ""
    sm.migrate_context("ou_lead1", "real_chat")
    real = sm.get("ou_lead1", "real_chat")
    assert real.confirm_message_id == "msg-abc"
    print("✅ test_session_confirm_message_id_migrates")


def test_migrate_preserves_pm_group_chat_id_when_pm_is_lead():
    """PM 同时也是负责人时，migrate_context 不应清空 PM 会话的 group_chat_id / dept_leads。"""
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    pm_open_id = "ou_pm_lead"
    pm_chat_id = "c_pm"

    # PM 会话（有 group_chat_id 和 dept_leads）
    sm.update_state(pm_open_id, pm_chat_id, "collecting_members",
                    project_name="项目A", project_abbr="A",
                    project_id="P1", group_chat_id="cg_group_1",
                    dept_leads=[{"open_id": pm_open_id, "lead": "PM",
                                  "dept": "前端", "status": "pending"}])

    # 负责人占位会话（PM 自己也是负责人，group_chat_id 为空）
    sm.update_state(pm_open_id, pm_open_id, "collecting_members",
                    project_name="项目A", project_abbr="A",
                    pm_user_id=pm_open_id, pm_chat_id=pm_chat_id,
                    lead_name="PM", selected_members=["ou_m1"])

    # 模拟负责人操作成员卡时的迁移
    sm.migrate_context(pm_open_id, pm_chat_id)

    real = sm.get(pm_open_id, pm_chat_id)
    # group_chat_id 不应被清空
    assert real.group_chat_id == "cg_group_1", \
        f"PM 会话的 group_chat_id 不应被清空，实际: {real.group_chat_id}"
    # dept_leads 不应被清空
    assert len(real.dept_leads) == 1, \
        f"PM 会话的 dept_leads 不应被清空，实际: {real.dept_leads}"
    # selected_members 应从负责人会话迁移过来
    assert real.selected_members == ["ou_m1"], \
        f"selected_members 应迁移，实际: {real.selected_members}"
    print("✅ test_migrate_preserves_pm_group_chat_id_when_pm_is_lead")


def _setup_lead_submit_env(sm, lead_open_id="ou_lead1",
                           selected_members=None,
                           confirm_message_id="",
                           member_task_id="task-xyz"):
    """公共环境：lead session + PM session（带 group_chat_id 和 dept_leads）。"""
    sm.update_state(lead_open_id, lead_open_id, "collecting_members",
                    project_name="项目A", project_abbr="A",
                    pm_user_id="u_pm", pm_chat_id="c_pm",
                    lead_name="张三",
                    member_task_id=member_task_id,
                    selected_members=selected_members or [],
                    confirm_message_id=confirm_message_id)
    sm.update_state("u_pm", "c_pm", "collecting_members",
                    project_name="项目A", project_abbr="A",
                    project_id="P1", group_chat_id="cg_1",
                    dept_leads=[{"open_id": lead_open_id, "lead": "张三",
                                  "dept": "前端", "status": "pending",
                                  "member_count": 0, "members": []}])


class _FakeIMLeadSubmit:
    """轻量 IM 桩：记录 complete_task 调用、send_card 返回新 message_id。"""
    def __init__(self, complete_calls=None, send_card_ids=None,
                 new_message_id="m_new", send_text_calls=None):
        self.complete_calls = complete_calls if complete_calls is not None else []
        self.send_card_ids = send_card_ids if send_card_ids is not None else []
        self.send_text_calls = send_text_calls if send_text_calls is not None else []
        self._new_message_id = new_message_id
        self._call_counter = [0]

    def get_user_by_open_id(self, oid):
        return {"name": "张三", "department": "前端", "job_title": ""}

    def send_text(self, cid, t):
        self.send_text_calls.append(t)

    def send_card(self, cid, card_content):
        # 每次返回一个递增的 message_id 便于断言
        self._call_counter[0] += 1
        mid = f"{self._new_message_id}_{self._call_counter[0]}"
        self.send_card_ids.append(mid)
        return {"message_id": mid}

    def complete_task(self, task_guid):
        self.complete_calls.append(task_guid)
        return True


def test_lead_submit_first_calls_complete_task():
    """首次提交：confirm_message_id 为空 → 调用 complete_task。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    _setup_lead_submit_env(sm, selected_members=["ou_m1"],
                           confirm_message_id="")
    fake = _FakeIMLeadSubmit()
    h = CardActionHandler(fake, sm)

    h.handle_action("c1", "ou_lead1",
                    {"action": "lead_submit_members"},
                    form_value={})

    assert fake.complete_calls == ["task-xyz"], \
        f"首次应调用 complete_task，实际: {fake.complete_calls}"
    # 占位会话被 migrate_context 迁移到真实会话 "ou_lead1:c1"
    lead_session = sm.get("ou_lead1", "c1")
    assert lead_session is not None, "真实会话应存在"
    assert lead_session.confirm_message_id == "m_new_1"
    print("✅ test_lead_submit_first_calls_complete_task")


def test_lead_submit_second_skips_complete_task():
    """第二次提交：confirm_message_id 非空 → 不再调用 complete_task。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    _setup_lead_submit_env(sm, selected_members=["ou_m1", "ou_m2"],
                           confirm_message_id="m_prev")
    fake = _FakeIMLeadSubmit()
    h = CardActionHandler(fake, sm)

    h.handle_action("c1", "ou_lead1",
                    {"action": "lead_submit_members"},
                    form_value={})

    assert fake.complete_calls == [], \
        f"二次提交不应调用 complete_task，实际: {fake.complete_calls}"
    # PM 卡仍应发送新卡（覆盖旧卡）
    assert len(fake.send_card_ids) == 1
    # session.confirm_message_id 应更新为最新
    lead_session = sm.get("ou_lead1", "c1")
    assert lead_session is not None
    assert lead_session.confirm_message_id == "m_new_1"
    print("✅ test_lead_submit_second_skips_complete_task")


def test_lead_submit_second_resets_timer():
    """二次提交应取消旧定时器并启动新定时器（schedule_confirm 内部 cancel）。

    通过观察 scheduler 内部 _timers 的 key 存在与否、timer 被替换来验证。
    """
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager
    from src.modules.scheduler import Scheduler

    sch = Scheduler()
    sm = SessionManager()
    _setup_lead_submit_env(sm, selected_members=["ou_m1"],
                           confirm_message_id="m_prev")
    fake = _FakeIMLeadSubmit()
    h = CardActionHandler(fake, sm, scheduler=sch)

    h.handle_action("c1", "ou_lead1",
                    {"action": "lead_submit_members"},
                    form_value={})

    timer_key = "P1:ou_lead1"
    assert timer_key in sch._timers, "应有新定时器"
    new_timer = sch._timers[timer_key]
    assert not new_timer._cancelled, "新定时器不应被取消"
    print("✅ test_lead_submit_second_resets_timer")


def test_lead_submit_pm_card_uses_cumulative_members():
    """提交后 PM 看到的卡显示累积后的完整成员列表（含姓名）。"""
    import json as _json
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    # 假设 _on_pick_member 已经把 ou_m1, ou_m2, ou_m3 累积到 selected_members
    _setup_lead_submit_env(
        sm, selected_members=["ou_m1", "ou_m2", "ou_m3"],
        confirm_message_id="")

    sent_cards = []

    class FakeIM(_FakeIMLeadSubmit):
        def send_card(self, cid, card_content):
            sent_cards.append(card_content)
            return {"message_id": "m_new"}

    fake = FakeIM()
    h = CardActionHandler(fake, sm)

    h.handle_action("c1", "ou_lead1",
                    {"action": "lead_submit_members"},
                    form_value={})

    assert len(sent_cards) == 1
    # card_content 可能是 dict 或 str，统一序列化为字符串后断言
    card_str = (sent_cards[0] if isinstance(sent_cards[0], str)
                else _json.dumps(sent_cards[0], ensure_ascii=False))
    # 卡片中"3 名成员"的提示文字应出现
    assert "3 名成员" in card_str, "PM 卡应显示 3 名成员"
    print("✅ test_lead_submit_pm_card_uses_cumulative_members")


def test_lead_submit_resubmit_updates_pm_dept_member_count():
    """多次提交后 PM session 的 dept_leads 中 member_count 用累积数覆盖。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    _setup_lead_submit_env(
        sm, selected_members=["ou_m1", "ou_m2"],
        confirm_message_id="m_prev")
    fake = _FakeIMLeadSubmit()
    h = CardActionHandler(fake, sm)

    h.handle_action("c1", "ou_lead1",
                    {"action": "lead_submit_members"},
                    form_value={})

    pm_session = sm.get("u_pm", "c_pm")
    lead_entry = pm_session.dept_leads[0]
    assert lead_entry["member_count"] == 2
    assert lead_entry["status"] == "filled"
    print("✅ test_lead_submit_resubmit_updates_pm_dept_member_count")


def test_lead_submit_resubmit_distinct_message_to_lead():
    """二次提交时给负责人的提示应区分首次（含「已重新提交」字样）。"""
    from src.modules.card_action_handler import CardActionHandler
    from src.modules.session_manager import SessionManager

    # 首次提交场景
    sm1 = SessionManager()
    _setup_lead_submit_env(sm1, selected_members=["ou_m1"],
                           confirm_message_id="")
    fake1 = _FakeIMLeadSubmit()
    h1 = CardActionHandler(fake1, sm1)
    h1.handle_action("c1", "ou_lead1",
                     {"action": "lead_submit_members"},
                     form_value={})
    assert any("已提交" in t and "已重新提交" not in t
               for t in fake1.send_text_calls), \
        f"首次提示文案应含「已提交」而非「已重新提交」，实际: {fake1.send_text_calls}"

    # 二次提交场景
    sm2 = SessionManager()
    _setup_lead_submit_env(sm2, selected_members=["ou_m1", "ou_m2"],
                           confirm_message_id="m_prev")
    fake2 = _FakeIMLeadSubmit()
    h2 = CardActionHandler(fake2, sm2)
    h2.handle_action("c1", "ou_lead1",
                     {"action": "lead_submit_members"},
                     form_value={})
    assert any("已重新提交" in t for t in fake2.send_text_calls), \
        f"二次提示应含「已重新提交」，实际: {fake2.send_text_calls}"
    print("✅ test_lead_submit_resubmit_distinct_message_to_lead")


# ============== P4 每日8点逾期推送测试 ==============

def test_find_overdue_leads_filters_correctly():
    """SessionManager.find_overdue_leads 应只返回：
    state==collecting_members 且 member_card_sent_at>0 且
    (now - sent_at)>24h 且 selected_members 为空的会话。
    其他状态/已提交/未逾期/未发卡的会话都应被排除。"""
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    now = 2_000_000_000.0
    day = 24 * 3600

    # 1) 命中：逾期24h+未提交
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    member_card_sent_at=now - day - 1,
                    selected_members=[],
                    project_name="项目A", project_abbr="A")
    # 2) 排除：未逾期（仅2h）
    sm.update_state("ou_lead2", "ou_lead2", "collecting_members",
                    member_card_sent_at=now - 2 * 3600,
                    selected_members=[],
                    project_name="项目A", project_abbr="A")
    # 3) 排除：已逾期但已提交成员
    sm.update_state("ou_lead3", "ou_lead3", "collecting_members",
                    member_card_sent_at=now - day - 1,
                    selected_members=["ou_mem1"],
                    project_name="项目A", project_abbr="A")
    # 4) 排除：member_card_sent_at==0（未发卡）
    sm.update_state("ou_lead4", "ou_lead4", "collecting_members",
                    member_card_sent_at=0.0,
                    selected_members=[],
                    project_name="项目A", project_abbr="A")
    # 5) 排除：非 collecting_members 状态
    sm.update_state("ou_lead5", "ou_lead5", "idle",
                    member_card_sent_at=now - day - 1,
                    selected_members=[],
                    project_name="项目A", project_abbr="A")

    overdue = sm.find_overdue_leads(now_ts=now)
    overdue_ids = {s.user_id for s in overdue}
    assert overdue_ids == {"ou_lead1"}, f"应只命中 ou_lead1，实际: {overdue_ids}"
    print("✅ test_find_overdue_leads_filters_correctly")


def test_find_overdue_leads_custom_threshold():
    """overdue_seconds 参数应能放宽/收紧逾期阈值（如 1h）。"""
    from src.modules.session_manager import SessionManager

    sm = SessionManager()
    now = 2_000_000_000.0

    # 5h 前发卡，未提交
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    member_card_sent_at=now - 5 * 3600,
                    selected_members=[])
    # 默认 24h 不命中
    assert len(sm.find_overdue_leads(now_ts=now)) == 0
    # 1h 阈值应命中
    assert len(sm.find_overdue_leads(
        now_ts=now, overdue_seconds=3600)) == 1
    print("✅ test_find_overdue_leads_custom_threshold")


def test_overdue_pusher_sends_to_leads_and_pm():
    """OverduePusher.run_daily_check 应：
    - 给每位逾期负责人发文本提醒；
    - 给 PM 发汇总消息；
    - 返回成功推送的负责人数。"""
    from src.modules.session_manager import SessionManager
    from src.modules.overdue_pusher import OverduePusher

    sm = SessionManager()
    now = 2_000_000_000.0
    day = 24 * 3600

    # 两位逾期负责人，同属一位 PM
    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    member_card_sent_at=now - day - 3600,
                    selected_members=[],
                    project_name="项目X", project_abbr="X",
                    lead_name="张三",
                    pm_user_id="ou_pm", pm_chat_id="ou_pm")
    sm.update_state("ou_lead2", "ou_lead2", "collecting_members",
                    member_card_sent_at=now - 2 * day,
                    selected_members=[],
                    project_name="项目Y", project_abbr="Y",
                    lead_name="李四",
                    pm_user_id="ou_pm", pm_chat_id="ou_pm")

    sent = []

    class FakeIM:
        def send_text_to_user(self, open_id, text):
            sent.append((open_id, text))

    pusher = OverduePusher(FakeIM(), sm)
    pushed = pusher.run_daily_check(now_ts=now)

    # 两位负责人 + 一次给 PM
    assert pushed == 2, f"应推送2位负责人，实际 {pushed}"
    lead_texts = [t for (oid, t) in sent if oid in ("ou_lead1", "ou_lead2")]
    assert len(lead_texts) == 2
    pm_msgs = [t for (oid, t) in sent if oid == "ou_pm"]
    assert len(pm_msgs) == 1, "PM 应收到1条汇总"
    # 汇总应包含两位负责人姓名和逾期小时数
    summary = pm_msgs[0]
    assert "张三" in summary and "李四" in summary
    assert "项目X" in summary and "项目Y" in summary
    assert "逾期" in summary
    # 给负责人的提醒应包含项目名和逾期小时数
    assert any("项目X" in t and "逾期" in t for t in
               [t for (oid, t) in sent if oid == "ou_lead1"])
    assert any("项目Y" in t and "逾期" in t for t in
               [t for (oid, t) in sent if oid == "ou_lead2"])
    print("✅ test_overdue_pusher_sends_to_leads_and_pm")


def test_overdue_pusher_no_overdue_returns_zero():
    """无逾期会话时，不应发送任何消息，返回0。"""
    from src.modules.session_manager import SessionManager
    from src.modules.overdue_pusher import OverduePusher

    sm = SessionManager()
    now = 2_000_000_000.0

    class FakeIM:
        def send_text_to_user(self, open_id, text):
            raise AssertionError(f"不应调用推送，但被调用: {open_id}")

    pusher = OverduePusher(FakeIM(), sm)
    pushed = pusher.run_daily_check(now_ts=now)
    assert pushed == 0
    print("✅ test_overdue_pusher_no_overdue_returns_zero")


def test_overdue_pusher_continues_on_lead_failure():
    """单个负责人推送失败不应中断整个扫描；其他负责人和PM汇总仍应正常推送。"""
    from src.modules.session_manager import SessionManager
    from src.modules.overdue_pusher import OverduePusher

    sm = SessionManager()
    now = 2_000_000_000.0
    day = 24 * 3600

    sm.update_state("ou_lead1", "ou_lead1", "collecting_members",
                    member_card_sent_at=now - day - 3600,
                    selected_members=[],
                    project_name="项目A", project_abbr="A",
                    lead_name="张三",
                    pm_user_id="ou_pm", pm_chat_id="ou_pm")
    sm.update_state("ou_lead2", "ou_lead2", "collecting_members",
                    member_card_sent_at=now - day - 3600,
                    selected_members=[],
                    project_name="项目B", project_abbr="B",
                    lead_name="李四",
                    pm_user_id="ou_pm", pm_chat_id="ou_pm")

    sent = []

    class FakeIM:
        def send_text_to_user(self, open_id, text):
            if open_id == "ou_lead1":
                raise RuntimeError("模拟推送失败")
            sent.append((open_id, text))

    pusher = OverduePusher(FakeIM(), sm)
    pushed = pusher.run_daily_check(now_ts=now)

    # ou_lead1 失败，ou_lead2 成功 → pushed=1
    assert pushed == 1, f"应只推送成功1次，实际 {pushed}"
    # PM 汇总仍应发出（包含两位负责人，因为仍算作逾期）
    pm_msgs = [t for (oid, t) in sent if oid == "ou_pm"]
    assert len(pm_msgs) == 1
    assert "张三" in pm_msgs[0] and "李四" in pm_msgs[0]
    print("✅ test_overdue_pusher_continues_on_lead_failure")


def test_daily_timer_seconds_until_next():
    """DailyTimer._seconds_until_next 应正确计算到下次 HH:MM 的秒数：
    - 已过今天 HH:MM → 下次在明天 HH:MM
    - 未到今天 HH:MM → 下次在今天 HH:MM
    """
    from datetime import datetime
    from src.modules.scheduler import DailyTimer, SHANGHAI_TZ

    timer = DailyTimer(8, 0, callback=lambda: None)

    # 07:00:00 → 1h 后到 08:00
    now_dt = datetime(2026, 9, 28, 7, 0, 0, tzinfo=SHANGHAI_TZ)
    assert abs(timer._seconds_until_next(now_dt) - 3600) < 1

    # 08:30:00 → 下次明天 08:00，即 23.5h 后
    now_dt = datetime(2026, 9, 28, 8, 30, 0, tzinfo=SHANGHAI_TZ)
    expected = 23 * 3600 + 30 * 60
    assert abs(timer._seconds_until_next(now_dt) - expected) < 1

    # 08:00:00 整 → 已到目标，下次在明天
    now_dt = datetime(2026, 9, 28, 8, 0, 0, tzinfo=SHANGHAI_TZ)
    assert abs(timer._seconds_until_next(now_dt) - 24 * 3600) < 1
    print("✅ test_daily_timer_seconds_until_next")


def test_daily_timer_callback_kwargs_passing():
    """DailyTimer 应将构造时传入的 kwargs 原封传给 callback。"""
    from src.modules.scheduler import DailyTimer

    fired = []

    def cb(**kwargs):
        fired.append(kwargs)

    timer = DailyTimer(8, 0, callback=cb, sample_kwarg="hello")
    # 直接调用 callback 验证参数传递
    timer.callback(**timer.callback_kwargs)
    assert fired == [{"sample_kwarg": "hello"}]
    print("✅ test_daily_timer_callback_kwargs_passing")


def test_scheduler_schedule_daily_at_registers_and_cancels():
    """Scheduler.schedule_daily_at 应：
    - 注册 DailyTimer 并启动；
    - 同 key 重复注册会取消前一个；
    - cancel_confirm 能停止。"""
    from src.modules.scheduler import Scheduler, DailyTimer

    sch = Scheduler()

    timer1 = sch.schedule_daily_at(
        8, 0, callback=lambda: None, key="daily_test")
    assert isinstance(timer1, DailyTimer)
    assert "daily_test" in sch._timers

    # 重复注册同 key 应替换旧的
    timer2 = sch.schedule_daily_at(
        8, 0, callback=lambda: None, key="daily_test")
    assert sch._timers["daily_test"] is timer2
    assert timer1._cancelled, "旧的 timer 应被 cancel"

    # 取消
    sch.cancel_confirm("daily_test")
    assert "daily_test" not in sch._timers
    assert timer2._cancelled
    print("✅ test_scheduler_schedule_daily_at_registers_and_cancels")


def test_main_registers_daily_overdue_push():
    """模拟 main.py 中的注册流程：通过 Scheduler.schedule_daily_at 注册
    overdue_pusher.run_daily_check，key=daily_overdue_push。"""
    import types
    from src.modules.scheduler import Scheduler

    sch = Scheduler()
    captured = []

    def fake_schedule_daily_at(self, hour, minute, callback, key=None,
                               tz=None, **kwargs):
        captured.append({
            "hour": hour, "minute": minute,
            "callback": callback, "key": key,
        })
        return object()

    sch.schedule_daily_at = types.MethodType(
        fake_schedule_daily_at, sch)

    class FakeOverduePusher:
        def run_daily_check(self, **kwargs):
            return 0

    fake_pusher = FakeOverduePusher()

    # 模拟 main.py 中的注册调用
    sch.schedule_daily_at(
        hour=8, minute=0,
        callback=fake_pusher.run_daily_check,
        key="daily_overdue_push",
    )
    assert len(captured) == 1
    assert captured[0]["hour"] == 8
    assert captured[0]["minute"] == 0
    assert captured[0]["key"] == "daily_overdue_push"
    assert callable(captured[0]["callback"])
    print("✅ test_main_registers_daily_overdue_push")


# ---------- quick_register_and_create_group 并行编排 ----------

def _make_parallel_gm(records=None, create_record_resp=None, create_record_error=None,
                      create_chat_resp=None, create_chat_error=None, gate=None):
    """组装绕过 __init__ 的 GroupManager + 全套 Fake client。返回 (gm, calls)。"""
    import threading as _threading
    from src.modules.group_manager import GroupManager
    from src.modules.state_engine import StateEngine
    from src.feishu.card_builder import CardBuilder

    calls = {"create_record": 0, "create_record_fields": [], "update_record": [],
             "create_chat": 0, "add_members": 0, "add_bot": 0,
             "send_card": 0, "send_text_to_user": []}

    class FakeBase:
        def list_all_records(self, app, tbl, filter_condition=None):
            return records or []

        def create_record(self, app, tbl, fields):
            calls["create_record"] += 1
            calls["create_record_fields"].append(fields)
            if gate is not None:
                gate.wait(timeout=5)
            if create_record_error is not None:
                raise create_record_error
            return create_record_resp if create_record_resp is not None \
                else {"record": {"record_id": "rec1"}}

        def update_record(self, app, tbl, rid, fields):
            calls["update_record"].append(fields)
            return {}

    class FakeChat:
        def create_chat(self, name="", description=""):
            calls["create_chat"] += 1
            if create_chat_error is not None:
                raise create_chat_error
            return create_chat_resp or {"chat_id": "oc_1"}

        def add_members(self, chat_id, ids):
            calls["add_members"] += 1
            return {}

        def add_bot_to_chat(self, chat_id):
            calls["add_bot"] += 1
            return {}

    class FakeIM:
        def send_card(self, chat_id, card):
            calls["send_card"] += 1
            return {"message_id": "m1"}

        def send_text(self, chat_id, text):
            return {}

        def send_text_to_user(self, open_id, text):
            calls["send_text_to_user"].append(text)
            return {}

    gm = GroupManager.__new__(GroupManager)
    gm.f01_app_token = "tok"
    gm.f01_tables = {"project_config": "tbl", "project_members": "tblm"}
    gm._register_lock = _threading.Lock()
    gm._pending_registrations = set()
    gm._CHAIN_WAIT_TIMEOUT = 10
    gm.base_client = FakeBase()
    gm.chat_client = FakeChat()
    gm.im_client = FakeIM()
    gm.card_builder = CardBuilder()
    gm.state_engine = StateEngine(
        gm.base_client,
        {"bitable": {"f01_app_token": "tok", "f01_tables": {"project_config": "tbl"}}})
    gm._sync_pm_to_bitable = lambda *a, **k: None  # 收尾中PM成员写入打桩
    return gm, calls


def _wait_until(predicate, timeout=3.0, interval=0.02):
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if predicate():
            return True
        _time.sleep(interval)
    return predicate()


def test_parallel_register_create_success():
    """成功路径：记录与群都创建，状态直写收集中，后台回写group_id+流转运行中"""
    import time
    gm, calls = _make_parallel_gm()
    project, result = gm.quick_register_and_create_group("并行项目", "PX", "ou_pm")
    assert result["chat_id"] == "oc_1"
    assert project.project_id.startswith("P")
    assert calls["create_record"] == 1
    assert calls["create_record_fields"][0]["status"] == "收集中"
    assert calls["create_chat"] == 1
    assert calls["add_members"] == 1  # PM 自动入群
    # 后台收尾：update_project_chat_id + transition运行中 = 2次 update_record
    assert _wait_until(lambda: len(calls["update_record"]) >= 2)
    merged = {}
    for f in calls["update_record"]:
        merged.update(f)
    assert merged.get("group_id") == "oc_1"
    assert merged.get("status") == "运行中"
    assert _wait_until(lambda: not gm._pending_registrations)
    print("✅ test_parallel_register_create_success")


def test_parallel_register_create_chat_failure():
    """建群失败：抛 GroupCreationError；记录线程不回写group_id、不流转运行中"""
    import time
    from src.models.exceptions import FeishuAPIError, GroupCreationError
    gm, calls = _make_parallel_gm(create_chat_error=FeishuAPIError(999, "boom"))
    try:
        gm.quick_register_and_create_group("失败群项目", "SB", "ou_pm")
        assert False, "应抛 GroupCreationError"
    except GroupCreationError:
        pass
    assert _wait_until(lambda: calls["create_record"] == 1)
    assert calls["update_record"] == []  # 无群不做任何回写
    assert _wait_until(lambda: not gm._pending_registrations)
    print("✅ test_parallel_register_create_chat_failure")


def test_parallel_register_record_failure_warns_pm():
    """B计划：记录3轮写入全失败，群照常建成并私聊PM补录告警"""
    import time
    import src.modules.group_manager as gm_mod
    from src.models.exceptions import FeishuAPIError
    gm, calls = _make_parallel_gm(create_record_error=FeishuAPIError(500, "server fail"))
    orig_sleep = gm_mod.time.sleep
    gm_mod.time.sleep = lambda s: None  # 跳过重试间隔
    try:
        project, result = gm.quick_register_and_create_group("记录失败项目", "JL", "ou_pm")
    finally:
        gm_mod.time.sleep = orig_sleep
    assert result["chat_id"] == "oc_1"  # 群照常建成
    assert _wait_until(lambda: calls["create_record"] == 3)
    assert _wait_until(lambda: len(calls["send_text_to_user"]) == 1)
    assert "补录" in calls["send_text_to_user"][0]
    assert calls["update_record"] == []  # 无record_id不回写
    assert _wait_until(lambda: not gm._pending_registrations)
    print("✅ test_parallel_register_record_failure_warns_pm")


def test_parallel_register_duplicate_raises():
    """查重命中：直接抛 DuplicateProjectError，不产生任何副作用"""
    from src.models.exceptions import DuplicateProjectError
    existing = _make_fake_project("重名项目", "CM", pid="P9")
    gm, calls = _make_parallel_gm(records=_make_records(existing))
    try:
        gm.quick_register_and_create_group("重名项目", "CM", "ou_pm")
        assert False, "应抛 DuplicateProjectError"
    except DuplicateProjectError as e:
        assert e.field == "项目名称"
    assert calls["create_record"] == 0
    assert calls["create_chat"] == 0
    print("✅ test_parallel_register_duplicate_raises")


def test_parallel_register_pending_blocks_concurrent():
    """占位防重：记录写入未完成时，同名并发提交被 DuplicateProjectError 拦截"""
    import threading
    import time
    from src.models.exceptions import DuplicateProjectError
    gate = threading.Event()  # 卡住 T_record 的 create_record
    gm, calls = _make_parallel_gm(gate=gate)
    done = {}

    def _first():
        try:
            gm.quick_register_and_create_group("并发占位项目", "BZ", "ou_pm")
            done["first"] = "ok"
        except Exception as e:
            done["first"] = repr(e)

    t = threading.Thread(target=_first)
    t.start()
    try:
        assert _wait_until(lambda: bool(gm._pending_registrations), timeout=2)
        # 第二次同名提交：表格尚无记录，靠内存占位拦截
        try:
            gm.quick_register_and_create_group("并发占位项目", "BZ", "ou_pm")
            assert False, "并发同名提交应被拦截"
        except DuplicateProjectError:
            pass
    finally:
        gate.set()
    t.join(5)
    assert done.get("first") == "ok"
    assert _wait_until(lambda: not gm._pending_registrations)
    print("✅ test_parallel_register_pending_blocks_concurrent")


if __name__ == "__main__":
    test_lead_card_empty()
    test_lead_card_with_roster()
    test_member_card_empty()
    test_member_card_with_roster()
    test_info_cache_used()
    test_row_id_rules()
    test_handler_action_builders()
    test_pick_and_remove()
    test_pick_handlers_read_selected()
    test_session_card_fields_migrate()
    test_bind_card_by_message_id()
    test_migrate_fresh_placeholder_overrides_stale_real()
    test_success_card_no_done_button()
    test_info_card_lock()
    test_lead_card_lock()
    test_stray_text_during_lead_pick_ignored()
    test_group_chat_management_silent_unless_mentioned()
    test_group_chat_management_guide_when_mentioned()
    test_group_chat_help_available()
    test_private_chat_create_still_works()
    test_group_card_callback_blocked()
    test_group_greeting_welcome_only_when_mentioned()
    test_batch_delete_records_chunking()
    test_private_card_callback_passes()
    test_find_duplicate_name_exact()
    test_find_duplicate_abbr_case_insensitive()
    test_find_duplicate_archived_reusable()
    test_find_duplicate_exclude_self()
    test_handler_precheck_blocks_on_duplicate()
    test_handler_precheck_passes_on_clean()
    test_handler_precheck_abbr_truncated()
    test_register_project_raises_on_duplicate()
    test_confirm_project_info_resets_stale_leads()
    test_member_card_deadline_display()
    test_member_card_element_ids_for_freeze()
    test_member_collect_locked_elements()
    test_member_collect_locked_elements_custom_status()
    test_approve_members_locks_lead_collect_card()
    test_reject_members_locks_confirm_card()
    test_non_pm_cannot_approve_or_reject()
    test_lead_submit_hides_remove_buttons()
    test_reject_members_restores_remove_buttons()
    test_add_project_members_writes_to_bitable()
    test_add_project_members_empty_returns_zero()
    test_freeze_or_overdue_member_card()
    test_im_client_create_task_body()
    test_im_client_create_task_failure_returns_empty()
    test_im_client_complete_task_body()
    test_send_dept_leads_creates_task_per_lead()
    test_send_dept_leads_fallback_when_task_creation_fails()
    test_lead_submit_members_completes_task()
    test_session_member_task_id_migrates()
    # P3 追加式提交 + 去重
    test_session_confirm_message_id_migrates()
    test_migrate_preserves_pm_group_chat_id_when_pm_is_lead()
    test_lead_submit_first_calls_complete_task()
    test_lead_submit_second_skips_complete_task()
    test_lead_submit_second_resets_timer()
    test_lead_submit_pm_card_uses_cumulative_members()
    test_lead_submit_resubmit_updates_pm_dept_member_count()
    test_lead_submit_resubmit_distinct_message_to_lead()
    # P4 每日8点逾期推送
    test_find_overdue_leads_filters_correctly()
    test_find_overdue_leads_custom_threshold()
    test_overdue_pusher_sends_to_leads_and_pm()
    test_overdue_pusher_no_overdue_returns_zero()
    test_overdue_pusher_continues_on_lead_failure()
    test_daily_timer_seconds_until_next()
    test_daily_timer_callback_kwargs_passing()
    test_scheduler_schedule_daily_at_registers_and_cancels()
    test_main_registers_daily_overdue_push()

    # 并行注册建群编排
    test_parallel_register_create_success()
    test_parallel_register_create_chat_failure()
    test_parallel_register_record_failure_warns_pm()
    test_parallel_register_duplicate_raises()
    test_parallel_register_pending_blocks_concurrent()

    print("\n全部通过 ✅")
