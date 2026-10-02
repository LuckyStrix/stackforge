"""polymaker.fetch_html: retries, then fails with a message that says the cache is fine."""
import unittest
import urllib.error
from unittest import mock

from tdforge.tools import polymaker


class Resp:
    def __init__(self, body=b"<html>ok</html>"):
        self.body, self.headers = body, {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Fetch(unittest.TestCase):
    def test_retries_then_succeeds(self):
        calls = [urllib.error.URLError("down"), TimeoutError("slow"), Resp()]
        with mock.patch("urllib.request.urlopen", side_effect=calls) as u:
            html = polymaker.fetch_html(pause=0)
        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(u.call_count, 3)

    def test_gives_up_with_cache_message(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(SystemExit) as cm:
                polymaker.fetch_html(attempts=2, pause=0)
        self.assertIn("cached catalogue is unchanged", str(cm.exception))

    def test_refresh_leaves_cache_alone_when_offline(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                mock.patch.object(polymaker, "Catalog") as cat:
            with self.assertRaises(SystemExit):
                polymaker.refresh("x.json", verbose=False)
        cat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
