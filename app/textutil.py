"""文字列の健全化ユーティリティ。

PG に不正な UTF-8 バイト列が書き込まれる事故（lone surrogate 由来）を
水際で防ぐ。クライアントがバイト途中で切った文字列を surrogateescape で
JSON 化すると、サーバ側 str に孤立サロゲートが残り、asyncpg がそのまま
不正バイトとして永続化してしまう（実害: left() 等の文字関数で 22021）。
"""

from __future__ import annotations


def sanitize_utf8(s: str | None) -> str:
    """孤立サロゲート等、UTF-8 として保存できない文字を � に置換して返す。"""
    if not s:
        return s or ""
    try:
        s.encode("utf-8")  # 正常系は何もしない（コピーを作らない）
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", errors="replace").decode("utf-8")


def sanitize_tags(tags: list[str] | None) -> list[str]:
    return [sanitize_utf8(t) for t in (tags or [])]
