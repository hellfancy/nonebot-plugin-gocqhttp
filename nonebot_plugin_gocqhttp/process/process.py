import asyncio
import re
import subprocess
import threading
from datetime import datetime
from itertools import count
from time import sleep
from typing import Any, Awaitable, Callable, Dict, Optional, Set, TypeVar, Union

import psutil
from nonebot.utils import escape_tag, run_sync
from pydantic import BaseModel, Field

from ..log import STDOUT, logger
from ..plugin_config import AccountConfig
from .config import ACCOUNTS_DATA_PATH, generate_config, generate_device
from .download import BINARY_PATH

LOG_REGEX = re.compile(
    r"^"
    r"\[(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] "
    r"\[(?P<level>[A-Z]+?)\]: "
    r"(?P<message>.*)"
    r"$"
)
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "FATAL"}

REGISTERED_PROCESSES: Dict[int, "GoCQProcess"] = {}


class BaseProcessInfo(BaseModel):
    status: str
    total_logs: int
    restarts: int


class RunningProcessInfo(BaseProcessInfo):
    pid: int
    memory_used: int
    swap_used: int
    cpu_percent: float
    start_time: float


class StoppedProcessInfo(BaseProcessInfo):
    code: int


ProcessInfo = Union[RunningProcessInfo, StoppedProcessInfo]


class ProcessLog(BaseModel):
    raw: str
    time: datetime = Field(default_factory=datetime.now)
    level: Optional[str] = None
    message: Optional[str] = None


LogListener = Callable[[ProcessLog], Awaitable[Any]]
LogListener_T = TypeVar("LogListener_T", bound=LogListener)


class GoCQProcess:
    process: Optional[subprocess.Popen] = None

    def __init__(
        self,
        account: AccountConfig,
        kill_timeout: float = 5,
        stop_timeout: float = 6,
        max_restarts: int = -1,
        restart_interval: float = 3,
        print_process_log: bool = True,
    ):
        if account.uin not in REGISTERED_PROCESSES:
            REGISTERED_PROCESSES[self.account.uin] = self
        else:
            raise ValueError(f"Account {account.uin} process is already registered.")

        self.account = account
        self.cwd = ACCOUNTS_DATA_PATH / str(account.uin)
        self.loop = asyncio.get_running_loop()

        self.stop_timeout, self.kill_timeout = stop_timeout, kill_timeout
        self.max_restarts, self.restart_interval = max_restarts, restart_interval

        self.log_listeners: Set[LogListener] = set()
        self.log_counter, self.restarted = 0, 0

        async def process_log(log: ProcessLog):
            message = log.message or log.raw
            level = log.level if log.level in LOG_LEVELS else STDOUT.name
            logger.log(level, f"<d>[{self.account.uin}]</d> {escape_tag(message)}")

        if print_process_log:
            self.listen_log(process_log)

        def daemon_thread_runner():
            for restarted in count():
                if not self.daemon_thread_running:
                    break
                if self.max_restarts >= 0 and restarted >= self.max_restarts:
                    break
                code = 0
                try:
                    code = self._daemon_thread_runner()
                except Exception:
                    logger.exception(
                        f"Thread {self.daemon_thread.name!r} raised unknown exception:"
                    )
                logger.warning(
                    f"<b>Process for <e>{self.account.uin}</e> exited</b> with code "
                    f"<r>{code}</r>, retrying to restart... "
                    f"<y>({restarted}/{self.max_restarts})</y>"
                )
                self.restarted += 1
                sleep(self.restart_interval)
            return

        self.daemon_thread = threading.Thread(
            target=daemon_thread_runner,
            name=f"{self.account.uin}-Daemon",
            daemon=True,
        )

    def _daemon_thread_runner(self):
        self.process = subprocess.Popen(
            [BINARY_PATH.absolute(), "faststart"],
            cwd=self.cwd.absolute(),
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert self.process.stdout is not None
        for output in iter(self.process.stdout.readline, ""):
            output = output.strip()
            if "アトリは、高性能ですから!" in output:
                logger.info(
                    "go-cqhttp for "
                    f"<e>{self.account.uin}</e> has successfully started."
                )

            self.log_counter += 1
            log_matched = LOG_REGEX.match(output)
            log_model = (
                ProcessLog(raw=output, **log_matched.groupdict())
                if log_matched
                else ProcessLog(raw=output)
            )
            for listener in self.log_listeners:
                asyncio.run_coroutine_threadsafe(listener(log_model), loop=self.loop)

        if self.process.returncode is None:
            self.process.terminate()
            self.process.wait(timeout=self.kill_timeout)

        return self.process.returncode

    @run_sync
    def start(self):
        generate_config(self.account)
        generate_device(self.account)

        self.daemon_thread_running = True
        self.daemon_thread.start()

    @run_sync
    def stop(self):
        self.daemon_thread_running = False
        if self.process is not None:
            self.process.terminate()
        if self.daemon_thread.is_alive():
            self.daemon_thread.join(self.stop_timeout)
        return

    @run_sync
    def status(self) -> ProcessInfo:
        if self.process is None:
            raise RuntimeError("Process not started yet.")
        process_status = psutil.Process(self.process.pid)
        if self.process.returncode is None:
            with process_status.oneshot():
                cpu = process_status.cpu_percent()
                memory = process_status.memory_info()
                create_time = process_status.create_time()
                status = process_status.status()
                pid = process_status.pid
            return RunningProcessInfo(
                pid=pid,
                memory_used=memory.rss,
                swap_used=memory.vms,
                cpu_percent=cpu,
                start_time=create_time,
                status=status,
                total_logs=self.log_counter,
                restarts=self.restarted,
            )
        else:
            return StoppedProcessInfo(
                status=process_status.status(),
                total_logs=self.log_counter,
                restarts=self.restarted,
                code=self.process.returncode,
            )
        return

    def listen_log(self, listener: LogListener_T) -> LogListener_T:
        self.log_listeners.add(listener)
        return listener
