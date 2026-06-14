"""らんさ〜ずシードの健全性テスト（データ同梱・構造・冪等関数の存在）。

DB を要する実取り込みは統合テスト側で担うため、ここではデータ整合と
モジュールの import 健全性のみを軽量に確認する。
"""

import json
from pathlib import Path

_DATA = Path(__file__).parent.parent / "app" / "data" / "ranraners.json"


def test_seed_data_present_and_valid() -> None:
    assert _DATA.exists(), "ranraners.json が同梱されていること"
    data = json.loads(_DATA.read_text(encoding="utf-8"))
    assert data["project_key"] == "ranraners"
    entries = data["entries"]
    # 104本の動画 + 3つのまとめ
    assert len(entries) >= 100
    assert all(e.get("raw_log", "").strip() for e in entries), "全エントリに raw_log がある"
    assert all(isinstance(e.get("tags", []), list) for e in entries)
    # 動画/まとめの種別が含まれる
    kinds = {e.get("meta", {}).get("kind") for e in entries}
    assert "video" in kinds
    assert "ai_summary" in kinds


def test_seed_function_importable() -> None:
    from app.seed_ranraners import maybe_seed_ranraners

    assert callable(maybe_seed_ranraners)


def test_no_duplicate_raw_logs() -> None:
    data = json.loads(_DATA.read_text(encoding="utf-8"))
    logs = [e["raw_log"] for e in data["entries"]]
    assert len(logs) == len(set(logs)), "raw_log に重複がない（ハッシュ重複スキップで全件入る）"
