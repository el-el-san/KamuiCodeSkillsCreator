#!/usr/bin/env python3
"""
MCP Skill Generator
Generates a Skill from .mcp.json and mcp_tool_catalog.yaml specifications.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    import requests
except ImportError:
    requests = None

# Force UTF-8 encoding for stdout/stderr (prevents UnicodeEncodeError on Windows)
# Python 3.7+ required. errors='backslashreplace' ensures no crash on unencodable chars.
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
except Exception:
    pass  # reconfigure failed, continue with default encoding

# Default catalog URL
CATALOG_URL = "https://raw.githubusercontent.com/Yumeno/kamuicode-config-manager/main/mcp_tool_catalog.yaml"


def load_mcp_config(path: str) -> dict:
    """Load .mcp.json configuration (single server, legacy).

    Supports formats:
    1. Direct: {"name": "...", "url": "...", ...}
    2. mcpServers wrapper: {"mcpServers": {"name": {...}}}
    """
    with open(path, encoding='utf-8') as f:
        data = json.load(f)

    # Handle mcpServers wrapper format
    if "mcpServers" in data:
        servers = data["mcpServers"]
        if isinstance(servers, dict) and servers:
            # Get first server
            server_name = next(iter(servers))
            server_config = servers[server_name]
            # Flatten to expected format
            result = {"name": server_name, **server_config}
            # Convert headers dict to auth_header/auth_value
            if "headers" in result:
                headers = result.pop("headers")
                if isinstance(headers, dict) and headers:
                    first_key = next(iter(headers))
                    result["auth_header"] = first_key
                    result["auth_value"] = headers[first_key]
                    result["all_headers"] = headers  # Keep all headers
            return result

    return data


def load_all_mcp_servers(path: str) -> dict[str, dict]:
    """Load all MCP server configurations from .mcp.json.

    Returns:
        Dict mapping server_name -> server_config
    """
    with open(path, encoding='utf-8') as f:
        data = json.load(f)

    result = {}

    # Handle mcpServers wrapper format
    if "mcpServers" in data:
        servers = data["mcpServers"]
        if isinstance(servers, dict):
            for server_name, server_config in servers.items():
                # Flatten to expected format
                config = {"name": server_name, **server_config}
                # Convert headers dict to auth_header/auth_value
                if "headers" in config:
                    headers = config.pop("headers")
                    if isinstance(headers, dict) and headers:
                        first_key = next(iter(headers))
                        config["auth_header"] = first_key
                        config["auth_value"] = headers[first_key]
                        config["all_headers"] = headers
                result[server_name] = config
    else:
        # Direct format - single server
        name = data.get("name", "mcp-server")
        result[name] = data

    return result


def load_tools_info(path: str) -> list[dict]:
    """Load tools.info specification (JSON array of tool definitions)."""
    with open(path, encoding='utf-8') as f:
        content = f.read().strip()
        # Handle various formats
        if content.startswith("["):
            return json.loads(content)
        elif content.startswith("{"):
            data = json.loads(content)
            return data.get("tools", [data])
        else:
            # Try line-by-line JSON
            tools = []
            for line in content.splitlines():
                if line.strip():
                    tools.append(json.loads(line))
            return tools


def fetch_catalog(catalog_url: str = CATALOG_URL) -> dict:
    """Fetch mcp_tool_catalog.yaml from URL.

    Returns:
        dict with 'metadata' and 'servers' keys
    """
    if requests is None:
        print("Error: 'requests' module required. Install with: pip install requests", file=sys.stderr)
        sys.exit(1)
    if yaml is None:
        print("Error: 'pyyaml' module required. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching catalog from {catalog_url}...")
    try:
        resp = requests.get(catalog_url, timeout=30)
        resp.raise_for_status()
        catalog = yaml.safe_load(resp.text)
        print(f"Catalog loaded: {catalog['metadata']['total_servers']} servers available")
        return catalog
    except requests.RequestException as e:
        print(f"Error fetching catalog: {e}", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"Error parsing YAML: {e}", file=sys.stderr)
        sys.exit(1)


def find_server_in_catalog(catalog: dict, server_id: str) -> dict | None:
    """Find server by ID in catalog.

    First tries exact match, then partial match with user selection.

    Returns:
        Server dict with 'id', 'status', 'tools' etc., or None if not found
    """
    servers = catalog.get("servers", [])

    # Try exact match first
    for server in servers:
        if server.get("id") == server_id:
            print(f"Found exact match: {server_id}")
            return server

    # No exact match - try partial match
    partial_matches = []
    server_id_lower = server_id.lower()
    for server in servers:
        sid = server.get("id", "")
        if server_id_lower in sid.lower():
            partial_matches.append(server)

    if not partial_matches:
        print(f"No server found matching '{server_id}'", file=sys.stderr)
        return None

    if len(partial_matches) == 1:
        chosen = partial_matches[0]
        print(f"Found partial match: {chosen['id']}")
        return chosen

    # Multiple matches - ask user to choose
    print(f"\nNo exact match for '{server_id}'. Found {len(partial_matches)} partial matches:\n")
    for i, server in enumerate(partial_matches, 1):
        status = server.get("status", "unknown")
        tools_count = len(server.get("tools", []))
        print(f"  {i}. {server['id']} [{status}] ({tools_count} tools)")

    print(f"\n  0. Cancel")

    while True:
        try:
            choice = input("\nSelect server number: ").strip()
            if choice == "0":
                print("Cancelled.")
                return None
            idx = int(choice) - 1
            if 0 <= idx < len(partial_matches):
                chosen = partial_matches[idx]
                print(f"Selected: {chosen['id']}")
                return chosen
            print(f"Invalid choice. Enter 1-{len(partial_matches)} or 0 to cancel.")
        except ValueError:
            print("Please enter a number.")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return None


def load_tools_from_catalog(catalog: dict, server_id: str) -> list[dict]:
    """Load tool definitions from catalog for a given server ID.

    Args:
        catalog: The full catalog dict
        server_id: Server ID to look up

    Returns:
        List of tool definitions
    """
    server = find_server_in_catalog(catalog, server_id)
    if not server:
        return []

    if server.get("status") == "error":
        print(f"Warning: Server '{server_id}' has error status: {server.get('error_message', 'unknown')}", file=sys.stderr)

    tools = server.get("tools", [])
    if not tools:
        print(f"Warning: Server '{server_id}' has no tools defined", file=sys.stderr)

    print(f"Loaded {len(tools)} tools from catalog")
    return tools


def identify_async_pattern(tools: list[dict]) -> dict:
    """
    Identify async tool pattern (submit/status/result).
    Returns dict with tool names for each role.
    """
    pattern = {"submit": [], "status": [], "result": []}

    submit_keywords = ["submit", "create", "start", "generate", "run", "execute", "request"]
    status_keywords = ["status", "check", "poll", "state", "progress"]
    result_keywords = ["result", "get", "fetch", "retrieve", "output", "download"]

    for tool in tools:
        name = tool.get("name", "").lower()
        desc = tool.get("description", "").lower()

        # Classify by name/description - prioritize name over description
        # Submit tools: have submit keywords, don't have status/result in name
        if any(kw in name or kw in desc for kw in submit_keywords):
            if not any(kw in name for kw in status_keywords + result_keywords):
                pattern["submit"].append(tool.get("name"))
        # Status tools: have status keywords, don't have submit/result in name
        if any(kw in name or kw in desc for kw in status_keywords):
            if not any(kw in name for kw in submit_keywords + result_keywords):
                pattern["status"].append(tool.get("name"))
        # Result tools: have result keywords, don't have submit/status in name
        if any(kw in name or kw in desc for kw in result_keywords):
            if not any(kw in name for kw in submit_keywords + status_keywords):
                pattern["result"].append(tool.get("name"))

    return pattern


def detect_media_type(tools: list[dict]) -> str | None:
    """Detect what type of media upload is needed (video/image/audio/file or None)."""
    for tool in tools:
        schema = tool.get("inputSchema", tool.get("parameters", {}))
        properties = schema.get("properties", {})
        for pname in properties.keys():
            pname_lower = pname.lower()
            if "video" in pname_lower:
                return "video"
            if "image" in pname_lower:
                return "image"
            if "audio" in pname_lower:
                return "audio"
            if any(kw in pname_lower for kw in ["file_url", "input_url", "media_url", "source_url"]):
                return "file"
    return None


def convert_tools_to_yaml_dict(tools: list[dict], mcp_config: dict = None, skill_name: str = None) -> dict:
    """Convert tools list to a compact YAML-friendly dict structure with usage examples.

    Args:
        tools: List of tool definitions
        mcp_config: MCP configuration dict (optional, for generating usage examples)
        skill_name: Skill name (optional, for generating usage examples)

    Returns:
        Dict with _usage section and tool names as keys
    """
    result = {}

    # Add usage section if mcp_config is provided
    if mcp_config:
        endpoint = mcp_config.get("url") or mcp_config.get("endpoint", "")
        pattern = identify_async_pattern(tools)
        example_args = get_required_params_example(tools)

        # Build header args for bash
        all_headers = mcp_config.get("all_headers", {})
        auth_header = mcp_config.get("auth_header", "")
        auth_value = mcp_config.get("auth_value", "")

        header_lines = []
        if all_headers:
            for k, v in all_headers.items():
                header_lines.append(f'  --header "{k}:{v}"')
        elif auth_header and auth_value:
            header_lines.append(f'  --header "{auth_header}:{auth_value}"')

        header_arg = " \\\n".join(header_lines) if header_lines else ""
        if header_arg:
            header_arg = " \\\n" + header_arg

        bash_example = f"""python .claude/skills/{skill_name}/scripts/mcp_async_call.py \\
  --endpoint "{endpoint}" \\
  --submit-tool "{pattern['submit'][0] if pattern['submit'] else 'SUBMIT_TOOL'}" \\
  --status-tool "{pattern['status'][0] if pattern['status'] else 'STATUS_TOOL'}" \\
  --result-tool "{pattern['result'][0] if pattern['result'] else 'RESULT_TOOL'}" \\
  --args '{example_args}'{header_arg} \\
  --output ./output"""

        result["_usage"] = {
            "description": "How to execute this MCP server's tools (run from project root)",
            "bash": bash_example,
            "wrapper": f"python .claude/skills/{skill_name}/scripts/{skill_name.replace('-', '_')}.py --args '{example_args}'" if skill_name else None,
            "options": {
                "--endpoint, -e": "MCPサーバーのエンドポイントURL",
                "--config, -c": ".mcp.jsonからエンドポイントを読み込み",
                "--submit-tool": "ジョブ送信用ツール名 (必須)",
                "--status-tool": "ステータス確認用ツール名 (必須)",
                "--result-tool": "結果取得用ツール名 (必須)",
                "--args, -a": "送信引数 (JSON文字列)",
                "--args-file": "送信引数をJSONファイルから読み込み",
                "--output, -o": "出力ディレクトリ (デフォルト: ./output)",
                "--output-file, -O": "出力ファイルパス (上書き許可)",
                "--auto-filename": "{request_id}_{timestamp}.{ext} 形式で命名",
                "--poll-interval": "ポーリング間隔秒数 (デフォルト: 2.0)",
                "--max-polls": "最大ポーリング回数 (デフォルト: 300)",
                "--header": "カスタムヘッダー追加 (Key:Value形式、複数可)",
                "--id-param": "ジョブIDパラメータ名 (デフォルト: request_id)",
                "--save-logs": "{output}/logs/ にログ保存",
                "--save-logs-inline": "出力ファイル横にログ保存",
            },
            "notes": {
                "execution": "必ずプロジェクトルートから実行すること",
                "output_path": "--output の相対パスはカレントディレクトリ基準",
                "multi_file": "複数URLがある場合は全て自動ダウンロード (連番サフィックス付与)",
                "extension": "拡張子は --output-file > Content-Type > URL の優先順位で決定",
            },
        }

    # Add tool definitions
    for tool in tools:
        name = tool.get("name", "")
        schema = tool.get("inputSchema", tool.get("parameters", {}))
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Build compact parameter structure (passthrough all schema fields)
        params = {}
        for pname, pspec in properties.items():
            params[pname] = {
                "type": pspec.get("type", "any"),
                "description": pspec.get("description", ""),
            }
            # Passthrough additional schema fields (enum, default, minimum, maximum, items, etc.)
            for key, value in pspec.items():
                if key not in ("type", "description"):
                    params[pname][key] = value

        result[name] = {
            "description": tool.get("description", ""),
            "required": required,
            "parameters": params,
        }

    return result


def get_required_params_example(tools: list[dict]) -> str:
    """Get example JSON with required parameters from submit tool."""
    for tool in tools:
        name = tool.get("name", "").lower()
        if any(kw in name for kw in ["submit", "create", "start", "generate", "run"]):
            schema = tool.get("inputSchema", tool.get("parameters", {}))
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            example = {}
            for pname in required:
                pspec = properties.get(pname, {})
                ptype = pspec.get("type", "string")
                if "video" in pname.lower():
                    example[pname] = "https://example.com/video.mp4"
                elif "image" in pname.lower():
                    example[pname] = "https://example.com/image.png"
                elif "audio" in pname.lower():
                    example[pname] = "https://example.com/audio.mp3"
                elif "url" in pname.lower():
                    example[pname] = "https://example.com/file"
                elif ptype == "number":
                    example[pname] = 1
                elif ptype == "boolean":
                    example[pname] = True
                else:
                    example[pname] = "your input here"
            if example:
                return json.dumps(example)
    return '{"prompt": "your input here"}'


def generate_skill_md(mcp_config: dict, tools: list[dict], skill_name: str, lazy: bool = False) -> str:
    """Generate SKILL.md content.

    Args:
        mcp_config: MCP configuration dict
        tools: List of tool definitions
        skill_name: Name of the skill
        lazy: If True, generate minimal SKILL.md with tool references only
    """
    endpoint = mcp_config.get("url") or mcp_config.get("endpoint", "")
    server_name = mcp_config.get("name", skill_name)
    auth_header = mcp_config.get("auth_header", "")
    auth_value = mcp_config.get("auth_value", "")

    pattern = identify_async_pattern(tools)

    # Build tool documentation
    if lazy:
        # Lazy mode: minimal tool list (name + description only)
        tool_list = []
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            tool_list.append(f"- **{name}**: {desc}")

        yaml_file = f"references/tools/{skill_name}.yaml"
        tool_docs_section = f"""## Available Tools

> **Note:** Detailed tool definitions are NOT included in this document to save context window.
> Before executing any tool, you MUST read the full specification from `{yaml_file}`.

**Quick reference** (name and description only):

{chr(10).join(tool_list)}

### How to Use Tools

1. **Read tool specification**: Use Read tool on `{yaml_file}`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments
"""
    else:
        # Full mode: existing behavior with detailed parameters
        tool_docs = []
        for tool in tools:
            name = tool.get("name", "")
            desc = tool.get("description", "")
            schema = tool.get("inputSchema", tool.get("parameters", {}))
            properties = schema.get("properties", {})

            params_doc = []
            for pname, pspec in properties.items():
                ptype = pspec.get("type", "any")
                pdesc = pspec.get("description", "")
                required = pname in schema.get("required", [])
                req_mark = "*" if required else ""

                # Build additional schema info
                extras = []
                if "default" in pspec:
                    extras.append(f"default: {pspec['default']}")
                if "enum" in pspec:
                    extras.append(f"options: {pspec['enum']}")
                if "minimum" in pspec:
                    extras.append(f"min: {pspec['minimum']}")
                if "maximum" in pspec:
                    extras.append(f"max: {pspec['maximum']}")
                if "items" in pspec and isinstance(pspec["items"], dict) and "enum" in pspec["items"]:
                    extras.append(f"options: {pspec['items']['enum']}")

                extra_str = f" [{', '.join(extras)}]" if extras else ""
                params_doc.append(f"  - `{pname}`{req_mark} ({ptype}): {pdesc}{extra_str}")

            tool_doc = f"### {name}\n{desc}\n\n**Parameters:**\n" + "\n".join(params_doc) if params_doc else f"### {name}\n{desc}"
            tool_docs.append(tool_doc)

        tool_docs_section = f"""## Available Tools

{chr(10).join(tool_docs)}
"""

    # Authentication section
    all_headers = mcp_config.get("all_headers", {})
    auth_section = ""
    if all_headers:
        headers_lines = "\n".join([f"{k}: {v}" for k, v in all_headers.items()])
        auth_section = f"""
## Authentication

This MCP requires the following headers:

```
{headers_lines}
```
"""
    elif auth_header and auth_value:
        auth_section = f"""
## Authentication

This MCP requires authentication header:

```
{auth_header}: {auth_value}
```
"""

    # Async pattern section
    async_section = ""
    if pattern["submit"] and pattern["status"] and pattern["result"]:
        async_section = f"""
## Async Pattern

This MCP uses async job pattern:

1. **Submit**: `{pattern['submit'][0]}` - Start job, returns request_id
2. **Status**: `{pattern['status'][0]}` - Poll job status with request_id
3. **Result**: `{pattern['result'][0]}` - Get result when completed

Use `scripts/mcp_async_call.py` for automated workflow.
"""

    # File upload section (if tools need URL inputs)
    upload_section = ""
    media_type = detect_media_type(tools)
    if media_type:
        media_configs = {
            "video": {
                "title": "Video Upload",
                "desc": "Before enhancing videos",
                "ext": "mp4",
                "param": "video_url",
                "win_path": "Videos\\clip.mp4",
                "unix_path": "videos/clip.mp4",
                "android_path": "Download/video.mp4"
            },
            "image": {
                "title": "Image Upload",
                "desc": "Before editing images",
                "ext": "png",
                "param": "image_url or image_urls",
                "win_path": "Pictures\\photo.jpg",
                "unix_path": "images/photo.png",
                "android_path": "Download/image.png"
            },
            "audio": {
                "title": "Audio Upload",
                "desc": "Before processing audio",
                "ext": "mp3",
                "param": "audio_url",
                "win_path": "Music\\audio.mp3",
                "unix_path": "music/audio.mp3",
                "android_path": "Download/audio.mp3"
            },
            "file": {
                "title": "File Upload",
                "desc": "Before processing files",
                "ext": "dat",
                "param": "file_url",
                "win_path": "Documents\\file.dat",
                "unix_path": "documents/file.dat",
                "android_path": "Download/file.dat"
            }
        }
        cfg = media_configs.get(media_type, media_configs["file"])
        upload_section = f"""
## {cfg['title']}

{cfg['desc']}, you need to upload local files to get URLs. Use fal_client:

```bash
# Upload {media_type}
python -c "import fal_client; url=fal_client.upload_file(r'/path/to/file.{cfg['ext']}'); print(f'URL: {{url}}')"

# Windows path example
python -c "import fal_client; url=fal_client.upload_file(r'C:\\Users\\name\\{cfg['win_path']}'); print(f'URL: {{url}}')"

# Unix/Linux path example
python -c "import fal_client; url=fal_client.upload_file('/home/user/{cfg['unix_path']}'); print(f'URL: {{url}}')"

# Android (Termux)
python -c "import fal_client; url=fal_client.upload_file('/storage/emulated/0/{cfg['android_path']}'); print(f'URL: {{url}}')"
```

The returned URL can be used in the `{cfg['param']}` parameter.
"""

    # Build header argument for Quick Start
    header_arg = ""
    auth_headers_python = ""
    if all_headers:
        header_lines = [f'  --header "{k}:{v}" \\' for k, v in all_headers.items()]
        header_arg = "\n" + "\n".join(header_lines)
        auth_headers_python = "\n".join([f'    "{k}": "{v}",' for k, v in all_headers.items()])
    elif auth_header and auth_value:
        header_arg = f'\n  --header "{auth_header}:{auth_value}" \\'
        auth_headers_python = f'    "{auth_header}": "{auth_value}",'

    # Get example args based on required parameters
    example_args = get_required_params_example(tools)

    # Generate different content based on lazy mode
    if lazy:
        # Lazy mode: minimal SKILL.md - defer details to YAML
        yaml_ref = f"references/tools/{skill_name}.yaml"
        skill_md = f"""---
name: {skill_name}
description: MCP skill for {server_name}. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling {server_name} tools that require async processing.
---

# {skill_name}

MCP integration for **{server_name}**.

## Endpoint

```
{endpoint}
```
{auth_section}{async_section}{upload_section}
{tool_docs_section}
## How to Execute

> Before executing any tool, **read the full specification** from `{yaml_ref}`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat {yaml_ref}

# Then execute (example from _usage.bash in YAML)
python .claude/skills/{skill_name}/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `{yaml_ref}`
- MCP Config: `references/mcp.json`
"""
    else:
        # Full mode: detailed SKILL.md with all information
        skill_md = f"""---
name: {skill_name}
description: MCP skill for {server_name}. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling {server_name} tools that require async processing.
---

# {skill_name}

MCP integration for **{server_name}**.

## Endpoint

```
{endpoint}
```
{auth_section}
## Quick Start

> **⚠️ 実行ディレクトリについて**
> このスキルは**プロジェクトルートから**実行してください。
> ユーザーが相対パス（例: `./output`）で保存先を指定した場合、プロジェクトルート基準で解釈してください。

```bash
python .claude/skills/{skill_name}/scripts/mcp_async_call.py \\
  --endpoint "{endpoint}" \\
  --submit-tool "{pattern['submit'][0] if pattern['submit'] else 'SUBMIT_TOOL'}" \\
  --status-tool "{pattern['status'][0] if pattern['status'] else 'STATUS_TOOL'}" \\
  --result-tool "{pattern['result'][0] if pattern['result'] else 'RESULT_TOOL'}" \\
  --args '{example_args}' \\{header_arg}
  --output ./output
```

## CLI Options

| オプション | 短縮 | 説明 | デフォルト |
|-----------|------|------|-----------|
| `--endpoint` | `-e` | MCPサーバーのエンドポイントURL | - |
| `--config` | `-c` | .mcp.jsonからエンドポイントを読み込み | - |
| `--submit-tool` | - | ジョブ送信用ツール名 (必須) | - |
| `--status-tool` | - | ステータス確認用ツール名 (必須) | - |
| `--result-tool` | - | 結果取得用ツール名 (必須) | - |
| `--args` | `-a` | 送信引数 (JSON文字列) | - |
| `--args-file` | - | 送信引数をJSONファイルから読み込み | - |
| `--output` | `-o` | 出力ディレクトリ | `./output` |
| `--output-file` | `-O` | 出力ファイルパス (上書き許可) | 自動生成 |
| `--auto-filename` | - | `{{request_id}}_{{timestamp}}.{{ext}}` 形式で命名 | 無効 |
| `--poll-interval` | - | ポーリング間隔 (秒) | `2.0` |
| `--max-polls` | - | 最大ポーリング回数 | `300` |
| `--header` | - | カスタムヘッダー追加 (`Key:Value`形式、複数可) | - |
| `--id-param` | - | ジョブIDパラメータ名 | `request_id` |
| `--save-logs` | - | `{{output}}/logs/` にログ保存 | 無効 |
| `--save-logs-inline` | - | 出力ファイル横にログ保存 | 無効 |

### 出力パス決定ルール

1. `--output-file` 指定時: そのパスに保存 (上書き許可)
2. `--auto-filename` 指定時: `{{request_id}}_{{timestamp}}.{{ext}}` 形式
3. 上記以外: URLまたはContent-Dispositionからファイル名を推測

### 拡張子決定ルール

1. `--output-file` の拡張子
2. レスポンスの `Content-Type` ヘッダー
3. URLのパス
4. 検出不可の場合は警告表示

### 複数ファイル対応

MCPサーバーが複数のURLを返す場合、全て自動でダウンロードされます:
- `--output-file result.png` → `result_1.png`, `result_2.png`, ...
- 戻り値に `saved_paths` (リスト) と `saved_path` (最初のファイル、後方互換) が含まれます

{async_section}{upload_section}
{tool_docs_section}
## Usage Examples

### Direct JSON-RPC Call

```python
import requests
import json
import uuid

ENDPOINT = "{endpoint}"
HEADERS = {{
    "Content-Type": "application/json",
{auth_headers_python}
}}

# Step 1: Initialize session (required for MCP protocol)
session_id = str(uuid.uuid4())
headers = {{**HEADERS, "Mcp-Session-Id": session_id}}

init_payload = {{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "initialize",
    "params": {{
        "protocolVersion": "2024-11-05",
        "capabilities": {{}},
        "clientInfo": {{"name": "client", "version": "1.0.0"}}
    }}
}}

resp = requests.post(ENDPOINT, headers=headers, json=init_payload)
# Get session ID from response headers
session_id = resp.headers.get("Mcp-Session-Id", session_id)
headers["Mcp-Session-Id"] = session_id

# Step 2: Submit request
payload = {{
    "jsonrpc": "2.0",
    "id": "2",
    "method": "tools/call",
    "params": {{
        "name": "{pattern['submit'][0] if pattern['submit'] else 'tool_name'}",
        "arguments": {example_args}
    }}
}}

resp = requests.post(ENDPOINT, headers=headers, json=payload)
result = resp.json()
request_id = result["result"]["request_id"]
print(f"Request ID: {{request_id}}")
```

### Polling Pattern

```python
import time

# Poll until complete
while True:
    status_payload = {{
        "jsonrpc": "2.0",
        "id": "3",
        "method": "tools/call",
        "params": {{
            "name": "{pattern['status'][0] if pattern['status'] else 'status_tool'}",
            "arguments": {{"request_id": request_id}}
        }}
    }}
    resp = requests.post(ENDPOINT, headers=headers, json=status_payload)
    status = resp.json()["result"]["status"]
    print(f"Status: {{status}}")

    if status in ["completed", "done", "success", "COMPLETED"]:
        break
    if status in ["failed", "error", "FAILED"]:
        raise Exception("Job failed")

    time.sleep(2)

# Get result
result_payload = {{
    "jsonrpc": "2.0",
    "id": "4",
    "method": "tools/call",
    "params": {{
        "name": "{pattern['result'][0] if pattern['result'] else 'result_tool'}",
        "arguments": {{"request_id": request_id}}
        }}
}}
resp = requests.post(ENDPOINT, headers=headers, json=result_payload)
result = resp.json()["result"]
print(f"Download URL: {{result['url']}}")
```

## References

- MCP Config: `references/mcp.json`
- Tool Specs: `references/tools.json`
"""
    return skill_md


def detect_id_param_name(tools: list[dict]) -> str:
    """Detect the ID parameter name used in status/result tools (request_id or session_id)."""
    for tool in tools:
        schema = tool.get("inputSchema", tool.get("parameters", {}))
        properties = schema.get("properties", {})
        # Check for request_id first (fal.ai style)
        if "request_id" in properties or "requestId" in properties:
            return "request_id"
        if "session_id" in properties or "sessionId" in properties:
            return "session_id"
    return "request_id"  # Default to request_id


def generate_wrapper_script(mcp_config: dict, tools: list[dict], skill_name: str) -> str:
    """Generate convenience wrapper script that delegates to mcp_async_call.py."""
    endpoint = mcp_config.get("url") or mcp_config.get("endpoint", "")
    pattern = identify_async_pattern(tools)
    id_param_name = detect_id_param_name(tools)

    # Get auth headers for --header arguments
    all_headers = mcp_config.get("all_headers", {})
    auth_header = mcp_config.get("auth_header", "")
    auth_value = mcp_config.get("auth_value", "")

    header_defaults = []
    if all_headers:
        for k, v in all_headers.items():
            header_defaults.append(f'    ("--header", "{k}:{v}"),')
    elif auth_header and auth_value:
        header_defaults.append(f'    ("--header", "{auth_header}:{auth_value}"),')

    header_defaults_str = "\n".join(header_defaults) if header_defaults else ""

    script = f'''#!/usr/bin/env python3
"""
{skill_name} - Wrapper with preset defaults for mcp_async_call.py

This wrapper pre-configures endpoint, tool names, and authentication.
All mcp_async_call.py options are supported and can override defaults.

Usage:
    python {skill_name.replace('-', '_')}.py --args '{{"prompt": "..."}}'
    python {skill_name.replace('-', '_')}.py --args '{{"prompt": "..."}}' --auto-filename --save-logs
    python {skill_name.replace('-', '_')}.py --help  # Show all available options
"""

import sys
import os

# Force UTF-8 encoding for stdout/stderr (prevents UnicodeEncodeError on Windows)
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
except Exception:
    pass

# Preset defaults (injected if not specified by user)
DEFAULTS = [
    ("--endpoint", "{endpoint}"),
    ("--submit-tool", "{pattern['submit'][0] if pattern['submit'] else 'submit'}"),
    ("--status-tool", "{pattern['status'][0] if pattern['status'] else 'status'}"),
    ("--result-tool", "{pattern['result'][0] if pattern['result'] else 'result'}"),
    ("--id-param", "{id_param_name}"),
{header_defaults_str}
]

def main():
    # Inject defaults into sys.argv (only if not already specified)
    args = sys.argv[1:]
    for key, value in DEFAULTS:
        # For --header, always add (can have multiple)
        if key == "--header":
            if key not in args:
                args = [key, value] + args
        # For other options, only add if not specified
        elif key not in args:
            args = [key, value] + args

    sys.argv = [sys.argv[0]] + args

    # Import and run mcp_async_call.py main
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_async_call import main as mcp_main
    mcp_main()


if __name__ == "__main__":
    main()
'''
    return script


def generate_skill(
    mcp_config_path: str,
    output_dir: str,
    skill_name: str | None = None,
    tools_info_path: str | None = None,
    catalog_url: str = CATALOG_URL,
    lazy: bool = False,
):
    """Generate complete skill from MCP config and catalog.

    Args:
        mcp_config_path: Path to .mcp.json
        output_dir: Output directory for generated skill
        skill_name: Skill name (auto-detected if not specified)
        tools_info_path: Optional path to tools.info (legacy mode)
        catalog_url: URL to mcp_tool_catalog.yaml
        lazy: If True, generate minimal SKILL.md (tool definitions in references/tools.json)
    """
    mcp_config = load_mcp_config(mcp_config_path)

    # Get server ID from mcp_config
    server_id = mcp_config.get("name", "")

    # Load tools - from catalog or legacy tools.info
    if tools_info_path:
        print(f"Using legacy tools.info: {tools_info_path}")
        tools = load_tools_info(tools_info_path)
    else:
        # Fetch from catalog
        catalog = fetch_catalog(catalog_url)
        tools = load_tools_from_catalog(catalog, server_id)
        if not tools:
            print(f"Error: Could not load tools for server '{server_id}'", file=sys.stderr)
            sys.exit(1)

    # Determine skill name
    if not skill_name:
        skill_name = mcp_config.get("name", "mcp-skill")
        skill_name = re.sub(r"[^a-z0-9-]", "-", skill_name.lower())

    skill_dir = Path(output_dir) / skill_name
    scripts_dir = skill_dir / "scripts"
    references_dir = skill_dir / "references"

    # Create directories
    os.makedirs(scripts_dir, exist_ok=True)
    os.makedirs(references_dir, exist_ok=True)

    # Generate SKILL.md
    skill_md = generate_skill_md(mcp_config, tools, skill_name, lazy=lazy)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding='utf-8')

    # Copy mcp_async_call.py
    async_script = Path(__file__).parent / "mcp_async_call.py"
    if async_script.exists():
        (scripts_dir / "mcp_async_call.py").write_text(async_script.read_text(encoding='utf-8'), encoding='utf-8')

    # Generate wrapper script
    wrapper = generate_wrapper_script(mcp_config, tools, skill_name)
    wrapper_path = scripts_dir / f"{skill_name.replace('-', '_')}.py"
    wrapper_path.write_text(wrapper, encoding='utf-8')
    os.chmod(wrapper_path, 0o755)

    # Save original configs as references
    (references_dir / "mcp.json").write_text(json.dumps(mcp_config, indent=2, ensure_ascii=False), encoding='utf-8')

    if lazy:
        # Lazy mode: save as YAML in tools/ directory
        tools_dir = references_dir / "tools"
        os.makedirs(tools_dir, exist_ok=True)

        # Convert to compact YAML structure
        yaml_data = convert_tools_to_yaml_dict(tools, mcp_config, skill_name)
        yaml_filename = f"{skill_name}.yaml"

        if yaml is None:
            print("Warning: pyyaml not installed, falling back to JSON format", file=sys.stderr)
            (tools_dir / f"{skill_name}.json").write_text(
                json.dumps(yaml_data, indent=2, ensure_ascii=False), encoding='utf-8'
            )
        else:
            yaml_content = yaml.dump(yaml_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
            (tools_dir / yaml_filename).write_text(yaml_content, encoding='utf-8')

        print(f"\n✓ Skill generated (lazy mode): {skill_dir}")
        print(f"  {skill_dir}/")
        print(f"  ├── SKILL.md")
        print(f"  ├── scripts/")
        print(f"  │   ├── mcp_async_call.py")
        print(f"  │   └── {skill_name.replace('-', '_')}.py")
        print(f"  └── references/")
        print(f"      ├── mcp.json")
        print(f"      └── tools/")
        print(f"          └── {yaml_filename}")
    else:
        # Normal mode: save as JSON
        (references_dir / "tools.json").write_text(json.dumps(tools, indent=2, ensure_ascii=False), encoding='utf-8')

        print(f"\n✓ Skill generated: {skill_dir}")
        print(f"  {skill_dir}/")
        print(f"  ├── SKILL.md")
        print(f"  ├── scripts/")
        print(f"  │   ├── mcp_async_call.py")
        print(f"  │   └── {skill_name.replace('-', '_')}.py")
        print(f"  └── references/")
        print(f"      ├── mcp.json")
        print(f"      └── tools.json")

    return str(skill_dir)


def get_default_output_dir() -> str:
    """Get default output directory (.claude/skills relative to cwd or script location)."""
    # Try current working directory first
    cwd_skills = Path.cwd() / ".claude" / "skills"
    if cwd_skills.parent.exists():  # .claude exists
        return str(cwd_skills)

    # Fall back to script location
    script_skills = Path(__file__).parent.parent.parent
    if script_skills.name == "skills" and script_skills.parent.name == ".claude":
        return str(script_skills)

    # Default to cwd/.claude/skills
    return str(cwd_skills)


def generate_skills_for_servers(
    mcp_config_path: str,
    output_dir: str,
    server_names: list[str] | None = None,
    catalog_url: str = CATALOG_URL,
    lazy: bool = False,
) -> list[str]:
    """Generate skills for multiple servers from a single mcp.json.

    Args:
        mcp_config_path: Path to .mcp.json
        output_dir: Output directory for generated skills
        server_names: List of server names to process (None = all servers)
        catalog_url: URL to mcp_tool_catalog.yaml
        lazy: If True, generate minimal SKILL.md

    Returns:
        List of generated skill directory paths
    """
    all_servers = load_all_mcp_servers(mcp_config_path)

    if not all_servers:
        print("Error: No servers found in mcp.json", file=sys.stderr)
        sys.exit(1)

    # Determine which servers to process
    if server_names:
        # Validate specified servers exist
        missing = set(server_names) - set(all_servers.keys())
        if missing:
            print(f"Error: Server(s) not found in mcp.json: {', '.join(missing)}", file=sys.stderr)
            print(f"Available servers: {', '.join(all_servers.keys())}", file=sys.stderr)
            sys.exit(1)
        servers_to_process = {name: all_servers[name] for name in server_names}
    else:
        servers_to_process = all_servers

    print(f"Processing {len(servers_to_process)} server(s): {', '.join(servers_to_process.keys())}\n")

    generated_paths = []
    for server_name, mcp_config in servers_to_process.items():
        print(f"--- Generating skill for: {server_name} ---")

        # Fetch catalog
        catalog = fetch_catalog(catalog_url)
        tools = load_tools_from_catalog(catalog, server_name)

        if not tools:
            print(f"Warning: No tools found for '{server_name}', skipping", file=sys.stderr)
            continue

        # Generate skill name from server name
        skill_name = re.sub(r"[^a-z0-9-]", "-", server_name.lower())

        # Generate skill
        path = generate_skill_internal(
            mcp_config=mcp_config,
            tools=tools,
            output_dir=output_dir,
            skill_name=skill_name,
            lazy=lazy,
        )
        generated_paths.append(path)

    return generated_paths


def generate_skill_internal(
    mcp_config: dict,
    tools: list[dict],
    output_dir: str,
    skill_name: str,
    lazy: bool = False,
) -> str:
    """Internal function to generate a single skill (no catalog fetch).

    Args:
        mcp_config: MCP configuration dict (already loaded)
        tools: List of tool definitions (already loaded)
        output_dir: Output directory for generated skill
        skill_name: Skill name
        lazy: If True, generate minimal SKILL.md

    Returns:
        Path to generated skill directory
    """
    skill_dir = Path(output_dir) / skill_name
    scripts_dir = skill_dir / "scripts"
    references_dir = skill_dir / "references"

    # Create directories
    os.makedirs(scripts_dir, exist_ok=True)
    os.makedirs(references_dir, exist_ok=True)

    # Generate SKILL.md
    skill_md = generate_skill_md(mcp_config, tools, skill_name, lazy=lazy)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding='utf-8')

    # Copy mcp_async_call.py
    async_script = Path(__file__).parent / "mcp_async_call.py"
    if async_script.exists():
        (scripts_dir / "mcp_async_call.py").write_text(async_script.read_text(encoding='utf-8'), encoding='utf-8')

    # Generate wrapper script
    wrapper = generate_wrapper_script(mcp_config, tools, skill_name)
    wrapper_path = scripts_dir / f"{skill_name.replace('-', '_')}.py"
    wrapper_path.write_text(wrapper, encoding='utf-8')
    os.chmod(wrapper_path, 0o755)

    # Save original configs as references
    (references_dir / "mcp.json").write_text(json.dumps(mcp_config, indent=2, ensure_ascii=False), encoding='utf-8')

    if lazy:
        # Lazy mode: save as YAML in tools/ directory
        tools_dir = references_dir / "tools"
        os.makedirs(tools_dir, exist_ok=True)

        # Convert to compact YAML structure
        yaml_data = convert_tools_to_yaml_dict(tools, mcp_config, skill_name)
        yaml_filename = f"{skill_name}.yaml"

        if yaml is None:
            print("Warning: pyyaml not installed, falling back to JSON format", file=sys.stderr)
            (tools_dir / f"{skill_name}.json").write_text(
                json.dumps(yaml_data, indent=2, ensure_ascii=False), encoding='utf-8'
            )
        else:
            yaml_content = yaml.dump(yaml_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
            (tools_dir / yaml_filename).write_text(yaml_content, encoding='utf-8')

        print(f"\n✓ Skill generated (lazy mode): {skill_dir}")
        print(f"  {skill_dir}/")
        print(f"  ├── SKILL.md")
        print(f"  ├── scripts/")
        print(f"  │   ├── mcp_async_call.py")
        print(f"  │   └── {skill_name.replace('-', '_')}.py")
        print(f"  └── references/")
        print(f"      ├── mcp.json")
        print(f"      └── tools/")
        print(f"          └── {yaml_filename}")
    else:
        # Normal mode: save as JSON
        (references_dir / "tools.json").write_text(json.dumps(tools, indent=2, ensure_ascii=False), encoding='utf-8')

        print(f"\n✓ Skill generated: {skill_dir}")
        print(f"  {skill_dir}/")
        print(f"  ├── SKILL.md")
        print(f"  ├── scripts/")
        print(f"  │   ├── mcp_async_call.py")
        print(f"  │   └── {skill_name.replace('-', '_')}.py")
        print(f"  └── references/")
        print(f"      ├── mcp.json")
        print(f"      └── tools.json")

    return str(skill_dir)


def main():
    default_output = get_default_output_dir()
    parser = argparse.ArgumentParser(
        description="Generate MCP Skill from config files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate skills for ALL servers in mcp.json
  python generate_skill.py -m mcp.json

  # Generate skill for specific server(s) only
  python generate_skill.py -m mcp.json -s fal-ai/flux-lora
  python generate_skill.py -m mcp.json -s server1 -s server2

  # Generate minimal SKILL.md (lazy loading mode)
  python generate_skill.py -m mcp.json --lazy

  # Generate skill with legacy tools.info (single server only)
  python generate_skill.py -m mcp.json -t tools.info

  # Use custom catalog URL
  python generate_skill.py -m mcp.json --catalog-url https://example.com/catalog.yaml

  # Specify custom output directory
  python generate_skill.py -m mcp.json -o ./custom/path
"""
    )
    parser.add_argument("--mcp-config", "-m", required=True, help="Path to .mcp.json")
    parser.add_argument("--servers", "-s", action="append", dest="servers",
                        help="Server name(s) to generate (can specify multiple, default: all)")
    parser.add_argument("--tools-info", "-t", help="Path to tools.info (legacy mode, single server only)")
    parser.add_argument("--output", "-o", default=default_output,
                        help=f"Output directory (default: {default_output})")
    parser.add_argument("--name", "-n", help="Skill name (auto-detected if not specified, only for single server)")
    parser.add_argument("--catalog-url", default=CATALOG_URL,
                        help=f"URL to mcp_tool_catalog.yaml (default: {CATALOG_URL})")
    parser.add_argument("--lazy", "-l", action="store_true",
                        help="Generate minimal SKILL.md (tool definitions in references/tools/*.yaml)")

    args = parser.parse_args()

    # Legacy mode: tools.info specified (single server only)
    if args.tools_info:
        if args.servers and len(args.servers) > 1:
            print("Error: --tools-info only supports single server mode", file=sys.stderr)
            sys.exit(1)
        generate_skill(
            mcp_config_path=args.mcp_config,
            output_dir=args.output,
            skill_name=args.name,
            tools_info_path=args.tools_info,
            catalog_url=args.catalog_url,
            lazy=args.lazy,
        )
    else:
        # Multi-server mode
        generate_skills_for_servers(
            mcp_config_path=args.mcp_config,
            output_dir=args.output,
            server_names=args.servers,
            catalog_url=args.catalog_url,
            lazy=args.lazy,
        )


if __name__ == "__main__":
    main()
