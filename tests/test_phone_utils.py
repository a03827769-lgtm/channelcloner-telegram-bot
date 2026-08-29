import unittest
from services.phone_utils import normalize_phone_number

class TestPhoneUtils(unittest.TestCase):
    def test_uzbek_9_digits(self):
        valid, e164, disp = normalize_phone_number("955494960")
        self.assertTrue(valid)
        self.assertEqual(e164, "+998955494960")

    def test_uzbek_with_spaces_and_brackets(self):
        valid, e164, disp = normalize_phone_number("(90) 123-45-67")
        self.assertTrue(valid)
        self.assertEqual(e164, "+998901234567")

    def test_uzbek_12_digits_no_plus(self):
        valid, e164, disp = normalize_phone_number("998955494960")
        self.assertTrue(valid)
        self.assertEqual(e164, "+998955494960")

    def test_uzbek_with_plus(self):
        valid, e164, disp = normalize_phone_number("+998 95 549 49 60")
        self.assertTrue(valid)
        self.assertEqual(e164, "+998955494960")

    def test_russian_starts_with_8(self):
        valid, e164, disp = normalize_phone_number("8 (926) 123-45-67")
        self.assertTrue(valid)
        self.assertEqual(e164, "+79261234567")

    def test_russian_starts_with_7(self):
        valid, e164, disp = normalize_phone_number("79261234567")
        self.assertTrue(valid)
        self.assertEqual(e164, "+79261234567")

    def test_us_phone(self):
        valid, e164, disp = normalize_phone_number("+1 (415) 555-2671")
        self.assertTrue(valid)
        self.assertEqual(e164, "+14155552671")

    def test_mask_phone_number(self):
        from services.phone_utils import mask_phone_number
        masked = mask_phone_number("+998901234567")
        self.assertEqual(masked, "+99890****67")
        self.assertEqual(mask_phone_number(""), "Mavjud emas")

if __name__ == "__main__":
    unittest.main()
