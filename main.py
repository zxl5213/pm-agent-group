import os
import sys
import yaml
import json
import logging
import signal
import threading
from flask import Flask, request, jsonify

from src.utils.logger import setup_logger
from src.modules.group_manager import GroupManager
from src.modules.session_manager import SessionManager
from src.modules.command_handler import CommandHandler
from src.modules.card_action_handler import CardActionHandler
from src.modules.scheduler import Scheduler
from src.modules.overdue_pusher import OverduePusher
from src.feishu.im_client import IMClient
from src.feishu.ws_client import WsClient

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

logger = logging.getLogger(__name__)

app = Flask(__name__)
config = None
group_manager = None
session_manager = None
command_handler = None
card_action_handler = None
im_client = None
ws_client = None
scheduler = None
overdue_pusher = None
shutdown_event = threading.Event()


def load_config():
    config_path = os.path.join(PROJECT_ROOT, "config", "settings.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def init_components():
    global group_manager, session_manager, command_handler, card_action_handler, im_client, ws_client, scheduler, overdue_pusher
    group_manager = GroupManager(config)
    im_client = IMClient(config)
    session_manager = SessionManager()
    timeout_minutes = config.get('member_confirm', {}).get('timeout_minutes', 30)
    scheduler = Scheduler(default_timeout_minutes=timeout_minutes)
    command_handler = CommandHandler(im_client, session_manager, group_manager)
    card_action_handler = CardActionHandler(im_client, session_manager, group_manager, scheduler)
    overdue_pusher = OverduePusher(im_client, session_manager)

    feishu_cfg = config['feishu']
    ws_client = WsClient(
        app_id=feishu_cfg['app_id'],
        app_secret=feishu_cfg['app_secret'],
        command_handler=command_handler,
        card_action_handler=card_action_handler,
        im_client=im_client,
        session_manager=session_manager,
    )


def start_ws_client():
    """在后台线程启动WebSocket长连接"""
    try:
        ws_client.start()
    except Exception as e:
        logger.error(f"WebSocket长连接异常: {e}", exc_info=True)


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "service": "pm-agent"})


@app.route("/api/group/create", methods=["POST"])
def api_create_group():
    data = request.json
    quick = data.get("quick_mode", False)
    member_ids = data.get("member_open_ids", [])
    common_kwargs = dict(
        project_name=data["project_name"],
        project_short_name=data.get("project_short_name", ""),
        pm_open_id=data["pm_open_id"],
        description=data.get("description", ""),
    )
    if quick:
        # 热路径：注册与建群并行编排
        project, result = group_manager.quick_register_and_create_group(
            member_open_ids=member_ids, **common_kwargs)
    else:
        project = group_manager.register_project(**common_kwargs)
        result = group_manager.create_project_group(
            project.project_id, quick_mode=False, member_open_ids=member_ids, project=project
        )
    return jsonify({
        "project_id": project.project_id,
        "chat_id": result.get("chat_id", ""),
        "status": result.get("status", ""),
    })


@app.route("/api/group/disband", methods=["POST"])
def api_disband_group():
    data = request.json
    result = group_manager.disband_project_group(
        data["project_id"],
        operator_open_id=data.get("operator_open_id", ""),
    )
    return jsonify(result)


def graceful_shutdown(signum, frame):
    logger.info(f"收到信号 {signum}，开始优雅停机...")
    shutdown_event.set()
    max_wait = config.get('graceful_shutdown', {}).get('max_wait_seconds', 600)
    shutdown_event.wait(timeout=max_wait)
    logger.info("优雅停机完成，退出进程")
    sys.exit(0)


if __name__ == "__main__":
    config = load_config()
    setup_logger(config)
    init_components()

    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    ws_thread = threading.Thread(target=start_ws_client, daemon=True)
    ws_thread.start()
    logger.info("WebSocket长连接线程已启动")

    # P4 每日 8:00（Asia/Shanghai）扫描并推送逾期未提交成员的负责人
    scheduler.schedule_daily_at(
        hour=8, minute=0,
        callback=overdue_pusher.run_daily_check,
        key="daily_overdue_push",
    )
    logger.info("已注册每日8:00逾期推送定时任务")

    webhook_config = config.get('webhook', {})
    port = webhook_config.get('port', 8080)
    host = webhook_config.get('host', '0.0.0.0')

    logger.info(f"PM Agent启动，HTTP监听 {host}:{port}")
    app.run(host=host, port=port, debug=False)
