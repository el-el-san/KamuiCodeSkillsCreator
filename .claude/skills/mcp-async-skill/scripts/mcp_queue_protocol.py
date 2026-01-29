#!/usr/bin/env python3
"""
MCP Queue Protocol
Wire format and message definitions for queue manager communication.

Wire Format:
  4-byte big-endian length prefix + UTF-8 JSON payload

Message Types:
  - ping / pong: Health check
  - submit_job: Submit a new job
  - job_accepted: Job accepted into queue
  - job_completed: Job finished (success or failure)
  - status: Request queue status
  - status_response: Queue status info
  - shutdown: Request daemon shutdown
"""

import json
import struct
from typing import Any

# Header: 4-byte unsigned int (big-endian)
HEADER_FORMAT = ">I"
HEADER_SIZE = 4
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB limit


class MessageType:
    """Message type constants."""
    PING = "ping"
    PONG = "pong"
    SUBMIT_JOB = "submit_job"
    JOB_ACCEPTED = "job_accepted"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    STATUS = "status"
    STATUS_RESPONSE = "status_response"
    SHUTDOWN = "shutdown"
    SHUTDOWN_ACK = "shutdown_ack"
    ERROR = "error"


def encode_message(msg_type: str, payload: dict | None = None) -> bytes:
    """
    Encode a message with length prefix.

    Args:
        msg_type: Message type string
        payload: Optional payload dict

    Returns:
        bytes: Length-prefixed JSON message
    """
    message = {"type": msg_type}
    if payload:
        message.update(payload)

    json_bytes = json.dumps(message, ensure_ascii=False).encode("utf-8")

    if len(json_bytes) > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {len(json_bytes)} > {MAX_MESSAGE_SIZE}")

    header = struct.pack(HEADER_FORMAT, len(json_bytes))
    return header + json_bytes


def decode_header(data: bytes) -> int:
    """
    Decode length header.

    Args:
        data: 4 bytes of header

    Returns:
        int: Message body length
    """
    if len(data) != HEADER_SIZE:
        raise ValueError(f"Invalid header size: {len(data)}")
    length = struct.unpack(HEADER_FORMAT, data)[0]
    if length > MAX_MESSAGE_SIZE:
        raise ValueError(f"Message too large: {length} > {MAX_MESSAGE_SIZE}")
    return length


def decode_message(data: bytes) -> dict:
    """
    Decode JSON message body.

    Args:
        data: UTF-8 JSON bytes

    Returns:
        dict: Parsed message
    """
    return json.loads(data.decode("utf-8"))


async def async_send_message(writer, msg_type: str, payload: dict | None = None) -> None:
    """
    Send a message asynchronously.

    Args:
        writer: asyncio StreamWriter
        msg_type: Message type
        payload: Optional payload
    """
    data = encode_message(msg_type, payload)
    writer.write(data)
    await writer.drain()


async def async_recv_message(reader) -> dict | None:
    """
    Receive a message asynchronously.

    Args:
        reader: asyncio StreamReader

    Returns:
        dict: Parsed message or None if connection closed
    """
    # Read header
    header = await reader.readexactly(HEADER_SIZE)
    if not header:
        return None

    length = decode_header(header)

    # Read body
    body = await reader.readexactly(length)
    if not body:
        return None

    return decode_message(body)


def sync_send_message(sock, msg_type: str, payload: dict | None = None) -> None:
    """
    Send a message synchronously.

    Args:
        sock: socket object
        msg_type: Message type
        payload: Optional payload
    """
    data = encode_message(msg_type, payload)
    sock.sendall(data)


def sync_recv_message(sock) -> dict | None:
    """
    Receive a message synchronously.

    Args:
        sock: socket object

    Returns:
        dict: Parsed message or None if connection closed
    """
    # Read header
    header = b""
    while len(header) < HEADER_SIZE:
        chunk = sock.recv(HEADER_SIZE - len(header))
        if not chunk:
            return None
        header += chunk

    length = decode_header(header)

    # Read body
    body = b""
    while len(body) < length:
        chunk = sock.recv(min(length - len(body), 8192))
        if not chunk:
            return None
        body += chunk

    return decode_message(body)


def make_submit_job_payload(
    job_id: str,
    endpoint: str,
    submit_tool: str,
    submit_args: dict,
    status_tool: str,
    result_tool: str,
    headers: dict | None = None,
    id_param_name: str = "request_id",
    poll_interval: float = 2.0,
    max_polls: int = 300,
    output_dir: str | None = None,
    output_file: str | None = None,
    auto_filename: bool = False,
    save_logs_to_dir: bool = False,
    save_logs_inline: bool = False,
) -> dict:
    """
    Create submit_job payload.

    All parameters match run_async_mcp_job() for easy conversion.
    """
    return {
        "job_id": job_id,
        "endpoint": endpoint,
        "submit_tool": submit_tool,
        "submit_args": submit_args,
        "status_tool": status_tool,
        "result_tool": result_tool,
        "headers": headers,
        "id_param_name": id_param_name,
        "poll_interval": poll_interval,
        "max_polls": max_polls,
        "output_dir": output_dir,
        "output_file": output_file,
        "auto_filename": auto_filename,
        "save_logs_to_dir": save_logs_to_dir,
        "save_logs_inline": save_logs_inline,
    }


def make_job_completed_payload(
    job_id: str,
    success: bool,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    """Create job_completed/job_failed payload."""
    payload = {
        "job_id": job_id,
        "success": success,
    }
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    return payload


def make_status_response_payload(
    running: int,
    queued: int,
    completed: int,
    failed: int,
    jobs: list[dict] | None = None,
) -> dict:
    """Create status_response payload."""
    return {
        "running": running,
        "queued": queued,
        "completed": completed,
        "failed": failed,
        "jobs": jobs or [],
    }
