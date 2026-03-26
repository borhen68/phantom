import os
import tempfile
import unittest
from unittest import mock
import hmac
import hashlib

from core.settings import scope_id
from integrations.messaging import (
    InboundMessage,
    MessagingService,
    parse_telegram_update,
    parse_whatsapp_payload,
    validate_telegram_secret,
    verify_whatsapp_signature,
    verify_whatsapp_handshake,
)


class MessagingTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.addCleanup(self.workspace.cleanup)
        self.env_patch = mock.patch.dict(os.environ, {
            "PHANTOM_HOME": self.home.name,
            "PHANTOM_WORKSPACE": self.workspace.name,
            "PHANTOM_SCOPE": "tests::messaging",
        }, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_parse_telegram_update_extracts_text_message(self):
        message = parse_telegram_update({
            "update_id": 10,
            "message": {
                "message_id": 25,
                "text": "ship the release",
                "chat": {"id": 12345},
                "from": {"id": 99, "first_name": "Ada"},
            },
        })

        self.assertIsNotNone(message)
        self.assertEqual(message.platform, "telegram")
        self.assertEqual(message.conversation_id, "12345")
        self.assertEqual(message.sender_id, "99")
        self.assertEqual(message.text, "ship the release")

    def test_parse_telegram_update_accepts_image_only_message(self):
        message = parse_telegram_update({
            "update_id": 10,
            "message": {
                "message_id": 26,
                "photo": [{"file_id": "abc"}],
                "chat": {"id": 12345},
                "from": {"id": 99, "first_name": "Ada"},
            },
        })

        self.assertIsNotNone(message)
        self.assertEqual(message.text, "")

    def test_parse_whatsapp_payload_extracts_text_message(self):
        messages = parse_whatsapp_payload({
            "entry": [{
                "changes": [{
                    "value": {
                        "contacts": [{"wa_id": "21612345678", "profile": {"name": "Meriem"}}],
                        "messages": [{
                            "from": "21612345678",
                            "id": "wamid.HBgM123",
                            "type": "text",
                            "text": {"body": "audit the repo"},
                        }],
                    },
                }],
            }],
        })

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].platform, "whatsapp")
        self.assertEqual(messages[0].conversation_id, "21612345678")
        self.assertEqual(messages[0].sender_name, "Meriem")

    def test_parse_whatsapp_payload_accepts_image_without_caption(self):
        messages = parse_whatsapp_payload({
            "entry": [{
                "changes": [{
                    "value": {
                        "contacts": [{"wa_id": "21612345678", "profile": {"name": "Meriem"}}],
                        "messages": [{
                            "from": "21612345678",
                            "id": "wamid.HBgM124",
                            "type": "image",
                            "image": {"id": "media-1"},
                        }],
                    },
                }],
            }],
        })

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "")

    def test_process_message_runs_in_conversation_scope(self):
        seen = []
        replies = []

        def fake_run_goal(**kwargs):
            seen.append((scope_id(), kwargs["goal"], kwargs["parallel"]))
            return {"summary": "done", "outcome": "success"}

        service = MessagingService(
            run_goal=fake_run_goal,
            telegram_sender=lambda conversation_id, text: replies.append((conversation_id, text)),
            whatsapp_sender=lambda conversation_id, text: replies.append((conversation_id, text)),
            max_workers=1,
        )

        service.process_message(InboundMessage(
            platform="telegram",
            message_id="1",
            conversation_id="chat-42",
            sender_id="user-7",
            sender_name="Ada",
            text="build release notes",
        ))

        self.assertEqual(seen, [("messaging::telegram::chat_42", "build release notes", True)])
        self.assertEqual(replies, [("chat-42", "done")])

    def test_process_message_prompts_for_task_when_text_missing(self):
        replies = []
        service = MessagingService(
            run_goal=lambda **kwargs: {"summary": "should not run", "outcome": "success"},
            telegram_sender=lambda conversation_id, text: replies.append((conversation_id, text)),
            max_workers=1,
        )

        service.process_message(InboundMessage(
            platform="telegram",
            message_id="2",
            conversation_id="chat-42",
            sender_id="user-7",
            sender_name="Ada",
            text="",
        ))

        self.assertEqual(len(replies), 1)
        self.assertIn("What do you want PHANTOM to do?", replies[0][1])

    def test_process_message_prompts_for_task_on_greeting(self):
        replies = []
        seen = []

        def fake_run_goal(**kwargs):
            seen.append(kwargs["goal"])
            return {"summary": "done", "outcome": "success"}

        service = MessagingService(
            run_goal=fake_run_goal,
            telegram_sender=lambda conversation_id, text: replies.append((conversation_id, text)),
            max_workers=1,
        )

        service.process_message(InboundMessage(
            platform="telegram",
            message_id="3",
            conversation_id="chat-42",
            sender_id="user-7",
            sender_name="Ada",
            text="hello",
        ))

        self.assertEqual(seen, [])
        self.assertEqual(len(replies), 1)
        self.assertIn("What do you want PHANTOM to do?", replies[0][1])

    def test_submit_dedupes_duplicate_messages(self):
        seen = []
        replies = []

        def fake_run_goal(**kwargs):
            seen.append(kwargs["goal"])
            return {"summary": "ok", "outcome": "success"}

        service = MessagingService(
            run_goal=fake_run_goal,
            telegram_sender=lambda conversation_id, text: replies.append((conversation_id, text)),
            max_workers=1,
        )
        message = InboundMessage(
            platform="telegram",
            message_id="same-id",
            conversation_id="c1",
            sender_id="u1",
            sender_name="Ada",
            text="audit repo",
        )

        self.assertTrue(service.submit(message))
        self.assertFalse(service.submit(message))
        service.shutdown(wait=True)

        self.assertEqual(seen, ["audit repo"])
        self.assertEqual(replies, [("c1", "ok")])

    def test_submit_dedupes_duplicate_messages_across_service_restart(self):
        seen = []
        replies = []

        def fake_run_goal(**kwargs):
            seen.append(kwargs["goal"])
            return {"summary": "ok", "outcome": "success"}

        message = InboundMessage(
            platform="telegram",
            message_id="same-id",
            conversation_id="c1",
            sender_id="u1",
            sender_name="Ada",
            text="audit repo",
        )

        service = MessagingService(
            run_goal=fake_run_goal,
            telegram_sender=lambda conversation_id, text: replies.append((conversation_id, text)),
            max_workers=1,
        )
        self.assertTrue(service.submit(message))
        service.shutdown(wait=True)

        restarted = MessagingService(
            run_goal=fake_run_goal,
            telegram_sender=lambda conversation_id, text: replies.append((conversation_id, text)),
            max_workers=1,
        )
        self.assertFalse(restarted.submit(message))
        restarted.shutdown(wait=True)

        self.assertEqual(seen, ["audit repo"])
        self.assertEqual(replies, [("c1", "ok")])

    def test_validate_telegram_secret(self):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "expected"}
        self.assertTrue(validate_telegram_secret(headers, "expected"))
        self.assertFalse(validate_telegram_secret(headers, "other"))

    def test_verify_whatsapp_handshake(self):
        status, body = verify_whatsapp_handshake(
            {
                "hub.mode": ["subscribe"],
                "hub.challenge": ["12345"],
                "hub.verify_token": ["secret"],
            },
            "secret",
        )

        self.assertEqual((status, body), (200, "12345"))

    def test_verify_whatsapp_signature(self):
        body = b'{"entry":[{"changes":[]}]}'
        secret = "app-secret"
        signature = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        self.assertTrue(verify_whatsapp_signature(body, signature, secret))
        self.assertFalse(verify_whatsapp_signature(body, "sha256=wrong", secret))
        self.assertTrue(verify_whatsapp_signature(body, "", None))


if __name__ == "__main__":
    unittest.main()
