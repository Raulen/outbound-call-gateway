# outbound-call-gateway

LiveKit SIP ↔ Ultravox Realtime audio bridge with an SQS-driven outbound dialer.

Calls are routed automatically by destination number prefix:

| Prefix | Country | LiveKit Project | SIP Provider |
|--------|---------|-----------------|--------------|
| `+55`  | Brazil  | Beneviah (stage) | Twilio Elastic SIP |
| `+56`  | Chile   | Switch           | Switch SIP   |

> Any prefix other than `+56` falls back to the Brazil (Twilio) profile — unknown prefixes do not raise an error.

---

## Install

Requires **Python >= 3.10**.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

All configuration is loaded from a `.env` file in the project root via `python-dotenv` (`load_dotenv(override=True)`). Values in `.env` always take precedence over shell environment variables.

### Per-country (LiveKit + SIP)

Each country has its own LiveKit project and SIP trunk. Set both blocks:

```env
# Brazil — Twilio
LIVEKIT_URL_BR=https://<your-br-project>.livekit.cloud
LIVEKIT_WSS_URL_BR=wss://<your-br-project>.livekit.cloud
LIVEKIT_API_KEY_BR=<key>
LIVEKIT_API_SECRET_BR=<secret>
SIP_TRUNK_ID_BR=<trunk-id>
SIP_FROM_NUMBER_BR=+55...
ULTRAVOX_VOICE_BR=<voice-id>   # optional; falls back to global ULTRAVOX_VOICE

# Chile — Switch
LIVEKIT_URL_CL=https://<your-cl-project>.livekit.cloud
LIVEKIT_WSS_URL_CL=wss://<your-cl-project>.livekit.cloud
LIVEKIT_API_KEY_CL=<key>
LIVEKIT_API_SECRET_CL=<secret>
SIP_TRUNK_ID_CL=<trunk-id>
SIP_FROM_NUMBER_CL=<number>
ULTRAVOX_VOICE_CL=<voice-id>   # optional; falls back to global ULTRAVOX_VOICE
```

### Shared

```env
# Ultravox
ULTRAVOX_API_KEY=<key>
ULTRAVOX_CALLS_URL=https://api.ultravox.ai/api/calls
ULTRAVOX_VOICE=<voice-id>      # global fallback; per-country ULTRAVOX_VOICE_XX takes priority
ULTRAVOX_SYSTEM_PROMPT=You are a helpful assistant.
ULTRAVOX_TEMPERATURE=0.3       # 0-1; the API default (0) sounds robotic/repetitive on voice calls
ULTRAVOX_MODEL=                # empty = API default; set to pin a model version
ULTRAVOX_JOIN_TIMEOUT=60s      # counts from call *creation*, not SIP answer — 30s (API default) can expire while still ringing
ULTRAVOX_GREETING_DELAY=4s     # how long the agent waits for the callee to speak after pickup before greeting first
ULTRAVOX_VOICEMAIL_HANGUP=1    # agent detects voicemail and hangs up instead of talking to it (default on)

# Audio
SAMPLE_RATE=16000  # use 16000 for SIP calls — 48000 (the code default) causes resampling artifacts
CHANNELS=1
FRAME_MS=20

# Jitter buffer (Ultravox -> LiveKit direction)
MAX_BUFFER_FRAMES=5   # discard old audio when the receive buffer exceeds this (100ms at 20ms/frame)
KEEP_BUFFER_FRAMES=2  # frames kept after a discard (40ms at 20ms/frame)

# SQS worker
MAX_CONCURRENT_CALLS=3  # simultaneous calls; 1 = strictly serial (safe rollback)

# AWS / SQS
AWS_REGION=us-east-1
AWS_PROFILE=<profile>          # used when static keys are not set
AWS_ACCESS_KEY_ID=<key>        # optional; overrides profile
AWS_SECRET_ACCESS_KEY=<secret> # optional; overrides profile
AWS_ACCOUNT_ID=<account-id>
SQS_QUEUE_NAME=TriggerCallQueue
```

### Full reference

**Per-country** — replace `XX` with the country code (`BR`, `CL`). All 7 are required for a country to receive calls; validation fails with `SystemExit` on the first missing one when a call routes to that country.

| Variable | Required | Notes |
|----------|----------|-------|
| `LIVEKIT_URL_XX` | yes | LiveKit project HTTPS URL (used by the server API: SIP dial-out, room delete) |
| `LIVEKIT_WSS_URL_XX` | yes | LiveKit project WSS URL (used by the RTC client) |
| `LIVEKIT_API_KEY_XX` | yes | |
| `LIVEKIT_API_SECRET_XX` | yes | |
| `SIP_TRUNK_ID_XX` | yes | LiveKit SIP trunk ID |
| `SIP_FROM_NUMBER_XX` | yes | Caller ID (E.164) |
| `ULTRAVOX_VOICE_XX` | yes* | *Satisfied by the global `ULTRAVOX_VOICE` fallback if unset |
| `LANGUAGE_HINT_XX` | no | BCP47 hint guiding Ultravox ASR/TTS. Code defaults: `pt-BR` (BR), `es-CL` (CL). Set to empty to stop sending the hint (rollback switch) |

**Shared**

| Variable | Default | Required | Notes |
|----------|---------|----------|-------|
| `ULTRAVOX_API_KEY` | — | yes | |
| `ULTRAVOX_CALLS_URL` | `https://api.ultravox.ai/api/calls` | no | |
| `ULTRAVOX_VOICE` | — | yes* | *Global voice fallback; required only if some country lacks `ULTRAVOX_VOICE_XX`. SQS `metadata.voiceId` overrides both. |
| `ULTRAVOX_SYSTEM_PROMPT` | `You are a helpful assistant.` | no | CLI fallback only; SQS calls always use the message's `prompt_text` |
| `ULTRAVOX_TEMPERATURE` | `0.3` | no | 0–1 |
| `ULTRAVOX_MODEL` | empty (API default) | no | Pin an Ultravox model version |
| `ULTRAVOX_JOIN_TIMEOUT` | `60s` | no | joinUrl expiry, counted from call creation |
| `ULTRAVOX_GREETING_DELAY` | `4s` | no | Silence tolerated after pickup before the agent greets first |
| `ULTRAVOX_VOICEMAIL_HANGUP` | `1` (on) | no | `1/true/yes` = on; anything else = off |
| `SAMPLE_RATE` | `48000` | no | **Set `16000` in production** (SIP resampling artifacts at 48kHz) |
| `CHANNELS` | `1` | no | |
| `FRAME_MS` | `20` | no | |
| `MAX_BUFFER_FRAMES` | `5` | no | Jitter buffer overflow threshold |
| `KEEP_BUFFER_FRAMES` | `2` | no | Frames kept after overflow discard |
| `MAX_CONCURRENT_CALLS` | `3` | SQS only | Simultaneous calls per worker; `1` = serial (rollback switch) |
| `ENVIRONMENT` | `dev` | no | `env` label on shipped logs (`prod` on Render) |
| `GRAFANA_LOKI_URL` / `GRAFANA_LOKI_USER` / `GRAFANA_TOKEN` | — | no | Grafana Cloud log shipping; all three unset = stdout only |
| `AWS_REGION` | `us-east-1` | SQS only | |
| `AWS_PROFILE` | — | SQS only* | *Used when static keys are unset or `none` |
| `AWS_ACCESS_KEY_ID` | — | no | Static key; overrides profile |
| `AWS_SECRET_ACCESS_KEY` | — | no | Static key; overrides profile |
| `AWS_ACCOUNT_ID` | — | SQS only | Used to build the queue URL |
| `SQS_QUEUE_NAME` | `TriggerCallQueue` | SQS only | |

"SQS only" = required only when running the SQS worker; the single-call CLI (`--to` / inbound) doesn't need AWS at all.

> **Adding a new country:** add a `_XX` variable block in `.env` (including `ULTRAVOX_VOICE_XX` if a per-country voice is needed) and register the prefix in `_PROFILE_MAP` inside `lk_ultravox_bridge/config.py`.

---

## Run

### SQS Worker (production)

Consumes `TRIGGER_CALL` messages from SQS and dials out automatically based on the number prefix.

```bash
python -m lk_ultravox_bridge --mode outbound
```

Or directly:

```bash
python -m lk_ultravox_bridge.sqs_worker
```

Calls run in parallel up to `MAX_CONCURRENT_CALLS` (default 3); a message is only pulled from the queue when a call slot is free.

Message handling: the message is deleted **as soon as the SIP dial is answered** (retrying after that point would double-call the person). Any failure before answer — parse error, Ultravox REST error, trunk rejection, nobody answered within 90s — leaves the message in the queue, and it returns after the visibility timeout (300s). There is no DLQ logic in the code — configure redrive on the queue itself.

### Bridge CLI (single call)

```bash
# Outbound: dial immediately — country profile selected by number prefix
python -m lk_ultravox_bridge --mode outbound --to +5511999999999

# Inbound: wait for SIP calls to arrive in a room
# (--room defaults to "asterisk-inbound-test" if omitted)
python -m lk_ultravox_bridge --mode inbound --room my-room
```

`python bridge.py` (root script) is an equivalent entry point with the same flags.

### Test scenarios (dev only)

For quick manual testing without editing `.env` between runs, pass `--scenario <name>` to load `scenarios/<name>.json` (or pass an explicit path to any `.json` file):

```bash
python -m lk_ultravox_bridge --mode outbound --to +5511999999999 --scenario debt_collect
```

A scenario may override `system_prompt`, `greeting_message`, `voice` and `temperature` for that call — all fields optional; anything omitted falls back to `.env` / country profile. The scenario's `greeting_message` exercises the same `firstSpeakerSettings` fallback path used by SQS `greetingMessage`. To add a scenario, drop a new JSON file in `scenarios/`.

---

## Call behavior

- **Callee speaks first**: the agent waits `ULTRAVOX_GREETING_DELAY` (default 4s) after pickup; if the callee stays silent, the agent greets first — with the `greetingMessage` from the SQS message / scenario when present, otherwise with a generic greeting prompt.
- **Voicemail detection** (`ULTRAVOX_VOICEMAIL_HANGUP`, default on): Twilio Elastic SIP Trunking has no AMD, so the model itself is the detector — a guard instruction is appended to the system prompt and the built-in `hangUp` tool is enabled. On recognizing a voicemail greeting/beep, the agent hangs up instead of talking to the recording.
- **Silence watchdog**: if Ultravox sends nothing over the WebSocket for ≥30s, the bridge ends the call instead of leaving the callee listening to silence.
- **Room teardown**: when the call ends, the bridge disconnects from the room **and deletes it via the LiveKit API** — deleting the room is what removes the SIP participant and sends BYE to the trunk when our side ends the call (voicemail hang-up, watchdog, error). Best-effort: a failed delete is logged as a warning, never masks the call result.
- **Call recording** is always enabled on the Ultravox side (`recordingEnabled=True`).
- **Language**: each call sends a `languageHint` (BCP47) to Ultravox guiding speech recognition and synthesis, taken from the country profile (`pt-BR` for BR, `es-CL` for CL). The voicemail-guard instruction is also written in the call's language. Note: since every prefix other than `+56` falls back to the BR profile, those calls inherit `pt-BR` (consistent with the voice and campaign prompt they already inherit).

## Observability

Logs-first: the worker ships its structured logs to **Grafana Cloud Loki**, and every dashboard metric (funnel, duration, in-flight, errors) is derived from those logs with LogQL. There is no separate metrics pipeline to operate at this scale — `GRAFANA_PROM_*` in `.env` is validated but reserved for when volume justifies it.

Shipping is **optional and non-blocking**: without the env vars below the worker runs stdout-only, exactly as before; with them, a background thread batches lines to Loki and **drops telemetry rather than ever delaying audio**. High-cardinality context (call id, room) stays inside the log line — only `app`, `env` and `level` are stream labels.

```env
ENVIRONMENT=dev            # label "env" on every log line (set "prod" on Render)
GRAFANA_LOKI_URL=https://logs-prod-XXX.grafana.net
GRAFANA_LOKI_USER=<numeric user from the Logs/Loki details page>
GRAFANA_TOKEN=<access policy token with logs:write>
```

The worker also emits a liveness heartbeat every 60s (`[HB] alive inFlight=N max=M`) and stamps each finished call with `durationS=` — both feed the dashboard and alerts.

### Dashboard

Import `observability/grafana-dashboard.json` (Grafana → Dashboards → New → Import → upload), picking your `grafanacloud-<stack>-logs` datasource when prompted. Panels: worker liveness, in-flight vs cap, failures, call funnel (received/answered/completed/answer rate), calls over time, call duration (avg/p95), time-to-answer, audio-quality events, and a warnings/errors log tail. The `env` variable filters dev/prod.

### Alerts (create once, in Grafana → Alerting)

Contact point: type **Microsoft Teams**, URL from the Teams channel's Workflows webhook. Two rules cover the operation:

**1. Worker down/stuck** — fires when no heartbeat lands for 5 minutes:

```logql
sum(count_over_time({app="outbound-call-gateway", env="prod"} |= `[HB] alive` [5m])) < 1
```

**2. Errors** — fires on any processing/infra failure in a 10-minute window:

```logql
sum(count_over_time({app="outbound-call-gateway", env="prod"} |~ `processing failed|receive failed|crashed unexpectedly` [10m])) > 0
```

Complement (outside Grafana, no code): a CloudWatch alarm on the queue's `ApproximateAgeOfOldestMessage` catches the pipeline stalling even if the whole observability stack is down.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Unit tests live in `tests/unit/` (131 tests: message contract, country routing, audio bridge, Ultravox REST client via respx, agent event handlers, SQS worker orchestration, log masking). The suite is fully offline and independent of `.env` (see the isolation rule in `tests/conftest.py`).

---

## Appendix: known voice IDs

| Voice ID | Notes |
|----------|-------|
| `7eb7586a-1831-40d1-88a4-8b690004cfb7` | ElevenLabs voice (use as `ULTRAVOX_VOICE` / `ULTRAVOX_VOICE_XX`) |