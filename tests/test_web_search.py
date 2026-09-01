import json
import unittest
from unittest.mock import patch

from src.tools.web_search import WebSearchTool


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(
            {"results": [{"title": "Python", "url": "https://python.org", "content": "Official site"}]}
        ).encode()


class WebSearchToolTests(unittest.TestCase):
    def test_requires_api_key(self):
        response = WebSearchTool(None).run("Python")
        self.assertIn("Tavily API Key", response["error"])

    @patch("src.tools.web_search.urlopen", return_value=_Response())
    def test_maps_tavily_results(self, _urlopen):
        response = WebSearchTool("test-key", result_limit=1).run("Python")
        self.assertEqual(response["results"][0]["url"], "https://python.org")
        self.assertEqual(response["results"][0]["snippet"], "Official site")


if __name__ == "__main__":
    unittest.main()
