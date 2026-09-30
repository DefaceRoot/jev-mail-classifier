from unittest.mock import MagicMock

from jev_mail.classify import classify, decide
from jev_mail.config import Decision
from tests.factories import make_config, probs


def test_classify_sends_one_question_per_category_in_config_order():
    client = MagicMock()
    client.decide.return_value = probs(scam=0.9)

    result = classify(client, make_config(), "state text")

    state, questions = client.decide.call_args.args
    assert state == "state text"
    assert list(questions.items())[:2] == [("scam", "Scam or phishing"), ("security", "Security alert")]
    assert result["scam"] == 0.9


def test_higher_priority_disposition_wins_and_every_label_is_kept():
    decision = decide(make_config(), probs(scam=0.95, security=0.9, receipt=0.75))

    assert decision.labels == ("Labels/JEV-SCAM", "Labels/JEV-SECURITY", "Labels/JEV-RECEIPT")
    assert decision.destination == "Folders/JEV Quarantine"


def test_keep_blocks_lower_priority_archive():
    decision = decide(make_config(), probs(action=0.9, cold_outreach=0.95))

    assert decision.labels == ("Labels/JEV-ACTION", "Labels/JEV-COLD")
    assert decision.destination is None


def test_archive_applies_when_nothing_above_it_has_an_opinion():
    decision = decide(make_config(), probs(receipt=0.9, cold_outreach=0.9))

    assert decision.destination == "Archive"


def test_matched_category_without_disposition_does_not_move():
    decision = decide(make_config(), probs(receipt=0.99))

    assert decision == Decision(labels=("Labels/JEV-RECEIPT",), destination=None, probabilities=probs(receipt=0.99))


def test_nothing_clears_threshold_gets_review_label_and_stays():
    decision = decide(make_config(), probs(scam=0.79, security=0.69, cold_outreach=0.5))

    assert decision.labels == ("Labels/JEV-REVIEW",)
    assert decision.destination is None


def test_category_threshold_overrides_default_and_is_inclusive():
    # scam has threshold 0.8, security falls back to the 0.7 default
    assert decide(make_config(), probs(scam=0.8)).labels == ("Labels/JEV-SCAM",)
    assert decide(make_config(), probs(security=0.7)).labels == ("Labels/JEV-SECURITY",)


def test_gmail_templates_nest_labels_and_use_all_mail():
    decision = decide(make_config(gmail=True), probs(cold_outreach=0.9))

    assert decision.labels == ("JEV/COLD",)
    assert decision.destination == "[Gmail]/All Mail"


def test_label_threshold_below_disposition_threshold_labels_without_moving():
    decision = decide(make_config(), probs(phishing=0.68))

    assert decision.labels == ("Labels/JEV-PHISHING",)
    assert decision.destination is None


def test_disposition_threshold_is_inclusive_and_moves_when_cleared():
    assert decide(make_config(), probs(phishing=0.8)).destination == "Folders/JEV Quarantine"
    assert decide(make_config(), probs(spam=0.95)).destination == "Archive"
    assert decide(make_config(), probs(spam=0.85)).destination is None


def test_lower_priority_archive_applies_when_higher_category_is_below_its_disposition_threshold():
    decision = decide(make_config(), probs(phishing=0.68, cold_outreach=0.9))

    assert decision.labels == ("Labels/JEV-PHISHING", "Labels/JEV-COLD")
    assert decision.destination == "Archive"


def test_keep_below_its_disposition_threshold_does_not_block_archive():
    from dataclasses import replace

    config = make_config()
    config.categories[config.categories.index(next(c for c in config.categories if c.name == "action"))] = replace(
        next(c for c in config.categories if c.name == "action"), disposition_threshold=0.95
    )

    assert decide(config, probs(action=0.9, cold_outreach=0.9)).destination == "Archive"
    assert decide(config, probs(action=0.96, cold_outreach=0.9)).destination is None
