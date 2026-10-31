#!/usr/bin/env python3
"""Standalone transcription runner used by the background worker.

Running transcription in a child process keeps the Whisper model out of the
long-lived worker. The implementation stays in ``WEBUI.webui``; importing it in
this helper sets ``STREAMVAULT_IMPORT_ONLY=1`` so import-time bootstrap side
effects (admin creation, badge sync) are skipped.
"""
from __future__ import annotations

import os
import sys


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (1, 2):
        print("Usage: run_transcribe.py <media> [transcript]", file=sys.stderr)
        return 2

    root = os.path.dirname(os.path.abspath(__file__))
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ["STREAMVAULT_IMPORT_ONLY"] = "1"

    from WEBUI.webui import transcribe_media_file

    ok, message = transcribe_media_file(args[0], args[1] if len(args) == 2 else None)
    print(message or ("Transkript erstellt" if ok else "Transkription fehlgeschlagen"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
