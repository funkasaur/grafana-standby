import json
import unittest
from unittest import mock

import grafana_standby as app


class NotifyTest(unittest.TestCase):
    base = {"http_timeout_s": 1, "primary_name": "grafana-a", "standby_name": "grafana-b"}

    def test_no_channel_sends_nothing(self):
        with mock.patch.object(app, "_post") as post:
            app.notify(dict(self.base), "hello", False)
        post.assert_not_called()

    def test_webhook_and_telegram(self):
        config = dict(self.base, webhook_url="http://hook.example/x", webhook_field="content",
                      telegram_bot_token="T", telegram_chat_id="1")
        with mock.patch.object(app, "_post", return_value=b'{"ok": true}') as post:
            app.notify(config, "hello", False)
        urls = [c.args[0] for c in post.call_args_list]
        self.assertEqual(urls, ["https://api.telegram.org/botT/sendMessage", "http://hook.example/x"])
        self.assertEqual(json.loads(post.call_args_list[1].args[1]), {"content": "hello"})

    def test_delivery_failure_is_reported_not_raised(self):
        config = dict(self.base, webhook_url="http://hook.example/x")
        with mock.patch.object(app, "_post", side_effect=OSError("down")), \
                mock.patch("sys.stderr") as err:
            app.notify(config, "hello", False)
        self.assertTrue(any("Webhook delivery failed" in str(c) for c in err.write.call_args_list))

    def test_failover_message_uses_names(self):
        with mock.patch.object(app, "notify") as notice, mock.patch.object(app, "outside_rules", return_value=[]):
            app.activate(dict(self.base, standby_folder_uid="s"), {"primary_paused_uids": []}, None, None, True, 3)
        self.assertEqual(notice.call_args.args[1], "FAILOVER: grafana-a unhealthy for 3 checks: grafana-b alerting ACTIVE (0 rules)")


if __name__ == "__main__":
    unittest.main()
