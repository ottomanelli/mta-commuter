#!/usr/bin/env python3
"""Follow one LIRR or Metro-North train until it leaves.

Where check_track.py answers once ("is there a track yet?"), this keeps watching a
train on a station's departure board and reports each thing that changes:

    started    it is being followed, with how it stands now
    track      its track was posted (or changed)
    delay      how late it is changed by two minutes or more
    arriving   it is pulling in to the station
    canceled   it was canceled
    update     its time or lateness moved a little: nothing to say, but anything showing
               the train (a screen, say) can be brought up to date
    departed   it has left (the watch ends)
    stopped    the watch was stopped, or timed out
    error      the board keeps refusing requests (the watch ends)

    watch_train.py start --train 1582 --station NYK --label "5:22 PM to Huntington" [--arrives "6:14 PM"]
    watch_train.py status [--json]
    watch_train.py stop [--train 1582] [--station NYK]

`start` returns at once and the watch carries on in the background; `--foreground`
keeps it in this process and prints each message, one per line.

Telling someone. Each event is handed to every "on event" command: the event as JSON on
its standard input, and `{message}` in its arguments replaced by a sentence ready to
send (a command that uses `{message}` is skipped for events with nothing to say, such as
`departed`). Commands come from `--on-event "..."` (repeatable) and from the optional
file data/watch.json:

    {"on_event": [["openclaw", "message", "send", "--channel", "telegram",
                   "--target", "123456", "--message", "{message}"]],
     "fast_seconds": 5}

With no command at all, events are only kept in the watch's record (see `status`).

It is polite to the board, which is the undocumented service behind the MTA's own apps
(see check_track.py): it asks every `fast_seconds` only in the last 20 minutes before a
train without a track leaves, slowly otherwise, backs off when refused, and gives up
after three hours.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

from check_track import STATION_NAMES, fetch_arrivals, find_train

DATA = Path(__file__).resolve().parent.parent / "data"
WATCH_DIR = DATA / ".watch"
CONFIG = DATA / "watch.json"

FAST_SECONDS = 5          # while a track is about to post
DELAY_SECONDS = 15        # once the track is known, or when following only lateness
SLOW_SECONDS = 30         # more than PREPARE_MINUTES before it leaves
PREPARE_MINUTES = 20
GONE_SECONDS = 180        # how long after its time before deciding it has left
DELAY_STEP_MINUTES = 2    # a change in lateness smaller than this is not news
MAX_MINUTES = 180
KEPT_EVENTS = 20


# ---------------------------------------------------------------- the record

def record_path(system, station, train):
    return WATCH_DIR / f"{system}-{station.upper()}-{train}.json"


def read_record(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def write_record(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(record))
    temporary.replace(path)


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


def records(active_only=True):
    """Every watch's record; by default only those whose watcher is still running."""
    found = []
    for path in sorted(WATCH_DIR.glob("*.json")) if WATCH_DIR.exists() else []:
        record = read_record(path)
        if record and (not active_only or (not record.get("ended") and alive(record.get("pid")))):
            record["path"] = str(path)
            found.append(record)
    return found


# ---------------------------------------------------------------- what changed

def clock(when):
    moment = time.localtime(when)
    return f"{moment.tm_hour % 12 or 12}:{moment.tm_min:02d} {'AM' if moment.tm_hour < 12 else 'PM'}"


def describe(record):
    return record.get("label") or f"train {record['train']} at {STATION_NAMES.get(record['station'], record['station'])}"


def step(record, live, now):
    """One look at the board. `live` is the train's entry, or None if it is not listed.
    Returns the events to report, and updates `record` in place with what is now known
    and what has been said. No network, no clock: everything it needs is passed in."""
    events = []
    name = describe(record)

    def shown():
        # What a display of the train would show; half-minute steps so drift is not a change.
        leaves = record.get("leaves")
        return [record.get("track"), record.get("delay_min"), round(leaves / 30) if leaves else None]

    def event(kind, message=None):
        if message:
            message = message[0].upper() + message[1:]
        record["shown"] = shown()
        events.append({
            "event": kind, "message": message, "at": now,
            "train": record["train"], "station": record["station"], "system": record["system"],
            "label": record.get("label"), "track": record.get("track"), "sched_track": record.get("sched_track"),
            "delay_min": record.get("delay_min"), "leaves": record.get("leaves"), "canceled": record.get("canceled", False),
        })

    if live:
        status = live.get("status") or {}
        otp = status.get("otp")
        if live.get("time"):
            record["scheduled"] = float(live["time"])
            record["leaves"] = float(live["time"]) + max(0, otp or 0)
        if live.get("sched_track"):
            record["sched_track"] = str(live["sched_track"])
        if otp is not None:
            record["delay_min"] = max(0, round(otp / 60))
        record["live"] = otp is not None
        track = str(live["track"]) if live.get("track") else None
        late = record.get("delay_min") or 0
        track_words = f", track {track}" if track else ""

        if status.get("canceled") and not record.get("canceled"):
            record["canceled"] = True
            event("canceled", f"Canceled: {name}.")
            record["ended"] = "canceled"
            return events

        if not record.get("said_started"):
            record["said_started"] = True
            record["said_delay"] = late if record["live"] else None
            record["track"] = track
            if not record["live"]:
                how = "no live report on it yet"
            elif late:
                how = f"{late} min late" + (f", now leaving about {clock(record['leaves'])}" if record.get("leaves") else "")
            else:
                how = "on time"
            expected = (f", expected on track {record['sched_track']}"
                        if record.get("sched_track") and not track else "")
            event("started", f"Following {name}: {how}{track_words}{expected}.")
        else:
            if track and track != record.get("track"):
                changed = record.get("track")
                record["track"] = track
                expected = record.get("sched_track")
                if changed:
                    note = f" (was {changed})"
                elif not expected:
                    note = ""
                else:
                    note = ", as expected" if expected == track else f", expected {expected}"
                if late:
                    when = f"running {late} min late" + (f", now leaving about {clock(record['leaves'])}" if record.get("leaves") else "")
                elif record.get("leaves") and record["leaves"] > now:
                    when = f"leaving in {max(0, round((record['leaves'] - now) / 60))} min"
                else:
                    when = "leaving now"
                # When it gets there, if that was given and still holds (it is not late).
                arrives = f", arrives {record['arrives']}" if record.get("arrives") and not late else ""
                event("track", f"Track {track}{note}: {name}, {when}{arrives}.")
                record["said_delay"] = late if record["live"] else record.get("said_delay")
                if record.get("until") == "track":
                    record["ended"] = "track"
                    return events
            if record["live"]:
                said = record.get("said_delay")
                if said is None or abs(late - said) >= DELAY_STEP_MINUTES:
                    record["said_delay"] = late
                    if said is None:
                        words = "on time" if late == 0 else f"{late} min late"
                        event("delay", f"{name} is {words}{track_words}.")
                    elif late == 0:
                        event("delay", f"{name} is back on time{track_words}.")
                    else:
                        about = f", now leaving about {clock(record['leaves'])}" if record.get("leaves") else ""
                        event("delay", f"{name} is now {late} min late (was {said}){about}{track_words}.")

    if live and live.get("stop_status") == "ARRIVING" and record.get("said_started") \
            and not record.get("said_arriving") and not record.get("ended"):
        record["said_arriving"] = True
        if not any(each["event"] == "started" for each in events):
            track = f", track {record['track']}" if record.get("track") else ""
            event("arriving", f"{name} is arriving now{track}.")

    if record.get("leaves") and now > record["leaves"] + GONE_SECONDS:
        record["ended"] = "departed"
        event("departed")
    elif not events and record.get("said_started") and record.get("shown") != shown():
        event("update")
    return events


def pause_for(record, now, fast=FAST_SECONDS):
    """How long to wait before looking again."""
    leaves = record.get("leaves")
    if not leaves or leaves - now > PREPARE_MINUTES * 60:
        return SLOW_SECONDS
    return fast if not record.get("track") else DELAY_SECONDS


# ---------------------------------------------------------------- telling someone

def load_config():
    try:
        config = json.loads(CONFIG.read_text())
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def deliver(event, commands, echo=False):
    """Hands one event to each command. A failing command never stops the watch."""
    message = event.get("message")
    if echo and message:
        print(message, flush=True)
    for command in commands:
        wants_message = any("{message}" in part for part in command)
        if wants_message and not message:
            continue
        argv = [part.replace("{message}", message or "") for part in command]
        try:
            subprocess.run(argv, input=json.dumps(event), capture_output=True, text=True, timeout=45)
        except Exception as error:                        # the command is someone else's; carry on
            print(f"on-event command failed: {type(error).__name__}", file=sys.stderr)


# ---------------------------------------------------------------- the watch

def wait(path, seconds, sleep):
    """Waits, but returns early if the watch has been told to stop."""
    waited = 0
    while waited < seconds:
        piece = min(2, seconds - waited)
        sleep(piece)
        waited += piece
        current = read_record(path)
        if not current or current.get("stop"):
            return


def watch(path, commands, fast, echo=False, sleep=time.sleep, fetch=fetch_arrivals):
    record = read_record(path)
    if not record:
        return
    started = time.time()
    refusals = 0
    while True:
        current = read_record(path)
        if not current or current.get("pid") != os.getpid() or current.get("stop"):
            if current and current.get("pid") == os.getpid():
                record["ended"] = "stopped"
                events = [{"event": "stopped", "message": None, "at": time.time(), "train": record["train"],
                           "station": record["station"], "system": record["system"], "label": record.get("label"),
                           "track": record.get("track"), "sched_track": record.get("sched_track"),
                           "delay_min": record.get("delay_min"), "leaves": record.get("leaves"),
                           "canceled": record.get("canceled", False)}]
                record["events"] = (record.get("events", []) + events)[-KEPT_EVENTS:]
                write_record(path, record)
                for event in events:
                    deliver(event, commands, echo)
            return
        try:
            live = find_train(fetch(record["station"], record["system"]), record["train"])
            refusals = 0
        except HTTPError as error:
            if error.code in (403, 429):
                refusals += 1
                if refusals >= 3:
                    record["ended"] = "error"
                    event = {"event": "error", "message": f"Stopped following {describe(record)}: the station's board is refusing requests.",
                             "at": time.time(), "train": record["train"], "station": record["station"], "system": record["system"],
                             "label": record.get("label"), "track": record.get("track"), "sched_track": record.get("sched_track"),
                             "delay_min": record.get("delay_min"), "leaves": record.get("leaves"), "canceled": False}
                    record["events"] = (record.get("events", []) + [event])[-KEPT_EVENTS:]
                    write_record(path, record)
                    deliver(event, commands, echo)
                    return
                sleep(60 * refusals)
            else:
                sleep(10)
            continue
        except Exception:
            sleep(10)
            continue

        now = time.time()
        events = step(record, live, now)
        if not record.get("ended") and now - started > MAX_MINUTES * 60:
            record["ended"] = "stopped"
            events.append({"event": "stopped", "message": None, "at": now, "train": record["train"],
                           "station": record["station"], "system": record["system"], "label": record.get("label"),
                           "track": record.get("track"), "sched_track": record.get("sched_track"),
                           "delay_min": record.get("delay_min"), "leaves": record.get("leaves"),
                           "canceled": record.get("canceled", False)})
        record["checked"] = now
        record["events"] = (record.get("events", []) + events)[-KEPT_EVENTS:]
        write_record(path, record)
        for event in events:
            deliver(event, commands, echo)
        if record.get("ended"):
            return
        wait(path, pause_for(record, now, fast), sleep)


# ---------------------------------------------------------------- commands

def start(args):
    path = record_path(args.system, args.station, args.train)
    old = read_record(path)
    if old and not old.get("ended") and alive(old.get("pid")):
        print(json.dumps({"ok": True, "already": True, "watch": public(old)}))
        return 0
    config = load_config()
    commands = [list(command) for command in config.get("on_event", []) if isinstance(command, list) and command]
    commands += [shlex.split(command) for command in args.on_event or []]
    fast = max(2, int(args.fast_seconds or config.get("fast_seconds") or FAST_SECONDS))
    record = {"train": str(args.train), "station": args.station.upper(), "system": args.system,
              "label": args.label, "arrives": args.arrives, "until": args.until, "began": time.time(),
              "pid": None, "events": []}
    if args.foreground:
        record["pid"] = os.getpid()
        write_record(path, record)
        watch(path, commands, fast, echo=True)
        return 0
    write_record(path, record)
    command = [sys.executable, str(Path(__file__).resolve()), "run", "--record", str(path), "--fast-seconds", str(fast)]
    for extra in args.on_event or []:
        command += ["--on-event", extra]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    record["pid"] = child.pid
    write_record(path, record)
    answer = {"ok": True, "watch": public(record)}
    if not commands:
        answer["note"] = "No on-event command is set, so nothing will be sent anywhere; use `status` to see what happens."
    print(json.dumps(answer))
    return 0


def run(args):
    """The background watcher itself (started by `start`)."""
    record = read_record(args.record)
    if not record:
        return 1
    # `start` writes our pid a moment after starting us; make sure it is there.
    record["pid"] = os.getpid()
    write_record(args.record, record)
    config = load_config()
    commands = [list(command) for command in config.get("on_event", []) if isinstance(command, list) and command]
    commands += [shlex.split(command) for command in args.on_event or []]
    watch(args.record, commands, max(2, int(args.fast_seconds or FAST_SECONDS)))
    return 0


def public(record):
    """A record as shown to a person or an agent."""
    last = next((event["message"] for event in reversed(record.get("events", [])) if event.get("message")), None)
    return {key: value for key, value in {
        "train": record.get("train"), "station": record.get("station"), "system": record.get("system"),
        "label": record.get("label"), "track": record.get("track"), "sched_track": record.get("sched_track"),
        "delay_min": record.get("delay_min"), "leaves": record.get("leaves"), "canceled": record.get("canceled", False),
        "last_said": last, "ended": record.get("ended"),
    }.items() if value is not None}


def status(args):
    found = records()
    if args.json:
        print(json.dumps({"watching": [public(record) for record in found]}))
    elif not found:
        print("Not following any train.")
    else:
        for record in found:
            track = f"track {record['track']}" if record.get("track") else (
                f"no track yet (expected {record['sched_track']})" if record.get("sched_track") else "no track yet")
            late = f", {record['delay_min']} min late" if record.get("delay_min") else ""
            print(f"Following {describe(record)}: {track}{late}.")
    return 0


def stop(args):
    stopped = []
    for record in records():
        if args.train and str(args.train) != record["train"]:
            continue
        if args.station and args.station.upper() != record["station"]:
            continue
        path = record.pop("path")
        record["stop"] = True
        write_record(path, record)
        stopped.append(public(record))
    print(json.dumps({"ok": True, "stopped": stopped}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Follow an MTA commuter rail train until it leaves.")
    commands = parser.add_subparsers(dest="command", required=True)

    begin = commands.add_parser("start", help="follow a train")
    begin.add_argument("--train", required=True, help="Train number")
    begin.add_argument("--station", default="NYK", help="Station code whose board to watch (default: NYK)")
    begin.add_argument("--system", "-s", choices=["lirr", "mnr"], default="lirr")
    begin.add_argument("--label", default=None, help='How to name it in messages, e.g. "5:22 PM to Huntington"')
    begin.add_argument("--arrives", default=None,
                       help='When it reaches where you are going, e.g. "6:19 PM"; added to the track message')
    begin.add_argument("--until", choices=["departure", "track"], default="departure",
                       help="stop once the track is posted, or keep reporting lateness until it leaves (default)")
    begin.add_argument("--on-event", action="append", help="a command to run for each event (repeatable)")
    begin.add_argument("--fast-seconds", type=int, default=None, help=f"how often to ask while a track is due (default {FAST_SECONDS}, least 2)")
    begin.add_argument("--foreground", action="store_true", help="watch in this process and print each message")
    begin.set_defaults(handler=start)

    runner = commands.add_parser("run")                   # internal
    runner.add_argument("--record", required=True)
    runner.add_argument("--on-event", action="append")
    runner.add_argument("--fast-seconds", type=int, default=None)
    runner.set_defaults(handler=run)

    show = commands.add_parser("status", help="what is being followed")
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=status)

    end = commands.add_parser("stop", help="stop following (everything, or one train)")
    end.add_argument("--train", default=None)
    end.add_argument("--station", default=None)
    end.set_defaults(handler=stop)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
