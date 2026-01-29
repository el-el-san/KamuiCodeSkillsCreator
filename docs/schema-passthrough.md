# スキーマ詳細パススルー

## 目的

ツールパラメータのJSON Schema情報（enum, default, minimum, maximum等）を完全に保持し、LLMがパラメータ制約を正確に理解できるようにします。

### 背景・課題

オリジナル実装では、ツールパラメータから `type` と `description` のみを抽出していました：

```python
# オリジナルの実装
params[pname] = {
    "type": pspec.get("type", "any"),
    "description": pspec.get("description", ""),
}
```

これにより以下の情報が失われていました：

| 失われる情報 | 例 | 影響 |
|-------------|-----|------|
| `enum` | `["16:9", "4:3", "1:1"]` | 有効な値がわからない |
| `default` | `30000` | デフォルト値がわからない |
| `minimum` | `1000` | 最小値制約がわからない |
| `maximum` | `100` | 最大値制約がわからない |
| `items.enum` | 配列要素の選択肢 | 配列パラメータの有効値がわからない |
| `pattern` | 正規表現パターン | 文字列フォーマットがわからない |

### 問題シナリオ

ユーザー: 「アスペクト比を16:9にして画像を生成して」

LLMの応答（スキーマ詳細なし）:
```json
{"prompt": "a cat", "aspect_ratio": "16:9"}
// ← "16:9"が有効な値か不明。"16x9"や"widescreen"かもしれない
```

LLMの応答（スキーマ詳細あり）:
```yaml
aspect_ratio:
  type: string
  enum: ["16:9", "4:3", "1:1", "9:16"]  # ← 有効な値が明確
```
→ `"16:9"` が正しい値であることを確認できる

### 解決アプローチ

- `type` と `description` 以外の全フィールドをパススルー
- 将来のJSON Schema拡張にも自動対応

## 最終要件

1. **完全なスキーマ保持**: カタログにある全てのスキーマ情報を保持
2. **フォーマット非依存**: YAML（Lazyモード）とMarkdown（通常モード）の両方で対応
3. **将来互換性**: 明示的なフィールド列挙ではなく、パススルー方式で未知のフィールドにも対応

## 機能設計

### サポートするスキーマフィールド

| フィールド | JSON Schema仕様 | 用途 |
|-----------|----------------|------|
| `type` | 基本 | データ型 |
| `description` | 基本 | パラメータ説明 |
| `enum` | Validation | 許可される値のリスト |
| `default` | Meta-data | デフォルト値 |
| `minimum` | Validation | 数値の最小値 |
| `maximum` | Validation | 数値の最大値 |
| `minLength` | Validation | 文字列の最小長 |
| `maxLength` | Validation | 文字列の最大長 |
| `pattern` | Validation | 正規表現パターン |
| `items` | Arrays | 配列要素のスキーマ |
| `format` | Semantic | データフォーマット（date, uri等） |
| `examples` | Meta-data | 値の例 |

### 出力形式

#### Lazyモード（YAML）

```yaml
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

    target_formats:
      type: array
      description: Output formats
      items:
        enum: ["glb", "fbx", "obj", "usdz", "stl"]
```

#### 通常モード（SKILL.md）

```markdown
### flux_lora_submit
Submit Flux LoRA image generation request

**Parameters:**
  - `prompt`* (string): Image prompt
  - `aspect_ratio` (string): Output aspect ratio [default: 1:1, options: ['16:9', '4:3', '1:1', '9:16']]
  - `steps` (integer): Number of inference steps [default: 28, min: 1, max: 100]
  - `target_formats` (array): Output formats [options: ['glb', 'fbx', 'obj', 'usdz', 'stl']]
```

## 実装仕様詳細

### Lazyモード（YAML生成）

#### `convert_tools_to_yaml_dict()` の変更

```python
def convert_tools_to_yaml_dict(tools, mcp_config=None, skill_name=None):
    # ...

    for tool in tools:
        name = tool.get("name", "")
        schema = tool.get("inputSchema", tool.get("parameters", {}))
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Build compact parameter structure (passthrough all schema fields)
        params = {}
        for pname, pspec in properties.items():
            # 基本フィールド
            params[pname] = {
                "type": pspec.get("type", "any"),
                "description": pspec.get("description", ""),
            }

            # 追加フィールドを全てパススルー
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

**ポイント:**
- `type` と `description` は明示的に設定（デフォルト値対応のため）
- それ以外は元のスキーマからそのままコピー
- 未知のフィールドも自動的に含まれる

### 通常モード（SKILL.md生成）

#### `generate_skill_md()` の変更

```python
def generate_skill_md(mcp_config, tools, skill_name, lazy=False):
    # ...

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
```

**ポイント:**
- Markdown形式で人間が読みやすい形式に整形
- `[default: X, options: [...], min: Y, max: Z]` 形式で追記
- 配列の `items.enum` も展開

### 実際のカタログデータ例

`mcp_tool_catalog.yaml` から取得されるデータ：

```yaml
servers:
  - id: 3d23d-kamui-meshy-v5-remesh
    status: online
    tools:
      - name: meshy_v5_remesh_submit
        description: Submit remeshing request for 3D model
        inputSchema:
          type: object
          required:
            - model_url
          properties:
            model_url:
              type: string
              description: URL or base64 data URI of 3D model

            target_polycount:
              type: number
              description: Target polygon count
              default: 30000
              minimum: 1000

            topology:
              type: string
              description: Mesh topology type
              default: triangle
              enum:
                - quad
                - triangle

            target_formats:
              type: array
              description: Output formats
              items:
                enum:
                  - glb
                  - fbx
                  - obj
                  - usdz
                  - stl
```

### 変換結果

#### Lazyモード出力（YAML）

```yaml
meshy_v5_remesh_submit:
  description: Submit remeshing request for 3D model
  required:
    - model_url
  parameters:
    model_url:
      type: string
      description: URL or base64 data URI of 3D model

    target_polycount:
      type: number
      description: Target polygon count
      default: 30000
      minimum: 1000

    topology:
      type: string
      description: Mesh topology type
      default: triangle
      enum:
        - quad
        - triangle

    target_formats:
      type: array
      description: Output formats
      items:
        enum:
          - glb
          - fbx
          - obj
          - usdz
          - stl
```

#### 通常モード出力（SKILL.md）

```markdown
### meshy_v5_remesh_submit
Submit remeshing request for 3D model

**Parameters:**
  - `model_url`* (string): URL or base64 data URI of 3D model
  - `target_polycount` (number): Target polygon count [default: 30000, min: 1000]
  - `topology` (string): Mesh topology type [default: triangle, options: ['quad', 'triangle']]
  - `target_formats` (array): Output formats [options: ['glb', 'fbx', 'obj', 'usdz', 'stl']]
```

## LLM活用シナリオ

### シナリオ1: enumからの選択

ユーザー: 「四角形メッシュでリメッシュして」

LLMの判断プロセス:
1. YAMLを読む: `topology.enum: ["quad", "triangle"]`
2. 「四角形」→ `"quad"` にマッピング
3. リクエスト: `{"model_url": "...", "topology": "quad"}`

### シナリオ2: 範囲制約の確認

ユーザー: 「ポリゴン数を500にして」

LLMの判断プロセス:
1. YAMLを読む: `target_polycount.minimum: 1000`
2. 500 < 1000 なので制約違反
3. ユーザーに警告: 「最小値は1000です。1000に設定しますか？」

### シナリオ3: デフォルト値の省略

ユーザー: 「モデルをリメッシュして」（詳細指定なし）

LLMの判断プロセス:
1. YAMLを読む: `target_polycount.default: 30000`, `topology.default: triangle`
2. デフォルト値があるパラメータは省略可能
3. 必須の `model_url` のみ指定してリクエスト

## まとめ

| 比較項目 | 変更前 | 変更後 |
|---------|--------|--------|
| 保持フィールド | type, description | 全フィールド |
| enum対応 | なし | あり |
| default対応 | なし | あり |
| min/max対応 | なし | あり |
| 将来拡張 | 手動追加必要 | 自動パススルー |
| LLMの制約理解 | 不完全 | 完全 |
