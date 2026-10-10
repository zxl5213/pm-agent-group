"""定时器 - 超时回调（成员确认自动认可、成员收集卡24h冻结/逾期等）

当负责人提交成员后，群内弹出确认卡。若PM在指定时间内未操作，
定时器自动执行「认可并拉入群」逻辑。
也用于成员收集卡24h到期时的冻结/逾期处理。
也支持每日定时循环任务（如每日8点逾期推送）。
"""
import threading
import logging
import time
from datetime import datetime, timedelta, timezone

# Asia/Shanghai 时区（UTC+8）
SHANGHAI_TZ = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)


class ConfirmTimer:
    """单个确认卡的超时定时器"""

    def __init__(self, timeout_minutes, callback, **callback_kwargs):
        self.timeout_seconds = timeout_minutes * 60
        self.callback = callback
        self.callback_kwargs = callback_kwargs
        self._timer = None
        self._cancelled = False
        self._completed = False

    def start(self):
        """启动定时器"""
        self._timer = threading.Timer(self.timeout_seconds, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()
        logger.info(f"定时器已启动，{self.timeout_seconds}秒后回调")

    def cancel(self):
        """取消定时器（PM已操作）"""
        self._cancelled = True
        if self._timer:
            self._timer.cancel()
        logger.info("定时器已取消")

    def _on_timeout(self):
        """超时回调"""
        if self._cancelled or self._completed:
            return
        self._completed = True
        logger.info("定时器超时，执行回调")
        try:
            self.callback(**self.callback_kwargs)
        except Exception as e:
            logger.error(f"定时器回调执行失败: {e}", exc_info=True)


class Scheduler:
    """全局定时器管理器"""

    def __init__(self, default_timeout_minutes=30):
        self.default_timeout = default_timeout_minutes
        self._timers = {}  # key -> ConfirmTimer

    def schedule_confirm(self, key, callback, timeout_minutes=None, **kwargs):
        """注册一个确认超时任务

        Args:
            key: 唯一标识（如 project_id:lead_open_id）
            callback: 超时回调函数
            timeout_minutes: 超时分钟数，默认使用全局配置
            **kwargs: 回调参数
        """
        # 取消已有同 key 的定时器
        self.cancel_confirm(key)

        timeout = timeout_minutes or self.default_timeout
        timer = ConfirmTimer(timeout, callback, **kwargs)
        self._timers[key] = timer
        timer.start()
        return timer

    def cancel_confirm(self, key):
        """取消指定的确认定时器"""
        timer = self._timers.pop(key, None)
        if timer:
            timer.cancel()

    def cancel_all(self):
        """取消所有定时器"""
        for key in list(self._timers.keys()):
            self.cancel_confirm(key)

    def schedule_daily_at(self, hour, minute, callback, key=None, tz=None, **kwargs):
        """注册每日定时循环任务（如每日8:00触发逾期推送）。

        Args:
            hour: 触发小时（0-23，本地时区）
            minute: 触发分钟（0-59）
            callback: 回调函数，签名 callback(**kwargs)
            key: 唯一标识，用于取消；默认 "daily_{hour}:{minute}"
            tz: 时区，默认 Asia/Shanghai
            **kwargs: 传给 callback 的参数

        Returns:
            启动的 DailyTimer 实例
        """
        key = key or f"daily_{hour}:{minute:02d}"
        # 取消已有同 key 的任务
        self.cancel_confirm(key)

        timer = DailyTimer(hour, minute, callback, tz=tz, **kwargs)
        self._timers[key] = timer
        timer.start()
        return timer


class DailyTimer:
    """每日定时循环任务（指定时区的 HH:MM 触发）

    实现方式：daemon 线程循环计算到下一次目标时刻的等待秒数，
    sleep 等待，到期触发回调，再循环。支持 cancel 立即停止。
    """

    def __init__(self, hour, minute, callback, tz=None, **callback_kwargs):
        self.hour = hour
        self.minute = minute
        self.callback = callback
        self.tz = tz or SHANGHAI_TZ
        self.callback_kwargs = callback_kwargs
        self._thread = None
        self._stop_event = threading.Event()
        self._cancelled = False

    def start(self):
        """启动后台 daemon 线程"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(
            f"每日定时任务已启动，将在 {self.tz} 的 {self.hour:02d}:{self.minute:02d} 触发")

    def cancel(self):
        """取消任务"""
        self._cancelled = True
        self._stop_event.set()
        logger.info("每日定时任务已取消")

    def _seconds_until_next(self, now_dt):
        """计算从 now_dt 到下一次 HH:MM 的等待秒数"""
        target = now_dt.replace(
            hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if now_dt >= target:
            # 已过今天的目标时刻，下次在明天
            target = target + timedelta(days=1)
        delta = (target - now_dt).total_seconds()
        # 安全上限：避免极端值，最长等 25 小时
        return max(0, min(delta, 25 * 3600))

    def _run(self):
        """主循环：等待→触发→继续等待"""
        while not self._cancelled:
            try:
                now_dt = datetime.now(self.tz)
                wait_seconds = self._seconds_until_next(now_dt)
                logger.info(
                    f"每日任务等待 {wait_seconds:.0f}s 后于 "
                    f"{self.hour:02d}:{self.minute:02d} 触发")
                # 用 Event.wait 替代 time.sleep，cancel 时能立即响应
                if self._stop_event.wait(timeout=wait_seconds):
                    # 被 cancel 唤醒
                    break
                if self._cancelled:
                    break
                logger.info(
                    f"每日定时触发：{self.hour:02d}:{self.minute:02d}，"
                    f"执行回调")
                try:
                    self.callback(**self.callback_kwargs)
                except Exception as e:
                    logger.error(
                        f"每日定时回调执行失败: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"每日定时循环异常: {e}", exc_info=True)
                # 异常后短暂休眠，避免 CPU 占满
                self._stop_event.wait(timeout=60)
