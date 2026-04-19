# Coworkセッション ノウハウ集
> 抽出日: 2026-04-20 / 対象セッション数: 約30セッション

---

## 1. セキュリティ

### Express.js セキュリティヘッダー設定
- helmet + express-rate-limit を組み合わせる
- `app.use(helmet())` でデフォルトヘッダーを一括設定
- rate-limitは認証エンドポイントに特に重要（例: 15分で100リクエスト）
- **タグ**: express, helmet, security, rate-limit

### IDOR脆弱性の修正（マジックリンク認証）
- トークンをURLクエリパラメータ → HTTPリクエストBodyに移動
- URLパラメータはサーバーログ・ブラウザ履歴に残るため危険
- **タグ**: IDOR, auth, security, magic-link

### localStorage → セッションクッキー（httpOnly）へ移行
- XSS攻撃でlocalStorageの認証情報が盗まれるリスクを排除
- httpOnly CookieはJSからアクセス不可でXSS耐性が高い
- **タグ**: localStorage, XSS, httpOnly, cookie, security

---

## 2. フロントエンド・ブラウザ自動化

### Playwright DOM調査デバッグ手法（段階的DIAGスクリプト）
- 段階的なDIAGスクリプト（DIAG1→DIAG2→...）でDOM構造を確認しながら進める
- `document.querySelectorAll` で要素確認後に操作する
- **タグ**: playwright, debug, DOM, browser-automation

### Playwright TypeScript TS2584エラー（document参照）の解決
- `document` がグローバルで参照できない場合は `window.document` を使う
- または tsconfig の lib に "dom" を追加
- **タグ**: playwright, typescript, TS2584

### Chrome拡張（Claude in Chrome）の権限付与手順
- chrome://extensions → 詳細 → サイトへのアクセスを「全てのサイト」に変更
- 権限不足だと特定サイトでツールが動作しない
- **タグ**: claude-in-chrome, chrome-extension, permissions

### API非対応システムのブラウザ自動化パターン
- ZOHO・freee等はClaude in Chromeで操作
- ファイル選択ダイアログ（OS標準）はブラウザから自動操作不可→手動委譲が必要
- **タグ**: browser-automation, ZOHO, freee

---

## 3. バックエンド・API開発

### Express 5 互換性対応
- Express 5では型定義が変更されているため、v4のコードは修正が必要
- レスポンスヘッダー操作APIが変更された部分に注意
- **タグ**: express5, typescript, migration

### Prisma スキーマへの新規モデル追加パターン
- schema.prisma に新モデルを追加 → `npx prisma migrate dev` → `npx prisma generate`
- enumも同じファイルに定義する
- **タグ**: prisma, schema, migration

### FastAPI + PostgreSQL + pgvector構成（ノウハウキングアーキテクチャ）
- Railway上でPostgreSQL 17 + pgvector拡張を使用
- SQLAlchemy 2.0 (async)でDB操作
- OpenAI text-embedding-3-largeでベクトル化
- **タグ**: fastapi, postgresql, pgvector, railway, embeddings

---

## 4. デプロイ・インフラ

### Railway 自動デプロイの運用
- mainブランチへのpush → 自動ビルド・デプロイ
- 「Deployment successful」確認後に本番URLで動作テスト
- **タグ**: railway, deploy, CI/CD

---

## 5. メール・通知システム

### HTMLメールテンプレート設計（スマホ最適化）
- max-width: 480px でスマホ最適化
- オレンジヘッダー（#E67E22）+ 薄オレンジ背景 + 左ボーダーで視認性確保
- フッターに「自動送信のため返信不要」文言を入れる
- **タグ**: email, html, mobile, template

### Indeed→メール→LINE/Slack通知システムのノウハウ
- LINE @all メンション: `<at:all></at:all>` タグを使用
- 通知失敗時のフォールバック: Slack Webhook に切り替え
- 環境変数未設定時のサイレント失敗対策: 起動時にチェックして早期エラー
- JSONファイル肥大化防止: uid: エントリを除外してファイルサイズ管理
- IMAPソケットのタイムアウト: 30秒に設定（デフォルトは無限待機でハング）
- **タグ**: LINE, Slack, IMAP, notification, indeed

### プロキャスト 日勤・夜勤メール配信スケジュール
- 日勤スタッフの集合時間実績（7:30〜8:50）に基づき配信時刻を最適化
- 朝7:00・夜18:00の2回送信が現場に合っている
- **タグ**: scheduling, email, nodejs, typescript

---

## 6. 外部サービス連携

### MFクラウド会計 OAuth認証フロー
- mfc_ca_authorize → 認証URL生成 → ブラウザで認可 → コードコピー → mfc_ca_exchange
- PCごとにトークンが異なるため、ノートPC・自宅デスク両方で設定が必要
- **タグ**: moneyforward, OAuth, authentication, MCP

### Backlog MCP設定
- npx/@nulab/backlog-api-cli 経由で接続
- 課題CRUD・プロジェクト管理・完了タスク一覧取得が自動化可能
- **タグ**: backlog, MCP, nulab

### Google Workspace Admin API の制限
- Coworkのサンドボックス環境からはAdmin SDK APIにアクセス不可
- 代替手段: mail-forwarding・address-creationスキルでブラウザ操作
- **タグ**: google-workspace, admin-api, limitation

### Gemini API キー取得手順
- Google Cloud Console → APIs & Services → Credentials → Create Credentials → API Key
- キーは環境変数で管理、リポジトリに含めない
- **タグ**: gemini, google-cloud, API-key

### Googleマップリンク実装パターン
- `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(address)}`
- 住所未定の場合はプレーンテキストを返す条件分岐が必須
- **タグ**: google-maps, email

---

## 7. Cowork・スキル開発

### 作業ツール優先順位（CLAUDE.md設定）
- ① MCPツール → ② API → ③ Claude in Chrome（ブラウザ操作）
- APIの設定値・認証情報はNotionで一元管理
- **タグ**: cowork, CLAUDE.md, workflow

### スキルファイル（.skill）の共有方法
- Slack・Google Driveのセキュリティフィルターでブロックされる
- 確実な共有方法: メール添付 → ドラッグ&ドロップ → Save skillボタン
- **タグ**: cowork, skill, file-sharing

### Cowork vs Claude Code の使い分け
- Cowork: ビジネスパーソン向け、GUI操作・業務自動化
- Claude Code: エンジニア向け、ターミナル・コーディング・git操作
- **タグ**: cowork, claude-code, comparison

### Chrome Remote Desktop スキル化
- remotedesktop.google.com → アクセス → 対象PC選択の手順を自動化
- 複数PCを管理する場合はPC名をスキル内に定義しておく
- **タグ**: chrome-remote-desktop, skill

### Slack 口調スキル設計パターン
- 相手ごとの口調プロファイルをスキル内に定義
- チャンネル優先・機密のみDMのルール
- 送信前に必ず確認プロセスを入れる
- **タグ**: slack, skill, tone

---

## 8. 要件定義・プロジェクト管理

### 勤怠管理アプリの法令遵守チェック
- 「実績記録からの書類生成」は適法だが「虚偽記録の生成」は文書偽造罪等のリスク
- 依頼者に法的リスクを説明し、適法な実装方向に誘導することが必須
- **タグ**: timecard, legal, compliance, requirements

### アナログ業態UX設計
- スマートフォン慣れ度が大きく異なるユーザーには「インストール不要」を優先提案
- LINEミニアプリ or Webブラウザ版を最初に検討
- **タグ**: UX, requirements, mobile, LINE
