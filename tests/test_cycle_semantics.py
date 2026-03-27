"""Тесты семантики исходов цикла (п.1–2)."""

from modules import cycle_semantics as cs


def test_merge_outcomes_empty_is_ok():
    assert cs.merge_outcomes([]) == cs.OK


def test_merge_outcomes_picks_worst():
    assert cs.merge_outcomes([cs.OK, cs.TRANSPORT_FAILURE]) == cs.TRANSPORT_FAILURE
    assert cs.merge_outcomes([cs.PAUSED, cs.INCOMPLETE_EVIDENCE]) == cs.INCOMPLETE_EVIDENCE


def test_severity_error_above_transport():
    assert cs.severity(cs.ERROR) > cs.severity(cs.TRANSPORT_FAILURE)
