#!/usr/bin/env python3
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

APP_DIR = Path(os.environ.get("APPLE_WATCHER_APP_DIR", "/opt/apple-watcher"))
DATA_DIR = Path(os.environ.get("APPLE_WATCHER_DATA_DIR", "/var/lib/apple-watcher"))
CONFIG_FILE = DATA_DIR / "config.json"
TARGETS_FILE = DATA_DIR / "targets.json"
BLOCK_STATE_FILE = DATA_DIR / "cooldowns.json"
ROTATION_FILE = DATA_DIR / "rotation.json"
WORKER_KEY_FILE = DATA_DIR / ".ssh/worker_key"
WORKER_KNOWN_HOSTS_FILE = DATA_DIR / ".ssh/known_hosts"
ADMIN_FILE = APP_DIR / "admin.html"
TELEGRAM_TOKEN_FILE = Path("/etc/apple-watcher/telegram-token")
TELEGRAM_CHAT_ID_FILE = Path("/etc/apple-watcher/telegram-chat-id")
BARK_LEGACY_FILE = Path("/etc/apple-watcher/bark-url")
APW_BIN = APP_DIR / "bin/apw"
WORKER_HOST = os.environ.get("APPLE_WATCHER_SUB_HOST", "").strip()
POLL_INTERVAL_SECONDS = 60
ROTATION_CYCLES = 20
ROTATION_SECONDS = POLL_INTERVAL_SECONDS * ROTATION_CYCLES
NODES = ("primary", "secondary") if WORKER_HOST else ("primary",)
ALERT_MODES = {"passive", "active", "timeSensitive", "critical"}
BUY_URLS = {
    "zh_HK": "https://www.apple.com/hk/shop/buy-iphone/iphone-18-pro",
    "en_MY": "https://www.apple.com/my/shop/buy-iphone/iphone-18-pro",
}

config_lock = threading.Lock()
runtime_lock = threading.Lock()
reload_event = threading.Event()
runtime = {
    "running": False,
    "pid": None,
    "snapshot": [],
    "lastEvent": None,
    "lastError": None,
    "generation": 0,
    "queryGroupCount": 0,
    "activeNode": None,
    "nodeCycle": 0,
    "cyclesPerNode": ROTATION_CYCLES,
    "phaseEndsAtMs": None,
    "metrics": {
        "requestsLastHour": 0,
        "responses541LastHour": 0,
        "consecutive541": 0,
        "lastSuccessAtMs": None,
        "circuitOpen": False,
        "circuitOpenUntilMs": None,
    },
}
current_process = None


def save_rotation(value):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = ROTATION_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o640)
    os.replace(temporary, ROTATION_FILE)


def load_rotation(now=None):
    now = time.time() if now is None else now
    try:
        value = json.loads(ROTATION_FILE.read_text(encoding="utf-8"))
        node = value["activeNode"]
        started = float(value["startedAt"])
        if node not in NODES or started <= 0 or started > now + ROTATION_SECONDS:
            raise ValueError("invalid rotation state")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        node, started = NODES[0], now
    switches = max(0, int((now - started) // ROTATION_SECONDS))
    if switches:
        node = NODES[(NODES.index(node) + switches) % len(NODES)]
        started += switches * ROTATION_SECONDS
    value = {"activeNode": node, "startedAt": started}
    save_rotation(value)
    return value


def watcher_command(node, interval, timeout):
    args = ["watch", "--targets", str(TARGETS_FILE), "--interval", str(interval),
            "--timeout", str(timeout)]
    if node == NODES[0]:
        return [str(APW_BIN), *args], None
    if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", WORKER_HOST) or WORKER_HOST.startswith("-"):
        raise RuntimeError("工作节点 SSH 地址未配置或格式无效")
    args[2] = "-"
    command = [
        "/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
        "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={WORKER_KNOWN_HOSTS_FILE}", "-i", str(WORKER_KEY_FILE), WORKER_HOST,
        str(APW_BIN), *args,
    ]
    return command, TARGETS_FILE.read_text(encoding="utf-8")


def target_key(target):
    return target.get("locale"), target.get("storeNumber"), target.get("partNumber")


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("配置必须是 JSON 对象")
    interval = value.get("intervalSeconds", 60)
    if not isinstance(interval, int) or not 30 <= interval <= 3600:
        raise ValueError("查询间隔必须是 30–3600 秒")
    users = value.get("users")
    if not isinstance(users, list) or len(users) > 50:
        raise ValueError("users 必须是最多 50 项的数组")

    clean_users = []
    seen_ids = set()
    total_targets = 0
    for index, user in enumerate(users):
        if not isinstance(user, dict):
            raise ValueError(f"第 {index + 1} 个用户格式无效")
        user_id = str(user.get("id", "")).strip()
        name = str(user.get("name", "")).strip()
        bark_url = str(user.get("barkUrl", "")).strip()
        mode = user.get("alertMode", "active")
        if not user_id or len(user_id) > 80 or user_id in seen_ids:
            raise ValueError(f"第 {index + 1} 个用户 ID 为空、重复或过长")
        if not name or len(name) > 80:
            raise ValueError(f"第 {index + 1} 个用户名称为空或过长")
        if bark_url:
            make_bark_url(bark_url, "测试", "测试", mode, name)
        if mode not in ALERT_MODES:
            raise ValueError(f"{name} 的提醒方式无效")
        targets = user.get("targets")
        if not isinstance(targets, list):
            raise ValueError(f"{name} 的监控目标必须是数组")
        clean_targets = []
        seen_targets = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError(f"{name} 含有无效目标")
            locale = str(target.get("locale", "")).strip()
            store = str(target.get("storeNumber", "")).strip()
            part = str(target.get("partNumber", "")).strip()
            if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}", locale):
                raise ValueError(f"地区代码 {locale!r} 无效")
            if not re.fullmatch(r"R\d{1,11}", store):
                raise ValueError(f"门店编号 {store!r} 无效")
            if not re.fullmatch(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)?", part) or len(part) > 64:
                raise ValueError(f"SKU {part!r} 无效")
            clean = {
                "locale": locale,
                "storeNumber": store,
                "partNumber": part,
                "storeTitle": str(target.get("storeTitle", "")).strip()[:160],
                "productName": str(target.get("productName", "")).strip()[:240],
            }
            key = target_key(clean)
            if key not in seen_targets:
                seen_targets.add(key)
                clean_targets.append(clean)
        total_targets += len(clean_targets)
        if len({target["partNumber"] for target in clean_targets}) > 2:
            raise ValueError(f"{name} 最多只能监控 2 个机型")
        seen_ids.add(user_id)
        clean_users.append({
            "id": user_id,
            "name": name,
            "barkUrl": bark_url,
            "alertMode": mode,
            "targets": clean_targets,
        })
    if total_targets > 256:
        raise ValueError("监控目标总数不能超过 256")
    return {"intervalSeconds": interval, "users": clean_users}


def load_config():
    with config_lock:
        return validate_config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))


def save_config(value):
    clean = validate_config(value)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(".tmp")
    with config_lock:
        try:
            previous = validate_config(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError):
            previous = None
        temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o640)
        os.replace(temporary, CONFIG_FILE)
    if previous is None or polling_signature(previous) != polling_signature(clean):
        request_reload()
    return clean


def all_targets(config):
    unique = {}
    for user in config["users"]:
        for target in user["targets"]:
            unique.setdefault(target_key(target), target)
    return list(unique.values())


def polling_signature(config):
    return config["intervalSeconds"], json.dumps(
        all_targets(config), ensure_ascii=False, sort_keys=True
    )


def migrate_legacy():
    bark_url = BARK_LEGACY_FILE.read_text(encoding="utf-8").strip()
    old_targets = APP_DIR / "targets.json"
    targets = json.loads(old_targets.read_text(encoding="utf-8")) if old_targets.exists() else [
        {"locale": "en_MY", "storeNumber": "R742", "partNumber": "MJXU4X/A"},
        {"locale": "en_MY", "storeNumber": "R742", "partNumber": "MJXW4X/A"},
    ]
    profiles = {
        "en_MY": ("vip-my", "VIP-MY", "critical"),
        "zh_HK": ("vip-hk", "VIP-HK", "passive"),
    }
    users = []
    for locale in dict.fromkeys(target.get("locale") for target in targets):
        user_id, name, mode = profiles.get(locale, (f"vip-{locale}", f"VIP-{locale}", "active"))
        users.append({
            "id": user_id,
            "name": name,
            "barkUrl": bark_url,
            "alertMode": mode,
            "targets": [target for target in targets if target.get("locale") == locale],
        })
    save_config({"intervalSeconds": 30, "users": users})


def make_bark_url(base, title, body, mode="active", group="Apple库存", click_url=None):
    parts = urlsplit(base.strip())
    if parts.scheme != "https" or not parts.netloc or not parts.path.strip("/"):
        raise ValueError("Bark 地址必须包含 HTTPS 主机和设备 Key")
    if mode not in ALERT_MODES:
        raise ValueError("Bark 提醒方式无效")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in ("level", "call", "sound", "volume", "group", "isArchive", "url"):
        query.pop(key, None)
    query.update({"level": mode, "group": f"Apple-{group}", "isArchive": "1"})
    if mode == "critical":
        query.update({"call": "1", "sound": "minuet", "volume": "10"})
    if click_url:
        query["url"] = click_url
    path = f'{parts.path.rstrip("/")}/{quote(title, safe="")}/{quote(body, safe="")}'
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def bark_push(user, title, body, click_url=None):
    if not user["barkUrl"]:
        return False
    try:
        request = Request(
            make_bark_url(
                user["barkUrl"], title, body, user["alertMode"], user["name"], click_url
            ),
            headers={"User-Agent": "apple-watcher-notifier/2"},
        )
        with urlopen(request, timeout=15) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError(f"HTTP {response.status}")
        return True
    except (HTTPError, URLError, OSError, RuntimeError, ValueError) as error:
        print(f"Bark 推送失败（{user['name']}）：{error}", file=sys.stderr, flush=True)
        return False


def telegram_push(text):
    try:
        token = TELEGRAM_TOKEN_FILE.read_text(encoding="utf-8").strip()
        chat_id = TELEGRAM_CHAT_ID_FILE.read_text(encoding="utf-8").strip()
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urlencode({"chat_id": chat_id, "text": text}).encode(),
            headers={"User-Agent": "apple-watcher-notifier/2"},
        )
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read(8192))
            if not 200 <= response.status < 300 or not result.get("ok"):
                raise RuntimeError(f"HTTP {response.status}")
        return True
    except (HTTPError, URLError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Telegram 推送失败：{error}", file=sys.stderr, flush=True)
        return False


def issue_summary(user, snapshot):
    keys = {target_key(target) for target in user["targets"]}
    states = [state for state in snapshot if target_key(state.get("target", {})) in keys]
    if not states and keys:
        return ("empty-snapshot",), f"{user['name']} 队列存在 empty-snapshot 错误\n本轮没有有效库存返回值。"
    unknown = [
        state for state in states
        if state.get("availability", {}).get("kind") == "unknown"
        and state.get("availability", {}).get("reason") != "not_yet_checked"
    ]
    complete = len(states) == len(keys) and all(
        state.get("availability", {}).get("kind") in {"in_stock", "out_of_stock"}
        for state in states
    )
    if complete:
        return None, None
    errors = sorted({
        (
            state.get("availability", {}).get("reason", "unhealthy-cycle"),
            state.get("availability", {}).get("detail", "无有效返回值"),
        )
        for state in unknown
    }) or [("unhealthy-cycle", "本轮未确认所有目标都取得有效库存返回值")]
    stores = sorted({state.get("target", {}).get("storeTitle", "未知门店") for state in unknown})
    shown = "、".join(stores[:4]) + (f" 等 {len(stores)} 家" if len(stores) > 4 else "")
    detail = "；".join(f"{reason}: {message}" for reason, message in errors)
    return tuple(errors), (
        f"{user['name']} 队列存在 {detail} 错误\n"
        f"{len(unknown) or len(states)} 个目标没有有效库存返回值：{shown or '未知门店'}\n这不代表无货。"
    )


def update_runtime(**changes):
    with runtime_lock:
        runtime.update(changes)


def status_payload():
    config = load_config()
    with runtime_lock:
        status = json.loads(json.dumps(runtime, ensure_ascii=False))
    states = {target_key(row.get("target", {})): row for row in status["snapshot"]}
    groups = {}
    for target in all_targets(config):
        state = states.get(target_key(target), {})
        enriched = state.get("target", target)
        scope = enriched.get("pickupLocation") or target["storeNumber"]
        key = (target["locale"], scope)
        group = groups.setdefault(key, {
            "id": "|".join(key),
            "locale": target["locale"],
            "partNumbers": [],
            "scope": scope,
            "stores": [],
            "subscribers": [],
            "lastCheckedMs": None,
        })
        if target["partNumber"] not in group["partNumbers"]:
            group["partNumbers"].append(target["partNumber"])
        if target["storeNumber"] not in group["stores"]:
            group["stores"].append(target["storeNumber"])
        checked = state.get("lastCheckedMs")
        if checked and (not group["lastCheckedMs"] or checked > group["lastCheckedMs"]):
            group["lastCheckedMs"] = checked
        for user in config["users"]:
            if user["name"] not in group["subscribers"] and any(
                target_key(candidate) == target_key(target) for candidate in user["targets"]
            ):
                group["subscribers"].append(user["name"])
    group_count = status.get("queryGroupCount") or len(groups)
    for group in groups.values():
        checked = group["lastCheckedMs"]
        group["nextCheckAtMs"] = checked + group_count * POLL_INTERVAL_SECONDS * 1000 if checked else None
    status.update({
        "vipCount": len(config["users"]),
        "queryGroupCount": group_count,
        "queryGroups": list(groups.values()),
    })
    return status


def request_reload():
    global current_process
    reload_event.set()
    with runtime_lock:
        process = current_process
    if process and process.poll() is None:
        process.terminate()


def handle_event(event, issues, stocked, metrics=None):
    event_type = event.get("type")
    changes = {"lastEvent": event_type, "lastError": None}
    if isinstance(metrics, dict):
        changes["metrics"] = metrics
    update_runtime(**changes)
    if event_type == "inStock":
        config = load_config()
        target = event.get("state", {}).get("target", {})
        key = target_key(target)
        if key in stocked:
            return
        stocked.add(key)
        for user in config["users"]:
            if any(target_key(candidate) == target_key(target) for candidate in user["targets"]):
                bark_push(
                    user,
                    f'Apple 有货：{target.get("storeTitle", target.get("storeNumber", "未知门店"))}',
                    f'{target.get("productName", target.get("partNumber", "未知型号"))}\nSKU：{target.get("partNumber", "?")}',
                    BUY_URLS.get(target.get("locale"), "https://www.apple.com/"),
                )
    elif event_type == "cycleComplete":
        config = load_config()
        snapshot = event.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, list) else []
        update_runtime(
            snapshot=snapshot,
            lastEvent="cycleComplete",
            lastError=None,
            queryGroupCount=event.get("queryGroupCount", 0),
        )
        for state in snapshot:
            if state.get("availability", {}).get("kind") == "out_of_stock":
                stocked.discard(target_key(state.get("target", {})))
        for user in config["users"]:
            signature, body = issue_summary(user, snapshot)
            if signature and issues.get(user["id"]) != signature:
                telegram_push(body)
            issues[user["id"]] = signature


def watcher_loop():
    global current_process
    issues = {}
    stocked = set()
    process_issue = None
    while True:
        try:
            config = load_config()
            targets = all_targets(config)
            if not targets:
                update_runtime(running=False, pid=None, snapshot=[], lastError=None)
                reload_event.wait()
                reload_event.clear()
                continue
            TARGETS_FILE.write_text(
                json.dumps(targets, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            rotation = load_rotation()
            phase_ends = rotation["startedAt"] + ROTATION_SECONDS
            timeout = max(1, math.ceil(phase_ends - time.time()))
            interval = POLL_INTERVAL_SECONDS
            command, stdin_text = watcher_command(rotation["activeNode"], interval, timeout)
            child_env = os.environ.copy()
            child_env["APW_BLOCK_STATE_FILE"] = str(BLOCK_STATE_FILE)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=child_env,
            )
            if stdin_text is not None:
                try:
                    process.stdin.write(stdin_text)
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
            with runtime_lock:
                current_process = process
                runtime.update(
                    running=True,
                    pid=process.pid,
                    generation=runtime["generation"] + 1,
                    activeNode=rotation["activeNode"],
                    nodeCycle=0,
                    phaseEndsAtMs=int(phase_ends * 1000),
                )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                try:
                    payload = json.loads(line)
                    event = payload.get("event")
                    if payload.get("error", {}).get("kind") == "timeout":
                        continue
                    if not isinstance(event, dict):
                        raise ValueError("missing event")
                    handle_event(event, issues, stocked, payload.get("metrics"))
                    if event.get("type") == "cycleComplete":
                        process_issue = None
                        with runtime_lock:
                            runtime["nodeCycle"] += 1
                except (json.JSONDecodeError, ValueError, KeyError) as error:
                    update_runtime(lastError=f"apw 输出无法解析：{error}")
            code = process.wait()
            with runtime_lock:
                current_process = None
                runtime.update(running=False, pid=None)
            if reload_event.is_set():
                reload_event.clear()
                issues.clear()
                continue
            if code == 4:
                save_rotation({
                    "activeNode": NODES[(NODES.index(rotation["activeNode"]) + 1) % len(NODES)],
                    "startedAt": time.time(),
                })
                process_issue = None
                continue
            message = f"监控进程退出码 {code}；15 秒后自动重启"
            update_runtime(lastError=message)
            if process_issue != message:
                telegram_push(f"VIP 队列存在 process-exit 错误\n{message}")
                process_issue = message
        except Exception as error:
            message = f"监控主循环错误：{error}"
            update_runtime(running=False, pid=None, lastError=message)
            if process_issue != message:
                telegram_push(f"VIP 队列存在 watcher-loop 错误\n{message}")
                process_issue = message
        time.sleep(15)


class Handler(BaseHTTPRequestHandler):
    server_version = "AppleWatcherAdmin/1"

    def log_message(self, template, *args):
        print(f"管理页：{template % args}", flush=True)

    def allowed(self):
        host = self.headers.get("Host", "").split(":", 1)[0]
        return host in {"127.0.0.1", "localhost"}

    def send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        self.send_bytes(
            status,
            "application/json; charset=utf-8",
            json.dumps(value, ensure_ascii=False).encode(),
        )

    def read_json(self):
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 1_048_576:
            raise ValueError("请求大小无效")
        return json.loads(self.rfile.read(size))

    def do_GET(self):
        if not self.allowed():
            return self.send_json(403, {"error": "只允许本机或 SSH 隧道访问"})
        if self.path == "/":
            return self.send_bytes(200, "text/html; charset=utf-8", ADMIN_FILE.read_bytes())
        if self.path == "/api/config":
            return self.send_json(200, load_config())
        if self.path == "/api/status":
            return self.send_json(200, status_payload())
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.allowed():
            return self.send_json(403, {"error": "只允许本机或 SSH 隧道访问"})
        origin = self.headers.get("Origin")
        if origin not in {None, "http://127.0.0.1:1420", "http://localhost:1420"}:
            return self.send_json(403, {"error": "来源无效"})
        try:
            value = self.read_json()
            if self.path == "/api/config":
                return self.send_json(200, save_config(value))
            if self.path == "/api/test-bark":
                user_id = str(value.get("userId", ""))
                user = next((item for item in load_config()["users"] if item["id"] == user_id), None)
                if not user:
                    raise ValueError("找不到该用户")
                if not bark_push(user, f"{user['name']} 提醒测试", "Bark 连接与提醒方式已生效"):
                    raise ValueError("Bark 测试失败，请查看服务日志")
                return self.send_json(200, {"ok": True})
            return self.send_json(404, {"error": "not found"})
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            return self.send_json(400, {"error": str(error)})


def self_test():
    example = validate_config({
        "intervalSeconds": 30,
        "users": [
            {
                "id": "vip-my",
                "name": "VIP-MY",
                "barkUrl": "https://api.day.app/key?icon=https%3A%2F%2Fx.test%2Fa.png",
                "alertMode": "critical",
                "targets": [
                    {"locale": "en_MY", "storeNumber": "R742", "partNumber": "MJXU4X/A"},
                    {"locale": "en_MY", "storeNumber": "R742", "partNumber": "MJXU4X/A"},
                ],
            }
        ],
    })
    assert example["intervalSeconds"] == 30
    assert len(all_targets(example)) == 1
    url = make_bark_url(example["users"][0]["barkUrl"], "有货 / test", "银色", "critical", "VIP-MY")
    assert "%2F" in url and "level=critical" in url and "call=1" in url and "sound=minuet" in url and "icon=" in url
    signature, body = issue_summary(
        example["users"][0],
        [{
            "target": example["users"][0]["targets"][0],
            "availability": {"kind": "unknown", "reason": "blocked", "detail": "HTTP 541"},
        }],
    )
    assert signature == (("blocked", "HTTP 541"),) and "VIP-MY 队列" in body
    too_many = json.loads(json.dumps(example))
    too_many["users"][0]["targets"].extend([
        {"locale": "en_MY", "storeNumber": "R742", "partNumber": "THIRD/A"},
        {"locale": "en_MY", "storeNumber": "R742", "partNumber": "FOURTH/A"},
    ])
    try:
        validate_config(too_many)
        raise AssertionError("VIP 机型上限未生效")
    except ValueError as error:
        assert "最多只能监控 2 个机型" in str(error)
    original_data_dir = globals()["DATA_DIR"]
    original_rotation_file = globals()["ROTATION_FILE"]
    original_nodes = globals()["NODES"]
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        globals()["DATA_DIR"] = Path(directory)
        globals()["ROTATION_FILE"] = Path(directory) / "rotation.json"
        globals()["NODES"] = ("primary", "secondary")
        first = load_rotation(1_000_000)
        assert first["activeNode"] == "primary"
        second = load_rotation(1_000_000 + ROTATION_SECONDS)
        assert second["activeNode"] == "secondary"
        third = load_rotation(1_000_000 + ROTATION_SECONDS * 2)
        assert third["activeNode"] == "primary"
        command, stdin_text = watcher_command("primary", POLL_INTERVAL_SECONDS, ROTATION_SECONDS)
        assert command[-4:] == ["--interval", "60", "--timeout", "1200"]
        assert stdin_text is None
    globals()["DATA_DIR"] = original_data_dir
    globals()["ROTATION_FILE"] = original_rotation_file
    globals()["NODES"] = original_nodes


def main():
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        return 0
    if sys.argv[1:] == ["--test"]:
        config = load_config()
        user = next((item for item in config["users"] if item["barkUrl"]), None)
        bark_ok = bool(user) and bark_push(user, "VIP 库存监控已配置", "管理页与热更新已启用")
        telegram_ok = telegram_push("VIP 队列 Telegram 错误提醒已配置")
        return 0 if bark_ok and telegram_ok else 1
    if sys.argv[1:] == ["--migrate-legacy"]:
        migrate_legacy()
        return 0

    threading.Thread(target=watcher_loop, name="watcher", daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 1420), Handler)
    print("管理页监听：http://127.0.0.1:1420（仅本机/SSH 隧道）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        request_reload()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
