#!/usr/bin/env python3
"""もったいないメーター Phase 0: Mac 常駐スクリプト。

    mottainai.py status        残り枠と換算を表示
    mottainai.py tick          取得 → 履歴に追記 → 判定 → 通知（launchd から 5 分おきに呼ぶ）
    mottainai.py install       config と launchd を用意し、ntfy の設定手順を表示
    mottainai.py uninstall     launchd を外す
    mottainai.py test-notify   通知経路のテスト

依存: Python 3 標準ライブラリのみ。認証情報は Claude Code / Codex CLI のものを読むだけ（refresh はしない）。
"""
import base64, glob, json, os, plistlib, secrets, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.expanduser("~/.config/mottainai")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_DIR = os.path.expanduser("~/.local/share/mottainai")
HISTORY_PATH = os.path.join(STATE_DIR, "history.jsonl")
ALERTED_PATH = os.path.join(STATE_DIR, "alerted.json")
LAST_PATH = os.path.join(STATE_DIR, "last.json")
LAUNCHD_LABEL = "jp.orgm.mottainai"
LAUNCHD_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
JST = timezone(timedelta(hours=9))

DEFAULT_CONFIG = {
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "ntfy_data_topic": "",   # iPhone の Web 画面が読むスナップショット用（空なら ntfy_topic + "-data"）
    "macos_notify": True,
    "quiet_hours": [23, 7],
    "ignore_untouched": True,
    "windows": {
        "5h": {"lead_min": 45, "floor_percent": 40},
        "weekly": {"lead_min": 720, "floor_percent": 25},
    },
    "pace": {"enabled": True, "lookback_min": 120, "leftover_floor_percent": 30},
    # 「残り = ○○ 何回分」の比喩。Claude は API 換算 $、Codex はトークン数で 1 単位を定義する。
    "custom_units": [
        {"name": "中くらいの PR", "usd": 3.0, "tokens": 6000000},
        {"name": "コードレビュー", "usd": 0.8, "tokens": 1500000},
    ],
    "claude_code_version": "2.1.269",
}

# API 換算用 $/MTok（input, output）。cache write は input×1.25、cache read は input×0.1 で近似。
CLAUDE_PRICING = [
    ("fable", 10.0, 50.0), ("mythos", 10.0, 50.0), ("opus", 5.0, 25.0),
    ("sonnet-4-6", 3.0, 15.0), ("sonnet", 2.0, 10.0), ("haiku", 1.0, 5.0),
]


# ---------- util ----------
def now_ts():
    return time.time()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    user = load_json(CONFIG_PATH, {})
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg


def fmt_dur(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}日{h}時間"
    if h:
        return f"{h}時間{m:02d}分"
    return f"{m}分"


def fmt_reset(ts):
    dt = datetime.fromtimestamp(ts, JST)
    wd = "月火水木金土日"[dt.weekday()]
    if dt.date() == datetime.now(JST).date():
        return dt.strftime("%H:%M")
    return f"{wd} {dt.strftime('%H:%M')}"


def http_json(url, headers, data=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r) if r.length != 0 else {}


# ---------- fetch ----------
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")


def load_claude_credentials():
    """Mac は Keychain、Windows / Linux は ~/.claude/.credentials.json。Mac でも Keychain に無ければファイルを見る。"""
    if IS_MAC:
        raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                             capture_output=True, text=True)
        if raw.returncode == 0 and raw.stdout.strip():
            return json.loads(raw.stdout.strip())
    path = os.path.expanduser("~/.claude/.credentials.json")
    cred = load_json(path, None)
    if not cred:
        raise RuntimeError("Claude Code のログイン情報が見つからない。claude を一度起動してログインしてください")
    return cred


CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def save_claude_credentials(full):
    """Keychain（Mac）または ~/.claude/.credentials.json に書き戻す。CLI と同じ置き場所に置くのが要点。"""
    payload = json.dumps(full, separators=(",", ":"))
    if IS_MAC:
        acct = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
        r = subprocess.run(["security", "add-generic-password", "-U", "-a", acct,
                            "-s", "Claude Code-credentials", "-w", payload],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return "keychain"
        print(f"Keychain 書き戻し失敗: {r.stderr.strip()}", file=sys.stderr)
    path = os.path.expanduser("~/.claude/.credentials.json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(payload)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return "file"


def refresh_claude(full, cfg):
    """access token を更新して保存し、更新後の claudeAiOauth を返す。"""
    cred = full["claudeAiOauth"]
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": f"claude-cli/{cfg['claude_code_version']} (external, cli)"}
    _, res = http_json("https://api.anthropic.com/v1/oauth/token", headers, {
        "grant_type": "refresh_token", "refresh_token": cred["refreshToken"], "client_id": CLAUDE_CLIENT_ID})
    cred["accessToken"] = res["access_token"]
    if res.get("refresh_token"):
        cred["refreshToken"] = res["refresh_token"]
    cred["expiresAt"] = int((now_ts() + res.get("expires_in", 28800)) * 1000)
    where = save_claude_credentials(full)
    print(f"Claude トークンを更新（保存先: {where}）", file=sys.stderr)
    return cred


def fetch_claude(cfg):
    full = load_claude_credentials()
    cred = full["claudeAiOauth"]
    if cred.get("expiresAt") and cred["expiresAt"] / 1000 < now_ts() + 300:
        full = load_claude_credentials()  # CLI が直前に更新していれば取り合いを避ける
        cred = full["claudeAiOauth"]
        if cred["expiresAt"] / 1000 < now_ts() + 300:
            try:
                cred = refresh_claude(full, cfg)
            except (urllib.error.HTTPError, KeyError) as e:
                raise RuntimeError(f"Claude のトークン更新に失敗（{e}）。claude を一度起動してください")
    headers = {
        "Authorization": f"Bearer {cred['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-code/{cfg['claude_code_version']}",
        "Accept": "application/json",
    }
    _, data = http_json("https://api.anthropic.com/api/oauth/usage", headers)
    wins = []
    for lim in data.get("limits") or []:
        rs = lim.get("resets_at")
        ts = datetime.fromisoformat(rs).timestamp() if rs else None
        kind = "5h" if lim["kind"] == "session" else "weekly"
        if lim["kind"] == "weekly_scoped":
            name = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "scoped"
            wid, label = f"claude.weekly.{name.lower()}", f"Claude {name} 週"
        elif lim["kind"] == "weekly_all":
            wid, label = "claude.weekly", "Claude 週"
        else:
            wid, label = "claude.5h", "Claude 5h"
        wins.append(dict(provider="claude", id=wid, label=label, kind=kind,
                         used=float(lim.get("percent") or 0), resets_at=ts))
    if not wins:  # 古い形
        for key, wid, label, kind in (("five_hour", "claude.5h", "Claude 5h", "5h"),
                                      ("seven_day", "claude.weekly", "Claude 週", "weekly")):
            w = data.get(key) or {}
            rs = w.get("resets_at")
            wins.append(dict(provider="claude", id=wid, label=label, kind=kind,
                             used=float(w.get("utilization") or 0),
                             resets_at=datetime.fromisoformat(rs).timestamp() if rs else None))
    return wins


def _jwt_claims(tok):
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


CODEX_AUTH_PATH = os.path.expanduser("~/.codex/auth.json")


def refresh_codex(auth):
    """access token を更新して auth.json に書き戻す（Codex CLI と同じ形）。"""
    t = auth["tokens"]
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": "mottainai-meter/0.1"}
    _, res = http_json("https://auth.openai.com/oauth/token", headers, {
        "client_id": CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
    t["access_token"] = res["access_token"]
    if res.get("id_token"):
        t["id_token"] = res["id_token"]
    if res.get("refresh_token"):
        t["refresh_token"] = res["refresh_token"]
    auth["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    tmp = CODEX_AUTH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(auth, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CODEX_AUTH_PATH)
    print("Codex トークンを更新（auth.json に書き戻し）", file=sys.stderr)
    return auth


def fetch_codex(cfg):
    auth = load_json(CODEX_AUTH_PATH, None)
    if not auth or "tokens" not in auth:
        raise RuntimeError("~/.codex/auth.json が無い。codex を一度起動してログインしてください")
    t = auth["tokens"]
    if _jwt_claims(t["access_token"]).get("exp", 0) < now_ts() + 300:
        auth = load_json(CODEX_AUTH_PATH, auth)  # CLI が直前に更新していれば取り合いを避ける
        t = auth["tokens"]
        if _jwt_claims(t["access_token"]).get("exp", 0) < now_ts() + 300:
            try:
                auth = refresh_codex(auth)
                t = auth["tokens"]
            except (urllib.error.HTTPError, KeyError) as e:
                raise RuntimeError(f"Codex のトークン更新に失敗（{e}）。codex を一度起動してください")
    headers = {
        "Authorization": f"Bearer {t['access_token']}",
        "ChatGPT-Account-Id": t.get("account_id") or "",
        "User-Agent": "mottainai-meter/0.1",
        "Accept": "application/json",
    }
    _, data = http_json("https://chatgpt.com/backend-api/wham/usage", headers)
    wins = []

    def add(prefix, label_prefix, rl):
        for key in ("primary_window", "secondary_window"):
            w = (rl or {}).get(key)
            if not w:
                continue
            secs = w.get("limit_window_seconds") or 0
            kind = "5h" if secs <= 6 * 3600 else "weekly"
            suffix = "5h" if kind == "5h" else "週"
            wins.append(dict(provider="codex", id=f"{prefix}.{kind}", label=f"{label_prefix} {suffix}", kind=kind,
                             used=float(w.get("used_percent") or 0), resets_at=w.get("reset_at")))

    add("codex", "Codex", data.get("rate_limit"))
    for extra in data.get("additional_rate_limits") or []:
        name = extra.get("limit_name") or "extra"
        add(f"codex.{name.lower()}", f"Codex {name}", extra.get("rate_limit"))
    return wins, data.get("plan_type")


# ---------- history / pace ----------
def append_history(wins, errors):
    os.makedirs(STATE_DIR, exist_ok=True)
    rec = {"t": now_ts(), "w": {w["id"]: [w["used"], w["resets_at"]] for w in wins}, "err": errors}
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_history(since_ts):
    out = []
    try:
        with open(HISTORY_PATH) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r["t"] >= since_ts:
                    out.append(r)
    except FileNotFoundError:
        pass
    return out


def pace_per_hour(wid, resets_at, lookback_min):
    """直近 lookback 分の同一窓サンプルから消費ペース %/h を線形回帰で出す。"""
    pts = [(r["t"], r["w"][wid][0]) for r in read_history(now_ts() - lookback_min * 60)
           if wid in r["w"] and r["w"][wid][1] == resets_at]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx == 0:
        return None
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx  # %/sec
    return max(0.0, slope * 3600)


# ---------- 換算 ----------
def claude_activity(since_ts):
    """Claude Code のローカルログから、since 以降の API 換算コストとターン数を出す。"""
    cost, turns, seen = 0.0, 0, set()
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        try:
            if os.path.getmtime(path) < since_ts:
                continue
            with open(path) as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    ts = d.get("timestamp")
                    if not ts or datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < since_ts:
                        continue
                    m = d.get("message") or {}
                    key = (m.get("id"), d.get("requestId"))
                    if key in seen:
                        continue
                    seen.add(key)
                    u = m.get("usage") or {}
                    model = m.get("model") or ""
                    pin, pout = 5.0, 25.0
                    for sub, a, b in CLAUDE_PRICING:
                        if sub in model:
                            pin, pout = a, b
                            break
                    cost += (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout
                             + u.get("cache_creation_input_tokens", 0) * pin * 1.25
                             + u.get("cache_read_input_tokens", 0) * pin * 0.1) / 1e6
                    turns += 1
        except OSError:
            continue
    return cost, turns


def codex_activity(since_ts):
    """Codex のローカルログから、since 以降のトークン数とターン数を出す。"""
    tokens, turns = 0, 0
    for path in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < since_ts:
                continue
            with open(path) as f:
                for line in f:
                    if '"token_count"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = d.get("timestamp")
                    if not ts or datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < since_ts:
                        continue
                    last = ((d.get("payload") or {}).get("info") or {}).get("last_token_usage") or {}
                    tokens += last.get("total_tokens", 0)
                    turns += 1
        except OSError:
            continue
    return tokens, turns


def codex_tokens_per_percent(kind, days=14):
    """Codex ログの token_count イベントには使用率も入っているので、
    トークン増分 ÷ 使用率増分 で「1% あたり何トークンか」を直接較正する。"""
    key = "primary" if kind == "weekly" else "primary"
    events = []
    since = now_ts() - days * 86400
    for path in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < since:
                continue
            with open(path) as f:
                for line in f:
                    if '"token_count"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    p = d.get("payload") or {}
                    info, rl = p.get("info") or {}, p.get("rate_limits") or {}
                    win = rl.get(key) or {}
                    if not win or "used_percent" not in win:
                        continue
                    mins = win.get("window_minutes") or 0
                    if (mins <= 360) != (kind == "5h"):
                        continue
                    ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                    events.append((ts, win["used_percent"], win.get("resets_at"),
                                   (info.get("last_token_usage") or {}).get("total_tokens", 0)))
        except (OSError, KeyError, ValueError):
            continue
    events.sort()
    d_tok, d_pct, prev = 0, 0.0, None
    for ts, pct, rs, tok in events:
        if prev and prev[2] == rs and pct >= prev[1]:
            d_tok += tok
            d_pct += pct - prev[1]
        prev = (ts, pct, rs)
    return d_tok / d_pct if d_pct > 0 else None


def conversions(w, cfg):
    """残り % を、いろいろな単位に換算した文字列のリスト。"""
    out = []
    remain = 100 - w["used"]
    if w["resets_at"] is None:
        return out
    span = 5 * 3600 if w["kind"] == "5h" else 7 * 86400
    since = w["resets_at"] - span
    usd_per_pct = tok_per_pct = None
    if w["provider"] == "claude" and w["used"] > 0:
        cost, turns = claude_activity(since)
        if cost > 0:
            usd_per_pct = cost / w["used"]
            out.append(f"API 換算 約 ${usd_per_pct * remain:,.0f}（この窓で ${cost:,.0f} 使って {w['used']:.0f}%）")
        if turns > 0:
            out.append(f"いまのペースの応答 約 {turns / w['used'] * remain:,.0f} 回分")
    elif w["provider"] == "codex":
        tok_per_pct = codex_tokens_per_percent(w["kind"])
        if tok_per_pct is None and w["used"] > 0:
            tokens, _ = codex_activity(since)
            tok_per_pct = tokens / w["used"] if tokens else None
        if tok_per_pct:
            out.append(f"約 {tok_per_pct * remain / 1e6:,.0f}M トークン分（ログから較正: 1% ≈ {tok_per_pct / 1e6:,.1f}M）")
    pace = pace_per_hour(w["id"], w["resets_at"], 24 * 60)
    if pace:
        out.append(f"直近の消費ペースであと {fmt_dur(remain / pace * 3600)}")
    for u in cfg.get("custom_units") or []:
        if usd_per_pct and u.get("usd"):
            out.append(f"{u['name']} 約 {usd_per_pct * remain / u['usd']:.1f} 回分")
        elif tok_per_pct and u.get("tokens"):
            out.append(f"{u['name']} 約 {tok_per_pct * remain / u['tokens']:.1f} 回分")
    return out


# ---------- 判定 ----------
def in_quiet_hours(cfg):
    start, end = cfg["quiet_hours"]
    h = datetime.now(JST).hour
    return (start <= h or h < end) if start > end else (start <= h < end)


def judge(w, cfg):
    """通知すべきなら (title, body) を返す。"""
    if w["resets_at"] is None:
        return None
    if cfg["ignore_untouched"] and w["used"] <= 0:
        return None
    remain = 100 - w["used"]
    ttr = w["resets_at"] - now_ts()
    rule = cfg["windows"][w["kind"]]
    lead = rule["lead_min"] * 60
    reasons = []
    if ttr <= lead and remain >= rule["floor_percent"]:
        reasons.append("time")
    pc = cfg["pace"]
    leftover = None
    if pc["enabled"] and ttr <= 2 * lead:
        pace = pace_per_hour(w["id"], w["resets_at"], pc["lookback_min"])
        if pace is not None:
            leftover = remain - pace * ttr / 3600
            if leftover >= pc["leftover_floor_percent"]:
                reasons.append("pace")
    if not reasons:
        return None
    title = f"{w['label']}枠が {fmt_dur(ttr)} で戻ります"
    if "pace" in reasons and "time" not in reasons:
        title = f"{w['label']}枠、今のペースだと {leftover:.0f}% 余ります"
    conv = conversions(w, cfg)
    body = f"残り {remain:.0f}%。" + ("いま使えばそのぶん得です。" if "time" in reasons else f"リセットは {fmt_reset(w['resets_at'])}。")
    if conv:
        body += " " + conv[0]
    return title, body


# ---------- 通知 ----------
def notify(cfg, title, body):
    sent = []
    if cfg.get("ntfy_topic"):
        try:
            http_json(cfg["ntfy_server"], {}, {"topic": cfg["ntfy_topic"], "title": title,
                                               "message": body, "priority": 4, "tags": ["hourglass"]})
            sent.append("ntfy")
        except (urllib.error.URLError, OSError) as e:
            print(f"ntfy 失敗: {e}", file=sys.stderr)
    if cfg.get("macos_notify") and IS_MAC:
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e",
                        f'display notification "{esc(body)}" with title "{esc(title)}"'],
                       capture_output=True)
        sent.append("macos")
    elif cfg.get("macos_notify") and IS_WIN:
        ps = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
            "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$t = $x.GetElementsByTagName('text'); $t.Item(0).AppendChild($x.CreateTextNode($env:MT_TITLE)) | Out-Null;"
            "$t.Item(1).AppendChild($x.CreateTextNode($env:MT_BODY)) | Out-Null;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('mottainai').Show([Windows.UI.Notifications.ToastNotification]::new($x))"
        )
        env = {**os.environ, "MT_TITLE": title, "MT_BODY": body}
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, env=env)
        if r.returncode == 0:
            sent.append("windows")
    return sent


def data_topic(cfg):
    return cfg.get("ntfy_data_topic") or (cfg["ntfy_topic"] + "-data" if cfg.get("ntfy_topic") else "")


def publish_snapshot(cfg, wins, errors, plan):
    """iPhone の Web 画面向けに、最新状態を ntfy のデータ用トピックへ 1 通流す（4KB 以内）。"""
    topic = data_topic(cfg)
    if not topic:
        return False
    snap = {
        "t": int(now_ts()), "plan": plan, "err": errors,
        "cfg": {k: [v["lead_min"], v["floor_percent"]] for k, v in cfg["windows"].items()},
        "quiet": cfg["quiet_hours"],
        "w": [{"id": w["id"], "label": w["label"], "kind": w["kind"], "used": round(w["used"], 1),
               "reset": int(w["resets_at"]) if w["resets_at"] else None,
               "conv": conversions(w, cfg)[:4],
               "pace": (lambda p: round(p, 2) if p else None)(
                   pace_per_hour(w["id"], w["resets_at"], cfg["pace"]["lookback_min"]) if w["resets_at"] else None)}
              for w in wins],
    }
    body = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
    try:
        http_json(cfg["ntfy_server"], {}, {"topic": topic, "message": body, "priority": 1,
                                           "tags": ["snapshot"], "title": "snapshot"})
        return True
    except (urllib.error.URLError, OSError) as e:
        print(f"snapshot 送信失敗: {e}", file=sys.stderr)
        return False


# ---------- commands ----------
def collect(cfg):
    wins, errors, plan = [], {}, None
    try:
        wins += fetch_claude(cfg)
    except Exception as e:  # noqa: BLE001
        errors["claude"] = str(e)
    try:
        cw, plan = fetch_codex(cfg)
        wins += cw
    except Exception as e:  # noqa: BLE001
        errors["codex"] = str(e)
    return wins, errors, plan


def cmd_status(cfg):
    wins, errors, plan = collect(cfg)
    print(f"更新 {datetime.now(JST).strftime('%m/%d %H:%M')}" + (f"  Codex plan: {plan}" if plan else ""))
    for w in wins:
        remain = 100 - w["used"]
        bar = "█" * int(remain // 5) + "░" * (20 - int(remain // 5))
        if w["resets_at"]:
            ttr = w["resets_at"] - now_ts()
            when = f"{fmt_reset(w['resets_at'])} にリセット（あと {fmt_dur(ttr)}）"
        else:
            when = "未開始"
        if cfg["ignore_untouched"] and w["used"] <= 0:
            when += "  ※未使用のため通知対象外"
        print(f"\n{w['label']:<14} {bar} 残り {remain:5.1f}%   {when}")
        for line in conversions(w, cfg):
            print(f"{'':14}   ・{line}")
        fire = judge(w, cfg)
        rule = cfg["windows"][w["kind"]]
        if fire:
            print(f"{'':14}   ▶ 通知条件を満たしています: {fire[0]}")
        elif w["resets_at"]:
            print(f"{'':14}   通知条件: リセット {rule['lead_min']} 分前に残り {rule['floor_percent']}% 以上")
    for k, v in errors.items():
        print(f"\n[{k}] 取得失敗: {v}")
    if in_quiet_hours(cfg):
        print(f"\n（静音時間帯 {cfg['quiet_hours'][0]}:00–{cfg['quiet_hours'][1]}:00 のため通知は出ません）")


def cmd_tick(cfg):
    wins, errors, plan = collect(cfg)
    append_history(wins, errors)
    save_json(LAST_PATH, {"t": now_ts(), "windows": wins, "errors": errors, "plan": plan})
    published = publish_snapshot(cfg, wins, errors, plan)
    alerted = load_json(ALERTED_PATH, {})
    fired = []
    if not in_quiet_hours(cfg):
        for w in wins:
            key = f"{w['id']}@{w['resets_at']}"
            if key in alerted:
                continue
            res = judge(w, cfg)
            if res:
                sent = notify(cfg, *res)
                alerted[key] = {"t": now_ts(), "title": res[0], "sent": sent}
                fired.append(res[0])
    # 古い alerted を掃除
    cutoff = now_ts() - 14 * 86400
    alerted = {k: v for k, v in alerted.items() if v["t"] >= cutoff}
    save_json(ALERTED_PATH, alerted)
    stamp = datetime.now(JST).strftime("%m/%d %H:%M")
    summary = " ".join(f"{w['id']}={100 - w['used']:.0f}%" for w in wins)
    print(f"{stamp} {summary}" + (f" ERR={errors}" if errors else "") + (f" FIRED={fired}" if fired else "")
          + ("" if published else " (snapshot 未送信)"))


def cmd_install(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    user = load_json(CONFIG_PATH, {})
    if not user.get("ntfy_topic"):
        user["ntfy_topic"] = "mottainai-" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
    merged = {**DEFAULT_CONFIG, **user}
    save_json(CONFIG_PATH, merged)
    os.makedirs(STATE_DIR, exist_ok=True)
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        py = pyw if os.path.exists(pyw) else sys.executable
        tr = f'"{py}" "{os.path.abspath(__file__)}" tick'
        r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", "5", "/TN", "mottainai",
                            "/TR", tr], capture_output=True, text=True)
        print(f"config: {CONFIG_PATH}")
        print(f"タスク スケジューラ: mottainai（5 分おき） {'登録済み' if r.returncode == 0 else r.stderr.strip()}")
        print("  確認: schtasks /Query /TN mottainai   手動実行: schtasks /Run /TN mottainai")
        _print_phone_setup(merged)
        return
    if not IS_MAC:
        print(f"config: {CONFIG_PATH}")
        print("常駐登録は Mac (launchd) と Windows (タスク スケジューラ) のみ対応。Linux は cron で `*/5 * * * * python3 mottainai.py tick` を登録してください。")
        _print_phone_setup(merged)
        return
    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [sys.executable, os.path.abspath(__file__), "tick"],
        "StartInterval": 300,
        "RunAtLoad": True,
        "StandardOutPath": os.path.join(STATE_DIR, "tick.log"),
        "StandardErrorPath": os.path.join(STATE_DIR, "tick.err"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"},
    }
    os.makedirs(os.path.dirname(LAUNCHD_PATH), exist_ok=True)
    with open(LAUNCHD_PATH, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "unload", LAUNCHD_PATH], capture_output=True)
    r = subprocess.run(["launchctl", "load", LAUNCHD_PATH], capture_output=True, text=True)
    print(f"config: {CONFIG_PATH}")
    print(f"launchd: {LAUNCHD_PATH} ({'loaded' if r.returncode == 0 else r.stderr.strip()})")
    print(f"ログ: {os.path.join(STATE_DIR, 'tick.log')}")
    _print_phone_setup(merged)


def _print_phone_setup(merged):
    print("\niPhone 側の設定:")
    print("  1. App Store で「ntfy」をインストール")
    print(f"  2. Subscribe to topic → 「{merged['ntfy_topic']}」を入力")
    print(f"  3. {os.path.abspath(__file__)} test-notify で届くか確認")
    print(f"  4. 残量画面: https://tomokick.github.io/mottainai-meter/#t={data_topic(merged)}")
    print("     Safari で開いて「ホーム画面に追加」")


def cmd_uninstall(cfg):
    if IS_WIN:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", "mottainai"], capture_output=True)
        print("タスク スケジューラから外しました。config と履歴は残しています。")
        return
    subprocess.run(["launchctl", "unload", LAUNCHD_PATH], capture_output=True)
    if os.path.exists(LAUNCHD_PATH):
        os.remove(LAUNCHD_PATH)
    print("launchd を外しました。config と履歴は残しています。")


def cmd_test_notify(cfg):
    sent = notify(cfg, "もったいないメーター", "通知経路のテストです。これが届けば準備完了。")
    print(f"送信: {sent or 'なし（ntfy_topic 未設定、macos_notify 無効）'}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    cfg = load_config()
    {"status": cmd_status, "tick": cmd_tick, "install": cmd_install,
     "uninstall": cmd_uninstall, "test-notify": cmd_test_notify}.get(cmd, lambda c: print(__doc__))(cfg)


if __name__ == "__main__":
    main()
