import os
import unittest
from unittest.mock import patch


os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("NOTION_TOKEN", "test-token")

import bot


class ReceiptParsingTests(unittest.TestCase):
    def test_parse_price_understands_dot_and_comma_decimals(self):
        cases = {
            "0.93": 0.93,
            "0,93": 0.93,
            "1.234,56": 1234.56,
            "1,234.56": 1234.56,
            "1 234,56": 1234.56,
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bot.parse_price(raw), expected)

    def test_detect_items_uses_line_total_not_weight_or_unit_price(self):
        lines = [
            "MERCADONA",
            "CEBOLLA 0,465 kg x 2,00 €/kg 0,93",
            "DATIL SIN HUESO 1,25",
            "SUBTOTAL 2,18",
            "TOTAL EUR 2,18",
        ]

        self.assertEqual(
            bot.detect_items(lines),
            [
                {"name": "CEBOLLA", "price": 0.93, "type": "Alimentation"},
                {"name": "DATIL SIN HUESO", "price": 1.25, "type": "Alimentation"},
            ],
        )
        self.assertEqual(bot.detect_total(lines), 2.18)

    def test_reconcile_uses_missing_item_instead_of_negative_ticket_adjustment(self):
        items = [{"name": "Pain", "price": 1.0, "type": "Alimentation"}]

        reconciled, warnings = bot.reconcile_items(items, 2.5)

        self.assertEqual(sum(item["price"] for item in reconciled), 2.5)
        self.assertEqual(reconciled[-1]["name"], "Article(s) non identifié(s)")
        self.assertTrue(warnings)


class NotionWeekTests(unittest.TestCase):
    def test_find_week_by_start_date_not_display_title(self):
        existing = {
            "results": [
                {
                    "id": "week-id",
                    "properties": {
                        "Semaine": {
                            "type": "title",
                            "title": [{"plain_text": "Semaine du 3 au 9 août 2026"}],
                        }
                    },
                }
            ]
        }

        with patch("bot.notion_request", return_value=existing) as request:
            page_id = bot.find_or_create_week(bot.date(2026, 8, 5))

        self.assertEqual(page_id, "week-id")
        query = request.call_args.kwargs["json"]
        self.assertEqual(
            query["filter"],
            {"property": "Début", "date": {"equals": "2026-08-03"}},
        )

    def test_calculate_week_spent_sums_linked_purchased_expenses(self):
        response = {
            "results": [
                {"properties": {"Prix réel": {"type": "number", "number": 1.52}}},
                {"properties": {"Prix réel": {"type": "number", "number": 1.09}}},
            ],
            "has_more": False,
        }

        with patch("bot.notion_request", return_value=response) as request:
            spent = bot.calculate_week_spent("week-id")

        self.assertEqual(spent, 2.61)
        filters = request.call_args.kwargs["json"]["filter"]["and"]
        self.assertIn(
            {"property": "Semaine", "relation": {"contains": "week-id"}},
            filters,
        )
        self.assertIn(
            {"property": "Statut", "select": {"equals": "Acheté"}},
            filters,
        )


if __name__ == "__main__":
    unittest.main()
