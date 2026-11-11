"""Twitch chat fidelity: IRC command routing, USERNOTICE, PRIVMSG meta, chat API.

Runs with the standard library runner:

    python -m unittest tests.test_chat_events -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

# Same test environment as tests/test_recorder_reliability.py. setdefault keeps
# whatever that module already set when both run in one discovery pass; heavy
# modules (config, webui, recorder) are only imported inside the tests.
_BASE = tempfile.mkdtemp(prefix="streamvault-chat-events-tests-")
for _key, _value in {
    "VOD_USER": "vaultadmin",
    "VOD_PASS": "VaultTestPass123!",
    "VOD_SECRET_KEY": "test-session-secret-key-0123456789",
    "STORAGE_ENCRYPTION_KEY": "test-storage-encryption-key-0123456789",
    "VOD_BASE_DIR": _BASE,
    "STREAMRECORDER_LOG_DIR": _BASE,
    "VOD_ALLOW_ENV_LOGIN": "1",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "1",
    "TWITCH_BADGE_AUTO_SYNC": "0",
}.items():
    os.environ.setdefault(_key, _value)

if os.name == "nt" and "fcntl" not in sys.modules:
    _fcntl = types.ModuleType("fcntl")
    _fcntl.LOCK_EX = 2
    _fcntl.LOCK_NB = 4
    _fcntl.LOCK_UN = 8
    _fcntl.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = _fcntl

from recorder import chat_events  # noqa: E402


class IrcCommandTests(unittest.TestCase):
    def test_usernotice_with_tags_and_prefix(self):
        line = "@badge-info=;msg-id=resub;login=alice :tmi.twitch.tv USERNOTICE #chan :hello"
        self.assertEqual(chat_events.irc_command(line), "USERNOTICE")

    def test_privmsg_text_mentioning_clearchat_stays_privmsg(self):
        line = "@display-name=Bob :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :CLEARCHAT #x lol"
        self.assertEqual(chat_events.irc_command(line), "PRIVMSG")

    def test_ping_without_prefix(self):
        self.assertEqual(chat_events.irc_command("PING :tmi.twitch.tv"), "PING")

    def test_clearmsg_and_empty_line(self):
        self.assertEqual(
            chat_events.irc_command("@login=x;target-msg-id=1 :tmi.twitch.tv CLEARMSG #chan :bad"),
            "CLEARMSG",
        )
        self.assertEqual(chat_events.irc_command(""), "")


class ParseUsernoticeTests(unittest.TestCase):
    def test_resub_with_message_and_streak(self):
        tags = {
            "msg-id": "resub",
            "display-name": "Alice",
            "login": "alice",
            "msg-param-sub-plan": "1000",
            "msg-param-cumulative-months": "14",
            "msg-param-should-share-streak": "1",
            "msg-param-streak-months": "6",
            "system-msg": "Alice subscribed at Tier 1. They've subscribed for 14 months!",
        }
        parsed = chat_events.parse_usernotice(tags, "still here Kappa")
        self.assertEqual(parsed["user"], "Alice")
        self.assertEqual(parsed["msg"], "still here Kappa")
        event = parsed["event"]
        self.assertEqual(event["type"], "notice")
        self.assertEqual(event["kind"], "resub")
        self.assertEqual(event["plan"], "1000")
        self.assertEqual(event["months"], 14)
        self.assertEqual(event["streak"], 6)

    def test_resub_streak_hidden_unless_shared(self):
        tags = {
            "msg-id": "resub",
            "login": "alice",
            "msg-param-cumulative-months": "3",
            "msg-param-should-share-streak": "0",
            "msg-param-streak-months": "3",
        }
        event = chat_events.parse_usernotice(tags, "")["event"]
        self.assertNotIn("streak", event)
        # display-name missing -> login
        self.assertEqual(chat_events.parse_usernotice(tags, "")["user"], "alice")

    def test_subgift_with_sender_count(self):
        tags = {
            "msg-id": "subgift",
            "display-name": "Gifter",
            "login": "gifter",
            "msg-param-sub-plan": "2000",
            "msg-param-recipient-display-name": "Lucky",
            "msg-param-gift-months": "1",
            "msg-param-months": "5",
            "msg-param-sender-count": "42",
        }
        parsed = chat_events.parse_usernotice(tags, "")
        event = parsed["event"]
        self.assertEqual(parsed["user"], "Gifter")
        self.assertEqual(parsed["msg"], "")
        self.assertEqual(event["kind"], "subgift")
        self.assertEqual(event["recipient"], "Lucky")
        self.assertEqual(event["sender_count"], 42)
        self.assertEqual(event["gift_months"], 1)
        self.assertEqual(event["months"], 5)
        self.assertNotIn("anon", event)

    def test_anonymous_submysterygift(self):
        tags = {
            "msg-id": "submysterygift",
            "display-name": "AnAnonymousGifter",
            "login": "ananonymousgifter",
            "msg-param-sub-plan": "1000",
            "msg-param-mass-gift-count": "10",
        }
        event = chat_events.parse_usernotice(tags, "")["event"]
        self.assertEqual(event["count"], 10)
        self.assertIs(event["anon"], True)
        anon_kind = chat_events.parse_usernotice(
            {"msg-id": "anonsubmysterygift", "msg-param-mass-gift-count": "5"}, ""
        )["event"]
        self.assertIs(anon_kind["anon"], True)

    def test_anongiftpaidupgrade_is_not_marked_anon(self):
        event = chat_events.parse_usernotice(
            {"msg-id": "anongiftpaidupgrade", "display-name": "Kept", "login": "kept"}, ""
        )["event"]
        self.assertNotIn("anon", event)

    def test_raid_uses_raider_display_name_and_viewer_count(self):
        tags = {
            "msg-id": "raid",
            "display-name": "RaiderLogin",
            "login": "raider",
            "msg-param-displayName": "Raider",
            "msg-param-viewerCount": "1234",
        }
        parsed = chat_events.parse_usernotice(tags, "")
        self.assertEqual(parsed["user"], "Raider")
        self.assertEqual(parsed["event"]["kind"], "raid")
        self.assertEqual(parsed["event"]["count"], 1234)

    def test_announcement_with_color(self):
        parsed = chat_events.parse_usernotice(
            {"msg-id": "announcement", "display-name": "Mod", "msg-param-color": "blue"},
            "Stream starts soon!",
        )
        self.assertEqual(parsed["event"]["color"], "BLUE")
        self.assertEqual(parsed["msg"], "Stream starts soon!")

    def test_shared_chat_notice_uses_source_msg_id(self):
        parsed = chat_events.parse_usernotice(
            {"msg-id": "sharedchatnotice", "source-msg-id": "sub", "display-name": "X"}, ""
        )
        self.assertEqual(parsed["event"]["kind"], "sub")

    def test_unknown_kind_keeps_system_msg_capped(self):
        system = "Something new happened " * 30
        parsed = chat_events.parse_usernotice(
            {"msg-id": "brandnewthing", "display-name": "X", "system-msg": system}, ""
        )
        event = parsed["event"]
        self.assertEqual(event["kind"], "brandnewthing")
        self.assertTrue(event["system"].startswith("Something new happened"))
        self.assertLessEqual(len(event["system"]), 300)

    def test_invalid_kind_returns_none(self):
        self.assertIsNone(chat_events.parse_usernotice({"msg-id": "<script>"}, ""))
        self.assertIsNone(chat_events.parse_usernotice({"msg-id": ""}, "hi"))
        self.assertIsNone(chat_events.parse_usernotice({"msg-id": "x" * 41}, ""))

    def test_charity_amount_and_payforward_anonymous(self):
        charity = chat_events.parse_usernotice(
            {
                "msg-id": "charitydonation",
                "display-name": "Giver",
                "msg-param-charity-name": "Good Cause",
                "msg-param-donation-amount": "5000",
                "msg-param-exponent": "2",
                "msg-param-donation-currency": "USD",
            },
            "",
        )["event"]
        self.assertEqual(charity["amount"], "50.00 USD")
        self.assertEqual(charity["charity"], "Good Cause")
        payforward = chat_events.parse_usernotice(
            {
                "msg-id": "standardpayforward",
                "display-name": "Payer",
                "msg-param-prior-gifter-anonymous": "true",
                "msg-param-prior-gifter-display-name": "Hidden",
                "msg-param-recipient-display-name": "Next",
            },
            "",
        )["event"]
        self.assertNotIn("other", payforward)
        self.assertEqual(payforward["recipient"], "Next")

    def test_process_twitch_message_handles_usernotice(self):
        from recorder import Streamrecorder as recorder

        line = (
            "@badges=subscriber/12;color=#FF0000;display-name=Alice;emotes=25:0-4;"
            "login=alice;msg-id=resub;msg-param-cumulative-months=12 "
            ":tmi.twitch.tv USERNOTICE #chan :Kappa hi"
        )
        user, message, emotes, badges, color, tags = recorder.process_twitch_message(line)
        self.assertEqual(user, "Alice")
        self.assertEqual(message, "Kappa hi")
        self.assertEqual(emotes, [{"id": "25", "start": 0, "end": 4}])
        self.assertEqual(color, "#FF0000")
        self.assertEqual(badges[0]["set_id"], "subscriber")
        self.assertEqual(chat_events.parse_usernotice(tags, message)["event"]["months"], 12)


class SanitizeEventTests(unittest.TestCase):
    def test_drops_invalid_plan_color_negative_numbers_and_foreign_fields(self):
        clean = chat_events.sanitize_event({
            "type": "notice",
            "kind": "resub",
            "plan": "9999",
            "color": "HOTPINK",
            "months": -4,
            "streak": "abc",
            "count": 0,
            "recipient": "  Multi   space\nname  ",
            "other": "x" * 200,
            "anon": "yes",
            "html": "<img src=x onerror=alert(1)>",
            "user": "spoofed",
        })
        self.assertEqual(clean, {
            "type": "notice",
            "kind": "resub",
            "recipient": "Multi space name",
            "other": "x" * 80,
        })

    def test_caps_large_numbers_and_rejects_bad_kind(self):
        clean = chat_events.sanitize_event({"type": "notice", "kind": "raid", "count": 10**12})
        self.assertEqual(clean["count"], 100_000_000)
        self.assertIsNone(chat_events.sanitize_event({"type": "notice", "kind": "Bad Kind"}))

    def test_moderation_events_unchanged(self):
        self.assertEqual(
            chat_events.sanitize_event({"type": "timeout", "seconds": 600, "kind": "x"}),
            {"type": "timeout", "seconds": 600},
        )


class PrivmsgMetaTests(unittest.TestCase):
    def test_bits_first_highlight_reply(self):
        meta = chat_events.parse_privmsg_meta({
            "bits": "150",
            "first-msg": "1",
            "msg-id": "highlighted-message",
            "reply-parent-display-name": "Parent",
            "reply-parent-msg-body": "original " * 40,
        })
        self.assertEqual(meta["bits"], 150)
        self.assertIs(meta["first"], True)
        self.assertIs(meta["highlight"], True)
        self.assertEqual(meta["reply"]["user"], "Parent")
        self.assertLessEqual(len(meta["reply"]["msg"]), 140)

    def test_power_up_message(self):
        self.assertEqual(
            chat_events.parse_privmsg_meta({"msg-id": "gigantified-emote-message"}),
            {"power": "gigantified"},
        )

    def test_plain_message_returns_none(self):
        self.assertIsNone(chat_events.parse_privmsg_meta({"first-msg": "0", "bits": "0"}))
        self.assertIsNone(chat_events.parse_privmsg_meta({}))

    def test_sanitize_meta_allowlist(self):
        self.assertEqual(
            chat_events.sanitize_meta({
                "bits": -5,
                "first": "true",
                "power": "explode",
                "reply": {"user": "", "msg": ""},
                "evil": 1,
            }),
            None,
        )


class TtsBotRoutingTests(unittest.TestCase):
    def test_usernotice_mentioning_privmsg_is_not_a_chat_message(self):
        from core import tts_bot

        line = "@msg-id=announcement :tmi.twitch.tv USERNOTICE #chan :x PRIVMSG #chan :!tts hi"
        self.assertIsNone(tts_bot.parse_privmsg(line))
        parsed = tts_bot.parse_privmsg("@display-name=Bob :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :!tts hi")
        self.assertEqual(parsed[:3], ("chan", "Bob", "!tts hi"))


class ChatApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from WEBUI import webui

        cls.webui = webui
        webui.app.config.update(TESTING=True)

    def setUp(self):
        from core import config

        self.client = self.webui.app.test_client()
        with self.client.session_transaction() as sess:
            now = time.time()
            sess.update(logged_in=True, user_role="admin", username=config.VOD_USER,
                        login_time=now, last_active=now, _csrf_token="t")
        self._tmp = tempfile.TemporaryDirectory(prefix="streamvault-chat-api-")
        self.addCleanup(self._tmp.cleanup)

    def _write_rows(self, name, rows):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def _gift_rows(self):
        base = {"t": 1000.0, "user": "Gifter", "msg": "", "color": "#00FF00"}
        return [
            {**base, "rel": 10.0, "event": {
                "type": "notice", "kind": "subgift", "plan": "1000",
                "recipient": "LuckyOne", "sender_count": 3,
                "system": "Gifter gifted a Tier 1 sub to LuckyOne!",
            }},
            {**base, "rel": 10.2, "event": {
                "type": "notice", "kind": "subgift", "plan": "1000",
                "recipient": "LuckyTwo", "sender_count": 4,
                "system": "Gifter gifted a Tier 1 sub to LuckyTwo!",
            }},
            {"t": 1001.0, "rel": 11.0, "user": "Cheerer", "msg": "Cheer100 nice",
             "meta": {"bits": 100, "first": True, "junk": "x"}},
        ]

    def test_vod_range_keeps_gift_subs_apart_and_hides_system(self):
        path = self._write_rows("gifts.jsonl", self._gift_rows())
        with mock.patch.object(self.webui, "ensure_vod_chat_path", return_value=path), \
                mock.patch.object(self.webui, "get_chat_index", return_value=None):
            response = self.client.get("/chat/vod/streamer/vod.mp4?start=0&end=100")
        self.assertEqual(response.status_code, 200)
        items = response.get_json()
        notices = [item for item in items if item.get("event", {}).get("type") == "notice"]
        self.assertEqual(len(notices), 2)
        self.assertIn("LuckyOne", notices[0]["event"]["text"])
        self.assertIn("LuckyTwo", notices[1]["event"]["text"])
        self.assertIn("Tier 1", notices[0]["event"]["text"])
        for notice in notices:
            self.assertNotIn("system", notice["event"])
            self.assertEqual(notice["msg"], "")
        cheer = [item for item in items if item.get("user") == "Cheerer"][0]
        self.assertEqual(cheer["meta"], {"bits": 100, "first": True})

    def test_vod_chunk_is_sanitized(self):
        chunk = os.path.join(self._tmp.name, "chunk_0.json")
        with open(chunk, "w", encoding="utf-8") as handle:
            json.dump(self._gift_rows(), handle)
        log_path = self._write_rows("log.jsonl", self._gift_rows())
        from core import chat_chunker

        with mock.patch.object(self.webui, "ensure_vod_chat_path", return_value=log_path), \
                mock.patch.object(chat_chunker, "get_chunk_file_path", return_value=chunk):
            response = self.client.get("/chat/vod/streamer/vod.mp4?chunk=0")
        self.assertEqual(response.status_code, 200)
        items = response.get_json()
        notices = [item for item in items if item.get("event", {}).get("type") == "notice"]
        self.assertEqual(len(notices), 2)
        self.assertNotIn("system", json.dumps(items))
        self.assertIn("LuckyTwo", notices[1]["event"]["text"])

    def test_unknown_notice_falls_back_to_system_text(self):
        parsed = self.webui.chat_item_from_row(
            {"rel": 1.0, "user": "X", "msg": "", "event": {
                "type": "notice", "kind": "brandnewthing", "system": "X did a new thing",
            }},
            time_key="rel",
        )
        item = parsed[0]
        self.assertEqual(item["event"]["text"], "X did a new thing")
        self.assertNotIn("system", item["event"])

    def test_raid_text(self):
        with self.webui.app.test_request_context("/"):
            text = self.webui.format_notice_text(
                {"type": "notice", "kind": "raid", "count": 25}, "Raider"
            )
        self.assertEqual(text, "Raider is raiding with 25 viewers!")


if __name__ == "__main__":
    unittest.main()
