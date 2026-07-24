"""Tests for source-duration precedence over Gemini defaults."""

import unittest
from unittest.mock import patch

import scrapers.spreadsheet_insert.insert_to_spreadsheet as ins
from scrapers.spreadsheet_insert.insert_to_spreadsheet import (
    FALLBACK_DURATION_MONTHS,
    extract_source_duration,
    map_record_to_row,
    parse_duration_to_months,
    resolve_duration_months,
)


def _base_semantics(**overrides):
    data = {
        "platform_category": "Data Analytics",
        "category": "Information Technology",
        "category_reasoning": "Analytics leadership.",
        "category_confidence": 0.9,
        "industry": "Technology",
        "industry_secondary": "Software and Services",
        "role_type": "Consultant",
        "raw_rate_low": None,
        "raw_rate_high": None,
        "rate_currency": None,
        "rate_period": None,
        "duration_months_low": 12,
        "duration_months_high": 12,
        "utilization": 1.0,
        "daily_rate_reasoning": "Default rate.",
    }
    data.update(overrides)
    return data


class TestParseDurationToMonths(unittest.TestCase):
    def test_single_month_values(self):
        self.assertEqual(parse_duration_to_months("3 Months"), (3.0, 3.0))
        self.assertEqual(parse_duration_to_months("12 months"), (12.0, 12.0))
        self.assertEqual(parse_duration_to_months("12-month contract"), (12.0, 12.0))
        self.assertEqual(parse_duration_to_months("Duration: 9 months"), (9.0, 9.0))
        self.assertEqual(parse_duration_to_months("6 Months"), (6.0, 6.0))

    def test_month_ranges(self):
        self.assertEqual(parse_duration_to_months("3-5 months"), (3.0, 5.0))
        self.assertEqual(parse_duration_to_months("6–12 months"), (6.0, 12.0))
        self.assertEqual(parse_duration_to_months("3 to 5 months"), (3.0, 5.0))
        self.assertEqual(parse_duration_to_months("Between 4 and 6 months"), (4.0, 6.0))

    def test_week_values(self):
        self.assertEqual(parse_duration_to_months("4 weeks"), (1.0, 1.0))
        self.assertEqual(parse_duration_to_months("6 weeks"), (1.5, 1.5))
        self.assertEqual(parse_duration_to_months("4-6 weeks"), (1.0, 1.5))
        self.assertEqual(parse_duration_to_months("10 weeks"), (2.5, 2.5))
        self.assertEqual(parse_duration_to_months("8 weeks"), (2.0, 2.0))

    def test_year_values(self):
        self.assertEqual(parse_duration_to_months("1 year"), (12.0, 12.0))
        self.assertEqual(parse_duration_to_months("2 years"), (24.0, 24.0))
        self.assertEqual(parse_duration_to_months("3-4 years"), (36.0, 48.0))

    def test_day_values(self):
        self.assertEqual(parse_duration_to_months("20 days"), (1.0, 1.0))
        self.assertEqual(parse_duration_to_months("40 days"), (2.0, 2.0))

    def test_experience_is_rejected(self):
        self.assertIsNone(parse_duration_to_months("5+ years of experience"))
        self.assertIsNone(parse_duration_to_months("6-10 years experience"))
        self.assertIsNone(parse_duration_to_months("Mid Level (6-10 years)"))
        self.assertIsNone(parse_duration_to_months("At least 3 years working with Power BI"))
        self.assertIsNone(parse_duration_to_months("Senior level with 11-15 years"))


class TestExtractSourceDuration(unittest.TestCase):
    def test_outsized_duration_field(self):
        project = {
            "platform": "outsized",
            "title": "Data Analytics Manager",
            "duration": "6 Months",
            "description": "Experience: Mid Level (6-10 years)",
        }
        self.assertEqual(extract_source_duration(project), "6 Months")

    def test_experience_in_description_without_duration_field(self):
        project = {
            "platform": "outsized",
            "title": "Analyst",
            "description": "5+ years of Power BI experience required.",
        }
        self.assertEqual(extract_source_duration(project), "")

    def test_labeled_duration_in_description(self):
        project = {
            "platform": "btg",
            "title": "PM",
            "description": "Scope details.\nDuration: 3-5 months\nSkills: Agile",
        }
        self.assertEqual(extract_source_duration(project), "3-5 months")


class TestDurationPrecedence(unittest.TestCase):
    def test_source_overrides_gemini(self):
        project = {
            "platform": "outsized",
            "title": "Data Analytics Manager",
            "duration": "6 Months",
        }
        semantics = _base_semantics(
            duration_months_low=12,
            duration_months_high=12,
        )
        dur_low, dur_high, source, raw = resolve_duration_months(project, semantics)
        self.assertEqual(dur_low, 6.0)
        self.assertEqual(dur_high, 6.0)
        self.assertEqual(source, "source")
        self.assertEqual(raw, "6 Months")

    def test_gemini_used_when_no_source(self):
        project = {"platform": "btg", "title": "Role"}
        semantics = _base_semantics(
            duration_months_low=3,
            duration_months_high=5,
        )
        dur_low, dur_high, source, _ = resolve_duration_months(project, semantics)
        self.assertEqual((dur_low, dur_high, source), (3.0, 5.0, "gemini"))

    def test_default_when_no_source_or_gemini(self):
        project = {"platform": "btg", "title": "Role"}
        semantics = _base_semantics(
            duration_months_low=None,
            duration_months_high=None,
        )
        dur_low, dur_high, source, _ = resolve_duration_months(project, semantics)
        self.assertEqual(dur_low, FALLBACK_DURATION_MONTHS)
        self.assertEqual(dur_high, FALLBACK_DURATION_MONTHS)
        self.assertEqual(dur_low, 12.0)
        self.assertEqual(source, "default")

    def test_experience_alone_does_not_become_sixty_months(self):
        project = {
            "platform": "outsized",
            "title": "Analyst",
            "description": "5+ years of Power BI experience",
        }
        semantics = _base_semantics(
            duration_months_low=None,
            duration_months_high=None,
        )
        dur_low, dur_high, source, _ = resolve_duration_months(project, semantics)
        self.assertNotEqual(dur_low, 60.0)
        self.assertEqual(source, "default")
        self.assertEqual(dur_low, 12.0)


class TestMappedRowDurationAndValue(unittest.TestCase):
    def test_outsized_six_month_project_overrides_gemini_and_value(self):
        project = {
            "platform": "outsized",
            "title": "Data Analytics Manager",
            "description": "Experience: Mid Level (6-10 years). Lead analytics delivery.",
            "duration": "6 Months",
            "detected_at": "2026-07-24 10:00:00",
            "url": "https://talent.outsized.com/example",
            "remote_type": "Remote",
            "location": "Remote",
        }
        semantics = _base_semantics(
            duration_months_low=12,
            duration_months_high=12,
            utilization=1.0,
            raw_rate_low=None,
            raw_rate_high=None,
        )
        with patch.object(ins, "query_gemini_semantics", return_value=semantics):
            row = map_record_to_row(project)

        self.assertEqual(row[10], "6")  # Duration Low
        self.assertEqual(row[11], "6")  # Duration High
        self.assertEqual(row[12], "1.0")  # Utilization
        # Default daily rate $799 × 6 × 20 × 1.0 = $95,880
        self.assertEqual(row[8], "$799")
        self.assertEqual(row[9], "$799")
        self.assertEqual(row[17], "$95,880")
        self.assertEqual(row[18], "$95,880")
        self.assertEqual(project.get("_duration_source"), "source")


if __name__ == "__main__":
    unittest.main()
