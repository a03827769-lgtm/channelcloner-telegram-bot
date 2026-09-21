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

    def test_affiliate_param_append_and_multi_params(self):
        rules = "amazon.com=tag=mytag-20&subid=999"
        raw_text = "See https://www.amazon.com/dp/B08N5WRWNW?psc=1 here."
        replaced = affiliate_replacer.replace_affiliate_links(raw_text, rules)
        self.assertIn("tag=mytag-20", replaced)
        self.assertIn("subid=999", replaced)
        self.assertIn("psc=1", replaced)
        self.assertTrue(replaced.endswith("here."))

if __name__ == "__main__":
    unittest.main()
