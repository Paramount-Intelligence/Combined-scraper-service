"""
Tests for normalized Category classification (Groq policy + deterministic fallback).

Fallback tests cover conservative rules used when Groq fails or returns an invalid category.
Prompt tests ensure the business classification guide is embedded for Groq.
"""
import os
import sys
import unittest

_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from insert_to_spreadsheet import (  # noqa: E402
    CATEGORIES,
    CATEGORY_CLASSIFICATION_POLICY,
    deterministic_category_fallback,
    resolve_normalized_category,
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
    def test_accepts_valid_groq_category(self):
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
        self.assertEqual(source, "groq")
        self.assertAlmostEqual(conf, 0.91)
        self.assertIn("Security", reason)

    def test_invalid_groq_category_uses_fallback_not_sme(self):
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
        self.assertEqual(source, "groq")
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


if __name__ == "__main__":
    unittest.main()
