"""
Unit tests for Unit tests for PEP 615 zoneinfo parsing & DST offsets
"""

import pytest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from engine import CalendarFunctionEngine


@pytest.mark.unit
class TestTimezoneEngine:
    @pytest.fixture
    def engine(self):
        return CalendarFunctionEngine(default_tz="UTC")

    def test_dynamic_utc_offset_standard_time(self, engine):
        """Verify UTC offset calculation for America/New_York during Eastern Standard Time (EST: UTC-5)."""
        est_date = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        context = engine._parse_and_format_timezone_context(
            "America/New_York", anchor_dt=est_date
        )

        assert context["iana_identifier"] == "America/New_York"
        assert context["utc_offset"] == "-05:00"
        assert context["is_dst"] == "False"

    def test_dynamic_utc_offset_daylight_saving_time(self, engine):
        """Verify UTC offset calculation for America/New_York during Eastern Daylight Time (EDT: UTC-4)."""
        edt_date = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        context = engine._parse_and_format_timezone_context(
            "America/New_York", anchor_dt=edt_date
        )

        assert context["iana_identifier"] == "America/New_York"
        assert context["utc_offset"] == "-04:00"
        assert context["is_dst"] == "True"

    def test_fallback_invalid_timezone_string(self, engine):
        """Verify graceful fallback to default UTC when an invalid timezone string is provided."""
        context = engine._parse_and_format_timezone_context("Invalid/Timezone_Location")

        assert context["iana_identifier"] == "UTC"
        assert context["utc_offset"] == "+00:00"
