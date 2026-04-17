#!/usr/bin/env python3
"""Thin wrapper — runs the bundled GPS config audit tool.

The actual implementation lives in ``sp_base.cli.config_audit``.
You can also run it directly via the installed entry point::

    sp-base-gps-audit [--port /dev/ttyUSB0] [--baud 57600]
"""
from sp_base.cli.config_audit import main

if __name__ == "__main__":
    main()
