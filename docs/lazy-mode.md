# Lazyモード

## 目的

Lazyモードは、MCPスキル生成時のコンテキストウィンドウ消費を最小化するために設計されました。

### 背景・課題

オリジナル実装では、SKILL.mdに全ツールの詳細パラメータ情報を埋め込んでいました：

```markdown
### flux_lora_submit
Submit Flux LoRA image generation request

**Parameters:**
  - `prompt`* (string): Image prompt
  - `lora_path` (string): LoRA model path
  - `aspect_ratio` (string): Output aspect ratio
  - `steps` (integer): Number of inference steps
  ...（数十のパラメータ）
```

MCPサーバーによっては数十のツールと数百のパラメータを持つため、SKILL.md読み込み時に大量のトークンを消費し、LLMのコンテキストウィンドウを圧迫していました。

### 解決アプローチ

- SKILL.mdには**ツール名と説明のみ**を記載
- 詳細なパラメータ情報は**外部YAMLファイル**に分離
- LLMは実行直前に必要なYAMLのみを読み込む

## 最終要件

1. **初期コンテキスト削減**: SKILL.md読み込み時のトークン消費を大幅に削減
2. **実行時情報取得**: LLMがツール実行前に必要な情報を取得できる
3. **自己完結型YAML**: YAMLファイル1つで実行方法まで理解できる
4. **後方互換性**: 通常モードも引き続きサポート

## 機能設計

### ディレクトリ構造

**通常モード:**
```
.claude/skills/<skill-name>/
├── SKILL.md              # 全ツール詳細含む（大）
├── scripts/
│   ├── mcp_async_call.py
│   └── <skill_name>.py
└── references/
    ├── mcp.json
    └── tools.json        # JSON形式
```

**Lazyモード:**
```
.claude/skills/<skill-name>/
├── SKILL.md              # ツール名+説明のみ（小）
├── scripts/
│   ├── mcp_async_call.py
│   └── <skill_name>.py
└── references/
    ├── mcp.json
    └── tools/
        └── <skill-name>.yaml  # YAML形式 + 実行例
```

### SKILL.mdの違い

**通常モード（詳細埋め込み）:**
```markdown
## Available Tools

### flux_lora_submit
Submit Flux LoRA image generation request

**Parameters:**
  - `prompt`* (string): Image prompt [default: none]
  - `lora_path` (string): LoRA model path
  - `aspect_ratio` (string): Output aspect ratio [options: ['16:9', '4:3', '1:1']]
  ...
```

**Lazyモード（軽量）:**

SKILL.mdには最小限の情報のみ記載し、CLI Options、Usage Examples、Quick Startなどの詳細は全てYAMLに集約します。

```markdown
---
name: flux-lora
description: MCP skill for flux-lora...
---

# flux-lora

MCP integration for **flux-lora**.

## Endpoint

\`\`\`
https://example.com/sse
\`\`\`

## Available Tools

> **Note:** Detailed tool definitions are NOT included in this document to save context window.
> Before executing any tool, you MUST read the full specification from `references/tools/flux-lora.yaml`.

**Quick reference** (name and description only):

- **flux_lora_submit**: Submit Flux LoRA image generation request
- **flux_lora_status**: Check job status
- **flux_lora_result**: Get generation result

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/<skill>.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/flux-lora.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

\`\`\`bash
# Read tool specification first
cat references/tools/flux-lora.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/flux-lora/scripts/mcp_async_call.py --help
\`\`\`

## References

- Tool Specs & Usage: `references/tools/flux-lora.yaml`
- MCP Config: `references/mcp.json`
```

**ポイント:**
- CLI Options テーブルは含まれない（YAMLの `_usage.options` を参照）
- Usage Examples (Python) は含まれない（YAMLの `_usage.bash` を参照）
- Quick Start は最小限のヘルプコマンドのみ

### YAML出力形式

```yaml
_usage:
  description: How to execute this MCP server's tools (run from project root)
  bash: |
    python .claude/skills/flux-lora/scripts/mcp_async_call.py \
      --endpoint "https://example.com/sse" \
      --submit-tool "flux_lora_submit" \
      --status-tool "flux_lora_status" \
      --result-tool "flux_lora_result" \
      --args '{"prompt": "your input here"}' \
      --header "Authorization:Bearer xxx" \
      --output ./output
  wrapper: python .claude/skills/flux-lora/scripts/flux_lora.py --args '{"prompt": "..."}'
  options:
    --endpoint, -e: MCPサーバーのエンドポイントURL
    --config, -c: .mcp.jsonからエンドポイントを読み込み
    --submit-tool: ジョブ送信用ツール名 (必須)
    --status-tool: ステータス確認用ツール名 (必須)
    --result-tool: 結果取得用ツール名 (必須)
    --args, -a: 送信引数 (JSON文字列)
    --args-file: 送信引数をJSONファイルから読み込み
    --output, -o: 出力ディレクトリ (デフォルト: ./output)
    --output-file, -O: 出力ファイルパス (上書き許可)
    --auto-filename: '{request_id}_{timestamp}.{ext} 形式で命名'
    --poll-interval: ポーリング間隔秒数 (デフォルト: 2.0)
    --max-polls: 最大ポーリング回数 (デフォルト: 300)
    --header: カスタムヘッダー追加 (Key:Value形式、複数可)
    --id-param: ジョブIDパラメータ名 (デフォルト: request_id)
    --save-logs: '{output}/logs/ にログ保存'
    --save-logs-inline: 出力ファイル横にログ保存
  notes:
    execution: 必ずプロジェクトルートから実行すること
    output_path: --output の相対パスはカレントディレクトリ基準
    multi_file: 複数URLがある場合は全て自動ダウンロード (連番サフィックス付与)
    extension: 拡張子は --output-file > Content-Type > URL の優先順位で決定

flux_lora_submit:
  description: Submit Flux LoRA image generation request
  required:
    - prompt
  parameters:
    prompt:
      type: string
      description: Image prompt
    aspect_ratio:
      type: string
      description: Output aspect ratio
      enum: ["16:9", "4:3", "1:1", "9:16"]
      default: "1:1"
    steps:
      type: integer
      description: Number of inference steps
      default: 28
      minimum: 1
      maximum: 100

flux_lora_status:
  description: Check job status
  required:
    - request_id
  parameters:
    request_id:
      type: string
      description: Request ID from submit

flux_lora_result:
  description: Get generation result
  required:
    - request_id
  parameters:
    request_id:
      type: string
      description: Request ID
```

## 実装仕様詳細

### CLIオプション

```bash
python generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --lazy  # このフラグでLazyモード有効化
```

### 関連関数

#### `generate_skill_md(mcp_config, tools, skill_name, lazy=False)`

SKILL.mdを生成する関数。`lazy=True`の場合：

```python
if lazy:
    # Lazy mode: minimal tool list (name + description only)
    tool_list = []
    for tool in tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        tool_list.append(f"- **{name}**: {desc}")

    yaml_file = f"references/tools/{skill_name}.yaml"
    tool_docs_section = f"""## Available Tools

> **Note:** Detailed tool definitions are NOT included...
"""
```

#### `convert_tools_to_yaml_dict(tools, mcp_config, skill_name)`

ツール定義をYAML形式に変換する関数：

```python
def convert_tools_to_yaml_dict(tools, mcp_config=None, skill_name=None):
    result = {}

    # Add _usage section with execution examples and CLI reference
    if mcp_config:
        result["_usage"] = {
            "description": "How to execute this MCP server's tools (run from project root)",
            "bash": bash_example,
            "wrapper": wrapper_example,
            "options": {
                "--endpoint, -e": "MCPサーバーのエンドポイントURL",
                "--config, -c": ".mcp.jsonからエンドポイントを読み込み",
                # ... all CLI options
            },
            "notes": {
                "execution": "必ずプロジェクトルートから実行すること",
                "output_path": "--output の相対パスはカレントディレクトリ基準",
                "multi_file": "複数URLがある場合は全て自動ダウンロード (連番サフィックス付与)",
                "extension": "拡張子は --output-file > Content-Type > URL の優先順位で決定",
            },
        }

    # Add tool definitions with full schema
    for tool in tools:
        params = {}
        for pname, pspec in properties.items():
            params[pname] = {
                "type": pspec.get("type", "any"),
                "description": pspec.get("description", ""),
            }
            # Passthrough all additional schema fields
            for key, value in pspec.items():
                if key not in ("type", "description"):
                    params[pname][key] = value

        result[name] = {
            "description": tool.get("description", ""),
            "required": required,
            "parameters": params,
        }

    return result
```

**`_usage`セクションの構造:**
- `description`: YAMLファイルの用途説明
- `bash`: 完全なコマンド例（プロジェクトルートからの絶対パス）
- `wrapper`: 簡易ラッパースクリプト呼び出し例
- `options`: CLI全オプションの説明（キー=オプション名、値=説明）
- `notes`: 実行時の注意事項

### LLMの利用フロー

1. **スキル読み込み**: LLMがSKILL.mdを読む（軽量なのでコンテキスト消費小）
2. **ツール特定**: ユーザーの指示から使用するツールを特定
3. **YAML読み込み**: `references/tools/<skill>.yaml`を読んで詳細確認
4. **実行**: `_usage`セクションの例を参考にコマンド構築・実行

### トークン節約効果の例

| MCPサーバー | ツール数 | 通常モード | Lazyモード | 削減率 |
|------------|---------|-----------|-----------|-------|
| fal-ai/flux-lora | 3 | ~2,500 tokens | ~300 tokens | 88% |
| 大規模MCP | 20+ | ~15,000 tokens | ~500 tokens | 97% |

※ Lazyモードでは CLI Options テーブル、Usage Examples (Python) も含まれないため、さらに軽量。
※ 実行時にYAMLを読むため、総トークン消費は同等。ただし初期コンテキスト占有を削減。

## 使用例

```bash
# Lazyモードでスキル生成
python scripts/generate_skill.py \
  --mcp-config ~/.mcp.json \
  --lazy

# 生成されたファイル確認
ls .claude/skills/fal-ai-flux-lora/
# SKILL.md  scripts/  references/

ls .claude/skills/fal-ai-flux-lora/references/tools/
# fal-ai-flux-lora.yaml
```
