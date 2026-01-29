# 出力パス戦略

## 目的

ダウンロードファイルの保存先パス・ファイル名・拡張子を柔軟かつ直感的に制御するための仕組みを提供します。

### 背景・課題

オリジナル実装では `--output` オプションが「ディレクトリ」と「ファイルパス」の両方に使われており、以下の問題がありました：

1. **曖昧な動作**: `--output ./result` がディレクトリなのかファイル名なのか不明確
2. **拡張子欠落**: ファイルが拡張子なしで保存されることがある
3. **上書き問題**: 同名ファイルが意図せず上書きされる
4. **デバッグ困難**: リクエスト/レスポンスのログが残らない

### 解決アプローチ

Linux CLIツール（curl, wget等）の慣例に従い、役割を明確に分離：

- `--output` (`-o`): 出力**ディレクトリ**指定
- `--output-file` (`-O`): 出力**ファイルパス**指定
- Content-Typeベースの拡張子自動検出
- 重複ファイル回避の自動サフィックス付与

## 最終要件

### 出力パス決定

1. `--output-file` 指定時はそのパスを使用（上書き許可）
2. 未指定時は `--output` ディレクトリ + 自動生成ファイル名
3. ファイル名のみの `--output-file` は `--output` と組み合わせ

### 拡張子決定

優先順位：
1. `--output-file` で明示的に指定された拡張子
2. ダウンロード時の `Content-Type` ヘッダーから推測
3. URLのパスから抽出
4. 検出できない場合は警告を表示（拡張子なし）

### ファイル名決定

優先順位：
1. `--output-file` で明示的に指定
2. `--auto-filename` 有効時: `{request_id}_{timestamp}.{ext}`
3. レスポンスの `Content-Disposition` ヘッダー
4. URLのパスから抽出
5. `request_id` があれば使用
6. フォールバック: `output`

### 重複回避

`--output-file` **未指定**の場合のみ、同名ファイル存在時にサフィックス付与：
- `output.png` → `output_1.png` → `output_2.png` → ...

## 機能設計

### CLIオプション

| オプション | 説明 | デフォルト |
|-----------|------|-----------|
| `--output, -o` | 出力ディレクトリ | `./output` |
| `--output-file, -O` | 出力ファイルパス（上書き許可） | なし |
| `--auto-filename` | `{request_id}_{timestamp}.{ext}` 形式 | 無効 |
| `--save-logs` | `{output}/logs/` にログ保存 | 無効 |
| `--save-logs-inline` | ファイル横にログ保存 | 無効 |

### パス解決ロジック

```
--output-file 指定あり？
  ├─ Yes: パス含む？
  │    ├─ Yes (絶対/相対パス): そのまま使用
  │    └─ No (ファイル名のみ): --output + ファイル名
  └─ No: --output + 自動生成ファイル名
              └─ 同名存在時: サフィックス付与
```

### Content-Type マッピング

```python
CONTENT_TYPE_MAP = {
    # Images
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",

    # Videos
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",

    # Audio
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "audio/webm": ".weba",

    # 3D Models
    "model/gltf-binary": ".glb",
    "model/gltf+json": ".gltf",
    "application/octet-stream": "",  # 拡張子推測不可

    # Documents
    "application/pdf": ".pdf",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/csv": ".csv",

    # Archives
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-tar": ".tar",
}
```

## 実装仕様詳細

### ヘルパー関数

#### `get_extension_from_content_type(content_type)`

```python
def get_extension_from_content_type(content_type: str) -> str:
    """Content-Typeヘッダーから拡張子を取得"""
    if not content_type:
        return ""
    # "image/png; charset=utf-8" → "image/png"
    mime_type = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_MAP.get(mime_type, "")
```

#### `get_extension_from_url(url)`

```python
def get_extension_from_url(url: str) -> str:
    """URLパスから拡張子を取得"""
    parsed = urlparse(url)
    path = parsed.path
    # クエリパラメータを除去
    if "?" in path:
        path = path.split("?")[0]
    _, ext = os.path.splitext(path)
    return ext.lower() if ext else ""
```

#### `get_unique_filepath(filepath)`

```python
def get_unique_filepath(filepath: str) -> str:
    """重複しないファイルパスを生成"""
    if not os.path.exists(filepath):
        return filepath

    base, ext = os.path.splitext(filepath)
    counter = 1
    while True:
        new_path = f"{base}_{counter}{ext}"
        if not os.path.exists(new_path):
            return new_path
        counter += 1
```

#### `resolve_output_path(output_dir, output_file, auto_filename, avoid_overwrite)`

```python
def resolve_output_path(
    output_dir: str | None,
    output_file: str | None,
    auto_filename: str,
    avoid_overwrite: bool = True
) -> str:
    """最終的な出力パスを決定"""
    if output_file:
        # output_file指定あり
        if os.path.isabs(output_file) or os.path.dirname(output_file):
            # フルパスまたは相対パス
            filepath = output_file
        else:
            # ファイル名のみ → output_dirと結合
            filepath = os.path.join(output_dir or ".", output_file)
        # output_file指定時は上書き許可（avoid_overwrite無視）
    else:
        # 自動生成ファイル名使用
        filepath = os.path.join(output_dir or "./output", auto_filename)
        if avoid_overwrite:
            filepath = get_unique_filepath(filepath)

    # ディレクトリ作成
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    return filepath
```

#### `generate_auto_filename(request_id, extension)`

```python
def generate_auto_filename(request_id: str | None, extension: str) -> str:
    """自動ファイル名を生成: {request_id}_{timestamp}.{ext}"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if request_id:
        # request_idが長い場合は短縮
        short_id = request_id[:8] if len(request_id) > 8 else request_id
        base = f"{short_id}_{timestamp}"
    else:
        base = f"output_{timestamp}"

    return f"{base}{extension}" if extension else base
```

### ログ保存

#### `--save-logs`: ディレクトリ保存

```
./output/
├── result.png
└── logs/
    ├── abc12345_request.json   # 送信したリクエスト
    └── abc12345_response.json  # 受信したレスポンス
```

#### `--save-logs-inline`: インライン保存

```
./output/
├── result.png
├── result_request.json
└── result_response.json
```

### ダウンロードフロー

```python
def download_file(url, output_dir, output_file, auto_filename_enabled, request_id, ...):
    # 1. ダウンロード実行
    response = requests.get(url, stream=True)

    # 2. 拡張子決定
    if output_file and os.path.splitext(output_file)[1]:
        # output_fileに拡張子あり
        extension = os.path.splitext(output_file)[1]
    else:
        # Content-Typeから取得
        content_type = response.headers.get("Content-Type", "")
        extension = get_extension_from_content_type(content_type)

        if not extension:
            # URLから取得
            extension = get_extension_from_url(url)

        if not extension:
            print("Warning: Could not detect file extension", file=sys.stderr)

    # 3. ファイル名決定
    if auto_filename_enabled:
        filename = generate_auto_filename(request_id, extension)
    elif output_file:
        filename = output_file
    else:
        # Content-Disposition or URL or request_id or "output"
        filename = get_filename_from_response(response, url, request_id) + extension

    # 4. パス解決
    filepath = resolve_output_path(output_dir, output_file, filename)

    # 5. 保存
    with open(filepath, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    return filepath
```

## 使用例

### 基本的な使用

```bash
# ディレクトリ指定（ファイル名は自動）
python mcp_async_call.py ... --output ./downloads

# 結果: ./downloads/output.png (または output_1.png, output_2.png...)
```

### ファイルパス指定

```bash
# フルパス指定（上書き許可）
python mcp_async_call.py ... --output-file ./results/my_image.png

# ファイル名のみ（--outputと組み合わせ）
python mcp_async_call.py ... --output ./downloads --output-file my_image.png

# 結果: ./downloads/my_image.png
```

### 自動ファイル命名

```bash
# request_idとタイムスタンプでユニーク名生成
python mcp_async_call.py ... --auto-filename

# 結果: ./output/abc12345_20250629_143052.png
```

### ログ保存

```bash
# logsフォルダに保存
python mcp_async_call.py ... --save-logs

# ファイル横に保存
python mcp_async_call.py ... --save-logs-inline
```

### 組み合わせ

```bash
# 全オプション組み合わせ
python mcp_async_call.py \
  --endpoint "https://mcp.example.com/sse" \
  --submit-tool "generate" \
  --status-tool "status" \
  --result-tool "result" \
  --args '{"prompt": "a cat"}' \
  --output ./my_project/assets \
  --auto-filename \
  --save-logs-inline

# 結果:
# ./my_project/assets/abc12345_20250629_143052.png
# ./my_project/assets/abc12345_20250629_143052_request.json
# ./my_project/assets/abc12345_20250629_143052_response.json
```

## 複数ファイル対応

### 背景・課題

一部のMCPサーバーは、1回のリクエストで複数のファイルを生成して返します：

- 画像生成: 「4枚生成して」→ 4つのURL
- 動画処理: 複数フォーマット出力
- オーディオ: 複数トラック出力

オリジナル実装では最初の1ファイルのみダウンロードしていました。

### 解決アプローチ

レスポンス全体を再帰的に探索し、全てのURLを抽出してダウンロード：

```python
def extract_download_urls(result: dict) -> list[str]:
    """Recursively extract all URLs from result."""
    urls = []
    seen = set()

    def _extract(obj):
        if isinstance(obj, str):
            if obj.startswith(("http://", "https://")) and obj not in seen:
                seen.add(obj)
                urls.append(obj)
            elif obj.startswith("{") or obj.startswith("["):
                try:
                    parsed = json.loads(obj)
                    _extract(parsed)
                except json.JSONDecodeError:
                    pass
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)
        elif isinstance(obj, dict):
            for value in obj.values():
                _extract(value)

    _extract(result)
    return urls
```

**特徴:**
- キー名に依存しない（`images`, `videos`, `outputs`等を個別指定不要）
- 任意のネスト構造に対応
- JSON文字列内のURLも再帰的に探索
- 重複URLは自動排除

### ファイル名の連番付与

複数ファイル時は自動でサフィックスを付与：

| ケース | 入力 | 出力 |
|-------|------|------|
| `--output-file result.png` + 4枚 | - | `result_1.png`, `result_2.png`, `result_3.png`, `result_4.png` |
| `--auto-filename` + 4枚 | - | `abc123_20250629_1.png`, `..._2.png`, `..._3.png`, `..._4.png` |
| 指定なし + 4枚 | - | URLから推測 + 重複回避サフィックス |

### 実装詳細

```python
# run_async_mcp_job内
download_urls = extract_download_urls(result_resp)

saved_paths = []
for i, url in enumerate(download_urls):
    print(f"[DOWNLOAD] ({i + 1}/{len(download_urls)}) {url}")

    # 複数ファイル時はインデックスサフィックスを付与
    current_output_file = output_file
    if output_file and len(download_urls) > 1:
        base, ext = os.path.splitext(output_file)
        current_output_file = f"{base}_{i + 1}{ext}"

    saved_path = download_file(
        url=url,
        output_dir=output_dir,
        output_file=current_output_file,
        ...
    )
    saved_paths.append(saved_path)
```

### 戻り値

```python
{
    "request_id": "abc123",
    "status": "completed",

    # 複数ファイル対応（リスト）
    "download_urls": ["url1", "url2", "url3", "url4"],
    "saved_paths": ["./output/result_1.png", "./output/result_2.png", ...],

    # 後方互換（最初のファイル）
    "download_url": "url1",
    "saved_path": "./output/result_1.png",

    "log_paths": [...]
}
```

### 使用例

```bash
# 4枚の画像を生成
python mcp_async_call.py \
  --endpoint "https://mcp.example.com/sse" \
  --submit-tool "generate_images" \
  --status-tool "status" \
  --result-tool "result" \
  --args '{"prompt": "a cat", "num_images": 4}' \
  --output ./downloads \
  --output-file cat.png

# 出力:
# [DOWNLOAD] (1/4) https://...
# [DOWNLOAD] Saved to: ./downloads/cat_1.png
# [DOWNLOAD] (2/4) https://...
# [DOWNLOAD] Saved to: ./downloads/cat_2.png
# [DOWNLOAD] (3/4) https://...
# [DOWNLOAD] Saved to: ./downloads/cat_3.png
# [DOWNLOAD] (4/4) https://...
# [DOWNLOAD] Saved to: ./downloads/cat_4.png
```

## 運用ルール

### ⚠️ プロジェクトルートから実行

**重要:** スキルは必ず**プロジェクトルートから**実行してください。

相対パス（`./output`等）はpythonコマンド実行時のカレントディレクトリを基準に解決されます。
Claude Code等のCLI AIツールは通常プロジェクトルートで動作するため、この規則に従うことで
ユーザーの期待通りの場所にファイルが保存されます。

```bash
# ✓ 正しい実行方法（プロジェクトルートから）
python .claude/skills/my-skill/scripts/mcp_async_call.py --output ./downloads
# → /project/downloads/ に保存（ユーザーの期待通り）

# ✗ 避けるべき実行方法（スキルディレクトリから）
cd .claude/skills/my-skill
python scripts/mcp_async_call.py --output ./downloads
# → /project/.claude/skills/my-skill/downloads/ に保存（意図しない場所）
```

### 生成されるSKILL.mdの記載

各スキルのSKILL.mdには、この運用ルールが記載されています：

```markdown
> **⚠️ 実行ディレクトリについて**
> このスキルは**プロジェクトルートから**実行してください。
> ユーザーが相対パス（例: `./output`）で保存先を指定した場合、プロジェクトルート基準で解釈してください。
```

### LLM向けガイドライン

AIがこのスキルを使用する際は以下を遵守してください：

1. **実行場所**: 常にプロジェクトルートから実行
2. **パスの解釈**: ユーザーの相対パス指定はプロジェクトルート基準
3. **コマンド形式**: `python .claude/skills/{skill}/scripts/...` を使用
