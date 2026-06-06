"""HO-83 Phase1（案A）: Notion 3DB エクスポート JSON を /api/admin/import へ投入するクライアント.

役割分担（案A）:
  - 変換・embedding生成・INSERT は **サーバ側**（app/routers/admin.py）が行う。
  - 本スクリプトは「薄いクライアント」: JSON を読み、PII を落とし、対象システム→project_key で
    グルーピングして、バッチで POST するだけ。**本番DB資格情報をこのPCに持ち出さない**のが利点。

前提:
  - knowhow に admin.router が組み込み済み・デプロイ済み（PR merge 後）。
  - 環境変数 ADMIN_IMPORT_KEY が Railway 側と一致して設定済み（未設定なら EP は 503）。

使い方:
    set ADMIN_IMPORT_KEY=<Railwayと同じシークレット>
    # まず dry-run（サーバ側で rollback・件数だけ返る・embedding課金なし）
    python scripts/import_via_admin.py \
        --base-url https://knowhow.up.railway.app \
        --learnings export/learnings.json \
        --devlogs   export/devlogs.json \
        --failures  export/failures.json \
        --dry-run
    # 本番投入（承認後・🔴本番DB書込）
    python scripts/import_via_admin.py --base-url https://knowhow.up.railway.app \
        --learnings export/learnings.json --devlogs export/devlogs.json --failures export/failures.json

PII（神谷方針・Chen Wei 分析）:
  - 開発ログ ID:22 の人名「脇本」→「担当者アカウント」、実IP（IPv4）→「[REDACTED_IP]」に置換。
  - 学びの人名は業務文脈で意味を持つため保持（meta フラグは EP 側の将来対応）。
  - 置換は POST 直前に全文字列フィールドへ適用＝サーバ／DBに PII を載せない。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

# §C-0.5 対象システム -> project_key（✅実在のみ確定。🟡要確認は migrate スクリプトと同一表で一貫性を保つ）。
SYSTEM_TO_PROJECT: dict[str, str] = {
    "プロレポ": "prorepo",
    "キャスト名簿くん": "cast-meibo",
    "キャスト名簿": "cast-meibo",
    "seiko": "seiko",
    "seiko/tkgathr2": "seiko",
    "tkgathr2": "seiko",
    "Indeed応募通知": "recruit",
    "Indeed": "recruit",
    "らくらく契約くん": "stepup_contract_maker",
    "ほうこちゃん": "houko",
    "ほうこ": "houko",
}
# 未確定・横断・その他は general へ（実在キー）。元の対象システムは tags に保持され復元可能。
DEFAULT_PROJECT = "general"
LEARNING_PROJECT = "cto-lab"

# --- PII 置換ルール（Chen Wei 分析 §4） ---
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_NAME_REPLACEMENTS = {
    "脇本さんアカウント": "担当者アカウント",  # より長い表現を先に（部分一致の二重置換を防ぐ）
    "脇本佳名子": "担当者",
    "脇本さん": "担当者",
    "脇本": "担当者",
}


def redact_pii(value: Any) -> Any:
    """文字列なら PII を置換。dict/list は再帰。それ以外はそのまま。"""
    if isinstance(value, str):
        s = value
        for name, repl in _NAME_REPLACEMENTS.items():
            if name in s:
                s = s.replace(name, repl)
        s = _IPV4.sub("[REDACTED_IP]", s)
        return s
    if isinstance(value, dict):
        return {k: redact_pii(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_pii(v) for v in value]
    return value


def load_json(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for key in ("results", "rows", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else []


def map_project_key(system: Any) -> str:
    if system in (None, ""):
        return DEFAULT_PROJECT
    return SYSTEM_TO_PROJECT.get(str(system).strip(), DEFAULT_PROJECT)


def _system_of(row: dict[str, Any]) -> Any:
    for k in ("対象システム", "system", "対象"):
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def post_batch(base_url: str, admin_key: str, kind: str, project_key: str,
               items: list[dict], dry_run: bool, timeout: int = 120) -> dict:
    payload = json.dumps(
        {"kind": kind, "project_key": project_key, "dry_run": dry_run, "items": items},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/admin/import",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Key": admin_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return {"_http_error": e.code, "_detail": body, "kind": kind, "project_key": project_key}


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def main() -> int:
    ap = argparse.ArgumentParser(description="HO-83 Phase1（案A）admin import クライアント")
    ap.add_argument("--base-url", default="https://knowhow.up.railway.app")
    ap.add_argument("--learnings", help="学びDB エクスポート JSON")
    ap.add_argument("--devlogs", help="開発ログDB エクスポート JSON")
    ap.add_argument("--failures", help="失敗事例DB エクスポート JSON")
    ap.add_argument("--dry-run", action="store_true", help="サーバ側で rollback・件数のみ（embedding課金なし）")
    ap.add_argument("--batch-size", type=int, default=100)
    args = ap.parse_args()

    admin_key = os.environ.get("ADMIN_IMPORT_KEY", "")
    if not admin_key:
        print("❌ ADMIN_IMPORT_KEY 未設定。Railway と同じシークレットを環境変数に設定してください。", file=sys.stderr)
        return 2

    learnings = [redact_pii(r) for r in load_json(args.learnings)]
    devlogs = [redact_pii(r) for r in load_json(args.devlogs)]
    failures = [redact_pii(r) for r in load_json(args.failures)]

    # グルーピング: learnings は全件 cto-lab。devlog/failure は対象システム→project_key で分割。
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in learnings:
        groups[("learning", LEARNING_PROJECT)].append(r)
    for r in devlogs:
        groups[("devlog", map_project_key(_system_of(r)))].append(r)
    for r in failures:
        groups[("failure", map_project_key(_system_of(r)))].append(r)

    mode = "[DRY-RUN]" if args.dry_run else "[LIVE 🔴本番DB書込]"
    print("=" * 78)
    print(f"HO-83 Phase1 admin import {mode}  base={args.base_url}")
    print(f"  学び {len(learnings)} / 開発ログ {len(devlogs)} / 失敗事例 {len(failures)}")
    print(f"  グループ数: {len(groups)}")
    print("=" * 78)

    tot_submitted = tot_imported = tot_skipped = tot_error = 0
    for (kind, project_key), items in sorted(groups.items()):
        for batch in chunked(items, args.batch_size):
            res = post_batch(args.base_url, admin_key, kind, project_key, batch, args.dry_run)
            if "_http_error" in res:
                print(f"  ❌ {kind}/{project_key} HTTP {res['_http_error']}: {res['_detail'][:200]}")
                tot_error += len(batch)
                continue
            imp = res.get("total_imported", 0)
            skp = res.get("total_skipped", 0)
            err = sum(1 for x in res.get("results", []) if x.get("status") == "error")
            tot_submitted += res.get("total_submitted", len(batch))
            tot_imported += imp
            tot_skipped += skp
            tot_error += err
            print(f"  {kind:8s}/{project_key:24s} submitted={len(batch):3d} imported={imp:3d} skipped={skp:3d} error={err:3d}")
            for x in res.get("results", []):
                if x.get("status") == "error":
                    print(f"      └ error idx={x.get('index')}: {x.get('detail')}")

    print("-" * 78)
    print(f"合計: submitted={tot_submitted} imported={tot_imported} skipped={tot_skipped} error={tot_error}")
    if args.dry_run:
        print("DRY-RUN 完了（サーバ側 rollback・DB 未変更）。")
    return 0 if tot_error == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
