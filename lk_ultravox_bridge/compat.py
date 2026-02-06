from __future__ import annotations

import asyncio
import uuid
import logging
import argparse
from typing import Optional

from .config import BridgeConfig
from .logging_utils import ConfigDumper
from .livekit_client import LiveKitTokenFactory, LiveKitSipDialer
from .ultravox_client import UltravoxCallClient
from .agent import BridgeAgent as _BridgeAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("lk-ultravox-bridge")

_cfg = BridgeConfig()

LIVEKIT_URL = _cfg.livekit_url
LIVEKIT_WSS_URL = _cfg.livekit_wss_url
LIVEKIT_API_KEY = _cfg.livekit_api_key
LIVEKIT_API_SECRET = _cfg.livekit_api_secret

SIP_TRUNK_ID = _cfg.sip_trunk_id
SIP_FROM_NUMBER = _cfg.sip_from_number

ULTRAVOX_API_KEY = _cfg.ultravox_api_key
ULTRAVOX_CALLS_URL = _cfg.ultravox_calls_url
ULTRAVOX_VOICE = _cfg.ultravox_voice
ULTRAVOX_SYSTEM_PROMPT = _cfg.ultravox_system_prompt

SAMPLE_RATE = _cfg.sample_rate
CHANNELS = _cfg.channels
FRAME_MS = _cfg.frame_ms


def require_env(name: str, val: str):
    _cfg.require(name, val)


def dump_effective_config():
    ConfigDumper(_cfg, log).dump_effective_config()


def generate_livekit_token(room: str, identity: str) -> str:
    return LiveKitTokenFactory(_cfg).generate_token(room, identity)


async def create_ultravox_ws_call(system_prompt: Optional[str] = None) -> str:
    return await UltravoxCallClient(_cfg, log).create_ws_call_join_url(system_prompt=system_prompt)


async def dial_out_livekit(room_name: str, to_number: str):
    return await LiveKitSipDialer(_cfg, log).dial_out(room_name, to_number)


class BridgeAgent(_BridgeAgent):
    def __init__(self, room_name: str):
        super().__init__(_cfg, log, room_name)


async def main():
    parser = argparse.ArgumentParser(description="LiveKit SIP <-> Ultravox serverWebSocket bridge")
    parser.add_argument("--mode", choices=["outbound", "inbound"], required=True)
    parser.add_argument("--to", help="E.164 number to dial (outbound mode), e.g. +5511999999999")
    parser.add_argument("--room", help="Room name. If omitted, uses a generated room for outbound or a fixed room for inbound.")
    args = parser.parse_args()

    require_env("LIVEKIT_API_KEY", LIVEKIT_API_KEY)
    require_env("LIVEKIT_API_SECRET", LIVEKIT_API_SECRET)
    require_env("ULTRAVOX_API_KEY", ULTRAVOX_API_KEY)
    require_env("ULTRAVOX_VOICE", ULTRAVOX_VOICE)

    dump_effective_config()

    if args.mode == "outbound" and not args.to:
        # Outbound mode without --to means: run as SQS worker (TriggerCallQueue).
        from .sqs_worker import main as sqs_main
        log.info("[Main] outbound without --to -> starting SQS worker")
        await sqs_main()
        return

    room_name = args.room or (f"test-call-{uuid.uuid4().hex[:6]}" if args.mode == "outbound" else "asterisk-inbound-test")
    log.info("Starting mode=%s room=%s", args.mode, room_name)

    agent = BridgeAgent(room_name)
    await agent.connect_livekit()

    uv_join_url = await create_ultravox_ws_call()

    if args.mode == "outbound":
        dial_task = asyncio.create_task(dial_out_livekit(room_name, args.to))
        log.info("[Main] dialing started in background")
    else:
        dial_task = None
        log.info("Inbound mode: waiting for SIP calls to arrive in room '%s'", room_name)

    log.info("[Main] starting bridge now")
    try:
        await agent.run_bridge(uv_join_url)
    except KeyboardInterrupt:
        log.info("Stopping due to KeyboardInterrupt")
    finally:
        if dial_task:
            try:
                await dial_task
            except Exception as e:
                log.exception("[Main] dial task failed: %r", e)