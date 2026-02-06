#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""CLI entrypoint.

Responsibility:
- Parse CLI args and orchestrate the high-level flow (outbound/inbound).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid

from .config import BridgeConfig
from .logging_utils import ConfigDumper
from .agent import BridgeAgent
from .ultravox_client import UltravoxCallClient
from .livekit_client import LiveKitSipDialer


log = logging.getLogger("lk-ultravox-bridge")


async def main_async():
    parser = argparse.ArgumentParser(description="LiveKit SIP <-> Ultravox serverWebSocket bridge")
    parser.add_argument("--mode", choices=["outbound", "inbound"], required=True)
    parser.add_argument("--to", help="E.164 number to dial (outbound mode), e.g. +5511999999999")
    parser.add_argument("--room", help="Room name. If omitted, uses a generated room for outbound or a fixed room for inbound.")
    args = parser.parse_args()

    cfg = BridgeConfig.from_env()
    cfg.validate_required()

    ConfigDumper(log).dump_effective_config(cfg)

    if args.mode == "outbound" and not args.to:
        parser.error("--to is required in outbound mode")

    room_name = args.room or (f"test-call-{uuid.uuid4().hex[:6]}" if args.mode == "outbound" else "asterisk-inbound-test")

    log.info("Starting mode=%s room=%s", args.mode, room_name)

    agent = BridgeAgent(room_name=room_name, cfg=cfg, log=log)
    await agent.connect_livekit()

    uv_join_url = await UltravoxCallClient(cfg, log).create_ws_call_join_url()

    if args.mode == "outbound":
        dial_task = asyncio.create_task(LiveKitSipDialer(cfg, log).dial_out(room_name, args.to))
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


def main():
    # Preserve original script behavior for logging config.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main_async())
