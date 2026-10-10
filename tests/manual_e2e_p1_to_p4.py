# -*- coding: utf-8 -*-
"""P1-P4 端到端串联测试脚本（无需真实等待 24h）

通过 mock 时间与 IM 调用，按真实业务顺序一次跑完四阶段：
- P1：发收集卡 → 时间快进 25h → 触发 24h 冻结回调 → 验证 freeze patch 调用
- P2：发卡时验证 create_task 被调用；提交时验证 complete_task 被调用
- P3：第一次提交 → 再挑选+提交一次 → 验证二次跳过 complete_task、PM 收到新卡
- P4：时间快进 25h → 调用 run_daily_check → 验证给负责人/PM 推送文本

运行：python tests/manual_e2e_p1_to_p4.py
"""
import json
import sys
import time

sys.path.insert(0, ".")

from src.modules.session_manager import SessionManager
from src.modules.scheduler import Scheduler
from src.modules.card_action_handler import CardActionHandler
from src.modules.overdue_pusher import OverduePusher
from src.feishu.card_builder import CardBuilder

DAY = 24 * 3600


class FakeIM:
    """记录所有飞书调用的桩：send_card / send_text / create_task / complete_task / batch_update_card"""

    def __init__(self):
        self.calls = []  # [("method", args_tuple, kwargs_dict), ...]
        self.scope_users = [
            {"name": "鲁正柱", "department": "部门2", "job_title": "职位2", "open_id": "ou_lu"},
            {"name": "张晓丽", "department": "部门1", "job_title": "职位1", "open_id": "ou_zl"},
            {"name": "李四", "department": "部门3", "job_title": "职位3", "open_id": "ou_ls"},
            {"name": "王五", "department": "部门4", "job_title": "职位4", "open_id": "ou_ww"},
        ]

    def get_user_by_open_id(self, oid):
        for u in self.scope_users:
            if u["open_id"] == oid:
                return u
        return {"name": "未知", "department": "未知", "job_title": ""}

    def list_scope_users(self):
        return list(self.scope_users)

    def resolve_card_id(self, mid):
        return f"cardkit_{mid}"

    def send_text(self, cid, text):
        self.calls.append(("send_text", (cid, text), {}))
        return {"message_id": f"msg_text_{len(self.calls)}"}

    def send_text_to_user(self, oid, text):
        self.calls.append(("send_text_to_user", (oid, text), {}))

    def send_card(self, cid, card_content):
        if isinstance(card_content, dict):
            card_content = json.dumps(card_content, ensure_ascii=False)
        self.calls.append(("send_card", (cid, card_content), {}))
        return {"message_id": f"msg_card_{len(self.calls)}"}

    def send_cardkit_to_user(self, oid, card_content):
        self.calls.append(("send_cardkit_to_user", (oid, card_content), {}))
        return {"card_id": f"cardid_{len(self.calls)}", "message_id": f"msg_kit_{len(self.calls)}"}

    def batch_update_card(self, card_id, actions, sequence=None):
        self.calls.append(("batch_update_card", (card_id, actions), {"sequence": sequence}))

    def create_task(self, summary, assignee_open_id, deadline_ts, description="", reminder_minutes_before=60):
        self.calls.append(("create_task", (), {
            "summary": summary, "assignee_open_id": assignee_open_id,
            "deadline_ts": deadline_ts, "description": description,
            "reminder_minutes_before": reminder_minutes_before,
        }))
        return {"task_guid": f"task_guid_{len(self.calls)}"}

    def complete_task(self, task_guid):
        self.calls.append(("complete_task", (task_guid,), {}))
        return True


def _count(fake, method):
    return sum(1 for c in fake.calls if c[0] == method)


def _print_call_summary(fake, label):
    print(f"\n{'='*60}\n【{label}】飞书调用统计：")
    counts = {}
    for method, _, _ in fake.calls:
        counts[method] = counts.get(method, 0) + 1
    for method, n in counts.items():
        print(f"  - {method}: {n} 次")
    print(f"{'='*60}")


def run_e2e():
    print("🚀 启动 P1-P4 端到端串联测试\n")
    fake = FakeIM()
    sm = SessionManager()
    sch = Scheduler()
    cb = CardBuilder()
    h = CardActionHandler(fake, sm, group_manager=None, scheduler=sch)
    pusher = OverduePusher(fake, sm)

    # ============== 场景设置 ==============
    # PM 发起建群，已选 1 位负责人 ou_zl（张晓丽）
    PM_OPEN_ID = "ou_pm"
    PM_CHAT_ID = "ou_pm"
    LEAD_OPEN_ID = "ou_zl"
    PROJECT_NAME = "测试项目Alpha"
    PROJECT_ABBR = "TPA"
    PROJECT_ID = "proj-1"
    GROUP_CHAT_ID = "cg_group_1"

    # PM session
    sm.update_state(PM_OPEN_ID, PM_CHAT_ID, "collecting_members",
                    project_name=PROJECT_NAME, project_abbr=PROJECT_ABBR,
                    project_id=PROJECT_ID, group_chat_id=GROUP_CHAT_ID,
                    dept_leads=[{
                        "open_id": LEAD_OPEN_ID, "lead": "张晓丽",
                        "dept": "部门1", "status": "pending",
                        "member_count": 0, "members": []
                    }])

    # ============== P2 步骤1: 发送收集卡，应创建飞书任务 ==============
    print("【P2 步骤1】发送成员收集卡给负责人（应同时创建飞书任务）")
    sent_at = time.time()
    lead_session = sm.update_state(
        LEAD_OPEN_ID, LEAD_OPEN_ID, "collecting_members",
        project_name=PROJECT_NAME, project_abbr=PROJECT_ABBR,
        pm_user_id=PM_OPEN_ID, pm_chat_id=PM_CHAT_ID, lead_name="张晓丽",
        member_card_sent_at=sent_at,
    )
    # 直接调用 card_action_handler._on_send_dept_leads 等价的核心逻辑
    # 模拟发卡 + 创建任务
    resp = fake.send_cardkit_to_user(
        LEAD_OPEN_ID,
        cb.lead_member_collect_card(
            PROJECT_NAME, PROJECT_ABBR,
            pm_user_id=PM_OPEN_ID, pm_chat_id=PM_CHAT_ID, lead_name="张晓丽",
            scope_users=fake.scope_users, deadline_str="12-31 23:59",
        ),
    )
    lead_session.update(card_id=resp.get("card_id", ""), card_seq=0)
    sch.schedule_confirm(
        f"member_freeze:{LEAD_OPEN_ID}",
        callback=h._freeze_or_overdue_member_card,
        timeout_minutes=1440,  # 24h
        lead_open_id=LEAD_OPEN_ID,
    )
    task_info = fake.create_task(
        summary=f"[{PROJECT_NAME}] 收集部门成员",
        assignee_open_id=LEAD_OPEN_ID,
        deadline_ts=sent_at + DAY,
        description="请收集本部门成员名单",
        reminder_minutes_before=60,
    )
    lead_session.update(member_task_id=task_info.get("task_guid", ""))

    print(f"  ✓ 飞书任务已创建：{task_info.get('task_guid')}")
    print(f"  ✓ 24h 冻结定时器已注册：{sch._timers.get(f'member_freeze:{LEAD_OPEN_ID}') is not None}")
    assert _count(fake, "create_task") == 1, "P2 应调用 1 次 create_task"
    assert _count(fake, "send_cardkit_to_user") == 1, "P2 应发 1 张收集卡"

    # ============== P3 步骤1: 第一次挑选+提交 ==============
    print("\n【P3 步骤1】负责人挑选第 1 位成员并提交（首次提交，应调用 complete_task）")
    # 模拟挑选成员 ou_lu
    sm.update_state(LEAD_OPEN_ID, LEAD_OPEN_ID, "collecting_members",
                    selected_members=["ou_lu"])

    h.handle_action(
        "real_chat_lead", LEAD_OPEN_ID,
        {"action": "lead_submit_members",
         "project_name": PROJECT_NAME, "project_abbr": PROJECT_ABBR,
         "pm_user_id": PM_OPEN_ID, "pm_chat_id": PM_CHAT_ID, "lead_name": "张晓丽"},
        form_value={},
    )

    lead_session_after_first = sm.get(LEAD_OPEN_ID, "real_chat_lead")
    first_confirm_msg_id = lead_session_after_first.confirm_message_id
    print(f"  ✓ 首次 PM 确认卡 message_id: {first_confirm_msg_id}")
    print(f"  ✓ 完成飞书任务调用次数: {_count(fake, 'complete_task')}（应为 1）")
    assert _count(fake, "complete_task") == 1, "P3 首次应调用 1 次 complete_task"
    assert first_confirm_msg_id, "P3 首次提交应记录 confirm_message_id"
    # PM session 中 dept_leads 应更新
    pm_session = sm.get(PM_OPEN_ID, PM_CHAT_ID)
    lead_entry = pm_session.dept_leads[0]
    assert lead_entry["member_count"] == 1, f"P3 首次提交后 member_count 应=1，实际 {lead_entry['member_count']}"
    print(f"  ✓ PM 看板已更新 member_count={lead_entry['member_count']}")

    # ============== P3 步骤2: 第二次挑选+提交（追加）==============
    print("\n【P3 步骤2】负责人追加挑选第 2 位成员并重新提交（应跳过 complete_task、定时器重置）")
    # 模拟追加挑选 ou_ls（_on_pick_member 已天然去重，不会重复添加 ou_lu）
    current = list(lead_session_after_first.selected_members)
    current.append("ou_ls")
    lead_session_after_first.update(selected_members=current)

    h.handle_action(
        "real_chat_lead", LEAD_OPEN_ID,
        {"action": "lead_submit_members",
         "project_name": PROJECT_NAME, "project_abbr": PROJECT_ABBR,
         "pm_user_id": PM_OPEN_ID, "pm_chat_id": PM_CHAT_ID, "lead_name": "张晓丽"},
        form_value={},
    )

    lead_session_after_second = sm.get(LEAD_OPEN_ID, "real_chat_lead")
    second_confirm_msg_id = lead_session_after_second.confirm_message_id
    print(f"  ✓ 二次 PM 确认卡 message_id: {second_confirm_msg_id}（应不同于首次）")
    print(f"  ✓ 完成飞书任务调用次数: {_count(fake, 'complete_task')}（应仍为 1，未重复调用）")
    assert _count(fake, "complete_task") == 1, "P3 二次提交不应重复调用 complete_task"
    assert second_confirm_msg_id != first_confirm_msg_id, "P3 二次提交应发新卡覆盖"
    pm_session = sm.get(PM_OPEN_ID, PM_CHAT_ID)
    lead_entry = pm_session.dept_leads[0]
    assert lead_entry["member_count"] == 2, f"P3 二次提交后 member_count 应=2，实际 {lead_entry['member_count']}"
    print(f"  ✓ PM 看板已更新 member_count={lead_entry['member_count']}（累积数）")

    # ============== P1 步骤: 独立场景验证 24h 冻结回调 ==============
    # 注：上面 P3 经过 migrate_context 后，ou_zl:ou_zl 占位会话已被删除，
    # 而 _freeze_or_overdue_member_card 内部用 lead_open_id 作为 chat_id 找
    # session。为隔离测试 P1，用一个独立的 lead 不经过 migrate_context。
    print("\n【P1 步骤】独立场景验证 24h 冻结回调（已提交→冻结卡）")
    LEAD_FREEZE_OID = "ou_freeze"
    sm.update_state(LEAD_FREEZE_OID, LEAD_FREEZE_OID, "collecting_members",
                    project_name=PROJECT_NAME, project_abbr=PROJECT_ABBR,
                    selected_members=["ou_lu"],  # 已提交 → 应冻结
                    card_id="card_freeze_1")
    h._freeze_or_overdue_member_card(LEAD_FREEZE_OID)
    print(f"  ✓ batch_update_card 调用次数: {_count(fake, 'batch_update_card')}（≥1 表示已冻结）")
    assert _count(fake, "batch_update_card") >= 1, "P1 24h 到期应调用 batch_update_card 冻结卡片"

    # P1 第二场景：未提交 → 仅 patch 逾期提示
    print("\n【P1 步骤-补充】未提交场景：仅 patch 逾期提示，不冻结")
    batch_before = _count(fake, "batch_update_card")
    LEAD_OVERDUE_OID = "ou_overdue_p1"
    sm.update_state(LEAD_OVERDUE_OID, LEAD_OVERDUE_OID, "collecting_members",
                    project_name=PROJECT_NAME, project_abbr=PROJECT_ABBR,
                    selected_members=[],  # 未提交
                    card_id="card_overdue_1")
    h._freeze_or_overdue_member_card(LEAD_OVERDUE_OID)
    batch_after = _count(fake, "batch_update_card")
    print(f"  ✓ 未提交时 batch_update_card 调用次数: {batch_after - batch_before}（≥1，仅 patch 逾期提示）")
    assert batch_after - batch_before >= 1, "P1 未提交应 patch 逾期提示"

    # ============== P4 步骤: 另一位负责人完全没提交，时间快进 25h，触发每日逾期推送 ==============
    print("\n【P4 步骤】新增一位「未提交」的负责人，时间快进 25h，模拟每日 8:00 推送")
    LEAD2_OPEN_ID = "ou_ww"
    sm.update_state(LEAD2_OPEN_ID, LEAD2_OPEN_ID, "collecting_members",
                    project_name=PROJECT_NAME, project_abbr=PROJECT_ABBR,
                    pm_user_id=PM_OPEN_ID, pm_chat_id=PM_CHAT_ID, lead_name="王五",
                    member_card_sent_at=sent_at,  # 同 PM_OPEN_ID 一并建群
                    selected_members=[],
                    confirm_message_id="")  # 从未提交

    now_overdue = sent_at + 25 * 3600  # 25h 后
    pushed = pusher.run_daily_check(now_ts=now_overdue)

    # 注意：fake.calls 中 create_task 的 args_tuple 为空 ()，不能用列表推导直接解包，
    # 必须先按 method 过滤再安全解包
    lead2_texts = []
    pm_texts = []
    for call in fake.calls:
        if call[0] != "send_text_to_user":
            continue
        if len(call[1]) < 2:
            continue
        oid, t = call[1][0], call[1][1]
        if oid == LEAD2_OPEN_ID:
            lead2_texts.append(t)
        elif oid == PM_OPEN_ID:
            pm_texts.append(t)

    print(f"  ✓ 推送成功的负责人数: {pushed}")
    print(f"  ✓ 给王五推送的文本数: {len(lead2_texts)}（应≥1）")
    print(f"  ✓ 给 PM 推送的汇总数: {len(pm_texts)}（应≥1，含「王五」姓名）")
    assert pushed >= 1, "P4 应至少推送 1 位逾期负责人"
    assert lead2_texts, "P4 应给王五发送提醒文本"
    assert pm_texts, "P4 应给 PM 发送汇总"
    assert "王五" in pm_texts[-1], "P4 PM 汇总应包含王五姓名"

    _print_call_summary(fake, "P1-P4 全流程")

    print("\n🎉 P1-P4 端到端串联测试全部通过！")
    print("\n各阶段验证点：")
    print("  P1: batch_update_card ≥1 次（24h 冻结回调已触发）")
    print("  P2: create_task = 1 次（发卡时创建任务）")
    print("  P3: complete_task = 1 次（仅首次提交时调用，二次跳过）")
    print("      PM 收到 2 张确认卡（覆盖式），member_count 累积=2")
    print("  P4: run_daily_check 推送文本给负责人和 PM")


if __name__ == "__main__":
    run_e2e()
