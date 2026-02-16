## Overview
つみあげくん（Knowledge DB）を使って、過去の開発セッションから学んだ知見を自動的に検索・記録するワークフロー。

## Procedure

### 1. セッション開始時：過去の知見を検索（recall）
タスクの内容を要約し、関連する過去の知見を検索する。

```bash
curl -s -X POST https://knowhow.up.railway.app/api/devin/recall \
  -H 'Content-Type: application/json' \
  -d '{"project_key":"<リポジトリ名>","query":"<タスクの要約>","top_k":5}'
```

結果があれば、その知見を参考にしてタスクを進める。

### 2. タスクを実行
通常通りタスクを実行する。過去の知見で得た情報を活用する。

### 3. セッション終了時：学びを記録（memorize）
タスク完了後、学んだことを記録する。

```bash
curl -s -X POST https://knowhow.up.railway.app/api/devin/memorize \
  -H 'Content-Type: application/json' \
  -d '{
    "project_key": "<リポジトリ名>",
    "tool": "devin",
    "status": "success",
    "environment": "local",
    "raw_log": "<学んだこと：エラーとその解決法、重要な設定値、アーキテクチャ決定の理由など>",
    "tags": ["<技術タグ>", "<カテゴリタグ>"]
  }'
```

### 4. フィードバック送信（知見が役立った場合）
recall で取得した知見が役に立った場合、フィードバックを送信する。

```bash
curl -s -X POST https://knowhow.up.railway.app/api/feedback \
  -H 'Content-Type: application/json' \
  -d '{
    "project_key": "<リポジトリ名>",
    "session_id": <memorize で返された session_id>,
    "query": "<recall で使ったクエリ>",
    "returned_chunk_ids": [<recall で返された chunk_id のリスト>],
    "selected_chunk_ids": [<実際に使った chunk_id のリスト>],
    "resolved": true,
    "was_helpful": "helpful"
  }'
```

## Specifications
- recall の結果が空でもエラーではない（まだ知見が蓄積されていない場合）
- memorize の raw_log には具体的で再利用可能な情報を含める
- tags には技術名やカテゴリを含める（例: ["fastapi", "railway", "deploy"]）
- was_helpful は "helpful" / "partial" / "unhelpful" のいずれか

## Advice
- raw_log は「次に同じ問題に遭遇した人が読んで役立つ」レベルの具体性で書く
- エラーメッセージとその解決法のペアは特に価値が高い
- 設定値（ポート番号、環境変数名など）も記録する
- 「なぜその判断をしたか」の理由も含めると後で参考になる

## Forbidden Actions
- 本番環境のデータを削除しない
- API キーやシークレットを raw_log に含めない
