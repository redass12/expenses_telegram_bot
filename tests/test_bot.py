import os
import unittest
from unittest.mock import MagicMock, patch


os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("NOTION_TOKEN", "test-token")

import bot


class ReceiptParsingTests(unittest.TestCase):
    RAPID_RECEIPT_TEXT = """LIDL SUPERMERCADOS S.A.U.
PAN BOCADILLO 1,29x 2 2,58 A
CIRUELA ROJA 0,71 A
TOTAL 3,29
28/07/2026 20:48:23"""

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

    def test_detect_purchase_date_understands_written_month(self):
        self.assertEqual(
            bot.detect_purchase_date(["28 Jul 2026"]),
            bot.date(2026, 7, 28),
        )

    def test_invalid_numeric_date_is_reported_as_uncertain(self):
        text = """LIDL SUPERMERCADOS S.A.U.
PAN BOCADILLO 2,58 A
TOTAL 2,58
28/87/2826 20:48:23"""

        with patch(
            "bot.read_receipt_with_rapidocr",
            return_value=(text, "rapidocr/ppocrv6", 0.72),
        ):
            receipt = bot.extract_receipt("ticket.jpg", "invalid-date")

        self.assertIn(
            "Date non détectée : date d’aujourd’hui utilisée.",
            receipt["warnings"],
        )

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

    def test_detect_items_removes_a_misread_multiplication_marker(self):
        self.assertEqual(
            bot.detect_items(["PAN BOCADILLO 1,29s 2 2,58 A"]),
            [{"name": "PAN BOCADILLO", "price": 2.58, "type": "Alimentation"}],
        )

    def test_detect_items_ignores_a_degraded_tax_summary_row(self):
        self.assertEqual(bot.detect_items(["a a 0.13 3,16 3,29"]), [])

    def test_reconcile_uses_missing_item_instead_of_negative_ticket_adjustment(self):
        items = [{"name": "Pain", "price": 1.0, "type": "Alimentation"}]

        reconciled, warnings = bot.reconcile_items(items, 2.5)

        self.assertEqual(sum(item["price"] for item in reconciled), 2.5)
        self.assertEqual(reconciled[-1]["name"], "Article(s) non identifié(s)")
        self.assertTrue(warnings)

    def test_lidl_screenshot_ignores_phone_ui_and_tax_suffixes(self):
        lines = [
            "02:37 = oll S G4):",
            "E 28 Jul 2026 AE",
            "LIDL SUPERMERCADOS S.A.U.",
            "Paseo del Carmen N° 20",
            "13250 Ciudad Real",
            "PAN BOCADILLO 1,29x 2 2,58 A",
            "CIRUELA ROJA 0,71 A",
            "0,206 kg x 3,45 EUR/kg",
            "TOTAL 3,29",
            "ENTREGA 3,29",
            "28/07/2026 20:48:23",
            "IMP.: 3,29 EUR",
            "A 4% 0,13 3,16 3,29",
            "Suma 0,13 3,16 3,29",
        ]

        self.assertEqual(bot.detect_merchant(lines), "LIDL SUPERMERCADOS S.A.U.")
        self.assertEqual(
            bot.detect_items(lines),
            [
                {"name": "PAN BOCADILLO", "price": 2.58, "type": "Alimentation"},
                {"name": "CIRUELA ROJA", "price": 0.71, "type": "Alimentation"},
            ],
        )
        self.assertEqual(bot.detect_total(lines), 3.29)

    def test_render_ocr_uses_one_bounded_high_value_attempt(self):
        variants = [
            ("contrast", "contrast-image"),
            ("otsu", "otsu-image"),
            ("adaptive", "adaptive-image"),
        ]

        with (
            patch.dict(
                os.environ,
                {"OCR_MAX_ATTEMPTS": "1", "OCR_PASS_TIMEOUT_SECONDS": "35"},
                clear=False,
            ),
            patch("bot.preprocess_images", return_value=variants),
            patch("bot.pytesseract.get_languages", return_value=["spa", "fra", "eng"]),
            patch(
                "bot.pytesseract.image_to_string",
                return_value="LIDL SUPERMERCADOS\nTOTAL 3,29",
            ) as image_to_string,
        ):
            _, method = bot.read_receipt_text("ticket.jpg")

        self.assertEqual(method, "adaptive/psm6")
        image_to_string.assert_called_once()
        self.assertEqual(image_to_string.call_args.kwargs["timeout"], 35)

    def test_rapidocr_boxes_are_reassembled_into_receipt_rows(self):
        boxes = [
            [[20, 10], [280, 10], [280, 30], [20, 30]],
            [[20, 50], [170, 50], [170, 70], [20, 70]],
            [[190, 51], [280, 51], [280, 71], [190, 71]],
            [[360, 49], [430, 49], [430, 69], [360, 69]],
            [[20, 90], [90, 90], [90, 110], [20, 110]],
            [[360, 91], [430, 91], [430, 111], [360, 111]],
        ]

        self.assertEqual(
            bot._rapidocr_rows(
                bot.np.asarray(boxes),
                bot.np.asarray([
                    "LIDL SUPERMERCADOS S.A.U.",
                    "PAN BOCADILLO",
                    "1,29x 2",
                    "2,58 A",
                    "TOTAL",
                    "3,29",
                ]),
                bot.np.asarray([0.99, 0.98, 0.97, 0.99, 0.99, 0.99]),
            ),
            [
                "LIDL SUPERMERCADOS S.A.U.",
                "PAN BOCADILLO 1,29x 2 2,58 A",
                "TOTAL 3,29",
            ],
        )

    def test_extract_receipt_prefers_rapidocr(self):
        with (
            patch(
                "bot.read_receipt_with_rapidocr",
                create=True,
                return_value=(self.RAPID_RECEIPT_TEXT, "rapidocr/ppocrv6", 0.98),
            ) as rapidocr,
            patch("bot.read_receipt_text", side_effect=AssertionError("fallback called")),
        ):
            receipt = bot.extract_receipt("ticket.jpg", "rapid-primary")

        rapidocr.assert_called_once_with("ticket.jpg")
        self.assertEqual(receipt["ocr_method"], "rapidocr/ppocrv6")
        self.assertEqual(receipt["total"], 3.29)

    def test_extract_receipt_warns_when_rapidocr_confidence_is_low(self):
        with patch(
            "bot.read_receipt_with_rapidocr",
            return_value=(self.RAPID_RECEIPT_TEXT, "rapidocr/ppocrv6", 0.82),
        ):
            receipt = bot.extract_receipt("ticket.jpg", "rapid-uncertain")

        self.assertIn(
            "Lecture difficile : vérifie attentivement les noms et montants.",
            receipt["warnings"],
        )

    def test_extract_receipt_falls_back_to_tesseract(self):
        with (
            patch(
                "bot.read_receipt_with_rapidocr",
                create=True,
                side_effect=RuntimeError("rapidocr unavailable"),
            ) as rapidocr,
            patch(
                "bot.read_receipt_text",
                return_value=(self.RAPID_RECEIPT_TEXT, "adaptive/psm6"),
            ) as tesseract,
        ):
            receipt = bot.extract_receipt("ticket.jpg", "fallback")

        rapidocr.assert_called_once_with("ticket.jpg")
        tesseract.assert_called_once_with("ticket.jpg")
        self.assertEqual(receipt["ocr_method"], "adaptive/psm6")


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


class RuntimeTests(unittest.TestCase):
    def test_render_environment_starts_a_secured_webhook(self):
        application = MagicMock()
        builder = MagicMock()
        builder.token.return_value.build.return_value = application

        with (
            patch("bot.ApplicationBuilder", return_value=builder),
            patch.dict(
                os.environ,
                {
                    "PORT": "10000",
                    "RENDER_EXTERNAL_HOSTNAME": "expenses-bot.onrender.com",
                    "WEBHOOK_PATH": "telegram",
                },
                clear=False,
            ),
        ):
            bot.main()

        kwargs = application.run_webhook.call_args.kwargs
        self.assertEqual(kwargs["listen"], "0.0.0.0")
        self.assertEqual(kwargs["port"], 10000)
        self.assertEqual(
            kwargs["webhook_url"],
            "https://expenses-bot.onrender.com/telegram",
        )
        self.assertEqual(len(kwargs["secret_token"]), 64)
        application.run_polling.assert_not_called()


if __name__ == "__main__":
    unittest.main()
