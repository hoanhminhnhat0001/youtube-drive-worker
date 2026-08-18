import unittest
from unittest.mock import patch

import worker


class WorkerTests(unittest.TestCase):
    def test_partition_is_stable_and_in_range(self):
        url = "https://youtu.be/example"
        self.assertEqual(worker.partition(url, 3), worker.partition(url, 3))
        self.assertIn(worker.partition(url, 3), range(3))

    def test_source_hash_is_stable(self):
        self.assertEqual(worker.source_hash("x"), worker.source_hash("x"))
        self.assertNotEqual(worker.source_hash("x"), worker.source_hash("y"))

    def test_error_redacts_url(self):
        url = "https://youtu.be/private-id"
        message = worker.safe_error(RuntimeError(f"failed for {url}"), url)
        self.assertNotIn(url, message)
        self.assertIn("<redacted-youtube-url>", message)

    def test_bot_challenge_error_is_actionable(self):
        message = worker.safe_error(RuntimeError("Sign in to confirm you're not a bot"), "https://youtu.be/x")
        self.assertTrue(message.startswith("YOUTUBE_YEU_CAU_DANG_NHAP:"))

    @patch.dict("os.environ", {"YOUTUBE_PROXY": "http://user:secret@proxy.example:8080"})
    def test_error_redacts_proxy_credentials(self):
        message = worker.safe_error(
            RuntimeError("failed via http://user:secret@proxy.example:8080"),
            "https://youtu.be/x",
        )
        self.assertNotIn("secret", message)
        self.assertIn("<redacted-proxy>", message)

    def test_same_folder_is_assigned_to_one_worker(self):
        key = "Vật Lý\u0001Thầy A\u0001Chương 1"
        self.assertEqual(worker.partition(key, 3), worker.partition(key, 3))

    def test_dedupe_marker_changes_with_folder(self):
        url = "https://youtu.be/example"
        self.assertNotEqual(worker.source_hash(f"{url}|folder-a"), worker.source_hash(f"{url}|folder-b"))

    def test_proxy_session_cells_are_stable_per_worker(self):
        self.assertEqual(worker.proxy_session_cell(0), "T2")
        self.assertEqual(worker.proxy_session_cell(2), "T2")


if __name__ == "__main__":
    unittest.main()
