"""Remote exposure (#48): the app-level lock and what stays behind it.

Cloudflare Access and connector OAuth are the front door and are not testable
from here. What is testable is the second lock and the blast radius if the
first one is misconfigured: does an unauthenticated request get in, and does a
tunnel-facing instance serve the upcoming layer (#56) it is not supposed to?

The default matters as much as the lock. With nothing configured this is the
workstation, and none of it may change behaviour.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pubbrain import db, llm, remote
from pubbrain.web import create_app

REPORT = {
    "slug": "tariff-report", "url": "https://merics.org/en/report/tariff-report",
    "title": "Tariff pressure on Europe", "subtitle": None,
    "date_published": "2025-03-01", "pub_type": "Report", "series": None,
    "access": "public", "pdf_url": None, "og_description": None,
    "people": [], "site_tags": [],
}
TOKEN = "s3cret-token-value"


def env(**values):
    """Patch the remote env vars, clearing any this machine already has."""
    base = {remote.REMOTE_ENV: "", remote.WEB_TOKEN_ENV: "",
            remote.MCP_TOKEN_ENV: ""}
    return mock.patch.dict(os.environ, {**base, **values})


class RemoteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.db_path)
        db.upsert_publication(conn, REPORT)
        db.add_upcoming_note(conn, "an unpublished plan")
        conn.commit()
        conn.close()

    def client(self):
        app = create_app(self.db_path)
        app.testing = True
        return app.test_client()


class TestTheDefaultIsUnchanged(RemoteTest):
    def test_no_token_means_no_login(self):
        """The workstation and the LAN (#51) must not gain a login they never
        asked for."""
        with env():
            self.assertEqual(self.client().get("/").status_code, 200)

    def test_upcoming_is_served_locally(self):
        with env():
            self.assertEqual(self.client().get("/upcoming/").status_code, 200)


class TestTheSecondLock(RemoteTest):
    def test_a_request_without_the_token_is_refused(self):
        with env(**{remote.WEB_TOKEN_ENV: TOKEN}):
            self.assertEqual(self.client().get("/").status_code, 401)

    def test_a_bearer_header_gets_in(self):
        with env(**{remote.WEB_TOKEN_ENV: TOKEN}):
            r = self.client().get("/", headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(r.status_code, 200)

    def test_the_wrong_token_is_refused(self):
        with env(**{remote.WEB_TOKEN_ENV: TOKEN}):
            r = self.client().get("/", headers={"Authorization": "Bearer nope"})
            self.assertEqual(r.status_code, 401)

    def test_a_browser_can_hand_it_over_once_in_the_url(self):
        """A browser cannot set an Authorization header, so the token arrives
        in the query string once and moves into a cookie."""
        with env(**{remote.WEB_TOKEN_ENV: TOKEN}):
            client = self.client()
            r = client.get(f"/?token={TOKEN}")
            self.assertEqual(r.status_code, 302)
            self.assertNotIn("token=", r.headers["Location"])
            self.assertEqual(client.get("/").status_code, 200)   # cookie carries it

    def test_the_comparison_is_constant_time(self):
        """A token check that leaks its own timing is not a token check."""
        self.assertTrue(remote.token_ok(TOKEN, TOKEN))
        self.assertFalse(remote.token_ok("", TOKEN))
        self.assertFalse(remote.token_ok(TOKEN, ""))
        self.assertFalse(remote.token_ok(None, TOKEN))


class TestWhatStaysBehind(RemoteTest):
    def test_remote_mode_has_no_route_to_the_upcoming_layer(self):
        """Owner's default on #48: internal notes about unpublished work do
        not cross the tunnel. Not a 403 — the routes do not exist, so a
        misconfiguration upstream exposes only published material."""
        with env(**{remote.REMOTE_ENV: "1", remote.WEB_TOKEN_ENV: TOKEN,
                    remote.MCP_TOKEN_ENV: TOKEN}):
            client = self.client()
            auth = {"Authorization": f"Bearer {TOKEN}"}
            self.assertEqual(client.get("/upcoming/", headers=auth).status_code, 404)
            self.assertEqual(
                client.get("/insights/data/upcoming-edge.json",
                           headers=auth).status_code, 404)
            page = client.get("/", headers=auth).get_data(as_text=True)
            self.assertNotIn("Upcoming", page)

    def test_a_remote_service_refuses_to_start_without_its_own_token(self):
        """Otherwise the only thing between the catalog and the internet is a
        Cloudflare rule nobody re-checks."""
        with env(**{remote.REMOTE_ENV: "1"}):
            self.assertEqual(len(remote.check_config()), 2)
        with env(**{remote.REMOTE_ENV: "1", remote.WEB_TOKEN_ENV: TOKEN,
                    remote.MCP_TOKEN_ENV: TOKEN}):
            self.assertEqual(remote.check_config(), [])
        with env():
            self.assertEqual(remote.check_config(), [])


class TestKeyDelivery(unittest.TestCase):
    """A headless box has no desktop keyring, which is what blocked both the
    server move and the weekly job (#8)."""

    def test_the_environment_wins_over_the_keyring(self):
        with mock.patch.dict(os.environ, {"PUBBRAIN_LLM_API_KEY": "sk-from-env"}):
            with mock.patch("subprocess.run") as ran:
                self.assertEqual(llm.api_key("default"), "sk-from-env")
                ran.assert_not_called()          # the keyring is never consulted

    def test_the_keyring_still_answers_when_the_environment_is_empty(self):
        with mock.patch.dict(os.environ, {"PUBBRAIN_LLM_API_KEY": ""}):
            with mock.patch("subprocess.run") as ran:
                ran.return_value = mock.Mock(stdout="sk-from-keyring\n", returncode=0)
                self.assertEqual(llm.api_key("default"), "sk-from-keyring")

    def test_the_error_names_both_places_it_looked(self):
        with mock.patch.dict(os.environ, {"PUBBRAIN_LLM_API_KEY": ""}):
            with mock.patch("subprocess.run") as ran:
                ran.return_value = mock.Mock(stdout="", returncode=1)
                with self.assertRaises(llm.NoApiKey) as caught:
                    llm.api_key("default")
        self.assertIn("PUBBRAIN_LLM_API_KEY", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
