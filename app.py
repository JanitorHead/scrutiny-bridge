#!/usr/bin/env python3
"""
scrutiny-bridge — a tiny notification bridge for Scrutiny (https://github.com/AnalogJ/scrutiny).

Scrutiny can only enable/disable notifications globally: it has no way to silence a single
disk or a single SMART attribute you have already reviewed and decided is a benign, stable
baseline (e.g. an old "Reported Uncorrectable Errors" count frozen in the drive firmware).
That means one known-benign disk keeps you from trusting the alert channel for the rest.

This bridge sits between Scrutiny's webhook and Telegram. On every notification it re-reads
the current SMART state from Scrutiny's own API, drops any alert whose ONLY problem is a
per-disk / per-attribute baseline you have whitelisted, and forwards everything else as a
readable Telegram message (model, serial, attribute name, raw value, trend, criticality).

Standard library only — no pip install, no build step.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---------------------------------------------------------------- configuration
def _env(name, default):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


SCRUTINY_API = _env("SCRUTINY_API", "http://scrutiny:8080/api").rstrip("/")
TG_TOKEN_FILE = _env("TG_TOKEN_FILE", "/app/telegram.token")
TG_TOKEN_ENV = _env("TG_TOKEN", "")           # optional: token straight from env
TG_CHAT = _env("TG_CHAT", "")                 # e.g. -1001234567890
TG_THREAD = _env("TG_THREAD", "")             # forum topic id (optional)
PORT = int(_env("PORT", "8091"))
HOST_LABEL = _env("HOST_LABEL", "")           # shown in the message header (optional)

# Whitelisted baselines: { "<wwn>": { "<attr_id>": <max_raw_value> | null } }
# An attribute flagged by Scrutiny is suppressed only if its raw value is <= the baseline.
# Anything new, or a baselined value that has grown, still gets through.
# A null baseline ignores the attribute's raw value entirely — for rolling counters whose
# raw is meaningless noise (e.g. 195 "Hardware ECC Recovered" on Seagate drives).
try:
    BASELINES = json.loads(_env("BASELINES", "{}"))
except json.JSONDecodeError:
    BASELINES = {}
    print("[bridge] WARN: BASELINES is not valid JSON, ignoring", flush=True)


def log(*a):
    print("[bridge]", *a, flush=True)


# ------------------------------------------------------------------- helpers
def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def read_token():
    if TG_TOKEN_ENV:
        return TG_TOKEN_ENV.strip()
    try:
        with open(TG_TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        log("ERROR: cannot read Telegram token:", e)
        return ""


def send_telegram(text):
    token = read_token()
    if not token or not TG_CHAT:
        log("ERROR: missing Telegram token or chat id, cannot send")
        return False
    payload = {
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if TG_THREAD:
        payload["message_thread_id"] = int(TG_THREAD)
    data = json.dumps(payload).encode("utf-8")
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        log("ERROR: Telegram HTTP", e.code, e.read()[:200])
    except Exception as e:  # noqa: BLE001
        log("ERROR: Telegram send failed:", e)
    return False


def human_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.0f %s" % (n, unit)
        n /= 1024
    return "%.1f TB" % n


def esc(s):
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# ------------------------------------------------------------------- core logic
def looks_like_test(raw_body, parsed):
    # Scrutiny sends its notification as a plain-text message via the generic webhook.
    # Its test is marked "TEST NOTIFICATION" / failure type "EmailTest"; real failures use
    # types like "SmartFailure". Match the specific test markers so a real failure whose
    # text happens to contain "test" is never misclassified as a test.
    body = raw_body.lower()
    if "test notification" in body or "emailtest" in body:
        return True
    if isinstance(parsed, dict):
        for key in ("failure_type", "subject", "title"):
            if "test" in str(parsed.get(key, "")).lower():
                return True
    return False


# Scrutiny per-attribute status is a bitfield:
#   0 = passed, 1 = failed_smart, 2 = warning_scrutiny, 4 = failed_scrutiny
# We alert on failures (1|4) only. A pure warning (2) — e.g. "could not determine
# failure rate" — is not actionable and matches Scrutiny's default notify_level "fail".
FAIL_BITS = 1 | 4


def attr_failing(status):
    try:
        return (int(status) & FAIL_BITS) != 0
    except (TypeError, ValueError):
        return False


def attr_is_baselined(wwn, attr_id, raw_value):
    """True if this flagged attribute is within a reviewed baseline (i.e. suppress it)."""
    dev = BASELINES.get(wwn) or {}
    if str(attr_id) not in dev:
        return False
    baseline = dev[str(attr_id)]
    if baseline is None:
        return True  # reviewed: raw is a meaningless rolling counter -> always suppress
    try:
        return float(raw_value) <= float(baseline)
    except (TypeError, ValueError):
        return False


def collect_reportable():
    """
    Read current state from Scrutiny. Return a list of dicts describing disks that have at
    least one flagged attribute NOT covered by a reviewed baseline. Empty list => nothing to
    report (all quiet, or only whitelisted baselines are flagged).
    """
    summary = _get_json(SCRUTINY_API + "/summary")
    devices = ((summary.get("data") or {}).get("summary")) or {}
    reportable = []

    for uuid, entry in devices.items():
        dev = entry.get("device") or {}
        if int(dev.get("device_status", 0) or 0) == 0:
            continue  # healthy per Scrutiny

        try:
            details = _get_json(SCRUTINY_API + "/device/%s/details" % uuid)
        except Exception as e:  # noqa: BLE001
            log("WARN: cannot fetch details for", uuid, e)
            # can't verify -> better to report than to hide a real failure
            reportable.append({"device": dev, "attrs": [], "unverified": True})
            continue

        data = details.get("data") or {}
        meta = details.get("metadata") or {}
        results = data.get("smart_results") or []
        attrs = (results[0].get("attrs") if results else {}) or {}
        ddev = data.get("device") or dev
        wwn = ddev.get("wwn") or dev.get("wwn") or ""

        flagged = []
        for attr_id, a in attrs.items():
            if not attr_failing(a.get("status", 0)):
                continue  # passed, or a non-actionable warning
            raw = a.get("raw_value")
            if attr_is_baselined(wwn, attr_id, raw):
                continue  # reviewed & stable -> suppress
            m = meta.get(str(attr_id)) or {}
            baseline = (BASELINES.get(wwn) or {}).get(str(attr_id))
            flagged.append({
                "id": attr_id,
                "name": m.get("display_name") or ("Attribute %s" % attr_id),
                "critical": bool(m.get("critical")),
                "raw": raw,
                "raw_string": a.get("raw_string"),
                "baseline": baseline,
                "description": m.get("description") or "",
            })

        if flagged:
            reportable.append({"device": ddev, "attrs": flagged, "unverified": False})
        else:
            log("suppressed: %s (%s) — only whitelisted baselines flagged"
                % (ddev.get("device_name"), wwn))

    return reportable


def build_message(reportable):
    head = "🔴 <b>Alerta SMART</b>"
    if HOST_LABEL:
        head += " · " + esc(HOST_LABEL)
    lines = [head, ""]
    for item in reportable:
        d = item["device"]
        name = esc(d.get("device_name", "?"))
        model = esc(d.get("model_name", "?"))
        serial = esc(d.get("serial_number", "?"))
        cap = human_bytes(d.get("capacity"))
        lines.append("💽 <b>%s</b> — %s · SN %s · %s" % (name, model, serial, cap))
        if item.get("unverified"):
            lines.append("   ⚠️ Scrutiny la marca con fallo (no pude leer los atributos).")
        for a in item["attrs"]:
            crit = " ⚠️crítico" if a["critical"] else ""
            trend = ""
            if a.get("baseline") is not None:
                trend = " (baseline revisado %s → ahora %s, ha subido)" % (
                    esc(a["baseline"]), esc(a.get("raw")))
            lines.append("   • <b>%s %s</b>%s: valor bruto <b>%s</b>%s" % (
                esc(a["id"]), esc(a["name"]), crit,
                esc(a.get("raw_string") or a.get("raw")), trend))
        lines.append("")
    lines.append("👉 Revisa el disco en Scrutiny. Si aparecen sectores pendientes "
                 "(197/198) o el contador sigue subiendo, planifica el reemplazo.")
    return "\n".join(lines).strip()


def handle_notification(raw_body):
    try:
        parsed = json.loads(raw_body) if raw_body.strip() else {}
    except json.JSONDecodeError:
        parsed = {}

    if looks_like_test(raw_body, parsed):
        log("test notification received")
        ok = send_telegram(
            "✅ <b>scrutiny-bridge</b> operativo.\n"
            "Prueba recibida y reenviada correctamente. "
            "Las alertas SMART reales llegarán por aquí (filtrando los baselines revisados)."
        )
        return "test", ok

    reportable = collect_reportable()
    if not reportable:
        log("nothing reportable (all quiet or only whitelisted baselines)")
        return "suppressed", True

    ok = send_telegram(build_message(reportable))
    return "alert", ok


# ------------------------------------------------------------------- http server
class Handler(BaseHTTPRequestHandler):
    def _ok(self, body=b'{"success":true}'):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        log("POST %s (%d bytes): %s" % (self.path, length, raw[:500]))
        try:
            kind, ok = handle_notification(raw)
            log("handled:", kind, "sent=" + str(ok))
        except Exception as e:  # noqa: BLE001
            log("ERROR handling notification:", repr(e))
        # Always 200 so Scrutiny records the delivery as successful.
        self._ok()

    def do_GET(self):  # noqa: N802
        # Simple health endpoint.
        self._ok(b'{"status":"ok","service":"scrutiny-bridge"}')

    def log_message(self, *a):  # silence default noisy logging
        pass


def main():
    log("starting on :%d  scrutiny_api=%s  baselines=%s"
        % (PORT, SCRUTINY_API, json.dumps(BASELINES)))
    if not TG_CHAT:
        log("WARN: TG_CHAT is empty — Telegram sends will fail until it is set")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
