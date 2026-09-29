import unittest
from unittest.mock import patch
from services.story_renderer import StoryCardRenderer

class TestPlaywrightToggle(unittest.TestCase):
    def setUp(self):
        self.renderer = StoryCardRenderer()

    @patch("services.story_renderer.settings")
    @patch.object(StoryCardRenderer, "render_story_composite_playwright")
    @patch.object(StoryCardRenderer, "render_story_composite_pil")
    def test_render_story_composite_skips_playwright_when_disabled(self, mock_pil, mock_playwright, mock_settings):
        mock_settings.ENABLE_PLAYWRIGHT = False
        mock_pil.return_value = "/tmp/story_output.png"

        result = self.renderer.render_story_composite(
            bg_base_path="dummy_bg.jpg",
            channel_title="Test Channel",
            photo_paths=[],
            caption="Test Caption"
        )

        self.assertEqual(result, "/tmp/story_output.png")
        mock_playwright.assert_not_called()
        mock_pil.assert_called_once()

    @patch("services.story_renderer.settings")
    @patch.object(StoryCardRenderer, "render_story_composite_playwright")
    @patch.object(StoryCardRenderer, "render_story_composite_pil")
    def test_render_card_overlay_skips_playwright_when_disabled(self, mock_pil, mock_playwright, mock_settings):
        mock_settings.ENABLE_PLAYWRIGHT = False
        mock_pil.return_value = "/tmp/overlay_output.png"

        result = self.renderer.render_card_overlay_png(
            channel_title="Test Channel",
            photo_paths=[],
            caption="Test Caption"
        )

        self.assertEqual(result, "/tmp/overlay_output.png")
        mock_playwright.assert_not_called()
        mock_pil.assert_called_once()

    @patch("services.story_renderer.settings")
    @patch.object(StoryCardRenderer, "render_story_composite_playwright")
    @patch.object(StoryCardRenderer, "render_story_composite_pil")
    def test_render_story_composite_calls_playwright_when_enabled(self, mock_pil, mock_playwright, mock_settings):
        mock_settings.ENABLE_PLAYWRIGHT = True
        mock_playwright.return_value = "/tmp/playwright_output.png"

        result = self.renderer.render_story_composite(
            bg_base_path="dummy_bg.jpg",
            channel_title="Test Channel",
            photo_paths=[],
            caption="Test Caption"
        )

        self.assertEqual(result, "/tmp/playwright_output.png")
        mock_playwright.assert_called_once()
        mock_pil.assert_not_called()

if __name__ == "__main__":
    unittest.main()
