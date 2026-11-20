"""initData, sessions, PIN and identifier tests."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from rpc_service import auth, db
from tests import make_cfg, sign_init_data

USER = {"id": 555000111, "first_name": "Nova", "username": "nova"}


class InitDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_accepted(self):
        data = auth.verify_init_data(sign_init_data(USER), self.cfg.tg_bot_token, 3600)
        self.assertEqual(data["user"]["id"], USER["id"])

    def test_tampered_rejected(self):
        with self.assertRaises(auth.AuthError) as ctx:
            auth.verify_init_data(sign_init_data(USER, tamper=True),
                                  self.cfg.tg_bot_token, 3600)
        self.assertEqual(ctx.exception.code, "init_data_bad_signature")

    def test_wrong_token_rejected(self):
        with self.assertRaises(auth.AuthError):
            auth.verify_init_data(sign_init_data(USER), "999:OTHER", 3600)

    def test_missing_hash_rejected(self):
        with self.assertRaises(auth.AuthError) as ctx:
            auth.verify_init_data("auth_date=1&user=%7B%7D", self.cfg.tg_bot_token, 3600)
        self.assertEqual(ctx.exception.code, "init_data_unsigned")

    def test_expired_rejected(self):
        old = int(time.time()) - 7200
        with self.assertRaises(auth.AuthError) as ctx:
            auth.verify_init_data(sign_init_data(USER, auth_date=old),
                                  self.cfg.tg_bot_token, 3600)
        self.assertEqual(ctx.exception.code, "init_data_expired")

    def test_duplicate_field_rejected(self):
        with self.assertRaises(auth.AuthError) as ctx:
            auth.verify_init_data(sign_init_data(USER) + "&user=%7B%7D",
                                  self.cfg.tg_bot_token, 3600)
        self.assertEqual(ctx.exception.code, "init_data_duplicate")

    def test_bot_user_rejected(self):
        with self.assertRaises(auth.AuthError) as ctx:
            auth.verify_init_data(sign_init_data({"id": 7, "is_bot": True}),
                                  self.cfg.tg_bot_token, 3600)
        self.assertEqual(ctx.exception.code, "init_data_bot_user")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        db.init(self.cfg.db_path)
        self.account = auth.create_account(self.cfg.db_path, "web", auth_secret="s" * 20)

    def tearDown(self):
        self.tmp.cleanup()

    def test_project_id_is_not_the_token(self):
        token = auth.issue_session(self.cfg.db_path, self.account["id"], 60)["session_token"]
        self.assertNotEqual(token, self.account["project_id"])
        self.assertTrue(self.account["project_id"].startswith("SH-"))

    def test_resolve_and_revoke(self):
        token = auth.issue_session(self.cfg.db_path, self.account["id"], 60)["session_token"]
        self.assertEqual(auth.resolve_session(self.cfg.db_path, token).account_id,
                         self.account["id"])
        auth.revoke_session(self.cfg.db_path, token)
        with self.assertRaises(auth.AuthError) as ctx:
            auth.resolve_session(self.cfg.db_path, token)
        self.assertEqual(ctx.exception.code, "session_revoked")

    def test_expired_rejected(self):
        token = auth.issue_session(self.cfg.db_path, self.account["id"], -1)["session_token"]
        with self.assertRaises(auth.AuthError) as ctx:
            auth.resolve_session(self.cfg.db_path, token)
        self.assertEqual(ctx.exception.code, "session_expired")

    def test_stored_hashed(self):
        token = auth.issue_session(self.cfg.db_path, self.account["id"], 60)["session_token"]
        row = db.fetch_one(self.cfg.db_path, "SELECT token_hash FROM sessions")
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(row["token_hash"], auth.token_digest(token))

    def test_bearer_parser(self):
        self.assertEqual(auth.bearer("Bearer abc"), "abc")
        self.assertEqual(auth.bearer("Basic abc"), "")
        self.assertEqual(auth.bearer(None), "")

    def test_telegram_id_format(self):
        acc = auth.create_account(self.cfg.db_path, "telegram", tg_user_id="42")
        self.assertEqual(acc["project_id"], "SH-42")


class PinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name))
        db.init(self.cfg.db_path)
        self.account = auth.create_account(self.cfg.db_path, "web", auth_secret="s" * 20)

    def tearDown(self):
        self.tmp.cleanup()

    def test_must_be_six_digits(self):
        with self.assertRaises(auth.AuthError):
            auth.set_pin(self.cfg.db_path, self.account["id"], "12ab56")

    def test_wrong_then_locked(self):
        auth.set_pin(self.cfg.db_path, self.account["id"], "123456")
        with self.assertRaises(auth.AuthError) as first:
            auth.check_pin(self.cfg.db_path, self.account["id"], "000000", 2, 5)
        self.assertEqual(first.exception.code, "pin_wrong")
        with self.assertRaises(auth.AuthError) as second:
            auth.check_pin(self.cfg.db_path, self.account["id"], "000000", 2, 5)
        self.assertEqual(second.exception.code, "pin_locked")

    def test_change_needs_current(self):
        auth.set_pin(self.cfg.db_path, self.account["id"], "123456")
        with self.assertRaises(auth.AuthError):
            auth.set_pin(self.cfg.db_path, self.account["id"], "654321")
        auth.set_pin(self.cfg.db_path, self.account["id"], "654321", "123456")


if __name__ == "__main__":
    unittest.main()