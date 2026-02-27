#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio

from dotenv import load_dotenv


def _run() -> None:
    # Load .env before importing compat so BridgeConfig sees environment values.
    load_dotenv(override=True)
    from lk_ultravox_bridge.compat import main

    asyncio.run(main())


if __name__ == "__main__":
    _run()
