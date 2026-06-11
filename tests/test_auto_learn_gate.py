"""蒸留品質ゲート（is_generic / passes_gate）および旧形式フォールバックの単体テスト。

DB非依存 — app.routers.auto_learn の純粋関数だけをテストする。
"""

from app.routers.auto_learn import (
    _normalize_distill_response,
    is_generic,
    passes_gate,
)


# ──────────────────────────────────────────────────────────────
# is_generic
# ──────────────────────────────────────────────────────────────

class TestIsGeneric:
    # ── 正例（一般論 → True）────────────────────────────────
    def test_generic_error_handling(self):
        assert is_generic("エラーハンドリングを適切に行う") is True

    def test_generic_test_important(self):
        assert is_generic("テストは重要である") is True

    def test_generic_env_var_note(self):
        assert is_generic("環境変数の設定に注意する") is True

    def test_generic_logging(self):
        assert is_generic("ログを確認することが大切") is True

    def test_generic_review_habit(self):
        assert is_generic("コードをレビューする習慣をつける") is True

    def test_generic_be_careful(self):
        assert is_generic("デプロイに注意すること") is True

    def test_generic_mindful(self):
        assert is_generic("テストを心がけること") is True

    # ── 負例（具体的 → False）───────────────────────────────
    def test_concrete_embedding_dim(self):
        # 具体トークン EMBEDDING_DIM=3072 を含む
        assert is_generic(
            "EMBEDDING_DIM=3072 だと insert 失敗で embedding が NULL 保存される"
        ) is False

    def test_concrete_command(self):
        assert is_generic(
            "poetry run pytest tests/ -q を実行すると ImportError が出る"
        ) is False

    def test_concrete_error_name(self):
        assert is_generic(
            "asyncpg.exceptions.UniqueViolationError は hash 重複で発生する"
        ) is False

    def test_concrete_file_path(self):
        assert is_generic(
            "app/routers/auto_learn.py の _distill 関数でタイムアウトが起きた"
        ) is False

    def test_concrete_number(self):
        assert is_generic(
            "max_tokens=900 に増やすことで JSON が途中で切れなくなった"
        ) is False

    def test_no_pattern_match(self):
        # パターン自体にマッチしないので False
        assert is_generic("セッションのhashを確認する") is False

    def test_concrete_with_kagi_brackets(self):
        # 「」内固有名を含む
        assert is_generic("「KbSession」を作成する際はhashが重要である") is False


# ──────────────────────────────────────────────────────────────
# passes_gate
# ──────────────────────────────────────────────────────────────

class TestPassesGate:
    def _make_lesson(self, summary="", evidence="", specificity=4, tags=None):
        return {
            "summary": summary,
            "evidence": evidence,
            "applicability": "テスト用",
            "specificity": specificity,
            "tags": tags or [],
        }

    def test_pass_all_conditions(self):
        lesson = self._make_lesson(
            summary="pgvector の Vector(3072) は insert 前に次元確認が必要",
            evidence="EMBEDDING_DIM=3072 で KbChunk.insert が NULL embedding エラーで失敗した",
            specificity=4,
        )
        ok, reason = passes_gate(lesson)
        assert ok is True
        assert reason == ""

    def test_fail_low_specificity(self):
        lesson = self._make_lesson(
            summary="asyncpg で接続エラーが出た場合は retry する",
            evidence="asyncpg.InterfaceError が発生",
            specificity=3,
        )
        ok, reason = passes_gate(lesson)
        assert ok is False
        assert "specificity=3" in reason

    def test_fail_empty_evidence(self):
        lesson = self._make_lesson(
            summary="poetry.lock の再生成は依存更新で必要",
            evidence="",
            specificity=4,
        )
        ok, reason = passes_gate(lesson)
        assert ok is False
        assert "evidence is empty" in reason

    def test_fail_generic_summary(self):
        lesson = self._make_lesson(
            summary="エラーハンドリングを適切に行う",
            evidence="",  # evidence も空なので evidence チェックが先に引っかかる
            specificity=4,
        )
        ok, reason = passes_gate(lesson)
        assert ok is False
        # evidence が空なので evidence エラーが返る
        assert "evidence is empty" in reason

    def test_fail_generic_summary_with_evidence(self):
        """evidence があっても summary が一般論なら不合格。"""
        lesson = self._make_lesson(
            summary="テストは重要である",
            evidence="pytest で 50件 pass した",
            specificity=4,
        )
        ok, reason = passes_gate(lesson)
        assert ok is False
        assert "generic" in reason

    def test_fail_specificity_zero(self):
        lesson = self._make_lesson(
            summary="asyncpg.UniqueViolationError は hash 重複で発生",
            evidence="hash カラムに UNIQUE 制約があり重複 insert で発生",
            specificity=0,
        )
        ok, reason = passes_gate(lesson)
        assert ok is False

    def test_pass_specificity_5(self):
        lesson = self._make_lesson(
            summary="railway up --detach 後に railway logs -n 100 で起動確認する手順",
            evidence="railway up --detach で deploy し railway logs で STARTED を確認した",
            specificity=5,
        )
        ok, reason = passes_gate(lesson)
        assert ok is True

    def test_pass_specificity_exactly_4(self):
        lesson = self._make_lesson(
            summary="KbChunk.alpha=3.0 で初期信頼度を下げ精査済み知見と分離する",
            evidence="alpha=8.0 では未検証知見が高スコアになりすぎた",
            specificity=4,
        )
        ok, reason = passes_gate(lesson)
        assert ok is True


# ──────────────────────────────────────────────────────────────
# _normalize_distill_response (旧形式フォールバック)
# ──────────────────────────────────────────────────────────────

class TestNormalizeDistillResponse:
    def test_new_format_passthrough(self):
        raw = {
            "skip": False,
            "lessons": [
                {
                    "summary": "テスト",
                    "evidence": "evidence",
                    "applicability": "app",
                    "specificity": 4,
                    "tags": [],
                }
            ],
        }
        result = _normalize_distill_response(raw)
        assert result["lessons"] == raw["lessons"]
        assert result["skip"] is False

    def test_old_format_converted_to_lesson(self):
        raw = {
            "skip": False,
            "summary": "EMBEDDING_DIM=3072 で insert 失敗",
            "tags": ["embedding", "pgvector"],
        }
        result = _normalize_distill_response(raw)
        assert "lessons" in result
        assert len(result["lessons"]) == 1
        lesson = result["lessons"][0]
        assert lesson["summary"] == "EMBEDDING_DIM=3072 で insert 失敗"
        assert lesson["tags"] == ["embedding", "pgvector"]

    def test_old_format_generic_gets_low_specificity(self):
        raw = {
            "skip": False,
            "summary": "エラーハンドリングを適切に行う",
            "tags": [],
        }
        result = _normalize_distill_response(raw)
        lesson = result["lessons"][0]
        # is_generic=True なので specificity=1
        assert lesson["specificity"] == 1

    def test_old_format_concrete_gets_specificity_3(self):
        raw = {
            "skip": False,
            "summary": "asyncpg.UniqueViolationError は hash 重複で発生する",
            "tags": [],
        }
        result = _normalize_distill_response(raw)
        lesson = result["lessons"][0]
        assert lesson["specificity"] == 3

    def test_old_format_empty_summary_returns_skip(self):
        raw = {"skip": False, "summary": "", "tags": []}
        result = _normalize_distill_response(raw)
        assert result["skip"] is True
        assert result["lessons"] == []

    def test_skip_true_passthrough_in_old_format(self):
        raw = {"skip": True, "summary": "something", "tags": []}
        result = _normalize_distill_response(raw)
        # skip=True がそのまま伝わる
        assert result["skip"] is True
