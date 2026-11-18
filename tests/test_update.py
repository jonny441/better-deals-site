import unittest

from scripts.update import parse_deal, parse_size


class DealParsingTests(unittest.TestCase):
    def test_multibuy_coupon_and_reward_are_per_item(self):
        item = {
            "current_price": 12,
            "pre_price_text": "2/",
            "sale_story": (
                "Also get savings with $5.00 on 2 Digital mfr coupon + "
                "Buy 2 get $6 ExtraBucks Rewards"
            ),
        }
        self.assertEqual(parse_deal(item)[:3], (6.0, 2.5, 3.0))

    def test_bogo_is_half_price_per_item(self):
        item = {"current_price": 8, "sale_story": "Buy one, get one free"}
        self.assertEqual(parse_deal(item)[:3], (8.0, 0.0, 4.0))

    def test_spend_reward_is_spread_over_required_items(self):
        item = {"current_price": 5, "sale_story": "Spend $20, get $10 ExtraBucks Rewards"}
        self.assertEqual(parse_deal(item)[:3], (5.0, 0.0, 2.5))


class SizeParsingTests(unittest.TestCase):
    def test_volume_nearest_item_keyword_wins(self):
        name = "Colgate 5.1 oz toothpaste or mouthwash 473-500mL"
        self.assertEqual(parse_size(name, "fl_oz", ["mouthwash"]), 15.99)

    def test_lower_end_of_range_is_conservative(self):
        self.assertEqual(parse_size("body wash 16-18 oz", "fl_oz", ["body wash"]), 16.0)

    def test_hyphenated_roll_count(self):
        self.assertEqual(parse_size("12-Mega-Rolls toilet paper", "roll", ["toilet paper"]), 12.0)

    def test_multiword_roll_description(self):
        self.assertEqual(parse_size("paper towels 6 Single Plus Rolls", "roll", ["paper towels"]), 6.0)


if __name__ == "__main__":
    unittest.main()
