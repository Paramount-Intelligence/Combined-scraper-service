"""
Tests for normalized Category classification (Gemini policy + deterministic fallback)
and Platform Category handling for the Gemini classification pipeline.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import insert_to_spreadsheet as ins  # noqa: E402
from insert_to_spreadsheet import (  # noqa: E402
    CATEGORIES,
    CATEGORY_CLASSIFICATION_POLICY,
    ProjectSemantics,
    deterministic_category_fallback,
    extract_source_platform_category,
    map_record_to_row,
    query_gemini_semantics,
    resolve_normalized_category,
    resolve_platform_category,
    _is_transient_gemini_error,
)


class TestAllowedCategories(unittest.TestCase):
    def test_categories_list_is_exact(self):
        expected = [
            "Business Process and Operations",
            "Data",
            "Finance and Accounting",
            "General Consulting",
            "GTM (Marketing + Sales)",
            "Information Technology",
            "Product Management",
            "Program and Project Management",
            "Research and Due Diligence",
            "Corporate Strategy and Development",
            "Subject Matter Expert",
        ]
        self.assertEqual(CATEGORIES, expected)


class TestResolveNormalizedCategory(unittest.TestCase):
    def test_accepts_valid_gemini_category(self):
        cat, reason, conf, source = resolve_normalized_category(
            {
                "category": "Information Technology",
                "category_reasoning": "Security leadership role.",
                "category_confidence": 0.91,
            },
            title="CISO",
            description="Leads information security.",
        )
        self.assertEqual(cat, "Information Technology")
        self.assertEqual(source, "gemini")
        self.assertAlmostEqual(conf, 0.91)
        self.assertIn("Security", reason)

    def test_invalid_gemini_category_uses_fallback_not_sme(self):
        cat, reason, conf, source = resolve_normalized_category(
            {
                "category": "Made Up Category",
                "category_reasoning": "should be ignored",
                "category_confidence": 0.99,
            },
            title="Business Transformation SME",
            description=(
                "Supports requirements gathering, stakeholder coordination, "
                "process analysis, and change-management activities."
            ),
        )
        self.assertEqual(source, "fallback")
        self.assertEqual(cat, "General Consulting")
        self.assertEqual(conf, 0.0)
        self.assertNotEqual(cat, "Subject Matter Expert")

    def test_empty_semantics_falls_back_to_general_consulting(self):
        cat, _, _, source = resolve_normalized_category({}, title="Analyst", description="Support work")
        self.assertEqual(source, "fallback")
        self.assertEqual(cat, "General Consulting")

    def test_confidence_clamped(self):
        cat, _, conf, source = resolve_normalized_category(
            {
                "category": "Data",
                "category_confidence": 1.7,
                "category_reasoning": "Power BI developer.",
            }
        )
        self.assertEqual(source, "gemini")
        self.assertEqual(cat, "Data")
        self.assertEqual(conf, 1.0)


class TestDeterministicCategoryFallback(unittest.TestCase):
    def _fb(self, title, description=""):
        return deterministic_category_fallback(title, description)

    def test_information_security_leader(self):
        self.assertEqual(
            self._fb("Information Security Leader for Law Firm"),
            "Information Technology",
        )

    def test_it_project_manager_erp(self):
        self.assertEqual(
            self._fb("IT Project Manager for ERP Implementation"),
            "Program and Project Management",
        )

    def test_sap_finance_implementation(self):
        self.assertEqual(
            self._fb(
                "SAP S/4HANA Finance Implementation Consultant",
                "Configure and implement SAP Finance modules.",
            ),
            "Information Technology",
        )

    def test_peptide_formulation_sme(self):
        self.assertEqual(
            self._fb("Technical Peptide Formulation SME"),
            "Subject Matter Expert",
        )

    def test_satellite_regulation_sme(self):
        self.assertEqual(
            self._fb("Satellite Regulation SME"),
            "Subject Matter Expert",
        )

    def test_general_business_analyst(self):
        self.assertEqual(self._fb("General Business Analyst"), "General Consulting")

    def test_ba_functional_requirements(self):
        result = self._fb(
            "Business Analyst gathering functional requirements",
            "Gather business and functional requirements, process mapping, stakeholder workshops.",
        )
        self.assertIn(
            result,
            {"General Consulting", "Business Process and Operations"},
        )

    def test_organizational_change_manager(self):
        self.assertEqual(
            self._fb(
                "Organizational Change Manager",
                "Stakeholder engagement, communications planning, adoption and readiness.",
            ),
            "General Consulting",
        )

    def test_it_change_manager(self):
        self.assertEqual(
            self._fb(
                "IT Change Manager managing release and change tickets",
                "Owns release management and change tickets for production systems.",
            ),
            "Information Technology",
        )

    def test_power_bi_developer(self):
        self.assertEqual(self._fb("Power BI Developer"), "Data")

    def test_enterprise_architect(self):
        self.assertEqual(self._fb("Enterprise Architect"), "Information Technology")

    def test_voice_of_customer(self):
        self.assertEqual(
            self._fb("Voice of Customer Research Lead"),
            "Research and Due Diligence",
        )

    def test_interim_cfo(self):
        self.assertEqual(self._fb("Interim CFO"), "Finance and Accounting")

    def test_interim_cio(self):
        self.assertEqual(self._fb("Interim CIO"), "Information Technology")

    def test_head_of_product(self):
        self.assertEqual(self._fb("Head of Product"), "Product Management")

    def test_technology_transformation_strategy(self):
        self.assertEqual(
            self._fb(
                "Technology Transformation Strategy Lead defining enterprise direction",
                "Sets future-state technology direction and investment priorities.",
            ),
            "Corporate Strategy and Development",
        )

    def test_technology_transformation_implementation(self):
        result = self._fb(
            "Technology Transformation Implementation Lead",
            "Leads technical implementation of the selected technology platform.",
        )
        self.assertIn(
            result,
            {"Information Technology", "Program and Project Management"},
        )

    def test_procurement_transformation(self):
        self.assertEqual(
            self._fb("Procurement Transformation Consultant"),
            "Business Process and Operations",
        )

    def test_training_content_developer(self):
        self.assertEqual(
            self._fb(
                "Training Content Developer",
                "Creates learning content and delivers adoption training workshops.",
            ),
            "General Consulting",
        )

    def test_training_program_manager(self):
        self.assertEqual(
            self._fb("Training Program Manager"),
            "Program and Project Management",
        )

    def test_pricing_strategy_gtm(self):
        self.assertEqual(
            self._fb(
                "Pricing Strategy Consultant developing GTM and commercialization strategy",
                "Pricing as part of go-to-market and commercialization strategy.",
            ),
            "GTM (Marketing + Sales)",
        )

    def test_pricing_research(self):
        self.assertEqual(
            self._fb(
                "Pricing Research Consultant conducting willingness-to-pay interviews",
                "Conducts willingness-to-pay interviews and pricing research.",
            ),
            "Research and Due Diligence",
        )

    def test_negative_sme_marketing_expert(self):
        result = self._fb(
            "Marketing Strategy Expert",
            "Supports general market analysis, presentation development, "
            "stakeholder workshops, and GTM planning.",
        )
        self.assertEqual(result, "GTM (Marketing + Sales)")
        self.assertNotEqual(result, "Subject Matter Expert")

    def test_negative_sme_business_transformation(self):
        result = self._fb(
            "Business Transformation SME",
            "Supports requirements gathering, stakeholder coordination, "
            "process analysis, and change-management activities.",
        )
        self.assertEqual(result, "General Consulting")
        self.assertNotEqual(result, "Subject Matter Expert")

    def test_expert_keyword_alone_not_sme(self):
        self.assertEqual(
            self._fb("Industry Expert Advisor", "Provides general advisory support."),
            "General Consulting",
        )


class TestCategoryPolicyInPrompt(unittest.TestCase):
    def test_policy_contains_key_guidance(self):
        policy = CATEGORY_CLASSIFICATION_POLICY
        snippets = [
            "Subject Matter Expert",
            "General Consulting",
            "IT Project Manager",
            "Program and Project Management",
            "Product Manager",
            "Voice of the Customer",
            "Enterprise Architect",
            "organizational change management",
            "Do not classify based only on the Platform Category",
            "When uncertain between SME and General Consulting",
        ]
        for s in snippets:
            self.assertIn(s, policy)

    def test_policy_rejects_keyword_only_sme(self):
        self.assertIn("Those terms alone are insufficient", CATEGORY_CLASSIFICATION_POLICY)


class TestPlatformCategoryRules(unittest.TestCase):
    def test_catalant_keeps_source_platform_category(self):
        project = {
            "platform": "catalant",
            "source_platform_category": "Technology Assessment",
        }
        semantics = {
            "platform_category": "Information Technology",
            "category": "Information Technology",
        }
        pc, src = resolve_platform_category(project, semantics)
        self.assertEqual(pc, "Technology Assessment")
        self.assertEqual(src, "catalant_source")
        cat, _, _, cat_src = resolve_normalized_category(semantics, title="Tech Assessment")
        self.assertEqual(cat, "Information Technology")
        self.assertEqual(cat_src, "gemini")

    def test_catalant_missing_source_is_unclassified(self):
        pc, src = resolve_platform_category({"platform": "catalant"}, {"platform_category": "IT"})
        self.assertEqual(pc, "Unclassified")
        self.assertEqual(src, "missing_catalant_source")

    def test_non_catalant_uses_gemini_platform_category(self):
        project = {"platform": "btg", "title": "Information Security Leader"}
        semantics = {
            "platform_category": "Information Security",
            "category": "Information Technology",
        }
        pc, src = resolve_platform_category(project, semantics)
        self.assertEqual(pc, "Information Security")
        self.assertEqual(src, "gemini")
        cat, _, _, cat_src = resolve_normalized_category(semantics)
        self.assertEqual(cat, "Information Technology")
        self.assertEqual(cat_src, "gemini")

    def test_extract_source_platform_category_priority(self):
        self.assertEqual(
            extract_source_platform_category(
                {
                    "source_platform_category": "Technology Assessment",
                    "platform_category": "Other",
                }
            ),
            "Technology Assessment",
        )


class TestGeminiProviderHelpers(unittest.TestCase):
    def test_missing_key_does_not_crash_import(self):
        # Module already imported; client may be None without key.
        self.assertTrue(hasattr(ins, "gemini_client"))
        self.assertTrue(hasattr(ins, "GEMINI_MODEL"))
        self.assertEqual(ins.GEMINI_MODEL, os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))

    def test_query_returns_error_without_client(self):
        with patch.object(ins, "gemini_client", None):
            with self.assertRaises(ins.PermanentGeminiConfigError):
                query_gemini_semantics("t", "d", {})

    def test_transient_errors_detected(self):
        self.assertTrue(_is_transient_gemini_error(RuntimeError("429 RESOURCE_EXHAUSTED")))
        self.assertTrue(_is_transient_gemini_error(TimeoutError("deadline exceeded timeout")))
        self.assertFalse(_is_transient_gemini_error(RuntimeError("401 invalid api key")))
        self.assertFalse(_is_transient_gemini_error(RuntimeError("403 permission denied")))

    def test_query_parses_structured_response(self):
        payload = ProjectSemantics(
            platform_category="Information Security",
            category="Information Technology",
            category_reasoning="Security leadership.",
            category_confidence=0.9,
            industry="Financial Services",
            industry_secondary="Financial Services",
            role_type="Consultant",
            raw_rate_low=None,
            raw_rate_high=None,
            rate_currency=None,
            rate_period=None,
            duration_months_low=6,
            duration_months_high=6,
            utilization=1.0,
            daily_rate_reasoning="No rate found.",
        )
        mock_response = MagicMock()
        mock_response.text = payload.model_dump_json()
        mock_response.usage_metadata = MagicMock(
            prompt_token_count=10,
            candidates_token_count=20,
            total_token_count=30,
        )
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        with patch.object(ins, "gemini_client", mock_client):
            result = query_gemini_semantics(
                "Information Security Leader",
                "Lead cyber security for a bank.",
                {"platform": "btg"},
            )
        self.assertEqual(result["category"], "Information Technology")
        self.assertEqual(result["platform_category"], "Information Security")
        mock_client.models.generate_content.assert_called_once()
        kwargs = mock_client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["model"], ins.GEMINI_MODEL)
        self.assertNotIn("temperature", kwargs.get("config").model_dump(exclude_none=True))

    def test_empty_response_retries_then_raises(self):
        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.usage_metadata = None
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response
        with patch.object(ins, "gemini_client", mock_client):
            with patch.object(ins, "time") as mock_time:
                mock_time.sleep = MagicMock()
                with self.assertRaises(ins.AIClassificationError):
                    query_gemini_semantics("t", "d", {"platform": "btg"})
        # primary attempts + fallback attempts
        self.assertGreaterEqual(
            mock_client.models.generate_content.call_count,
            ins.AI_ATTEMPTS_PER_MODEL,
        )

    def test_permanent_error_not_retried(self):
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = RuntimeError("401 invalid api key")
        with patch.object(ins, "gemini_client", mock_client):
            with self.assertRaises(ins.PermanentGeminiConfigError):
                query_gemini_semantics("t", "d", {})
        self.assertEqual(mock_client.models.generate_content.call_count, 1)


class TestSpreadsheetRowCompatibility(unittest.TestCase):
    def test_row_shape_and_catalant_platform_category(self):
        project = {
            "platform": "catalant",
            "source_platform_category": "Technology Assessment",
            "title": "Tech Assessor",
            "description": "Assess enterprise technology options.",
            "detected_at": "2026-07-24 10:00:00",
            "url": "https://example.com/job/1",
            "remote_type": "Remote",
            "location": "Remote",
        }
        semantics = {
            "platform_category": "Information Technology",
            "category": "Information Technology",
            "category_reasoning": "Technology assessment work.",
            "category_confidence": 0.88,
            "industry": "Technology",
            "industry_secondary": "Software and Services",
            "role_type": "Consultant",
            "raw_rate_low": None,
            "raw_rate_high": None,
            "rate_currency": None,
            "rate_period": None,
            "duration_months_low": 6,
            "duration_months_high": 6,
            "utilization": 1.0,
            "daily_rate_reasoning": "No rate.",
        }
        with patch.object(ins, "query_gemini_semantics", return_value=semantics):
            row = map_record_to_row(project)
        self.assertEqual(len(row), 22)
        self.assertEqual(row[2], "Technology Assessment")  # Platform Category
        self.assertEqual(row[3], "Information Technology")  # Category
        self.assertEqual(row[4], "Tech Assessor")
        self.assertEqual(row[16], "Catalant")
        self.assertEqual(row[19], "https://example.com/job/1")
        self.assertEqual(row[21], "CATALANT")

    def test_btg_platform_category_from_gemini(self):
        project = {
            "platform": "btg",
            "title": "Information Security Leader",
            "description": "Lead information security.",
            "detected_at": "2026-07-24 10:00:00",
            "url": "https://example.com/btg/1",
            "remote_type": "Hybrid",
        }
        semantics = {
            "platform_category": "Information Security",
            "category": "Information Technology",
            "category_reasoning": "Security leadership.",
            "category_confidence": 0.9,
            "industry": "Financial Services",
            "industry_secondary": "Financial Services",
            "role_type": "Consultant",
            "duration_months_low": 3,
            "duration_months_high": 6,
            "utilization": 1.0,
            "daily_rate_reasoning": "No rate.",
        }
        with patch.object(ins, "query_gemini_semantics", return_value=semantics):
            row = map_record_to_row(project)
        self.assertEqual(row[2], "Information Security")
        self.assertEqual(row[3], "Information Technology")
        self.assertEqual(row[16], "BTG")
        self.assertEqual(len(row), 22)


if __name__ == "__main__":
    unittest.main()
