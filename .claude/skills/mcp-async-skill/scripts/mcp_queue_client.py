#!/usr/bin/env python3
"""
MCP Queue Client
Client for submitting jobs to the queue daemon.

Features:
- Auto-detect and start daemon if not running
- submit_and_wait() API (same interface as run_async_mcp_job)
- CLI for status check and shutdown

Usage:
    # As library
    from mcp_queue_client import submit_and_wait
    result = submit_and_wait(endpoint=..., submit_tool=..., ...)

    # CLI
    python mcp_queue_client.py --status     # Show queue status
    python mcp_queue_client.py --shutdown   # Shutdown daemon
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Force UTF-8 encoding
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
except Exception:
    pass

# Add script directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from mcp_queue_protocol import (
    MessageType,
    sync_send_message,
    sync_recv_message,
    make_submit_job_payload,
)

# Default paths (same as daemon)
DEFAULT_RUNTIME_DIR = Path.home() / ".cache" / "mcp-queue"
DEFAULT_SOCKET_NAME = "mcp-queue.sock"
DEFAULT_PID_NAME = "mcp-queue.pid"

# Daemon script location - search order: local, then base skill
def _find_daemon_script() -> Path:
    """Find daemon script, checking local dir first, then base skill."""
    local = Path(__file__).parent / "mcp_queue_daemon.py"
    if local.exists():
        return local
    # Try base skill (mcp-async-skill)
    base_skill = Path(__file__).parent.parent.parent / "mcp-async-skill" / "scripts" / "mcp_queue_daemon.py"
    if base_skill.exists():
        return base_skill
    return local  # Return local path for error message

DAEMON_SCRIPT = _find_daemon_script()


def get_socket_path(runtime_dir: Path | None = None) -> Path:
    """Get socket path."""
    runtime_dir = runtime_dir or DEFAULT_RUNTIME_DIR
    return runtime_dir / DEFAULT_SOCKET_NAME


def get_pid_path(runtime_dir: Path | None = None) -> Path:
    """Get PID file path."""
    runtime_dir = runtime_dir or DEFAULT_RUNTIME_DIR
    return runtime_dir / DEFAULT_PID_NAME


def is_daemon_running(runtime_dir: Path | None = None) -> bool:
    """Check if daemon is running."""
    pid_path = get_pid_path(runtime_dir)
    socket_path = get_socket_path(runtime_dir)

    if not pid_path.exists() or not socket_path.exists():
        return False

    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return True
    except (ValueError, OSError):
        return False


def start_daemon(runtime_dir: Path | None = None, config_path: Path | None = None) -> bool:
    """
    Start daemon if not running.

    Returns:
        bool: True if daemon is now running
    """
    if is_daemon_running(runtime_dir):
        return True

    if not DAEMON_SCRIPT.exists():
        print(f"[QUEUE] Daemon script not found: {DAEMON_SCRIPT}", file=sys.stderr)
        return False

    # Build command
    cmd = [sys.executable, str(DAEMON_SCRIPT), "--background"]
    if runtime_dir:
        cmd.extend(["--runtime-dir", str(runtime_dir)])
    if config_path:
        cmd.extend(["--config", str(config_path)])

    print(f"[QUEUE] Starting daemon...", file=sys.stderr)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"[QUEUE] Failed to start daemon: {e.stderr}", file=sys.stderr)
        return False

    # Wait for daemon to be ready
    socket_path = get_socket_path(runtime_dir)
    for _ in range(30):  # Wait up to 3 seconds
        if socket_path.exists():
            # Try to connect
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(str(socket_path))
                sync_send_message(sock, MessageType.PING)
                resp = sync_recv_message(sock)
                sock.close()
                if resp and resp.get("type") == MessageType.PONG:
                    print(f"[QUEUE] Daemon started", file=sys.stderr)
                    return True
            except Exception:
                pass
        time.sleep(0.1)

    print(f"[QUEUE] Daemon failed to start (timeout)", file=sys.stderr)
    return False


def connect(runtime_dir: Path | None = None, auto_start: bool = True, config_path: Path | None = None) -> socket.socket:
    """
    Connect to daemon.

    Args:
        runtime_dir: Runtime directory
        auto_start: Auto-start daemon if not running
        config_path: Config file path for daemon

    Returns:
        socket: Connected socket

    Raises:
        ConnectionError: If unable to connect
    """
    if auto_start and not is_daemon_running(runtime_dir):
        if not start_daemon(runtime_dir, config_path):
            raise ConnectionError("Failed to start daemon")

    socket_path = get_socket_path(runtime_dir)
    if not socket_path.exists():
        raise ConnectionError(f"Socket not found: {socket_path}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(600)  # 10 minute timeout for long jobs
    sock.connect(str(socket_path))
    return sock


def get_status(runtime_dir: Path | None = None) -> dict | None:
    """Get queue status."""
    try:
        sock = connect(runtime_dir, auto_start=False)
        sync_send_message(sock, MessageType.STATUS)
        resp = sync_recv_message(sock)
        sock.close()

        if resp and resp.get("type") == MessageType.STATUS_RESPONSE:
            return resp
        return None
    except Exception as e:
        print(f"[QUEUE] Error getting status: {e}", file=sys.stderr)
        return None


def shutdown_daemon(runtime_dir: Path | None = None) -> bool:
    """Shutdown daemon."""
    try:
        sock = connect(runtime_dir, auto_start=False)
        sync_send_message(sock, MessageType.SHUTDOWN)
        resp = sync_recv_message(sock)
        sock.close()

        if resp and resp.get("type") == MessageType.SHUTDOWN_ACK:
            print("[QUEUE] Daemon shutdown requested", file=sys.stderr)
            return True
        return False
    except Exception as e:
        print(f"[QUEUE] Error shutting down: {e}", file=sys.stderr)
        return False


def submit_and_wait(
    endpoint: str,
    submit_tool: str,
    submit_args: dict,
    status_tool: str,
    result_tool: str,
    output_dir: str | None = "./output",
    output_file: str | None = None,
    auto_filename: bool = False,
    poll_interval: float = 2.0,
    max_polls: int = 300,
    headers: dict | None = None,
    completed_statuses: list[str] | None = None,
    failed_statuses: list[str] | None = None,
    id_param_name: str = "request_id",
    save_logs_to_dir: bool = False,
    save_logs_inline: bool = False,
    runtime_dir: Path | None = None,
    config_path: Path | None = None,
) -> dict:
    """
    Submit job to queue and wait for completion.

    This function has the same interface as run_async_mcp_job() for easy replacement.

    Args:
        endpoint: MCP server endpoint URL
        submit_tool: Name of the submit tool
        submit_args: Arguments for the submit tool
        status_tool: Name of the status checking tool
        result_tool: Name of the result retrieval tool
        output_dir: Output directory (--output)
        output_file: Output file path (--output-file)
        auto_filename: If True, use {request_id}_{timestamp}.{ext} format
        poll_interval: Seconds between status polls
        max_polls: Maximum number of poll attempts
        headers: Additional HTTP headers (auth, etc.)
        completed_statuses: (ignored - daemon handles this)
        failed_statuses: (ignored - daemon handles this)
        id_param_name: Parameter name for job ID (request_id or session_id)
        save_logs_to_dir: Save logs to {output_dir}/logs/
        save_logs_inline: Save logs alongside downloaded file
        runtime_dir: Runtime directory for daemon
        config_path: Config file path for daemon

    Returns dict with:
        - request_id: Job ID
        - status: Final job status
        - download_urls: List of all download URLs found
        - saved_paths: List of all saved file paths
        - download_url: First download URL (backward compat)
        - saved_path: First saved file path (backward compat)
        - log_paths: List of log file paths (if logging enabled)
    """
    job_id = str(uuid.uuid4())

    # Connect to daemon
    sock = connect(runtime_dir, auto_start=True, config_path=config_path)

    try:
        # Submit job
        payload = make_submit_job_payload(
            job_id=job_id,
            endpoint=endpoint,
            submit_tool=submit_tool,
            submit_args=submit_args,
            status_tool=status_tool,
            result_tool=result_tool,
            headers=headers,
            id_param_name=id_param_name,
            poll_interval=poll_interval,
            max_polls=max_polls,
            output_dir=output_dir,
            output_file=output_file,
            auto_filename=auto_filename,
            save_logs_to_dir=save_logs_to_dir,
            save_logs_inline=save_logs_inline,
        )

        sync_send_message(sock, MessageType.SUBMIT_JOB, payload)
        print(f"[QUEUE] Job submitted: {job_id}", file=sys.stderr)

        # Wait for acceptance
        resp = sync_recv_message(sock)
        if not resp:
            raise RuntimeError("Connection closed unexpectedly")

        if resp.get("type") == MessageType.ERROR:
            raise RuntimeError(f"Job submission error: {resp.get('error')}")

        if resp.get("type") != MessageType.JOB_ACCEPTED:
            raise RuntimeError(f"Unexpected response: {resp.get('type')}")

        print(f"[QUEUE] Job accepted, waiting for completion...", file=sys.stderr)

        # Wait for completion
        sock.settimeout(None)  # No timeout while waiting
        while True:
            resp = sync_recv_message(sock)
            if not resp:
                raise RuntimeError("Connection closed while waiting for job")

            msg_type = resp.get("type")

            if msg_type == MessageType.JOB_COMPLETED:
                print(f"[QUEUE] Job completed", file=sys.stderr)
                return resp.get("result", {})

            elif msg_type == MessageType.JOB_FAILED:
                error = resp.get("error", "Unknown error")
                raise RuntimeError(f"Job failed: {error}")

            elif msg_type == MessageType.ERROR:
                raise RuntimeError(f"Error: {resp.get('error')}")

    finally:
        try:
            sock.close()
        except Exception:
            pass


def print_status(status: dict) -> None:
    """Pretty print queue status."""
    print("\n=== MCP Queue Status ===")
    print(f"Running: {status.get('running', 0)}")
    print(f"Queued:  {status.get('queued', 0)}")
    print(f"Completed: {status.get('completed', 0)}")
    print(f"Failed: {status.get('failed', 0)}")

    jobs = status.get("jobs", [])
    if jobs:
        print("\n--- Jobs ---")
        for job in jobs:
            job_id = job.get("job_id", "?")[:8]
            job_status = job.get("status", "?")
            endpoint = job.get("endpoint", "?")
            tool = job.get("submit_tool", "?")
            print(f"  {job_id}... [{job_status}] {endpoint} -> {tool}")


def main():
    parser = argparse.ArgumentParser(description="MCP Queue Client")
    parser.add_argument("--status", "-s", action="store_true", help="Show queue status")
    parser.add_argument("--shutdown", action="store_true", help="Shutdown daemon")
    parser.add_argument("--start", action="store_true", help="Start daemon")
    parser.add_argument("--runtime-dir", help="Runtime directory")
    parser.add_argument("--config", "-c", help="Config file path")

    # Job submission arguments (for testing)
    parser.add_argument("--endpoint", "-e", help="MCP endpoint")
    parser.add_argument("--submit-tool", help="Submit tool name")
    parser.add_argument("--status-tool", help="Status tool name")
    parser.add_argument("--result-tool", help="Result tool name")
    parser.add_argument("--args", "-a", help="Submit arguments (JSON)")
    parser.add_argument("--output", "-o", default="./output", help="Output directory")

    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir) if args.runtime_dir else None
    config_path = Path(args.config) if args.config else None

    if args.status:
        status = get_status(runtime_dir)
        if status:
            print_status(status)
        else:
            print("Daemon not running or unreachable")
        sys.exit(0 if status else 1)

    if args.shutdown:
        success = shutdown_daemon(runtime_dir)
        sys.exit(0 if success else 1)

    if args.start:
        success = start_daemon(runtime_dir, config_path)
        sys.exit(0 if success else 1)

    # Job submission
    if args.endpoint and args.submit_tool:
        submit_args = json.loads(args.args) if args.args else {}
        result = submit_and_wait(
            endpoint=args.endpoint,
            submit_tool=args.submit_tool,
            submit_args=submit_args,
            status_tool=args.status_tool or "status",
            result_tool=args.result_tool or "result",
            output_dir=args.output,
            runtime_dir=runtime_dir,
            config_path=config_path,
        )
        print("\n=== Result ===")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0)

    # Default: show status
    if is_daemon_running(runtime_dir):
        status = get_status(runtime_dir)
        if status:
            print_status(status)
    else:
        print("Daemon not running")
        print("\nUsage:")
        print("  python mcp_queue_client.py --start       # Start daemon")
        print("  python mcp_queue_client.py --status      # Show status")
        print("  python mcp_queue_client.py --shutdown    # Stop daemon")


if __name__ == "__main__":
    main()
