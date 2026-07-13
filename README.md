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
ULTRAVOX_VOICEMAIL_HANGUP=1  # agent detects voicemail and hangs up instead of talking to it (default on)

# Audio
SAMPLE_RATE=16000  # use 16000 for SIP calls — 48000 (the code default) causes resampling artifacts
CHANNELS=1
FRAME_MS=20

# AWS / SQS
AWS_REGION=us-east-1
AWS_PROFILE=<profile>          # used when static keys are not set
AWS_ACCESS_KEY_ID=<key>        # optional; overrides profile
AWS_SECRET_ACCESS_KEY=<secret> # optional; overrides profile
AWS_ACCOUNT_ID=<account-id>
SQS_QUEUE_NAME=TriggerCallQueue
```

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

### Bridge CLI (single call)

```bash
# Outbound: dial immediately — country profile selected by number prefix
python -m lk_ultravox_bridge --mode outbound --to +5511999999999

# Inbound: wait for SIP calls to arrive in a room
python -m lk_ultravox_bridge --mode inbound --room my-room
```

### Test scenarios (dev only)

For quick manual testing without editing `.env` between runs, pass `--scenario <name>` to load `scenarios/<name>.json`:

```bash
python -m lk_ultravox_bridge --mode outbound --to +5511999999999 --scenario debt_collect
```

A scenario may override `system_prompt`, `greeting_message`, `voice` and `temperature` for that call — all fields optional; anything omitted falls back to `.env` / country profile. The scenario's `greeting_message` exercises the same `firstSpeakerSettings` fallback path used by SQS `greetingMessage`. To add a scenario, drop a new JSON file in `scenarios/`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Unit tests live in `tests/unit/`. The suite is fully offline and independent of `.env` (see the isolation rule in `tests/conftest.py`).

### Voices 11Labs

7eb7586a-1831-40d1-88a4-8b690004cfb7 