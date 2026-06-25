from __future__ import annotations

import datetime as dt
import shutil
import time
import re
import socket
from typing import Any

import psutil
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_text_system_status_monitor",
    "久孤(ksjiu)",
    "astrbot_plugin_text_system_status_monitor",
    "1.2.0",
)
class TextStatusPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._program_start = dt.datetime.now()

        self._last_net_bytes_sent = 0
        self._last_net_bytes_recv = 0
        self._last_net_sample_ts = 0.0

        self._last_disk_read = 0
        self._last_disk_write = 0
        self._last_disk_sample_ts = 0.0

        try:
            io = psutil.net_io_counters()
            self._last_net_bytes_sent = io.bytes_sent
            self._last_net_bytes_recv = io.bytes_recv
            self._last_net_sample_ts = time.monotonic()
        except Exception:
            pass

        try:
            dio = psutil.disk_io_counters()
            self._last_disk_read = dio.read_bytes
            self._last_disk_write = dio.write_bytes
            self._last_disk_sample_ts = time.monotonic()
        except Exception:
            pass

        self.master_admin = str(self.config.get("master_admin", "")).strip()
        initial_list = self.config.get("allowed_qq_list", [])
        self.allowed_qqs = {str(qq).strip() for qq in initial_list if qq}
        if self.master_admin:
            self.allowed_qqs.add(self.master_admin)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=0)
    async def handle_everything(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        if not msg:
            return

        if re.match(r"^[\\/](status|状态)$", msg, re.I):
            event.stop_event()
            event.should_call_llm = False

            user_id = str(event.get_sender_id())
            if user_id not in self.allowed_qqs:
                yield event.plain_result("拒绝访问：无权限。")
                return

            try:
                status_text = await self._generate_pure_text_status()
                yield event.plain_result(status_text)
            except Exception as e:
                logger.error(f"Status Error: {e}")
                yield event.plain_result("数据获取异常。")
            return

        config_match = re.match(r"^[\\/]设置列表\s+(增加|删除)\s+(\d+)", msg)
        if config_match:
            event.stop_event()
            event.should_call_llm = False

            user_id = str(event.get_sender_id())
            if user_id != self.master_admin:
                yield event.plain_result("权限不足。")
                return

            action, target = config_match.group(1), config_match.group(2)
            if action == "删除" and target == self.master_admin:
                yield event.plain_result("不可移除主管理。")
                return

            if action == "增加":
                self.allowed_qqs.add(target)
                yield event.plain_result(f"已加入：{target}")
            else:
                self.allowed_qqs.discard(target)
                yield event.plain_result(f"已移除：{target}")
            return

    async def _generate_pure_text_status(self) -> str:
        now = time.monotonic()

        io = psutil.net_io_counters()
        elapsed = max(0.001, now - self._last_net_sample_ts)
        up_kbps = (io.bytes_sent - self._last_net_bytes_sent) / elapsed / 1024
        down_kbps = (io.bytes_recv - self._last_net_bytes_recv) / elapsed / 1024

        self._last_net_sample_ts = now
        self._last_net_bytes_sent = io.bytes_sent
        self._last_net_bytes_recv = io.bytes_recv

        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        sys_state = self._get_system_load(cpu, mem)

        net_state = await self._get_network_state(up_kbps, down_kbps)

        disk_state, disk_info = self._get_disk_io(now)

        du = shutil.disk_usage("/")
        uptime = str(dt.datetime.now() - self._program_start).split('.')[0]

        return (
            "--- 系统报告 ---\n"
            f"运行：{uptime}\n"
            f"负载：CPU {cpu}% | 内存 {mem}%\n"
            f"系统状态：{sys_state}\n"
            f"网络状态(ms)：{net_state}\n"
            f"磁盘状态：{disk_state}\n"
            f"磁盘IO：{disk_info}\n"
            f"存储：{du.free // (1024**3)}G/{du.total // (1024**3)}G\n"
            f"流量：上传{up_kbps:.1f}K/s 下载{down_kbps:.1f}K/s\n"
            "----------------"
        )

    def _get_system_load(self, cpu: float, mem: float) -> str:
        if cpu < 30 and mem < 40:
            return "空闲"
        elif cpu < 60 and mem < 70:
            return "一般"
        elif cpu < 85 and mem < 90:
            return "偏高"
        else:
            return "繁忙"

    async def _get_network_state(self, up: float, down: float) -> str:
        latency = self._tcp_latency("114.114.114.114", 53)

        if latency is None:
            return "不可用"

        if up > 2048 or down > 5120:
            return "拥塞"

        if latency > 300:
            return "严重延迟"
        if latency > 150:
            return "偏高"
        if latency > 80:
            return "略高"

        return "正常"

    def _tcp_latency(self, host: str, port: int, timeout: float = 1.0) -> float | None:
        try:
            start = time.monotonic()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()
            return (time.monotonic() - start) * 1000
        except Exception:
            return None

    def _get_disk_io(self, now: float):
        try:
            dio = psutil.disk_io_counters()
            elapsed = max(0.001, now - self._last_disk_sample_ts)
            read_kb = (dio.read_bytes - self._last_disk_read) / elapsed / 1024
            write_kb = (dio.write_bytes - self._last_disk_write) / elapsed / 1024

            self._last_disk_sample_ts = now
            self._last_disk_read = dio.read_bytes
            self._last_disk_write = dio.write_bytes

            state = "正常"
            if read_kb > 50000 or write_kb > 50000:
                state = "繁忙"

            info = f"读 {read_kb:.1f}K/s 写 {write_kb:.1f}K/s"
            return state, info
        except Exception:
            return "未知", "N/A"
