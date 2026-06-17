# -*- coding: utf-8 -*-
"""日報1件を knowhow /api/nippou へ冪等POSTする汎用スクリプト。

使い方:
    railway run python scripts/post_nippou.py <payload.json>

payload.json の例（department は stepup|soumu|koutsu）:
    {"department":"stepup","report_date":"2026-06-17","bucho":"SU参謀 本部長 橘 遼一",
     "bucho_comment":"…","title":"…","summary":"…","body_md":"…","metrics":{...}}

KB_API_KEY は環境変数から読む（値は表示しない）。NIPPOU_BASE で送信先を上書き可。
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("NIPPOU_BASE", "https://knowhow.up.railway.app")
KEY = os.environ.get("KB_API_KEY", "")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: post_nippou.py <payload.json>")
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        payload = json.load(f)
    if not payload.get("department") or not payload.get("report_date"):
        print("ERROR: department と report_date は必須")
        return 2
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/nippou", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if KEY:
        req.add_header("X-API-Key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
            print(
                "OK",
                payload["department"],
                payload["report_date"],
                "id=" + str(body.get("id")),
                "created=" + str(body.get("created")),
            )
            return 0
    except urllib.error.HTTPError as e:  # noqa: PERF203
        print("HTTPError", e.code, e.read().decode("utf-8", "replace")[:200])
        return 1
    except Exception as e:  # noqa: BLE001
        print("ERROR", repr(e))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
