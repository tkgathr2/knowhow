"""Apply schema.sql to the database.

Usage:
    DATABASE_URL=postgresql://... python scripts/apply_schema.py
"""

import os
import sys
from pathlib import Path

import psycopg2


def main() -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is not set")
        sys.exit(1)

    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    if not schema_path.exists():
        print(f"ERROR: {schema_path} not found")
        sys.exit(1)

    sql = schema_path.read_text(encoding="utf-8")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("Schema applied successfully.")
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
