# outbound-call-gateway

LiveKit SIP (Switch/Asterisk trunk) <-> Ultravox Realtime bridge, with an optional SQS worker that consumes TRIGGER_CALL messages.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (bridge CLI - original)

```bash
python -m lk_ultravox_bridge --mode outbound --to +56225956622
# or: start SQS-driven outbound (no --to)
python -m lk_ultravox_bridge --mode outbound
python bridge.py --mode inbound --room asterisk-inbound-test
```

## Run (SQS worker)

Config via env vars:

```bash
export AWS_REGION=us-east-1
export AWS_PROFILE=riachuelo-stage
export AWS_ACCOUNT_ID=481955878483
export SQS_QUEUE_NAME=TriggerCallQueue

python -m lk_ultravox_bridge.sqs_worker
```

If you must use static keys, set:
- AWS_ACCESS_KEY_ID
- AWS_SECRET_ACCESS_KEY
