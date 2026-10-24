#!/usr/bin/env python3
"""Standalone Twitch IRC listener for Seele TTS.

Connects as an anonymous justinfan client and stays in chat whether or not
the recorder is capturing. Synthesis and the OBS queue live in tts_service;
this process only owns the chat connection.
"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
import time

from core import tts_service

logger = logging.getLogger("tts_bot")

_stop = threading.Event()


def streamers() -> list[str]:
    raw = os.environ.get("TTS_STREAMERS", "seele_x3")
    names: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        name = tts_service._norm(part)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _unescape_tag(value: str) -> str:
    return (
        value.replace("\\s", " ")
        .replace("\\:", ";")
        .replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\\\", "\\")
    )


def parse_privmsg(line: str) -> tuple[str, str, str, dict] | None:
    """Return (channel, username, message, tags) for a PRIVMSG line."""
    raw = str(line or "").strip("\r\n")
    if not raw:
        return None
    tags: dict[str, str] = {}
    rest = raw
    if rest.startswith("@"):
        try:
            tag_str, rest = rest[1:].split(" ", 1)
        except ValueError:
            return None
        for tag in tag_str.split(";"):
            if "=" not in tag:
                continue
            key, value = tag.split("=", 1)
            tags[key] = _unescape_tag(value)
    if " PRIVMSG " not in rest:
        return None
    try:
        prefix, payload = rest.split(" PRIVMSG ", 1)
        target, message = payload.split(" :", 1)
    except ValueError:
        return None
    channel = target.strip().lstrip("#").lower()
    username = str(tags.get("display-name") or "").strip()
    if not username:
        nick = prefix.lstrip(":").split("!", 1)[0].strip()
        username = nick or "unknown"
    if message.startswith("\x01ACTION "):
        message = message[len("\x01ACTION ") :].removesuffix("\x01")
    if not channel:
        return None
    return channel, username, message, tags


def handle_chat(
    streamer: str,
    username: str,
    message: str,
    tags: dict | None = None,
) -> bool:
    """Enqueue TTS for one chat line. Returns True when the line was a TTS command."""
    tags = tags or {}
    streamer = tts_service._norm(streamer)
    if not streamer or streamer not in set(streamers()):
        return False
    if tts_service.channel_point_triggers_tts(tags):
        voice, body = tts_service.parse_tts_payload(message)
        _ok, info = tts_service.enqueue(
            streamer,
            username,
            body or "Channel Points",
            voice=voice,
        )
        logger.info("[%s] channel-points TTS %s: %s", streamer, username, info)
        return True
    handled, info = tts_service.handle_tts_command(
        streamer, username, message, tags=tags
    )
    if handled:
        logger.info("[%s] TTS %s: %s", streamer, username, info)
    return handled


def _send(sock: socket.socket, payload: str) -> None:
    sock.sendall(payload.encode("utf-8", errors="replace"))


def listen_forever(channels: list[str]) -> None:
    server = "irc.chat.twitch.tv"
    port = 6667
    backoff = 2
    while not _stop.is_set():
        nickname = f"justinfan{random.randint(100000, 999999)}"
        sock = socket.socket()
        sock.settimeout(10)
        try:
            sock.connect((server, port))
            _send(sock, "PASS lucky\r\n")
            _send(sock, "CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
            _send(sock, f"NICK {nickname}\r\n")
            for channel in channels:
                _send(sock, f"JOIN #{channel}\r\n")
            logger.info("IRC verbunden als %s, Channels: %s", nickname, ", ".join(channels))
            backoff = 2
            buffer = ""
            last_ping = time.time()
            while not _stop.is_set():
                try:
                    data = sock.recv(4096)
                except TimeoutError:
                    data = None
                except OSError:
                    logger.warning("IRC-Verbindung verloren")
                    break
                if data == b"":
                    logger.warning("IRC-Verbindung geschlossen, reconnect")
                    break
                if data:
                    buffer += data.decode("utf-8", errors="ignore")
                now = time.time()
                if now - last_ping > 30:
                    try:
                        _send(sock, "PING :tmi.twitch.tv\r\n")
                    except OSError:
                        break
                    last_ping = now
                while "\r\n" in buffer:
                    line, buffer = buffer.split("\r\n", 1)
                    if line.startswith("PING"):
                        try:
                            _send(sock, "PONG :tmi.twitch.tv\r\n")
                        except OSError:
                            break
                        continue
                    parsed = parse_privmsg(line)
                    if not parsed:
                        continue
                    channel, username, message, tags = parsed
                    try:
                        handle_chat(channel, username, message, tags)
                    except Exception:
                        logger.exception("[%s] TTS-Verarbeitung fehlgeschlagen", channel)
        except Exception:
            logger.exception("IRC-Verbindung fehlgeschlagen")
        finally:
            try:
                sock.close()
            except OSError:
                pass
        if _stop.is_set():
            break
        logger.info("Reconnect in %ss", backoff)
        _stop.wait(backoff)
        backoff = min(30, backoff * 2)


def _request_stop(signum, _frame) -> None:
    logger.info("Stop (%s)", signum)
    _stop.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    channels = streamers()
    if len(channels) != 1:
        logger.error(
            "Dieser TTS-Bot darf genau einen Channel haben (gesetzt: %s)",
            ", ".join(channels) or "keiner",
        )
        return
    if not tts_service.enabled():
        logger.error("TTS_ENABLED ist aus, Bot bleibt aus")
        return
    tts_service.start_synth_worker()
    logger.info("TTS-Bot startet für %s", ", ".join(channels))
    listen_forever(channels)


if __name__ == "__main__":
    main()
