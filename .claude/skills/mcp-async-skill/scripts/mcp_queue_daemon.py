#!/usr/bin/env python3
"""
MCP Queue Daemon
Manages job queue with rate limiting and concurrency control.

Features:
- Unix Domain Socket server
- asyncio-based event loop
- Semaphore-based concurrency limiting
- Token bucket rate limiting
- WAL (Write-Ahead Log) for crash recovery
- termux-wake-lock support for Android
- PID/socket file management

Usage:
    python mcp_queue_daemon.py                  # Start daemon (foreground)
    python mcp_queue_daemon.py --background     # Start daemon (background)
    python mcp_queue_daemon.py --status         # Show queue status
    python mcp_queue_daemon.py --shutdown       # Shutdown daemon
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

# Add script directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from mcp_queue_protocol import (
    MessageType,
    async_send_message,
    async_recv_message,
    make_job_completed_payload,
    make_status_response_payload,
    HEADER_SIZE,
)

# Default paths
DEFAULT_RUNTIME_DIR = Path.home() / ".cache" / "mcp-queue"
DEFAULT_SOCKET_NAME = "mcp-queue.sock"
DEFAULT_PID_NAME = "mcp-queue.pid"
DEFAULT_WAL_NAME = "mcp-queue.wal"
DEFAULT_CONFIG_NAME = "queue_config.yaml"

# Default configuration
DEFAULT_CONFIG = {
    "max_concurrent": 2,
    "start_interval": 1.0,
    "poll_interval": 30.0,
    "global_rate_per_min": 10,
    "global_burst": 5,
    "job_timeout": 900,
    "client_idle_timeout": 0,
    "endpoint_rates": {},
}

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mcp-queue-daemon")


class JobStatus(Enum):
    """Job status enumeration."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    """Job representation."""
    job_id: str
    endpoint: str
    submit_tool: str
    submit_args: dict
    status_tool: str
    result_tool: str
    headers: dict | None = None
    id_param_name: str = "request_id"
    poll_interval: float = 30.0
    max_polls: int = 300
    output_dir: str | None = None
    output_file: str | None = None
    auto_filename: bool = False
    save_logs_to_dir: bool = False
    save_logs_inline: bool = False
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: dict | None = None
    error: str | None = None
    client_writer: Any = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """Convert to dict (excluding non-serializable fields)."""
        # Manual conversion to avoid deepcopy of socket objects
        return {
            "job_id": self.job_id,
            "endpoint": self.endpoint,
            "submit_tool": self.submit_tool,
            "submit_args": self.submit_args,
            "status_tool": self.status_tool,
            "result_tool": self.result_tool,
            "headers": self.headers,
            "id_param_name": self.id_param_name,
            "poll_interval": self.poll_interval,
            "max_polls": self.max_polls,
            "output_dir": self.output_dir,
            "output_file": self.output_file,
            "auto_filename": self.auto_filename,
            "save_logs_to_dir": self.save_logs_to_dir,
            "save_logs_inline": self.save_logs_inline,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error": self.error,
            # Exclude client_writer (not serializable)
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        """Create from dict."""
        d = d.copy()
        d["status"] = JobStatus(d.get("status", "pending"))
        d.pop("client_writer", None)
        return cls(**d)


class TokenBucket:
    """Token bucket rate limiter."""

    def __init__(self, rate_per_min: float, burst: int):
        """
        Args:
            rate_per_min: Tokens added per minute
            burst: Maximum tokens (bucket size)
        """
        self.rate_per_sec = rate_per_min / 60.0
        self.burst = burst
        self.tokens = float(burst)
        self.last_update = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """
        Acquire a token, waiting if necessary.

        Returns:
            float: Wait time in seconds (0 if immediate)
        """
        if self.rate_per_sec <= 0:
            return 0.0

        total_wait = 0.0
        while True:
            async with self._lock:
                now = time.time()
                elapsed = now - self.last_update
                self.tokens = min(self.burst, self.tokens + elapsed * self.rate_per_sec)
                self.last_update = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return total_wait

                wait_time = (1.0 - self.tokens) / self.rate_per_sec

            await asyncio.sleep(wait_time)
            total_wait += wait_time


class WAL:
    """Write-Ahead Log for crash recovery."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()

    async def append(self, entry: dict) -> None:
        """Append entry to WAL."""
        async with self._lock:
            entry["timestamp"] = time.time()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def clear(self) -> None:
        """Clear WAL (after successful recovery)."""
        async with self._lock:
            if self.path.exists():
                self.path.unlink()

    def read_all(self) -> list[dict]:
        """Read all entries from WAL."""
        if not self.path.exists():
            return []
        entries = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid WAL entry: {line[:100]}")
        return entries


class JobDispatcher:
    """Job dispatcher with queue management."""

    def __init__(
        self,
        max_concurrent: int,
        rate_limiter: TokenBucket,
        endpoint_rate_limiters: dict[str, TokenBucket] | None,
        wal: WAL,
        job_timeout: float = 900,
        start_interval: float = 1.0,
    ):
        self.max_concurrent = max_concurrent
        self.rate_limiter = rate_limiter
        self.endpoint_rate_limiters = endpoint_rate_limiters or {}
        self.wal = wal
        self.job_timeout = job_timeout
        self.start_interval = start_interval  # Minimum interval between job starts

        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.jobs: dict[str, Job] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.completed_count = 0
        self.failed_count = 0
        self._running = True
        self._workers: list[asyncio.Task] = []
        self._last_start_time: float = 0.0
        self._start_lock = asyncio.Lock()  # Ensures sequential job starts

    async def start(self, num_workers: int | None = None) -> None:
        """Start worker tasks."""
        num_workers = num_workers or self.max_concurrent
        for i in range(num_workers):
            task = asyncio.create_task(self._worker(i))
            self._workers.append(task)
        logger.info(f"Started {num_workers} worker(s)")

    async def stop(self) -> None:
        """Stop all workers."""
        self._running = False
        # Cancel all workers
        for task in self._workers:
            task.cancel()
        # Wait for workers to finish
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("All workers stopped")

    async def submit(self, job: Job) -> None:
        """Submit a job to the queue."""
        self.jobs[job.job_id] = job
        await self.wal.append({"action": "submit", "job": job.to_dict()})
        await self.queue.put(job.job_id)
        logger.info(f"Job {job.job_id} submitted (queue size: {self.queue.qsize()})")

    async def _worker(self, worker_id: int) -> None:
        """Worker coroutine that processes jobs."""
        logger.info(f"Worker {worker_id} started")
        while self._running:
            try:
                # Get job from queue (with timeout to check _running)
                try:
                    job_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                job = self.jobs.get(job_id)
                if not job:
                    logger.warning(f"Job {job_id} not found, skipping")
                    continue

                # Wait for global rate limit
                wait_time = await self.rate_limiter.acquire()
                if wait_time > 0:
                    logger.info(f"Rate limit: waiting {wait_time:.1f}s")

                # Wait for endpoint-specific rate limit (if configured)
                endpoint_limiter = self.endpoint_rate_limiters.get(job.endpoint)
                if endpoint_limiter:
                    ep_wait = await endpoint_limiter.acquire()
                    if ep_wait > 0:
                        logger.info(f"Endpoint rate limit: waiting {ep_wait:.1f}s")

                # Enforce minimum interval between job starts (do not hold semaphore while waiting)
                async with self._start_lock:
                    now = time.time()
                    elapsed = now - self._last_start_time
                    if elapsed < self.start_interval:
                        wait_start = self.start_interval - elapsed
                        logger.info(f"Start interval: waiting {wait_start:.2f}s")
                        await asyncio.sleep(wait_start)
                    self._last_start_time = time.time()

                # Acquire semaphore (concurrency limit) just before execution
                async with self.semaphore:
                    await self._execute_job(job)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Worker {worker_id} error: {e}")

        logger.info(f"Worker {worker_id} stopped")

    async def _execute_job(self, job: Job) -> None:
        """Execute a single job."""
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        await self.wal.append({"action": "start", "job_id": job.job_id})
        logger.info(f"Executing job {job.job_id}")

        try:
            # Mock mode: endpoint starts with "mock://" - simulate polling for testing
            if job.endpoint.startswith("mock://"):
                duration = job.submit_args.get("duration", 3.0)
                mock_poll_interval = job.submit_args.get("mock_poll_interval", 2.0)
                poll_count = max(1, int(duration / mock_poll_interval))

                logger.info(f"Mock job {job.job_id}: {poll_count} polls @ {mock_poll_interval}s interval")
                for i in range(poll_count):
                    await asyncio.sleep(mock_poll_interval)
                    logger.info(f"Mock job {job.job_id}: poll {i+1}/{poll_count}")

                result = {
                    "request_id": job.job_id,
                    "status": "completed",
                    "mock": True,
                    "duration": duration,
                    "poll_count": poll_count,
                    "poll_interval": mock_poll_interval,
                    "saved_path": None,
                }
            else:
                # Import here to avoid circular dependency
                from mcp_async_call import run_async_mcp_job

                # Run with timeout
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        run_async_mcp_job,
                        endpoint=job.endpoint,
                        submit_tool=job.submit_tool,
                        submit_args=job.submit_args,
                        status_tool=job.status_tool,
                        result_tool=job.result_tool,
                        output_dir=job.output_dir,
                        output_file=job.output_file,
                        auto_filename=job.auto_filename,
                        poll_interval=job.poll_interval,
                        max_polls=job.max_polls,
                        headers=job.headers,
                        id_param_name=job.id_param_name,
                        save_logs_to_dir=job.save_logs_to_dir,
                        save_logs_inline=job.save_logs_inline,
                    ),
                    timeout=self.job_timeout,
                )

            job.status = JobStatus.COMPLETED
            job.result = result
            job.completed_at = time.time()
            self.completed_count += 1
            await self.wal.append({"action": "complete", "job_id": job.job_id, "result": result})
            logger.info(f"Job {job.job_id} completed")

            # Notify client
            await self._notify_client(job, success=True)

        except asyncio.TimeoutError:
            job.status = JobStatus.FAILED
            job.error = f"Job timed out after {self.job_timeout}s"
            job.completed_at = time.time()
            self.failed_count += 1
            await self.wal.append({"action": "fail", "job_id": job.job_id, "error": job.error})
            logger.error(f"Job {job.job_id} timed out")
            await self._notify_client(job, success=False)

        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            job.completed_at = time.time()
            self.failed_count += 1
            await self.wal.append({"action": "fail", "job_id": job.job_id, "error": job.error})
            logger.exception(f"Job {job.job_id} failed: {e}")
            await self._notify_client(job, success=False)

    async def _notify_client(self, job: Job, success: bool) -> None:
        """Notify client of job completion."""
        if not job.client_writer:
            return

        try:
            msg_type = MessageType.JOB_COMPLETED if success else MessageType.JOB_FAILED
            payload = make_job_completed_payload(
                job_id=job.job_id,
                success=success,
                result=job.result,
                error=job.error,
            )
            await async_send_message(job.client_writer, msg_type, payload)
        except Exception as e:
            logger.warning(f"Failed to notify client for job {job.job_id}: {e}")

    def get_status(self) -> dict:
        """Get queue status."""
        running = sum(1 for j in self.jobs.values() if j.status == JobStatus.RUNNING)
        pending = sum(1 for j in self.jobs.values() if j.status == JobStatus.PENDING)

        jobs_info = []
        for job in self.jobs.values():
            jobs_info.append({
                "job_id": job.job_id,
                "status": job.status.value,
                "endpoint": job.endpoint,
                "submit_tool": job.submit_tool,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
            })

        return make_status_response_payload(
            running=running,
            queued=pending,
            completed=self.completed_count,
            failed=self.failed_count,
            jobs=jobs_info,
        )


class QueueServer:
    """Unix Domain Socket server for queue management."""

    def __init__(
        self,
        socket_path: Path,
        pid_path: Path,
        dispatcher: JobDispatcher,
        poll_interval_default: float,
        job_timeout: float,
        client_idle_timeout: float,
    ):
        self.socket_path = socket_path
        self.pid_path = pid_path
        self.dispatcher = dispatcher
        self.poll_interval_default = poll_interval_default
        self.job_timeout = job_timeout
        self.client_idle_timeout = client_idle_timeout
        self.server: asyncio.Server | None = None
        self._running = True

    async def start(self) -> None:
        """Start the server."""
        # Clean up old socket
        if self.socket_path.exists():
            self.socket_path.unlink()

        # Write PID file
        self.pid_path.write_text(str(os.getpid()))

        # Start server
        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )

        logger.info(f"Server listening on {self.socket_path}")

        # Start dispatcher
        await self.dispatcher.start()

    async def stop(self) -> None:
        """Stop the server."""
        self._running = False

        # Stop dispatcher
        await self.dispatcher.stop()

        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        # Clean up files
        if self.socket_path.exists():
            self.socket_path.unlink()
        if self.pid_path.exists():
            self.pid_path.unlink()

        logger.info("Server stopped")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle client connection."""
        peer = "client"
        try:
            while self._running:
                try:
                    if self.client_idle_timeout and self.client_idle_timeout > 0:
                        msg = await asyncio.wait_for(
                            async_recv_message(reader),
                            timeout=self.client_idle_timeout,
                        )
                    else:
                        msg = await async_recv_message(reader)
                except asyncio.TimeoutError:
                    logger.warning(f"{peer}: Connection timeout")
                    break

                if not msg:
                    break

                msg_type = msg.get("type")
                logger.debug(f"{peer}: Received {msg_type}")

                if msg_type == MessageType.PING:
                    await async_send_message(writer, MessageType.PONG)

                elif msg_type == MessageType.SUBMIT_JOB:
                    poll_interval = msg.get("poll_interval")
                    if not poll_interval or poll_interval <= 0:
                        poll_interval = self.poll_interval_default

                    max_polls = msg.get("max_polls")
                    if not max_polls or max_polls <= 0:
                        if poll_interval > 0:
                            max_polls = int(self.job_timeout / poll_interval)
                        else:
                            max_polls = 300
                        max_polls = max(1, max_polls)

                    job = Job(
                        job_id=msg.get("job_id", str(uuid.uuid4())),
                        endpoint=msg["endpoint"],
                        submit_tool=msg["submit_tool"],
                        submit_args=msg["submit_args"],
                        status_tool=msg["status_tool"],
                        result_tool=msg["result_tool"],
                        headers=msg.get("headers"),
                        id_param_name=msg.get("id_param_name", "request_id"),
                        poll_interval=poll_interval,
                        max_polls=max_polls,
                        output_dir=msg.get("output_dir"),
                        output_file=msg.get("output_file"),
                        auto_filename=msg.get("auto_filename", False),
                        save_logs_to_dir=msg.get("save_logs_to_dir", False),
                        save_logs_inline=msg.get("save_logs_inline", False),
                        client_writer=writer,
                    )
                    await self.dispatcher.submit(job)
                    await async_send_message(
                        writer,
                        MessageType.JOB_ACCEPTED,
                        {"job_id": job.job_id},
                    )
                    # Keep connection open to receive completion notification
                    # Client should wait for JOB_COMPLETED/JOB_FAILED

                elif msg_type == MessageType.STATUS:
                    status = self.dispatcher.get_status()
                    await async_send_message(writer, MessageType.STATUS_RESPONSE, status)

                elif msg_type == MessageType.SHUTDOWN:
                    logger.info("Shutdown requested")
                    await async_send_message(writer, MessageType.SHUTDOWN_ACK)
                    self._running = False
                    asyncio.get_event_loop().call_soon(
                        lambda: asyncio.create_task(self.stop())
                    )
                    break

                else:
                    await async_send_message(
                        writer,
                        MessageType.ERROR,
                        {"error": f"Unknown message type: {msg_type}"},
                    )

        except asyncio.IncompleteReadError:
            pass  # Client disconnected
        except Exception as e:
            logger.exception(f"{peer}: Error handling client: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _load_config_file(path: Path) -> dict | None:
    """Load config from YAML or JSON file."""
    if not path.exists():
        return None

    try:
        with open(path, encoding="utf-8") as f:
            if path.suffix in (".yaml", ".yml"):
                if yaml is None:
                    logger.warning(f"pyyaml not installed, cannot read {path}")
                    return None
                return yaml.safe_load(f)
            else:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load config from {path}: {e}")
        return None


def load_config(config_path: Path | None = None, skill_dir: Path | None = None) -> dict:
    """
    Load configuration.

    Priority:
    1. Environment variables
    2. Config file (specified path)
    3. Config file (skill folder)
    4. Config file (script directory)
    5. Config file (current directory)
    6. Default values

    Supports both YAML (.yaml, .yml) and JSON (.json) formats.
    """
    config = DEFAULT_CONFIG.copy()

    file_config = None
    config_names = ["queue_config.yaml", "queue_config.yml", "queue_config.json"]

    # Try to load from specified path
    if config_path:
        file_config = _load_config_file(config_path)

    # Try skill directory
    if file_config is None and skill_dir:
        for name in config_names:
            skill_config = skill_dir / name
            file_config = _load_config_file(skill_config)
            if file_config is not None:
                logger.info(f"Loaded config from skill dir: {skill_config}")
                break

    # Try script directory (where mcp_queue_daemon.py is located)
    if file_config is None:
        script_dir = Path(__file__).parent
        for name in config_names:
            script_config = script_dir / name
            file_config = _load_config_file(script_config)
            if file_config is not None:
                logger.info(f"Loaded config from script dir: {script_config}")
                break

    # Try parent of script directory (skill root)
    if file_config is None:
        skill_root = Path(__file__).parent.parent
        for name in config_names:
            root_config = skill_root / name
            file_config = _load_config_file(root_config)
            if file_config is not None:
                logger.info(f"Loaded config from skill root: {root_config}")
                break

    # Try current directory
    if file_config is None:
        cwd = Path.cwd()
        for name in config_names:
            cwd_config = cwd / name
            file_config = _load_config_file(cwd_config)
            if file_config is not None:
                logger.info(f"Loaded config from cwd: {cwd_config}")
                break

    # Merge config from file
    if file_config:
        for k, v in file_config.items():
            if not k.startswith("//"):  # Skip comment keys
                config[k] = v
    else:
        logger.warning("No config file found, using defaults")

    # Override from environment
    if os.environ.get("MCP_QUEUE_MAX_CONCURRENT"):
        config["max_concurrent"] = int(os.environ["MCP_QUEUE_MAX_CONCURRENT"])
    if os.environ.get("MCP_QUEUE_RATE_PER_MIN"):
        config["global_rate_per_min"] = float(os.environ["MCP_QUEUE_RATE_PER_MIN"])
    if os.environ.get("MCP_QUEUE_BURST"):
        config["global_burst"] = int(os.environ["MCP_QUEUE_BURST"])
    if os.environ.get("MCP_QUEUE_JOB_TIMEOUT"):
        config["job_timeout"] = float(os.environ["MCP_QUEUE_JOB_TIMEOUT"])
    if os.environ.get("MCP_QUEUE_CLIENT_IDLE_TIMEOUT"):
        config["client_idle_timeout"] = float(os.environ["MCP_QUEUE_CLIENT_IDLE_TIMEOUT"])

    return config


def acquire_wake_lock() -> subprocess.Popen | None:
    """Acquire termux-wake-lock if available."""
    if shutil.which("termux-wake-lock"):
        try:
            proc = subprocess.Popen(
                ["termux-wake-lock"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Acquired termux-wake-lock")
            return proc
        except Exception as e:
            logger.warning(f"Failed to acquire wake lock: {e}")
    return None


def release_wake_lock() -> None:
    """Release termux-wake-lock if available."""
    if shutil.which("termux-wake-unlock"):
        try:
            subprocess.run(
                ["termux-wake-unlock"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Released termux-wake-lock")
        except Exception:
            pass


async def run_daemon(
    runtime_dir: Path,
    config: dict,
) -> None:
    """Run the daemon."""
    socket_path = runtime_dir / DEFAULT_SOCKET_NAME
    pid_path = runtime_dir / DEFAULT_PID_NAME
    wal_path = runtime_dir / DEFAULT_WAL_NAME

    # Create runtime directory
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Initialize components
    rate_limiter = TokenBucket(
        rate_per_min=config["global_rate_per_min"],
        burst=config["global_burst"],
    )
    endpoint_rate_limiters = {}
    for endpoint, rate_cfg in (config.get("endpoint_rates") or {}).items():
        try:
            rate_per_min = float(rate_cfg.get("rate_per_min", config["global_rate_per_min"]))
            burst = int(rate_cfg.get("burst", config["global_burst"]))
            endpoint_rate_limiters[endpoint] = TokenBucket(rate_per_min=rate_per_min, burst=burst)
        except Exception as e:
            logger.warning(f"Invalid endpoint_rates for {endpoint}: {e}")

    wal = WAL(wal_path)
    dispatcher = JobDispatcher(
        max_concurrent=config["max_concurrent"],
        rate_limiter=rate_limiter,
        endpoint_rate_limiters=endpoint_rate_limiters,
        wal=wal,
        job_timeout=config["job_timeout"],
        start_interval=config.get("start_interval", 1.0),
    )

    # Recover from WAL
    wal_entries = wal.read_all()
    if wal_entries:
        logger.info(f"Recovering {len(wal_entries)} entries from WAL")
        # TODO: Implement proper WAL recovery
        await wal.clear()

    poll_interval_default = float(config.get("poll_interval", 30.0))
    if poll_interval_default <= 0:
        poll_interval_default = 30.0

    server = QueueServer(
        socket_path=socket_path,
        pid_path=pid_path,
        dispatcher=dispatcher,
        poll_interval_default=poll_interval_default,
        job_timeout=float(config.get("job_timeout", 900)),
        client_idle_timeout=float(config.get("client_idle_timeout", 0)),
    )

    # Setup signal handlers
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))

    # Acquire wake lock
    wake_lock_proc = acquire_wake_lock()

    try:
        await server.start()

        # Run until stopped
        while server._running:
            await asyncio.sleep(1)

    finally:
        release_wake_lock()
        await server.stop()


def get_runtime_dir() -> Path:
    """Get runtime directory path."""
    return DEFAULT_RUNTIME_DIR


def is_daemon_running(runtime_dir: Path | None = None) -> bool:
    """Check if daemon is running."""
    runtime_dir = runtime_dir or get_runtime_dir()
    pid_path = runtime_dir / DEFAULT_PID_NAME
    socket_path = runtime_dir / DEFAULT_SOCKET_NAME

    if not pid_path.exists() or not socket_path.exists():
        return False

    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return True
    except (ValueError, OSError):
        return False


def main():
    parser = argparse.ArgumentParser(description="MCP Queue Daemon")
    parser.add_argument("--background", "-b", action="store_true", help="Run in background")
    parser.add_argument("--config", "-c", help="Config file path")
    parser.add_argument("--runtime-dir", help=f"Runtime directory (default: {DEFAULT_RUNTIME_DIR})")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else get_runtime_dir()
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    logger.info(f"Config: max_concurrent={config['max_concurrent']}, "
                f"start_interval={config.get('start_interval', 1.0)}s, "
                f"rate={config['global_rate_per_min']}/min, "
                f"burst={config['global_burst']}")

    if args.background:
        # Fork to background
        pid = os.fork()
        if pid > 0:
            print(f"Daemon started (PID: {pid})")
            sys.exit(0)
        # Child process continues
        os.setsid()

    asyncio.run(run_daemon(runtime_dir, config))


if __name__ == "__main__":
    main()
