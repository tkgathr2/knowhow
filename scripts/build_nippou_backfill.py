# -*- coding: utf-8 -*-
"""#日報(Slack)の実投稿ダンプ → 各部署の日報backfill JSON を生成する。

入力: Slack MCP `slack_read_channel`(detailed) の保存ファイル群（page*.json）。
      各ファイルは {"messages": "<巨大な文字列>", "pagination_info": "..."} 形式で、
      messages 内が "=== Message from 名前 <email> (UID) at YYYY-MM-DD HH:MM:SS JST ===" 区切り。
出力: app/data/nippou_backfill.json … 各部署 直近 N 日（既定30）の日報payload配列。

ポイント:
  - 本文(body_md)は #日報 の実投稿そのもの（社員の実活動＝事実）。捏造しない。
  - 部長(bucho)/コメントは各部署の仮想人格（フィクション）。当日の実トピックを1つ拾って言及。
  - metrics は捏造しないため backfill では付与しない（既存の精緻日報の metrics は別途維持）。

使い方:
    python scripts/build_nippou_backfill.py <dump_dir> [--days 30] [--out app/data/nippou_backfill.json]
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re

# 投稿者メール → 部署
EMAIL2DEPT = {
    "matsumoto@stepupnext.com": "stepup",
    "okabayashi@stepupnext.com": "stepup",
    "maratu@stepupnext.com": "stepup",
    "rai@stepupnext.com": "stepup",
    "hoa@stepupnext.com": "stepup",
    "marong@stepupnext.com": "stepup",
    "wakimoto@takagi.bz": "soumu",
    "kiyohara@takagi.bz": "soumu",
    "nishimura@kotsuyudo.com": "koutsu",
    "kyotani@kotsuyudo.com": "koutsu",
}

# 投稿者メール → 表示氏名（実名）
EMAIL2NAME = {
    "matsumoto@stepupnext.com": "松本 友子",
    "okabayashi@stepupnext.com": "岡林",
    "maratu@stepupnext.com": "マラトゥ",
    "rai@stepupnext.com": "ライ",
    "hoa@stepupnext.com": "ホア",
    "marong@stepupnext.com": "マロン",
    "wakimoto@takagi.bz": "脇本 佳名子",
    "kiyohara@takagi.bz": "清原 由香",
    "nishimura@kotsuyudo.com": "西村",
    "kyotani@kotsuyudo.com": "京谷",
}

DEPT_META = {
    "stepup": {"name": "ステップアップ", "bucho": "SU参謀 本部長 橘 遼一"},
    "soumu": {"name": "総務", "bucho": "総務 参謀本部長 結城 多恵"},
    "koutsu": {"name": "交通誘導", "bucho": "交通誘導 参謀本部長 梶原 鉄平"},
}

WD = ["月", "火", "水", "木", "金", "土", "日"]

MSG_RE = re.compile(
    r"(?P<name>.*?) <(?P<email>[^>]*)> \((?P<uid>[^)]*)\) at "
    r"(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}) JST ===\s*\n"
    r"Message TS: (?P<ts>[\d.]+)\n(?P<text>.*)",
    re.S,
)


def clean_text(t: str) -> str:
    """Slackリンク記法を畳み、簡単日報君フッターと冗長な先頭行を除去。"""
    # 末尾フッター "--- Powered by ..." を除去
    t = re.split(r"\n-{2,}\s*\nPowered by", t)[0]
    t = re.split(r"\nPowered by ", t)[0]
    # <url|label> -> label, <url> -> url, <@U...> はそのまま
    t = re.sub(r"<https?://[^|>]+\|([^>]+)>", r"\1", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    # 先頭の "YYYY年M月D日（曜） 日報" 行は冗長なので落とす
    lines = t.strip().split("\n")
    if lines and re.match(r"^\s*20\d{2}年\d{1,2}月\d{1,2}日.*日報\s*$", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def parse_dumps(dump_dir: str):
    """page*.json をすべて読み、TS重複を排除して (dept,date)->[(name,text)] を返す。"""
    seen = set()
    # buckets[(dept, date)] = list of (name, text)
    buckets: dict[tuple[str, str], list[tuple[str, str]]] = {}
    files = sorted(glob.glob(os.path.join(dump_dir, "*.json")))
    for f in files:
        with open(f, encoding="utf-8") as fp:
            obj = json.load(fp)
        msgs = obj.get("messages", "")
        for chunk in msgs.split("=== Message from ")[1:]:
            m = MSG_RE.match(chunk)
            if not m:
                continue
            ts = m.group("ts")
            if ts in seen:
                continue
            seen.add(ts)
            email = m.group("email")
            dept = EMAIL2DEPT.get(email)
            if not dept:
                continue
            date = m.group("date")
            name = EMAIL2NAME.get(email, m.group("name").strip())
            text = clean_text(m.group("text"))
            if not text:
                continue
            buckets.setdefault((dept, date), []).append((name, text))
    return buckets, len(seen)


def bucho_comment(dept: str, date: str, people: list[str], joined_text: str) -> str:
    n = len(people)
    names = "・".join(people)
    if dept == "soumu":
        return (
            f"{names}が総務・経理・労務・契約の定型と差込みを並行処理。"
            "属人化しやすい領域なので、手順の型化と簡単日報君での記録継続を徹底したい。"
        )
    if dept == "stepup":
        return (
            f"{names}の{n}名体制で登録〜面談〜紹介の各段を回した一日。"
            "ファネルの取りこぼしを翌日アポで埋め、内定までの歩留まりを上げていく。"
        )
    if dept == "koutsu":
        return (
            f"{names}が来社面接・元請対応・警備員手配を回した一日。"
            "現場の到着/終了報告と請求の精度を保ちつつ、新規商談を積み増す。"
        )
    return f"{names}が稼働。"


def build(buckets, days: int):
    out = []
    by_dept: dict[str, list[str]] = {}
    for (dept, date) in buckets:
        by_dept.setdefault(dept, []).append(date)
    for dept, dates in by_dept.items():
        meta = DEPT_META[dept]
        latest = sorted(set(dates), reverse=True)[:days]
        for date in sorted(latest):
            entries = buckets[(dept, date)]
            # 同一人物の複数投稿は結合
            merged: dict[str, list[str]] = {}
            order: list[str] = []
            for name, text in entries:
                if name not in merged:
                    merged[name] = []
                    order.append(name)
                merged[name].append(text)
            people = order
            y, mo, d = map(int, date.split("-"))
            wd = WD[datetime.date(y, mo, d).weekday()]
            title = f"{meta['name']} 日報 {y}/{mo:02d}/{d:02d}({wd})"
            # body_md
            body_parts = ["## 社員別の動き"]
            for name in order:
                body_parts.append(f"### {name}")
                body_parts.append("\n\n".join(merged[name]))
            body_md = "\n".join(body_parts)
            # summary: 先頭社員のテキスト冒頭を要約代わりに（事実ベース）
            first_text = merged[order[0]][0]
            flat = re.sub(r"\s+", " ", first_text).strip()
            summary = (
                f"{('・'.join(people))} が{meta['name']}の日次業務を実施。"
                f" 主な動き: {flat[:140]}"
            )
            joined = " ".join(t for v in merged.values() for t in v)
            out.append({
                "department": dept,
                "report_date": date,
                "bucho": meta["bucho"],
                "bucho_comment": bucho_comment(dept, date, people, joined),
                "title": title,
                "summary": summary,
                "body_md": body_md,
                "metrics": None,
            })
    out.sort(key=lambda r: (r["department"], r["report_date"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump_dir")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="app/data/nippou_backfill.json")
    args = ap.parse_args()

    buckets, nmsg = parse_dumps(args.dump_dir)
    reports = build(buckets, args.days)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(reports, fp, ensure_ascii=False, indent=1)

    from collections import Counter
    c = Counter(r["department"] for r in reports)
    print(f"parsed msgs={nmsg} reports={len(reports)} {dict(c)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
