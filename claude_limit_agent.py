#!/usr/bin/env python3
"""
Run Claude inside a small watchdog terminal wrapper.

The agent watches Claude's terminal output for common session-limit/reset
messages. When it finds a reset time, it waits until that time and sends a
continuation prompt back into the same Claude process.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pty
import re
import select
import shlex
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass
from typing import Iterable


DEFAULT_RESUME_TEXT = "continue, lets do the rest ..."


@dataclass(frozen=True)
class LimitMatch:
    source: str
    resume_at: dt.datetime


class LimitParser:
    """Parse likely Claude CLI limit messages into local datetimes."""

    TIME_PATTERNS = [
        re.compile(
            r"(?:try again|come back|resume|available|reset|resets|until)[^\n]{0,80}?"
            r"(?P<date>today|tomorrow)?\s*(?:at\s*)?"
            r"(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?P<date>today|tomorrow)\s+(?:at\s*)?"
            r"(?P<time>\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)",
            re.IGNORECASE,
        ),
    ]
    ISO_PATTERN = re.compile(
        r"(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?"
        r"(?:Z|[+-]\d{2}:?\d{2})?)"
    )
    RELATIVE_PATTERN = re.compile(
        r"(?:try again|come back|resume|available|reset|resets|until)[^\n]{0,80}?"
        r"(?:in|after)\s+"
        r"(?P<duration>[^\n]{1,80})",
        re.IGNORECASE,
    )
    HOURS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)", re.IGNORECASE)
    MINUTES_PATTERN = re.compile(
        r"(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)", re.IGNORECASE
    )

    def __init__(self, minimum_delay_seconds: int = 30) -> None:
        self.minimum_delay_seconds = minimum_delay_seconds

    def parse(self, text: str, now: dt.datetime | None = None) -> LimitMatch | None:
        now = now or dt.datetime.now().astimezone()
        candidates = [
            self._parse_iso(text, now),
            self._parse_relative(text, now),
            self._parse_clock_time(text, now),
        ]
        valid = [match for match in candidates if match is not None]
        if not valid:
            return None
        return min(valid, key=lambda match: match.resume_at)

    def _parse_iso(self, text: str, now: dt.datetime) -> LimitMatch | None:
        for match in self.ISO_PATTERN.finditer(text):
            stamp = match.group("stamp").replace("Z", "+00:00")
            try:
                parsed = dt.datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            parsed = parsed.astimezone(now.tzinfo)
            if self._is_plausible(parsed, now):
                return LimitMatch(match.group(0), parsed)
        return None

    def _parse_relative(self, text: str, now: dt.datetime) -> LimitMatch | None:
        for match in self.RELATIVE_PATTERN.finditer(text):
            duration = match.group("duration")
            hour_match = self.HOURS_PATTERN.search(duration)
            minute_match = self.MINUTES_PATTERN.search(duration)
            hours = float(hour_match.group(1)) if hour_match else 0
            minutes = float(minute_match.group(1)) if minute_match else 0
            if hours == 0 and minutes == 0:
                continue
            parsed = now + dt.timedelta(hours=hours, minutes=minutes)
            if self._is_plausible(parsed, now):
                return LimitMatch(match.group(0), parsed)
        return None

    def _parse_clock_time(self, text: str, now: dt.datetime) -> LimitMatch | None:
        for pattern in self.TIME_PATTERNS:
            for match in pattern.finditer(text):
                parsed_time = self._clock_to_time(match.group("time"))
                if parsed_time is None:
                    continue
                date_word = (match.groupdict().get("date") or "").lower()
                day = now.date()
                if date_word == "tomorrow":
                    day += dt.timedelta(days=1)
                parsed = dt.datetime.combine(day, parsed_time, tzinfo=now.tzinfo)
                if date_word != "tomorrow" and parsed <= now:
                    parsed += dt.timedelta(days=1)
                if self._is_plausible(parsed, now):
                    return LimitMatch(match.group(0), parsed)
        return None

    def _clock_to_time(self, raw: str) -> dt.time | None:
        cleaned = raw.lower().replace(".", "").strip()
        meridiem = None
        if cleaned.endswith("am") or cleaned.endswith("pm"):
            meridiem = cleaned[-2:]
            cleaned = cleaned[:-2].strip()
        parts = cleaned.split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (TypeError, ValueError):
            return None
        if minute < 0 or minute > 59:
            return None
        if meridiem:
            if hour < 1 or hour > 12:
                return None
            if meridiem == "pm" and hour != 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0
        elif hour < 0 or hour > 23:
            return None
        return dt.time(hour=hour, minute=minute)

    def _is_plausible(self, parsed: dt.datetime, now: dt.datetime) -> bool:
        earliest = now + dt.timedelta(seconds=self.minimum_delay_seconds)
        latest = now + dt.timedelta(days=2)
        return earliest <= parsed <= latest


class ClaudeLimitAgent:
    def __init__(
        self,
        command: list[str],
        resume_text: str,
        dry_run: bool,
        lead_seconds: int,
        quiet: bool,
    ) -> None:
        self.command = command
        self.resume_text = resume_text
        self.dry_run = dry_run
        self.lead_seconds = lead_seconds
        self.quiet = quiet
        self.parser = LimitParser()
        self.master_fd: int | None = None
        self.original_termios: list[int | bytes] | None = None
        self.scheduled_for: dt.datetime | None = None
        self.output_buffer = ""
        self.stop_event = threading.Event()

    def run(self) -> int:
        self._log(f"Starting: {shlex.join(self.command)}")
        self.original_termios = termios.tcgetattr(sys.stdin.fileno())
        self.master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            self.command,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        try:
            tty.setraw(sys.stdin.fileno())
            return self._pump(process)
        finally:
            self.stop_event.set()
            if self.original_termios is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.original_termios)
            if self.master_fd is not None:
                os.close(self.master_fd)

    def _pump(self, process: subprocess.Popen[bytes]) -> int:
        assert self.master_fd is not None
        while process.poll() is None:
            readable, _, _ = select.select([sys.stdin.fileno(), self.master_fd], [], [], 0.2)
            if self.master_fd in readable:
                try:
                    data = os.read(self.master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                self._observe(data)
            if sys.stdin.fileno() in readable:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    break
                os.write(self.master_fd, data)
        return process.wait()

    def _observe(self, data: bytes) -> None:
        text = data.decode(errors="ignore")
        self.output_buffer = (self.output_buffer + text)[-6000:]
        match = self.parser.parse(self.output_buffer)
        if match is None:
            return
        if self.scheduled_for and abs((self.scheduled_for - match.resume_at).total_seconds()) < 60:
            return
        self.scheduled_for = match.resume_at
        thread = threading.Thread(target=self._resume_later, args=(match,), daemon=True)
        thread.start()

    def _resume_later(self, match: LimitMatch) -> None:
        resume_at = match.resume_at - dt.timedelta(seconds=self.lead_seconds)
        seconds = max(0, (resume_at - dt.datetime.now().astimezone()).total_seconds())
        self._log(
            "Detected limit. Will resume at "
            f"{match.resume_at.strftime('%Y-%m-%d %H:%M:%S %Z')} "
            f"(matched: {match.source!r})."
        )
        if self.stop_event.wait(seconds):
            return
        if self.master_fd is None:
            return
        payload = f"{self.resume_text}\n".encode()
        if self.dry_run:
            self._log(f"Dry run: would send {self.resume_text!r}")
            return
        os.write(self.master_fd, payload)
        self._log(f"Sent resume prompt: {self.resume_text!r}")

    def _log(self, message: str) -> None:
        if not self.quiet:
            sys.stderr.write(f"\r\n[claude-limit-agent] {message}\r\n")
            sys.stderr.flush()


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Claude and automatically continue after session limits reset."
    )
    parser.add_argument(
        "--command",
        default="claude",
        help="Command to run. Use quotes for arguments, e.g. --command 'claude --continue'.",
    )
    parser.add_argument(
        "--resume-text",
        default=DEFAULT_RESUME_TEXT,
        help="Text to send when the detected limit expires.",
    )
    parser.add_argument(
        "--lead-seconds",
        type=int,
        default=2,
        help="Send the resume text this many seconds before the detected reset time.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect and schedule, but do not send anything into Claude.",
    )
    parser.add_argument("--quiet", action="store_true", help="Hide agent status messages.")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = parse_args(argv)
    command = shlex.split(args.command)
    if not command:
        raise SystemExit("--command cannot be empty")
    agent = ClaudeLimitAgent(
        command=command,
        resume_text=args.resume_text,
        dry_run=args.dry_run,
        lead_seconds=max(0, args.lead_seconds),
        quiet=args.quiet,
    )
    try:
        return agent.run()
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        sys.stderr.write(f"Command not found: {command[0]}\n")
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
