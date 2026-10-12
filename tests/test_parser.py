"""
Tests for CBR XML parser and business logic.
Run:  python -m pytest tests/ -v
"""

import pytest
from decimal import Decimal
from datetime import date

# Import functions from main.py (parent directory)
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from main import parse_xml, THRESHOLD_PERCENT


# ---------------------------------------------------------------------------
# Test data: realistic CBR XML snippets
# ---------------------------------------------------------------------------

SAMPLE_XML_BASIC = b"""<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="10.10.2026" name="Foreign Currency Market">
    <Valute ID="R01235">
        <NumCode>840</NumCode>
        <CharCode>USD</CharCode>
        <Nominal>1</Nominal>
        <Name>\xc4\xee\xeb\xeb\xe0\xf0 \xd1\xd8\xc0</Name>
        <Value>92,5000</Value>
        <VunitRate>92,5000</VunitRate>
    </Valute>
    <Valute ID="R01239">
        <NumCode>978</NumCode>
        <CharCode>EUR</CharCode>
        <Nominal>1</Nominal>
        <Name>\xc5\xe2\xf0\xee</Name>
        <Value>101,2340</Value>
        <VunitRate>101,2340</VunitRate>
    </Valute>
</ValCurs>"""

# JPY has Nominal=100 — rate must be divided
SAMPLE_XML_NOMINAL = b"""<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="10.10.2026" name="Foreign Currency Market">
    <Valute ID="R01820">
        <NumCode>392</NumCode>
        <CharCode>JPY</CharCode>
        <Nominal>100</Nominal>
        <Name>\xdf\xef\xee\xed\xf1\xea\xe0\xff \xe8\xe5\xed\xe0</Name>
        <Value>62,2076</Value>
        <VunitRate>0,622076</VunitRate>
    </Valute>
</ValCurs>"""

# Broken XML: missing CharCode
SAMPLE_XML_BROKEN = b"""<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="10.10.2026" name="Foreign Currency Market">
    <Valute ID="R99999">
        <NumCode>999</NumCode>
        <Nominal>1</Nominal>
        <Name>Broken</Name>
        <Value>1,0000</Value>
    </Valute>
</ValCurs>"""

SAMPLE_XML_EMPTY = b"""<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="10.10.2026" name="Foreign Currency Market">
</ValCurs>"""


# ---------------------------------------------------------------------------
# Tests: parse_xml
# ---------------------------------------------------------------------------

class TestParseXmlBasic:
    """Basic parsing: date, number of currencies, field values."""

    def test_returns_correct_date(self):
        rate_date, _ = parse_xml(SAMPLE_XML_BASIC)
        assert rate_date == date(2026, 10, 10)

    def test_returns_correct_count(self):
        _, records = parse_xml(SAMPLE_XML_BASIC)
        assert len(records) == 2

    def test_usd_rate_value(self):
        _, records = parse_xml(SAMPLE_XML_BASIC)
        usd = next(r for r in records if r["currency_code"] == "USD")
        assert usd["rate"] == Decimal("92.5000")

    def test_eur_rate_value(self):
        _, records = parse_xml(SAMPLE_XML_BASIC)
        eur = next(r for r in records if r["currency_code"] == "EUR")
        assert eur["rate"] == Decimal("101.2340")

    def test_rate_date_in_each_record(self):
        rate_date, records = parse_xml(SAMPLE_XML_BASIC)
        for r in records:
            assert r["rate_date"] == rate_date


class TestParseXmlNominal:
    """Nominal > 1: rate must be divided by nominal."""

    def test_jpy_nominal_is_100(self):
        _, records = parse_xml(SAMPLE_XML_NOMINAL)
        jpy = records[0]
        assert jpy["nominal"] == 100

    def test_jpy_rate_divided_by_nominal(self):
        """62.2076 / 100 = 0.622076 per 1 JPY."""
        _, records = parse_xml(SAMPLE_XML_NOMINAL)
        jpy = records[0]
        expected = Decimal("62.2076") / 100
        assert jpy["rate"] == expected

    def test_jpy_rate_is_not_raw_value(self):
        """Rate must NOT be 62.2076 (that's per 100 JPY, not per 1)."""
        _, records = parse_xml(SAMPLE_XML_NOMINAL)
        jpy = records[0]
        assert jpy["rate"] != Decimal("62.2076")


class TestParseXmlCommaDecimal:
    """CBR uses comma as decimal separator — must be handled."""

    def test_comma_replaced(self):
        _, records = parse_xml(SAMPLE_XML_BASIC)
        usd = next(r for r in records if r["currency_code"] == "USD")
        # If comma wasn't replaced, Decimal("92,5000") would raise an error
        # or produce a wrong value. We simply check it parsed correctly.
        assert usd["rate"] > Decimal("0")


class TestParseXmlEdgeCases:
    """Broken / empty XML."""

    def test_broken_valute_skipped(self):
        """Missing CharCode → valute is skipped, no crash."""
        _, records = parse_xml(SAMPLE_XML_BROKEN)
        assert len(records) == 0

    def test_empty_valcurs(self):
        """No Valute elements → empty list, no crash."""
        rate_date, records = parse_xml(SAMPLE_XML_EMPTY)
        assert records == []
        assert rate_date == date(2026, 10, 10)


# ---------------------------------------------------------------------------
# Tests: change_pct calculation logic
# ---------------------------------------------------------------------------

class TestChangePctLogic:
    """
    change_pct is computed in upsert_rate (requires DB).
    Here we test the FORMULA in isolation.
    """

    @staticmethod
    def calc_change(rate: Decimal, previous: Decimal) -> Decimal:
        """Same formula as in main.py → upsert_rate."""
        return (rate - previous) / previous * 100

    def test_positive_change(self):
        pct = self.calc_change(Decimal("95.0"), Decimal("90.0"))
        assert round(pct, 2) == Decimal("5.56")

    def test_negative_change(self):
        pct = self.calc_change(Decimal("85.0"), Decimal("90.0"))
        assert round(pct, 2) == Decimal("-5.56")

    def test_no_change(self):
        pct = self.calc_change(Decimal("90.0"), Decimal("90.0"))
        assert pct == Decimal("0")

    def test_small_change_below_threshold(self):
        pct = self.calc_change(Decimal("90.5"), Decimal("90.0"))
        assert abs(pct) < THRESHOLD_PERCENT

    def test_large_change_above_threshold(self):
        pct = self.calc_change(Decimal("95.0"), Decimal("90.0"))
        assert abs(pct) > THRESHOLD_PERCENT


# ---------------------------------------------------------------------------
# Tests: threshold alert logic
# ---------------------------------------------------------------------------

class TestAlertThreshold:
    """Verify that alerts fire only when |change_pct| > threshold."""

    @staticmethod
    def should_alert(change_pct: Decimal, threshold: Decimal) -> bool:
        return change_pct is not None and abs(change_pct) > threshold

    def test_above_threshold_triggers(self):
        assert self.should_alert(Decimal("3.5"), Decimal("2.0")) is True

    def test_below_threshold_no_trigger(self):
        assert self.should_alert(Decimal("1.5"), Decimal("2.0")) is False

    def test_exact_threshold_no_trigger(self):
        """Exactly equal to threshold → no alert (strict >)."""
        assert self.should_alert(Decimal("2.0"), Decimal("2.0")) is False

    def test_negative_change_triggers(self):
        """Drop of -3.5% should also trigger."""
        assert self.should_alert(Decimal("-3.5"), Decimal("2.0")) is True

    def test_none_change_no_trigger(self):
        assert self.should_alert(None, Decimal("2.0")) is False
