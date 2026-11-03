import pytest

from emporio.policy import TOPIC_DESCRIPTIONS, TOPICS


def test_every_topic_has_text_and_description(policies):
    for topic in TOPICS:
        assert policies.get(topic).startswith(f"## {TOPICS[topic]}.")
        assert topic in TOPIC_DESCRIPTIONS


def test_sections_hold_the_rules_the_agent_relies_on(policies):
    assert "7 (sete) dias corridos" in policies.get("returns")
    assert "parcela mínima de R$ 80,00" in policies.get("payment")
    assert "24 horas úteis" in policies.get("service")
    assert "Rua 14 de Maio, 3200" in policies.get("about")


def test_unknown_topic_is_rejected(policies):
    with pytest.raises(KeyError):
        policies.get("refunds")
