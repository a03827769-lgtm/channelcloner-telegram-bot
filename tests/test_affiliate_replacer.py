import unittest
from services.affiliate_replacer import affiliate_replacer

class TestAffiliateReplacer(unittest.TestCase):
    def test_affiliate_rule_replacement(self):
        rules = """
        aliexpress.com=https://s.click.aliexpress.com/e/_Dk1234
        uzum.uz=https://uzum.uz/?ref=my_custom_aff_id
        """
        raw_text = "Check out this great deal on https://aliexpress.com/item/1005001.html and also https://uzum.uz/product/123 !"
        replaced = affiliate_replacer.replace_affiliate_links(raw_text, rules)

        self.assertIn("https://s.click.aliexpress.com/e/_Dk1234", replaced)
        self.assertIn("https://uzum.uz/?ref=my_custom_aff_id", replaced)
        self.assertNotIn("aliexpress.com/item/1005001.html", replaced)

if __name__ == "__main__":
    unittest.main()
