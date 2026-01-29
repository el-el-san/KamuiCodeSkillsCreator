# KamuiCodeSkillsCreator

MCP (Model Context Protocol) のHTTPサーバからスキルを生成し、非同期ジョブ (submit/status/result) をキュー制御付きで実行するためのツール群です。`.mcp.json` とカタログからスキルを自動生成し、JSON-RPC 2.0 の実行フローとダウンロード処理を一括で扱えます。

**現在のバージョン:** v2.1.0

## 主な機能

- `.mcp.json` + `mcp_tool_catalog.yaml` からスキルを自動生成（複数サーバ対応）
- Lazyモードで `SKILL.md` を軽量化し、詳細定義は YAML に分離
- JSON Schema の詳細（`enum` / `default` / `minimum` / `maximum` / `items` など）をパススルー保持
- JSON-RPC 2.0 の submit → status → result パターンを自動化
- キューデーモンによる並列数制限・レート制限・WAL で安定運用
- 出力パス戦略（`--output` / `--output-file` / 拡張子自動検出 / 重複回避 / ログ保存）

## 必要環境

- Python 3.7+
- 依存ライブラリ: `requests`, `pyyaml`
- 任意: `fal_client`（ローカルファイルのアップロード補助）

```bash
pip install requests pyyaml
# 任意
pip install fal_client
```

## 使い方

### 1) スキル生成

```bash
python .claude/skills/mcp-async-skill/scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json
```

Lazyモード（コンテキスト節約）:

```bash
python .claude/skills/mcp-async-skill/scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --lazy
```

### 2) キューデーモン起動

```bash
python .claude/skills/mcp-async-skill/scripts/mcp_queue_daemon.py --background
```

### 3) 非同期ツール実行

```bash
python .claude/skills/mcp-async-skill/scripts/mcp_async_call.py \
  --endpoint "https://mcp.example.com/sse" \
  --submit-tool "submit_tool_name" \
  --status-tool "status_tool_name" \
  --result-tool "result_tool_name" \
  --args '{"prompt": "a cat"}' \
  --output ./output
```

### 4) キュー操作（状態確認 / 停止）

```bash
python .claude/skills/mcp-async-skill/scripts/mcp_queue_client.py --status
python .claude/skills/mcp-async-skill/scripts/mcp_queue_client.py --shutdown
```

## 生成されるスキル構成

**通常モード:**

```
.claude/skills/<skill-name>/
├── SKILL.md
├── queue_config.yaml
├── scripts/
│   ├── mcp_async_call.py
│   ├── mcp_queue_client.py
│   ├── mcp_queue_protocol.py
│   └── <skill_name>.py
└── references/
    ├── mcp.json
    └── tools.json
```

**Lazyモード:**

```
.claude/skills/<skill-name>/
├── SKILL.md
├── queue_config.yaml
├── scripts/
│   ├── mcp_async_call.py
│   ├── mcp_queue_client.py
│   ├── mcp_queue_protocol.py
│   └── <skill_name>.py
└── references/
    ├── mcp.json
    └── tools/
        └── <skill-name>.yaml
```

## 設定（並列・レート制限）

`queue_config.yaml` で同時実行数やレート制限を調整できます。

- `max_concurrent`: 同時実行数の上限
- `start_interval`: ジョブ開始間隔（秒）
- `global_rate_per_min` / `global_burst`: グローバルレート制限
- `endpoint_rates`: エンドポイント別のレート制限

## 出力パス戦略（要点）

- `--output` は出力ディレクトリ
- `--output-file` は出力ファイルパス（上書き許可）
- 拡張子は `--output-file` > `Content-Type` > URL の順で決定
- `--output-file` 未指定時は同名回避のため連番サフィックス付与

## ドキュメント

- `docs/lazy-mode.md`
- `docs/schema-passthrough.md`
- `docs/output-path-strategy.md`

## バージョン履歴

- **v2.1.0（現状）**: 並列処理の制限を加えたもの（キューデーモンで同時実行数・起動間隔・レート制限を管理）
- **v2.0.0**: フォーク版（ https://github.com/Yumeno/LazyKamuiCodeSkillsCreator.git ）を取り込んだもの
- **v1.0.0**: 初期版
