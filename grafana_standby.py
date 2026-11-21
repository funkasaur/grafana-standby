#!/usr/bin/env python3
"""Keep a Grafana standby synchronized and activate it after primary failure."""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


UTC = dt.timezone.utc


class APIError(Exception):
    pass


def timestamp(value):
    if not value or value.startswith("0001-01-01"):
        return None
    try:
        value = value.replace("Z", "+00:00")
        # datetime accepts only microseconds; Grafana emits nanoseconds.
        if "." in value:
            prefix, rest = value.split(".", 1)
            digits = len(rest) - len(rest.lstrip("0123456789"))
            value = prefix + "." + rest[:digits][:6] + rest[digits:]
        parsed = dt.datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def atomic_write(path, content, mode=0o600):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".grafana-standby-", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Primary:
    """Read-only primary API. Deliberately exposes no write method."""

    def __init__(self, config):
        self.url = config["primary_url"].rstrip("/")
        self.token = config["primary_token"]
        self.timeout = config["http_timeout_s"]

    def get(self, path):
        return request(self.url, self.token, self.timeout, "GET", path)


class Standby:
    def __init__(self, config, dry_run):
        self.url = config["standby_url"].rstrip("/")
        self.token = config["standby_token"]
        self.timeout = config["http_timeout_s"]
        self.dry_run = dry_run

    def get(self, path):
        return request(self.url, self.token, self.timeout, "GET", path)

    def write(self, method, path, body=None):
        if self.dry_run:
            print(f"DRY RUN: {method} standby {path}")
            return None
        return request(self.url, self.token, self.timeout, method, path, body)


def request(base, token, timeout, method, path, body=None):
    url = base + path
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if method != "GET":
        headers.update({"Content-Type": "application/json", "X-Disable-Provenance": "true"})
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise APIError(f"{method} {path}: HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise APIError(f"{method} {path}: request failed ({type(exc).__name__})") from None


def rule_path(uid):
    return "/api/v1/provisioning/alert-rules/" + urllib.parse.quote(uid, safe="")


def rules(client):
    result = client.get("/api/v1/provisioning/alert-rules")
    if not isinstance(result, list):
        raise APIError("alert-rules response is not a list")
    return result


def outside_rules(client, folder_uid):
    return [rule for rule in rules(client) if rule.get("folderUID", rule.get("folderUid")) != folder_uid]


def rule_paused(rule):
    return bool(rule.get("isPaused", False))


def comparable(rule):
    """Rule fields that matter for sync: id, updated and provenance are local to each Grafana."""
    return {k: v for k, v in rule.items() if k not in ("id", "updated", "provenance")}


def put_rule(standby, rule, paused):
    # Never send the source's numeric id: ids are per database and collide on the standby (HTTP 409).
    body = comparable(rule)
    body["isPaused"] = paused
    path = rule_path(rule["uid"])
    try:
        standby.write("PUT", path, body)
    except APIError as exc:
        if "HTTP 404" not in str(exc):
            raise
        standby.write("POST", "/api/v1/provisioning/alert-rules", body)


def interval_seconds(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    parts = re.findall(r"(\d+(?:\.\d+)?)([smhd])", value)
    if parts and "".join(n + unit for n, unit in parts) == value:
        return sum(float(n) * units[unit] for n, unit in parts)
    try:
        return float(value)
    except ValueError:
        return 0


def current_health(primary, max_age, paused_uids=()):
    try:
        health = primary.get("/api/health")
        if not isinstance(health, dict) or health.get("database") != "ok":
            return False
        tree = primary.get("/api/prometheus/grafana/api/v1/rules")
        groups = tree.get("data", {}).get("groups", [])
        if not isinstance(groups, list):
            return False
        active_count = 0
        paused_uids = set(paused_uids)
        now = dt.datetime.now(UTC)
        for group in groups:
            group_rules = group.get("rules", [])
            active = []
            for rule in group_rules:
                grafana_alert = rule.get("grafana_alert") or {}
                uid = rule.get("uid") or grafana_alert.get("uid")
                paused = rule.get("isPaused", grafana_alert.get("isPaused", uid in paused_uids))
                if not paused:
                    active.append(rule)
            if not active:
                continue
            active_count += 1
            allowance = max(max_age, 3 * interval_seconds(group.get("interval")))
            # Judge each active rule by its own lastEvaluation. The group's lastEvaluation is not usable: Grafana 13
            # reports the zero time for any group that contains a paused rule. A rule that has
            # never been evaluated (zero time: just created) is skipped; the group still needs one fresh rule, and any
            # evaluated rule older than the allowance means the scheduler has stalled.
            group_evaluation = timestamp(group.get("lastEvaluation"))
            fresh = False
            for rule in active:
                evaluation = timestamp(rule["lastEvaluation"]) if "lastEvaluation" in rule else group_evaluation
                if evaluation is None:
                    continue
                if not 0 <= (now - evaluation).total_seconds() <= allowance:
                    return False
                fresh = True
            if not fresh:
                return False
        return active_count > 0
    except (APIError, AttributeError, TypeError):
        return False


def _post(url, data, headers, timeout):
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as response:
        return response.read()


def notify(config, message, dry_run):
    """Send a failover/failback notice to every configured channel. Delivery failures never stop the tool."""
    if dry_run:
        print("DRY RUN: notice: " + message)
        return
    timeout = config["http_timeout_s"]
    if config.get("telegram_bot_token") and config.get("telegram_chat_id"):
        url = "https://api.telegram.org/bot" + config["telegram_bot_token"] + "/sendMessage"
        payload = urllib.parse.urlencode({"chat_id": config["telegram_chat_id"], "text": message}).encode()
        try:
            if not json.loads(_post(url, payload, {}, timeout)).get("ok"):
                raise ValueError("Telegram returned failure")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            print("Telegram delivery failed", file=sys.stderr)
    if config.get("webhook_url"):
        # JSON {"<webhook_field>": message}: "text" suits Slack/Mattermost-style webhooks, "content" Discord.
        body = json.dumps({config.get("webhook_field", "text"): message}).encode()
        try:
            _post(config["webhook_url"], body, {"Content-Type": "application/json"}, timeout)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            print("Webhook delivery failed", file=sys.stderr)


def state_load(path):
    try:
        with open(path, encoding="utf-8") as source:
            state = json.load(source)
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        return state
    except FileNotFoundError:
        return {}


def state_save(config, state, dry_run):
    if not dry_run:
        atomic_write(config["state_path"], json.dumps(state, indent=2, sort_keys=True) + "\n")


def metrics(config, state, standby, dry_run):
    count = sum(not rule_paused(rule) for rule in outside_rules(standby, config["standby_folder_uid"]))
    values = {
        "grafana_standby_active": int(state.get("mode", "standby") == "active"),
        "grafana_standby_primary_healthy": int(bool(state.get("primary_healthy", False))),
        "grafana_standby_last_check_timestamp_seconds": int(state.get("last_check_ts", 0)),
        "grafana_standby_last_sync_success_timestamp_seconds": int(state.get("last_sync_success_ts", 0)),
        "grafana_standby_paused_refresh_timestamp_seconds": int(state.get("paused_refresh_ts", 0)),
        "grafana_standby_rules_unpaused": count,
    }
    content = "".join(f"# TYPE {key} gauge\n{key} {value}\n" for key, value in values.items())
    if dry_run:
        print("DRY RUN: write textfile metrics")
    else:
        atomic_write(config["textfile_path"], content, 0o644)


def activate(config, state, primary, standby, dry_run, failures=None):
    if state.get("mode", "standby") == "active":
        print("Already active")
        return
    paused = state.get("primary_paused_uids")
    if paused is None:
        try:
            paused = [r["uid"] for r in rules(primary) if rule_paused(r)]
        except APIError:
            paused = []
    paused = set(paused)
    count = 0
    for rule in outside_rules(standby, config["standby_folder_uid"]):
        if rule["uid"] not in paused and rule_paused(rule):
            put_rule(standby, rule, False)
            count += 1
    print(f"Activated {count} standby rules")
    if not dry_run:
        state["mode"] = "active"
    minutes = failures if failures is not None else state.get("fail_count", 0)
    notify(config, f"FAILOVER: {config['primary_name']} unhealthy for {minutes} checks: "
                   f"{config['standby_name']} alerting ACTIVE ({count} rules)", dry_run)


def deactivate(config, state, standby, dry_run):
    if state.get("mode", "standby") == "standby":
        print("Already standby")
        return
    count = 0
    for rule in outside_rules(standby, config["standby_folder_uid"]):
        if not rule_paused(rule):
            put_rule(standby, rule, True)
            count += 1
    print(f"Paused {count} standby rules")
    if not dry_run:
        state["mode"] = "standby"
    notify(config, f"Failback: {config['primary_name']} healthy again: {config['standby_name']} paused", dry_run)


def check(config, state, primary, standby, dry_run):
    healthy = current_health(primary, config["eval_max_age_s"], state.get("primary_paused_uids", ()))
    state["primary_healthy"] = healthy
    state["last_check_ts"] = int(dt.datetime.now(UTC).timestamp())
    if healthy:
        if state["last_check_ts"] - state.get("paused_refresh_ts", 0) >= config["paused_refresh_s"]:
            try:
                source_rules = rules(primary)
                state["primary_paused_uids"] = [r["uid"] for r in source_rules if rule_paused(r)]
                source_folders = {f["uid"]: f for f in primary.get("/api/folders?limit=1000")}
                dest_folders = {f["uid"] for f in standby.get("/api/folders?limit=1000")}
                dest_rules = {r["uid"]: r for r in outside_rules(standby, config["standby_folder_uid"])}
                # Copy rules only in standby mode: during a failover (primary healthy again but not yet for
                # recover_threshold checks) writing paused copies would silence the standby early.
                for rule in (source_rules if state.get("mode", "standby") == "standby" else []):
                    folder_uid = rule.get("folderUID", rule.get("folderUid"))
                    if folder_uid == config["standby_folder_uid"]:
                        continue
                    body = dict(rule, isPaused=True)
                    if rule["uid"] not in dest_rules or comparable(dest_rules[rule["uid"]]) != comparable(body):
                        if folder_uid and folder_uid not in dest_folders:
                            folder = source_folders[folder_uid]
                            standby.write("POST", "/api/folders", {"uid": folder_uid, "title": folder["title"]})
                            dest_folders.add(folder_uid)
                        put_rule(standby, rule, True)
                state["paused_refresh_ts"] = state["last_check_ts"]
            except (APIError, KeyError, TypeError, ValueError) as exc:
                print(f"Primary paused-rule and copy refresh failed: {exc}", file=sys.stderr)
        state["fail_count"] = 0
        state["recover_count"] = state.get("recover_count", 0) + 1
    else:
        state["recover_count"] = 0
        state["fail_count"] = state.get("fail_count", 0) + 1
    print(f"Primary {'healthy' if healthy else 'unhealthy'}; failures={state['fail_count']} recoveries={state['recover_count']}")
    if state.get("mode", "standby") == "standby" and state["fail_count"] >= config["fail_threshold"]:
        activate(config, state, primary, standby, dry_run, state["fail_count"])
    elif state.get("mode") == "active" and state["recover_count"] >= config["recover_threshold"]:
        deactivate(config, state, standby, dry_run)
    state_save(config, state, dry_run)
    metrics(config, state, standby, dry_run)


def sync(config, state, primary, standby, dry_run):
    if state.get("mode", "standby") == "active":
        print("Standby active; sync skipped")
        return
    # Fetch the entire primary snapshot before making any changes.
    folders = primary.get("/api/folders?limit=1000")
    source_rules = rules(primary)
    policies = primary.get("/api/v1/provisioning/policies")
    templates = primary.get("/api/v1/provisioning/templates")
    mutes = primary.get("/api/v1/provisioning/mute-timings")
    search = primary.get("/api/search?type=dash-db&limit=5000")
    dashboards = [primary.get("/api/dashboards/uid/" + urllib.parse.quote(item["uid"], safe="")) for item in search]
    if not all(isinstance(x, list) for x in (folders, templates, mutes, search)):
        raise APIError("unexpected primary list response")
    dest_folders = {x["uid"]: x for x in standby.get("/api/folders?limit=1000")}
    dest_rules = {x["uid"]: x for x in outside_rules(standby, config["standby_folder_uid"])}
    dest_templates = {x["name"]: x for x in standby.get("/api/v1/provisioning/templates")}
    dest_mutes = {x["name"]: x for x in standby.get("/api/v1/provisioning/mute-timings")}
    dest_contacts = {x["name"] for x in standby.get("/api/v1/provisioning/contact-points")}
    dest_search = standby.get("/api/search?type=dash-db&limit=5000")
    dest_dashboards = {item["uid"]: standby.get("/api/dashboards/uid/" + urllib.parse.quote(item["uid"], safe=""))
                       for item in dest_search}
    for folder in folders:
        uid = folder["uid"]
        if uid == config["standby_folder_uid"]:
            continue
        if uid in dest_folders:
            if dest_folders[uid].get("title") != folder["title"]:
                standby.write("PUT", "/api/folders/" + urllib.parse.quote(uid, safe=""), {"title": folder["title"]})
        else:
            standby.write("POST", "/api/folders", {"uid": uid, "title": folder["title"]})
    source_uids = set()
    for rule in source_rules:
        if rule.get("folderUID", rule.get("folderUid")) == config["standby_folder_uid"]:
            continue
        source_uids.add(rule["uid"])
        body = dict(rule, isPaused=True)
        if rule["uid"] not in dest_rules or comparable(dest_rules[rule["uid"]]) != comparable(body):
            put_rule(standby, rule, True)
    for uid in dest_rules.keys() - source_uids:
        standby.write("DELETE", rule_path(uid))
    def receivers(node):
        found = {node["receiver"]} if node.get("receiver") else set()
        for route in node.get("routes", []):
            found.update(receivers(route))
        return found

    missing_receivers = receivers(policies) - dest_contacts
    if missing_receivers:
        print("Missing standby policy receivers: " + ", ".join(sorted(missing_receivers)), file=sys.stderr)
    else:
        standby.write("PUT", "/api/v1/provisioning/policies", policies)
    for template in templates:
        name = urllib.parse.quote(template["name"], safe="")
        if dest_templates.get(template["name"]) != template:
            standby.write("PUT", "/api/v1/provisioning/templates/" + name, template)
    for name in dest_templates.keys() - {x["name"] for x in templates}:
        standby.write("DELETE", "/api/v1/provisioning/templates/" + urllib.parse.quote(name, safe=""))
    for mute in mutes:
        name = urllib.parse.quote(mute["name"], safe="")
        if mute["name"] not in dest_mutes:
            standby.write("POST", "/api/v1/provisioning/mute-timings", mute)
        elif dest_mutes[mute["name"]] != mute:
            standby.write("PUT", "/api/v1/provisioning/mute-timings/" + name, mute)
    for name in dest_mutes.keys() - {x["name"] for x in mutes}:
        standby.write("DELETE", "/api/v1/provisioning/mute-timings/" + urllib.parse.quote(name, safe=""))
    for item in dashboards:
        dashboard = dict(item["dashboard"])
        uid = dashboard["uid"]
        dashboard["id"] = None  # IDs are local to a Grafana database.
        body = {"dashboard": dashboard, "folderUid": item["meta"].get("folderUid", ""), "overwrite": True}
        existing = dest_dashboards.get(uid)
        if existing is None or (existing["dashboard"].get("version") != dashboard.get("version")
                or dict(existing["dashboard"], id=None) != dashboard
                or existing["meta"].get("folderUid", "") != body["folderUid"]):
            standby.write("POST", "/api/dashboards/db", body)
            if not dry_run:
                managed = state.setdefault("managed_dashboard_uids", [])
                if uid not in managed:
                    managed.append(uid)
                state_save(config, state, dry_run)
    source_dashboard_uids = {item["dashboard"]["uid"] for item in dashboards}
    if not dry_run:
        retained = set(state.get("managed_dashboard_uids", ())) & dest_dashboards.keys()
        state["managed_dashboard_uids"] = sorted(retained | source_dashboard_uids)
        state_save(config, state, dry_run)
    for uid, item in dest_dashboards.items():
        if (uid in state.get("managed_dashboard_uids", ()) and uid not in source_dashboard_uids
                and item["meta"].get("folderUid", "") != config["standby_folder_uid"]):
            standby.write("DELETE", "/api/dashboards/uid/" + urllib.parse.quote(uid, safe=""))
            if not dry_run:
                state["managed_dashboard_uids"].remove(uid)
                state_save(config, state, dry_run)
    if not dry_run and not missing_receivers:
        state["primary_paused_uids"] = [r["uid"] for r in source_rules if rule_paused(r)]
        state["paused_refresh_ts"] = int(dt.datetime.now(UTC).timestamp())
        state["last_sync_success_ts"] = int(dt.datetime.now(UTC).timestamp())
    state_save(config, state, dry_run)
    metrics(config, state, standby, dry_run)
    print("Sync incomplete: missing policy receivers" if missing_receivers else "Sync complete")
    return bool(missing_receivers)


def status(config, state, standby):
    rs = outside_rules(standby, config["standby_folder_uid"])
    print(f"state: {state.get('mode', 'standby')}")
    print(f"fail_count: {state.get('fail_count', 0)}")
    print(f"recover_count: {state.get('recover_count', 0)}")
    print(f"last_check_ts: {state.get('last_check_ts', 0)}")
    print(f"last_sync_success_ts: {state.get('last_sync_success_ts', 0)}")
    print(f"rules_paused: {sum(rule_paused(r) for r in rs)}")
    print(f"rules_unpaused: {sum(not rule_paused(r) for r in rs)}")


@contextlib.contextmanager
def locked(path):
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/grafana-standby/config.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("command", choices=("check", "sync", "activate", "deactivate", "status"))
    args = parser.parse_args(argv)
    try:
        with open(args.config, encoding="utf-8") as source:
            config = json.load(source)
        for name, default in (("state_path", "/var/lib/grafana-standby/state.json"),
                              ("textfile_path", "/var/lib/node_exporter/textfile_collector/grafana_standby.prom"),
                              ("fail_threshold", 3), ("recover_threshold", 5),
                              ("eval_max_age_s", 300), ("paused_refresh_s", 900), ("http_timeout_s", 10),
                              ("primary_name", "primary Grafana"), ("standby_name", "standby Grafana")):
            config.setdefault(name, default)
        with locked(config["state_path"]):
            state = state_load(config["state_path"])
            primary = Primary(config)
            standby = Standby(config, args.dry_run)
            if args.command == "check":
                check(config, state, primary, standby, args.dry_run)
            elif args.command == "sync":
                sync_failed = sync(config, state, primary, standby, args.dry_run)
            elif args.command == "activate":
                activate(config, state, primary, standby, args.dry_run)
                state_save(config, state, args.dry_run)
                metrics(config, state, standby, args.dry_run)
            elif args.command == "deactivate":
                deactivate(config, state, standby, args.dry_run)
                state_save(config, state, args.dry_run)
                metrics(config, state, standby, args.dry_run)
            else:
                status(config, state, standby)
        return 1 if args.command == "sync" and sync_failed else 0
    except (APIError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
