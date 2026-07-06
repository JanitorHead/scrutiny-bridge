# scrutiny-bridge

**Per-disk, per-attribute alert filtering for [Scrutiny](https://github.com/AnalogJ/scrutiny) —
silence one reviewed disk without muting the whole notification channel.**

Scrutiny is an excellent SMART dashboard, but its notifications are all-or-nothing: there is no
way to tell it *"I've reviewed this one attribute on this one disk, it's a stable baseline, stop
alerting on it"* while keeping alerts for everything else. scrutiny-bridge adds exactly that. It
sits between Scrutiny's webhook and your Telegram chat, drops alerts whose only problem is a
baseline you've whitelisted, and forwards everything else as a readable message.

It never touches Scrutiny's stored health data, so it's completely safe: the worst it can do is
send or not send a Telegram message.

---

## The problem

A common, harmless scenario: an older drive has a small, frozen count in a *critical* SMART
attribute — the classic one is `187 Reported Uncorrectable Errors` stuck at, say, `2` from years
ago. The counter lives in the drive firmware and can never be reset. Scrutiny applies its
Backblaze-derived heuristic, decides the failure rate for that attribute is "too high", and marks
the drive **failed** — forever.

Now every notification run reminds you about a disk you've already inspected and judged fine. Your
only built-in options are:

- **Mute notifications entirely** — and risk missing a *real* failure on another disk, or
- **Live with a permanent false alarm** — until you stop trusting the channel and ignore it.

Scrutiny has no per-disk or per-attribute ignore list ([a](https://github.com/AnalogJ/scrutiny/issues/255)
long-[standing](https://github.com/AnalogJ/scrutiny/issues/373) request). scrutiny-bridge is a
small, self-contained way to get one.

## What it does

- Receives Scrutiny's notification via webhook instead of Scrutiny talking to Telegram directly.
- On **every** notification, re-reads the **current** SMART state from Scrutiny's own API.
- **Drops** the alert if the only flagged attributes are ones you've whitelisted as a stable
  baseline (raw value at or below the value you reviewed).
- **Forwards** anything else — a new failing attribute, a whitelisted counter that has *grown*, a
  different disk — to Telegram as a readable message: disk, model, serial, attribute name, raw
  value, whether it's a critical attribute, and how it compares to your baseline.
- **Never** writes to Scrutiny. It only reads the API and sends Telegram messages.

## How it works

```
                   ┌──────────────────────────────────────────────┐
   Scrutiny  ──────▶            scrutiny-bridge                     │
   (webhook)       │                                                │
                   │  1. GET  /api/summary            ── which disks are flagged?
                   │  2. GET  /api/device/{uuid}/details ── which attributes, what raw values?
                   │  3. compare flagged attributes against your BASELINES                    │
                   │                                                │
                   │     only whitelisted baselines?  ──▶  drop (log, send nothing)           │
                   │     anything else / grown / new? ──▶  Telegram │
                   └──────────────────────────────────────────────┘
```

The key design choice: the bridge **ignores the contents of the webhook** and instead re-reads the
live state from Scrutiny each time. That makes it stateless and self-correcting — the decision is
always based on the disk's *current* attributes, not on whatever a particular notification
happened to contain. If a baselined counter creeps upward, or a brand-new problem appears, it gets
through on the very next notification.

Standard library only — no `pip install`, no build step. It runs on any `python:3-slim` container.

## Requirements

- A running **Scrutiny** instance with its web API reachable from the bridge (the default omnibus
  setup exposes it; the bridge talks to `http://<scrutiny-host>:8080/api`).
- A **Telegram bot** token and the **chat id** you want alerts in (optionally a forum-topic id).
- **Docker**, or any host with **Python 3.9+** (no third-party packages needed).

## Quick start

### 1. Get the files

Clone the repo (or copy `app.py` and `start.sh`) into a directory you'll mount at `/app`:

```sh
git clone https://github.com/JanitorHead/scrutiny-bridge.git
```

### 2. Provide the bot token

Create a one-line file with your bot token next to `app.py` (it is **never** baked into the image,
the template, or an env var by default):

```sh
printf '123456789:AA...your-bot-token...' > scrutiny-bridge/telegram.token
chmod 600 scrutiny-bridge/telegram.token
```

`.gitignore` already excludes `*.token`, so you can't commit it by accident.

### 3. Run it

**Docker:**

```sh
docker run -d --name scrutiny-bridge \
  --network <the-network-scrutiny-is-on> \
  -e TG_CHAT=-1001234567890 \
  -e 'BASELINES={"0x5000c5000000abcd":{"187":2}}' \
  -v "$PWD/scrutiny-bridge:/app" \
  python:3.12-slim sh /app/start.sh
```

**docker compose:**

```yaml
services:
  scrutiny-bridge:
    image: python:3.12-slim
    container_name: scrutiny-bridge
    command: sh /app/start.sh
    networks: [scrutiny]        # same network as your scrutiny container
    environment:
      SCRUTINY_API: http://scrutiny:8080/api
      TG_CHAT: "-1001234567890"
      TG_THREAD: "12"           # optional: forum-topic id
      HOST_LABEL: nas           # optional: shown in the message header
      BASELINES: '{"0x5000c5000000abcd":{"187":2}}'
    volumes:
      - ./scrutiny-bridge:/app  # must contain app.py, start.sh, telegram.token
    restart: unless-stopped
```

**Unraid:** a ready-made template is in [`deploy/my-scrutiny-bridge.xml`](deploy/my-scrutiny-bridge.xml).
Drop it in `/boot/config/plugins/dockerMan/templates-user/`, put `app.py`, `start.sh` and
`telegram.token` in the app directory, fill in `TG_CHAT` / `BASELINES`, and add the container.

The bridge only needs to be reachable **from the Scrutiny container**, so it doesn't require a
published host port — keeping it on an internal Docker network is enough (and preferable).

### 4. Point Scrutiny at the bridge

In Scrutiny's `scrutiny.yaml`, set the notification URL to the bridge's webhook endpoint using
shoutrrr's generic scheme, then restart Scrutiny:

```yaml
notify:
  urls:
    - "generic+http://scrutiny-bridge:8091/webhook"
```

Click **Send test notification** in Scrutiny's settings (or `POST /api/health/notify`). You should
get a confirmation message in Telegram, and the bridge log will show `test notification received`.

## Configuration

All configuration is through environment variables:

| Variable        | Default                     | Meaning |
|-----------------|-----------------------------|---------|
| `SCRUTINY_API`  | `http://scrutiny:8080/api`  | Base URL of Scrutiny's API, reachable from the bridge. |
| `PORT`          | `8091`                      | Port the bridge listens on for Scrutiny's webhook. |
| `TG_CHAT`       | *(required)*                | Telegram chat id — e.g. a group like `-100…`. |
| `TG_THREAD`     | *(empty)*                   | Optional forum-topic id inside the group. |
| `TG_TOKEN_FILE` | `/app/telegram.token`       | Path to a file holding the Telegram bot token. |
| `TG_TOKEN`      | *(empty)*                   | Alternative to the file: token straight from the env. |
| `HOST_LABEL`    | *(empty)*                   | Optional label shown in the message header (e.g. the machine's name). |
| `BASELINES`     | `{}`                        | JSON whitelist of reviewed baselines — see below. |

### Baselines — the whitelist

`BASELINES` is a JSON object keyed by disk **WWN** (which is stable across `/dev/sdX` renames),
then by SMART **attribute id**, mapping to the highest raw value you consider benign:

```json
{ "0x5000c5000000abcd": { "187": 2 } }
```

Read as: *for that disk, a flagged attribute `187` with a raw value of `2` or less is a reviewed
baseline — don't notify. If it reaches `3`, or any other attribute is flagged, notify.*

You can list several disks and several attributes:

```json
{
  "0x5000c5000000abcd": { "187": 2, "197": 0 },
  "0x5000c5000000ef01": { "5": 8 }
}
```

**Finding the values:**

- **WWN** — shown on each disk's page in the Scrutiny dashboard, or via `GET /api/summary`.
- **Attribute id** — the SMART id (e.g. `187`, `5`, `197`), shown in the attribute table on the
  disk's page.
- **Baseline value** — the current raw value you've decided is stable. Only set a baseline for a
  value you have actually reviewed and understand.

## What the notifications look like

**A whitelisted baseline is suppressed** — nothing is sent, and the bridge logs why:

```
[bridge] POST /webhook (…): Failure Type: SmartFailure …
[bridge] suppressed: sdc (0x5000c5000000abcd) — only whitelisted baselines flagged
[bridge] nothing reportable (all quiet or only whitelisted baselines)
```

**Anything else is forwarded to Telegram**, e.g. when a baselined counter has grown:

```
🔴 Alerta SMART · nas

💽 sdc — ExampleDrive 4TB · SN EX00-000-0001 · 3.6 TB
   • 187 Reported Uncorrectable Errors ⚠️crítico: valor bruto 5 (baseline revisado 2 → ahora 5, ha subido)

👉 Revisa el disco en Scrutiny. Si aparecen sectores pendientes (197/198) o el contador sigue subiendo, planifica el reemplazo.
```

> **Message language:** the alert copy ships in Spanish (the author's language). It's just plain
> strings in `app.py` (`build_message` and the test handler) — edit them for your own wording or
> translate them to whatever you like.

## How the filter decides

Scrutiny gives each attribute a `status` that is a **bitfield**:

| bit | meaning              |
|-----|----------------------|
| `0` | passed               |
| `1` | failed (SMART)       |
| `2` | warning (Scrutiny)   |
| `4` | failed (Scrutiny)    |

The bridge treats an attribute as **alert-worthy only if a failure bit is set** (`1` or `4`). A
pure **warning** (`2`) — for example *"could not determine failure rate"* on an attribute whose
raw value is `0` — is not actionable and is ignored, matching Scrutiny's own default
`notify_level: fail`.

This matters in practice: a drive can be marked failed because of one real attribute *and* carry
an unrelated warning on another. Counting "any non-zero status" would make such a disk impossible
to whitelist. By keying off failure bits, the bridge whitelists exactly the attribute you
reviewed and still reacts to genuinely new failures.

A disk is only inspected if Scrutiny already considers it not-healthy (`device_status != 0`), so
healthy disks are never fetched or reported.

## Security notes

- The bot token is read from a **file** (`TG_TOKEN_FILE`) by default, so it isn't baked into the
  image, the Unraid template, or the container's environment. Keep that file `chmod 600` and out
  of git (`.gitignore` covers `*.token`).
- The bridge only ever makes **outbound** requests to your Scrutiny API and to Telegram. It serves
  a webhook endpoint and a health check — nothing else.
- Because only Scrutiny needs to reach it, you can keep the bridge on an **internal Docker
  network with no published host port**. Do that unless you have a reason not to.

## Limitations

- **The dashboard tile stays red.** The bridge governs *notifications only*; it deliberately never
  modifies Scrutiny's stored health state. A whitelisted disk keeps showing as failed in the UI —
  that's cosmetic, and the safe trade-off.
- **Telegram only**, for now. The forwarding step is a small function; adding another target is
  straightforward.
- **Baselines are exact-match by WWN and attribute id.** A replaced disk gets a new WWN and simply
  won't match — which is the correct behavior (a new disk shouldn't inherit an old one's excuse).

## Troubleshooting

- **No test message arrives.** Check the bridge log (`docker logs scrutiny-bridge`). If you see the
  POST but no send, verify `TG_CHAT` and that `telegram.token` contains a valid token on one line.
  Confirm Scrutiny's `notify.urls` points at `generic+http://<bridge>:<port>/webhook` and that the
  bridge is on a network Scrutiny can resolve.
- **A real alert was suppressed.** Make sure the attribute you expected isn't accidentally covered
  by a `BASELINES` entry, and that its `status` actually carries a failure bit (a pure warning is
  ignored by design — see above).
- **Everything is suppressed / nothing is ever suppressed.** Double-check the WWN in `BASELINES`
  matches exactly what `GET /api/summary` reports (including the `0x` prefix), and that `BASELINES`
  is valid JSON (the bridge logs a warning and falls back to `{}` if it can't parse it).

## License

MIT — see [LICENSE](LICENSE).
