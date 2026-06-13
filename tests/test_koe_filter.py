"""app.koe_filter（会話判定フィルタ）の単体テスト。"""

from app import koe_filter


def _u(speaker, content, raw=None):
    return {"speaker": speaker, "speaker_raw": raw or speaker, "content": content}


def test_announcement_dominant_is_noise():
    # 機内アナウンス主体（社長ほぼ不在）→ noise
    utts = [
        _u("Speaker 2", "シートベルトを腰の低い位置でお締めください。"),
        _u("Speaker 6", "Please fasten your seat belt. emergency exits are located..."),
        _u("Speaker 5", "客室乗務員にお知らせください。非常口をご確認ください。"),
        _u("西原さん", "はい。"),
    ]
    ok, reason, _ = koe_filter.is_conversation(utts)
    assert ok is False
    assert reason == "announcement_dominant"


def test_owner_active_is_conversation():
    # 社長が複数回しっかり喋る打ち合わせ → 会話
    utts = [
        _u("高木豊大", "山田コンサルとの協業を進めたい。複数社で引き合いを出そう。", "Atsuhiro Takagi"),
        _u("松本さん", "外国人材は在住者中心でマッチングできます。"),
        _u("高木豊大", "技能実習の廃止後の特定技能はどうなる？", "Atsuhiro Takagi"),
        _u("松本さん", "2027年廃止で特定技能に移行します。"),
        _u("高木豊大", "じゃあ次回、川平さんに繋いでもらおう。", "Atsuhiro Takagi"),
    ]
    ok, reason, score = koe_filter.is_conversation(utts)
    assert ok is True
    assert reason == "owner_active"
    assert score["owner"] == 3


def test_dialog_with_owner_is_conversation():
    # 社長1回＋複数話者・アナウンス無し → 対話として会話
    utts = [
        _u("高木豊大", "この件どう思う？", "Atsuhiro Takagi"),
        _u("西原さん", "いいと思います。進めましょう。"),
        _u("松本さん", "私も賛成です。"),
    ]
    ok, reason, _ = koe_filter.is_conversation(utts)
    assert ok is True
    assert reason == "dialog"


def test_ambient_single_speaker_is_noise():
    # 社長不在・単一話者の環境音的 → noise
    utts = [
        _u("Speaker 1", "うーん。"),
        _u("Speaker 1", "んー。"),
        _u("Speaker 1", "そうか。"),
    ]
    ok, reason, _ = koe_filter.is_conversation(utts)
    assert ok is False
    assert reason == "low_signal_or_ambient"


def test_empty():
    ok, reason, score = koe_filter.is_conversation([])
    assert ok is False
    assert reason == "no_utterances"
    assert score["n"] == 0


def test_owner_absent_multiparty_is_noise():
    # 社長不在・複数話者・非アナウンス（英語PA放送など）→ noise（owner必須化で偽陽性を防ぐ）
    utts = [
        _u("Speaker 2", "Welcome aboard."),
        _u("Speaker 3", "Boarding will begin shortly."),
        _u("Speaker 2", "Please have your boarding pass ready."),
        _u("Speaker 4", "Thank you for your patience."),
        _u("Speaker 3", "Gate 12 is now open."),
        _u("Speaker 2", "We will begin with priority boarding."),
    ]
    ok, reason, _ = koe_filter.is_conversation(utts)
    assert ok is False
    assert reason == "low_signal_or_ambient"


def test_announce_ratio_boundary_exactly_40pct_is_noise():
    # アナウンス語比率ちょうど0.4 → noise（>=0.4 が効く）。owner含む5発話中2発話がアナウンス。
    utts = [
        _u("高木豊大", "今日はよろしく", "Atsuhiro Takagi"),
        _u("高木豊大", "進めよう", "Atsuhiro Takagi"),
        _u("西原さん", "はい"),
        _u("CA", "シートベルトをお締めください"),
        _u("CA2", "客室乗務員にお知らせください"),
    ]
    s = koe_filter.conversation_score(utts)
    assert s["announce_ratio"] == 0.4
    ok, reason, _ = koe_filter.is_conversation(utts)
    assert ok is False
    assert reason == "announcement_dominant"


def test_owner_ratio_boundary_exactly_15pct_is_conversation():
    # owner_ratio ちょうど0.15以上（owner2/n13≈0.154）→ 会話（owner_active）
    utts = [_u("高木豊大", "あ", "Atsuhiro Takagi"), _u("高木豊大", "うん", "Atsuhiro Takagi")]
    utts += [_u("西原さん", f"発言{i}") for i in range(11)]  # owner 2 / n 13
    s = koe_filter.conversation_score(utts)
    assert s["owner"] == 2 and s["n"] == 13
    assert s["owner_ratio"] >= 0.15
    ok, reason, _ = koe_filter.is_conversation(utts)
    assert ok is True
    assert reason == "owner_active"


def test_score_fields():
    utts = [_u("高木豊大", "あ", "Atsuhiro Takagi"), _u("西原さん", "はい")]
    s = koe_filter.conversation_score(utts)
    assert s["n"] == 2
    assert s["owner"] == 1
    assert s["speakers"] == 2
