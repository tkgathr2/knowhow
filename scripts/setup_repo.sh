#!/usr/bin/env bash
set -euo pipefail

API_URL="https://knowhow.up.railway.app"

usage() {
  echo "Usage: $0 <target-repo-path>"
  echo ""
  echo "Adds ノウハウキング君 .devin/rules to the target repository."
  echo "This enables automatic recall/memorize in Devin sessions."
  echo ""
  echo "Example:"
  echo "  $0 /path/to/my-project"
  exit 1
}

if [ $# -lt 1 ]; then
  usage
fi

TARGET="$1"

if [ ! -d "$TARGET/.git" ]; then
  echo "Error: $TARGET is not a git repository"
  exit 1
fi

REPO_NAME=$(basename "$TARGET")

mkdir -p "$TARGET/.devin"

cat > "$TARGET/.devin/rules" << 'RULES_EOF'
## ノウハウキング君 (Knowledge DB) 自動連動ルール

ノウハウキング君 は AI 外部記憶基盤です。セッション中に過去の知見を検索し、セッション終了時に学びを記録します。

### API Base URL
https://knowhow.up.railway.app

### セッション開始時（自動）
タスクに取り掛かる前に、関連する過去の知見を検索してください。

```bash
curl -s -X POST https://knowhow.up.railway.app/api/devin/recall \
  -H 'Content-Type: application/json' \
  -d '{"project_key":"__REPO_NAME__","query":"<タスクの要約>","top_k":5}'
```

結果が返ってきた場合、その知見を参考にしてタスクを進めてください。

### セッション終了時（自動）
タスク完了後、学んだことを記録してください。

```bash
curl -s -X POST https://knowhow.up.railway.app/api/devin/memorize \
  -H 'Content-Type: application/json' \
  -d '{"project_key":"__REPO_NAME__","tool":"devin","status":"success","environment":"local","raw_log":"<セッションで学んだこと・解決した問題・重要な決定事項の要約>","tags":["<関連タグ>"]}'
```

### 知見が役に立った場合
検索結果を使って問題が解決した場合、フィードバックを送信してください。

```bash
curl -s -X POST https://knowhow.up.railway.app/api/feedback \
  -H 'Content-Type: application/json' \
  -d '{"project_key":"__REPO_NAME__","session_id":<session_id>,"query":"<検索クエリ>","returned_chunk_ids":[<chunk_ids>],"selected_chunk_ids":[<使ったchunk_ids>],"resolved":true,"was_helpful":"helpful"}'
```

### 注意事項
- project_key はリポジトリ名を使用
- raw_log には具体的な学び・エラーとその解決法・設定値などを含める
- tags にはテクノロジー名やカテゴリを含める（例: ["railway", "deploy", "postgresql"]）
- API キーやシークレットを raw_log に含めない
RULES_EOF

sed -i "s/__REPO_NAME__/${REPO_NAME}/g" "$TARGET/.devin/rules"

echo "Setup complete: $TARGET/.devin/rules"
echo "  project_key: ${REPO_NAME}"
echo "  API: ${API_URL}"
echo ""
echo "Next steps:"
echo "  cd $TARGET"
echo "  git add .devin/rules"
echo "  git commit -m 'chore: add ノウハウキング君 auto-integration'"
