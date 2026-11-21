import contextlib
import copy
import datetime as dt
import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import grafana_standby as app  # noqa: E402


def rule(uid, folder="main", paused=False):
    return {"uid": uid, "folderUID": folder, "title": uid, "isPaused": paused,
            "condition": "A", "data": [{"refId": "A"}]}


class HealthWithoutSocketTest(unittest.TestCase):
    def test_active_groups_and_interval(self):
        now = dt.datetime.now(dt.timezone.utc)
        fresh = now.isoformat()
        stale = (now - dt.timedelta(minutes=10)).isoformat()

        class Primary:
            groups = []

            def get(self, path):
                if path == "/api/health":
                    return {"database": "ok"}
                return {"data": {"groups": self.groups}}

        primary = Primary()
        primary.groups = [{"interval": "1m", "rules": [{"uid": "a", "lastEvaluation": fresh}]},
                          {"interval": "1m", "rules": [{"uid": "b", "lastEvaluation": stale}]}]
        self.assertFalse(app.current_health(primary, 300))
        primary.groups[1]["rules"][0]["isPaused"] = True
        self.assertTrue(app.current_health(primary, 300))
        primary.groups[1]["rules"][0].pop("isPaused")
        self.assertTrue(app.current_health(primary, 300, {"b"}))
        primary.groups[1]["rules"][0] = {"grafana_alert": {"uid": "b", "isPaused": True},
                                           "lastEvaluation": stale}
        self.assertTrue(app.current_health(primary, 300))
        primary.groups[1]["rules"][0]["grafana_alert"]["isPaused"] = False
        primary.groups[1]["interval"] = "4m"
        self.assertTrue(app.current_health(primary, 300))
        primary.groups[1]["rules"][0]["lastEvaluation"] = "unparseable"
        self.assertFalse(app.current_health(primary, 300))

    def test_same_group_stale_rule_and_group_timestamp(self):
        now = dt.datetime.now(dt.timezone.utc)
        fresh = now.isoformat()
        stale = (now - dt.timedelta(minutes=10)).isoformat()

        class Primary:
            def get(self, path):
                if path == "/api/health":
                    return {"database": "ok"}
                return {"data": {"groups": self.groups}}

        primary = Primary()
        for group_evaluation in (None, fresh):
            group = {"interval": "1m", "rules": [{"uid": "a", "lastEvaluation": fresh},
                                                    {"uid": "b", "lastEvaluation": stale}]}
            if group_evaluation:
                group["lastEvaluation"] = group_evaluation
            primary.groups = [group]
            self.assertFalse(app.current_health(primary, 300))
            del group["rules"][1]["lastEvaluation"]
            # No timestamp of its own: judged by the group's, skipped when there is none.
            self.assertTrue(app.current_health(primary, 300))

    def test_live_zero_group_time_with_paused_rule(self):
        # Grafana 13 reports the zero time for a group holding a paused rule ; a just-created
        # rule shows the zero time until its first evaluation.
        now = dt.datetime.now(dt.timezone.utc)
        fresh = now.isoformat()
        zero = "0001-01-01T00:00:00Z"

        class Primary:
            def get(self, path):
                if path == "/api/health":
                    return {"database": "ok"}
                return {"data": {"groups": self.groups}}

        primary = Primary()
        primary.groups = [{"interval": "1m", "lastEvaluation": zero,
                           "rules": [{"uid": "a", "lastEvaluation": fresh},
                                     {"uid": "p", "isPaused": True, "lastEvaluation": zero}]}]
        self.assertTrue(app.current_health(primary, 300))
        primary.groups[0]["rules"].append({"uid": "new", "lastEvaluation": zero})
        self.assertTrue(app.current_health(primary, 300))
        primary.groups[0]["rules"][0]["lastEvaluation"] = zero
        self.assertFalse(app.current_health(primary, 300))


class Fake:
    def __init__(self):
        self.health = {"database": "ok"}
        self.evaluation = dt.datetime.now(dt.timezone.utc).isoformat()
        self.groups = None
        self.rules = []
        self.folders = []
        self.policies = {"receiver": "telegram"}
        self.contacts = [{"name": "telegram"}]
        self.templates = []
        self.mutes = []
        self.dashboards = {}
        self.writes = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, code, value=None):
                payload = json.dumps(value).encode() if value is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/health":
                    return self.respond(200, fake.health)
                if path == "/api/prometheus/grafana/api/v1/rules":
                    groups = fake.groups if fake.groups is not None else [{"interval": "1m", "rules": [{"uid": "a", "lastEvaluation": fake.evaluation}]}]
                    return self.respond(200, {"data": {"groups": groups}})
                if path == "/api/v1/provisioning/alert-rules":
                    return self.respond(200, fake.rules)
                if path == "/api/folders":
                    return self.respond(200, fake.folders)
                if path == "/api/v1/provisioning/policies":
                    return self.respond(200, fake.policies)
                if path == "/api/v1/provisioning/contact-points":
                    return self.respond(200, fake.contacts)
                if path == "/api/v1/provisioning/templates":
                    return self.respond(200, fake.templates)
                if path == "/api/v1/provisioning/mute-timings":
                    return self.respond(200, fake.mutes)
                if path == "/api/search":
                    return self.respond(200, [{"uid": uid} for uid in fake.dashboards])
                if path.startswith("/api/dashboards/uid/"):
                    return self.respond(200, fake.dashboards[path.rsplit("/", 1)[1]])
                return self.respond(404)

            def write(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(body) if body else None
                path = self.path
                fake.writes.append((self.command, path, body, self.headers.get("X-Disable-Provenance")))
                if path.startswith("/api/v1/provisioning/alert-rules/"):
                    uid = path.rsplit("/", 1)[1]
                    existing = next((r for r in fake.rules if r["uid"] == uid), None)
                    if self.command == "PUT" and existing is None:
                        return self.respond(404)
                    fake.rules = [r for r in fake.rules if r["uid"] != uid]
                    if self.command == "PUT":
                        fake.rules.append(body)
                elif path == "/api/v1/provisioning/alert-rules" and self.command == "POST":
                    fake.rules.append(body)
                elif path == "/api/folders" and self.command == "POST":
                    fake.folders.append(body)
                elif path.startswith("/api/folders/") and self.command == "PUT":
                    uid = path.rsplit("/", 1)[1]
                    next(f for f in fake.folders if f["uid"] == uid)["title"] = body["title"]
                elif path == "/api/dashboards/db" and self.command == "POST":
                    uid = body["dashboard"]["uid"]
                    fake.dashboards[uid] = {"dashboard": dict(body["dashboard"], id=1),
                                            "meta": {"folderUid": body["folderUid"]}}
                elif path.startswith("/api/dashboards/uid/") and self.command == "DELETE":
                    fake.dashboards.pop(path.rsplit("/", 1)[1])
                return self.respond(200, {})

            do_POST = write
            do_PUT = write
            do_DELETE = write

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class StandbyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.primary = Fake()
        self.standby = Fake()
        self.addCleanup(self.primary.close)
        self.addCleanup(self.standby.close)
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.json"
        self.metrics_path = self.root / "metrics.prom"
        self.config_path = self.root / "config.json"
        self.primary.rules = [rule("a"), rule("b", paused=True)]
        self.standby.rules = [rule("a", paused=True), rule("b", paused=True), rule("always", "standby")]
        self.config = {"primary_url": self.primary.url, "primary_token": "secret-primary",
                       "standby_url": self.standby.url, "standby_token": "secret-standby",
                       "telegram_bot_token": "secret-telegram", "telegram_chat_id": "123",
                       "webhook_url": "http://127.0.0.1:9/hook",
                       "standby_folder_uid": "standby", "state_path": str(self.state_path),
                       "textfile_path": str(self.metrics_path), "http_timeout_s": 1}
        self.config_path.write_text(json.dumps(self.config))
        self.telegram = mock.patch.object(app, "notify")
        self.notice = self.telegram.start()
        self.addCleanup(self.telegram.stop)

    def run_cmd(self, command, dry=False, expected=0):
        args = ["--config", str(self.config_path)]
        if dry:
            args.append("--dry-run")
        args.append(command)
        with contextlib.redirect_stdout(__import__("io").StringIO()):
            result = app.main(args)
        self.assertEqual(result, expected)

    def state(self):
        return json.loads(self.state_path.read_text())

    def test_thresholds_and_flapping(self):
        self.primary.health = {"database": "bad"}
        self.run_cmd("check")
        self.run_cmd("check")
        self.assertEqual(self.state()["mode"] if "mode" in self.state() else "standby", "standby")
        self.primary.health = {"database": "ok"}
        self.run_cmd("check")
        self.assertEqual(self.state()["fail_count"], 0)
        self.primary.health = {"database": "bad"}
        for _ in range(3):
            self.run_cmd("check")
        self.assertEqual(self.state()["mode"], "active")
        self.assertEqual(len([w for w in self.standby.writes if w[1].endswith("/a")]), 1)
        self.run_cmd("check")
        self.assertEqual(len([w for w in self.standby.writes if w[1].endswith("/a")]), 1)
        self.primary.health = {"database": "ok"}
        for _ in range(4):
            self.run_cmd("check")
        self.assertEqual(self.state()["mode"], "active")
        self.run_cmd("check")
        self.assertEqual(self.state()["mode"], "standby")
        self.assertTrue(next(r for r in self.standby.rules if r["uid"] == "a")["isPaused"])

    def test_unhealthy_evaluation_and_database(self):
        self.primary.evaluation = "0001-01-01T00:00:00Z"
        self.run_cmd("check")
        self.assertFalse(self.state()["primary_healthy"])
        self.primary.evaluation = "2020-01-01T00:00:00.123456789+00:00"
        self.run_cmd("check")
        self.assertFalse(self.state()["primary_healthy"])
        self.primary.evaluation = dt.datetime.now(dt.timezone.utc).isoformat()
        self.primary.health = {"database": "bad"}
        self.run_cmd("check")
        self.assertFalse(self.state()["primary_healthy"])

    def test_every_active_group_must_be_fresh(self):
        fresh = dt.datetime.now(dt.timezone.utc).isoformat()
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
        self.primary.groups = [{"interval": "1m", "rules": [{"uid": "a", "lastEvaluation": fresh}]},
                               {"interval": "1m", "rules": [{"uid": "b", "lastEvaluation": stale, "isPaused": False}]}]
        self.run_cmd("check")
        self.assertFalse(self.state()["primary_healthy"])
        self.primary.groups[1]["rules"][0]["isPaused"] = True
        self.run_cmd("check")
        self.assertTrue(self.state()["primary_healthy"])
        self.assertTrue(self.state()["paused_refresh_ts"])

    def test_paused_refresh_failure_does_not_change_health(self):
        self.primary.rules = [{"uid": "b", "isPaused": True}]
        self.run_cmd("check")
        self.assertEqual(self.state()["primary_paused_uids"], ["b"])
        state = self.state()
        state["paused_refresh_ts"] = 0
        self.state_path.write_text(json.dumps(state))
        original_get = app.Primary.get
        def broken_get(primary, path):
            if path == "/api/v1/provisioning/alert-rules":
                raise app.APIError("refresh failed")
            return original_get(primary, path)
        with mock.patch.object(app.Primary, "get", broken_get):
            self.run_cmd("check")
        self.assertTrue(self.state()["primary_healthy"])
        self.assertEqual(self.state()["primary_paused_uids"], ["b"])
        self.assertEqual(self.state()["paused_refresh_ts"], 0)

    def test_missing_nested_policy_receiver_fails_after_other_sync(self):
        self.primary.policies = {"receiver": "telegram", "routes": [{"receiver": "missing"}]}
        self.primary.templates = [{"name": "test", "template": "body"}]
        self.run_cmd("sync", expected=1)
        self.assertFalse(any(w[1] == "/api/v1/provisioning/policies" for w in self.standby.writes))
        self.assertTrue(any(w[1].startswith("/api/v1/provisioning/templates/") for w in self.standby.writes))
        self.assertNotIn("last_sync_success_ts", self.state())

    def test_dashboard_unchanged_and_removed_only_if_managed(self):
        dash = {"dashboard": {"uid": "same", "id": 4, "version": 2, "title": "Same"},
                "meta": {"folderUid": "main"}}
        self.primary.dashboards = {"same": copy.deepcopy(dash)}
        self.standby.dashboards = {"same": copy.deepcopy(dash),
                                   "local": {"dashboard": {"uid": "local"}, "meta": {"folderUid": "main"}}}
        self.run_cmd("sync")
        self.assertFalse(any(w[1] == "/api/dashboards/db" for w in self.standby.writes))
        self.assertEqual(self.state()["managed_dashboard_uids"], ["same"])
        self.assertIn("local", self.standby.dashboards)
        self.primary.dashboards["same"]["dashboard"]["title"] = "Changed"
        self.run_cmd("sync")
        self.assertTrue(any(w[1] == "/api/dashboards/db" for w in self.standby.writes))
        self.primary.dashboards.clear()
        self.run_cmd("sync")
        self.assertNotIn("same", self.standby.dashboards)
        self.assertIn("local", self.standby.dashboards)
        self.assertEqual(self.state()["managed_dashboard_uids"], [])
        self.assertFalse(any(w[1] == "/api/dashboards/uid/local" for w in self.standby.writes))

    def test_healthy_refresh_does_not_repause_while_active(self):
        self.primary.folders = [{"uid": "main", "title": "Main"}]
        self.standby.folders = [{"uid": "main", "title": "Main"}]
        self.run_cmd("sync")
        state = self.state()
        state.update(mode="active", paused_refresh_ts=0)
        self.state_path.write_text(json.dumps(state))
        for r in self.standby.rules:
            r["isPaused"] = False
        self.primary.rules[0]["title"] = "Changed"
        self.standby.writes.clear()
        self.run_cmd("check")
        self.assertFalse(any(w[1].startswith("/api/v1/provisioning/alert-rules") for w in self.standby.writes))
        self.assertTrue(all(not r["isPaused"] for r in self.standby.rules))

    def test_healthy_refresh_copies_new_and_changed_rules_then_activates(self):
        self.primary.folders = [{"uid": "main", "title": "Main"}]
        self.standby.folders = [{"uid": "main", "title": "Main"}]
        self.run_cmd("sync")
        self.primary.folders.append({"uid": "new-folder", "title": "New Folder"})
        self.primary.rules.append(rule("new", "new-folder"))
        self.primary.rules[0]["title"] = "Changed"
        state = self.state()
        state["paused_refresh_ts"] = 0
        self.state_path.write_text(json.dumps(state))
        self.run_cmd("check")
        copied = {r["uid"]: r for r in self.standby.rules}
        self.assertTrue(copied["new"]["isPaused"])
        self.assertEqual(copied["a"]["title"], "Changed")
        self.assertIn("new-folder", {f["uid"] for f in self.standby.folders})
        folder_write = next(i for i, w in enumerate(self.standby.writes)
                            if w[1] == "/api/folders" and w[2]["uid"] == "new-folder")
        rule_write = next(i for i, w in enumerate(self.standby.writes)
                          if w[1] == "/api/v1/provisioning/alert-rules" and w[2]["uid"] == "new")
        self.assertLess(folder_write, rule_write)
        self.primary.health = {"database": "bad"}
        for _ in range(3):
            self.run_cmd("check")
        self.assertFalse(next(r for r in self.standby.rules if r["uid"] == "new")["isPaused"])

    def test_activate_exclusions_and_telegram_failure(self):
        self.state_path.write_text(json.dumps({"primary_paused_uids": ["b"]}))
        self.notice.side_effect = Exception("Telegram unavailable")
        # The real notifier contains its own failure handling.
        self.telegram.stop()
        with mock.patch.object(app.urllib.request, "urlopen", wraps=app.urllib.request.urlopen) as urlopen:
            original = urlopen.side_effect
            def failing_telegram(req, *args, **kwargs):
                if req.full_url.startswith("https://api.telegram.org/"):
                    raise app.urllib.error.URLError("offline")
                return original(req, *args, **kwargs) if original else urlopen._mock_wraps(req, *args, **kwargs)
            urlopen.side_effect = failing_telegram
            self.run_cmd("activate")
        self.assertEqual(self.state()["mode"], "active")
        self.assertFalse(next(r for r in self.standby.rules if r["uid"] == "a")["isPaused"])
        self.assertTrue(next(r for r in self.standby.rules if r["uid"] == "b")["isPaused"])
        self.assertFalse(any(w[1].endswith("/always") for w in self.standby.writes))

    def test_sync_create_update_delete_and_active_refusal(self):
        self.primary.folders = [{"uid": "main", "title": "Main"}]
        self.primary.rules = [rule("a", paused=False), rule("new", paused=True)]
        self.standby.rules = [rule("a", paused=True), rule("old", paused=True), rule("always", "standby")]
        self.primary.dashboards = {"dash": {"dashboard": {"uid": "dash", "id": 3, "title": "Dash"}, "meta": {"folderUid": "main"}}}
        self.run_cmd("sync")
        self.assertEqual({r["uid"] for r in self.standby.rules}, {"a", "new", "always"})
        self.assertEqual(self.state()["primary_paused_uids"], ["new"])
        self.assertTrue(self.state()["last_sync_success_ts"])
        self.assertTrue(all(w[3] == "true" for w in self.standby.writes))
        self.assertTrue(any(w[1] == "/api/dashboards/db" and w[2]["dashboard"]["id"] is None for w in self.standby.writes))
        previous = len(self.standby.writes)
        self.state_path.write_text(json.dumps({"mode": "active"}))
        self.run_cmd("sync")
        self.assertEqual(len(self.standby.writes), previous)

    def test_primary_get_only_across_commands(self):
        self.primary.folders = [{"uid": "main", "title": "Main"}]
        for command in ("sync", "activate", "status", "deactivate", "check"):
            self.run_cmd(command)
        self.assertEqual(self.primary.writes, [])

    def test_dry_run_writes_no_state_metrics_or_api(self):
        self.primary.health = {"database": "bad"}
        self.config["fail_threshold"] = 1
        self.config_path.write_text(json.dumps(self.config))
        self.run_cmd("check", dry=True)
        self.assertFalse(self.state_path.exists())
        self.assertFalse(self.metrics_path.exists())
        self.assertEqual(self.standby.writes, [])
        self.notice.assert_called_once()

    def test_textfile_content(self):
        self.run_cmd("check")
        content = self.metrics_path.read_text()
        for name in ("active", "primary_healthy", "last_check_timestamp_seconds",
                     "last_sync_success_timestamp_seconds", "paused_refresh_timestamp_seconds", "rules_unpaused"):
            self.assertIn("grafana_standby_" + name + " ", content)
        self.assertIn("grafana_standby_primary_healthy 1", content)
        self.assertIn("grafana_standby_rules_unpaused 0", content)


if __name__ == "__main__":
    unittest.main()
