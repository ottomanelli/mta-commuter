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
    watch_train.py setup --channel telegram --target <chat id> [--thread-id <topic>]

How it keeps watching. By default `start` hands the watching to OpenClaw's scheduler:
it adds a job that runs `watch_train.py tick` every 20 seconds, and removes it when the
train has left. Each tick looks at the board for a few seconds (every `fast_seconds`
while a track is due), prints anything new for the scheduler to deliver to the chat, and
exits; what has been seen and said is kept in the watch's record between ticks. Nothing
is left running, so it survives restarts and works wherever OpenClaw does.
`--background` instead keeps one process running until the train leaves (used when
on-event commands are set, see below); `--foreground` does that in this process and
prints each message, one per line.

Telling someone. Each event is handed to every "on event" command: the event as JSON on
its standard input, and `{message}` in its arguments replaced by a sentence ready to
send (a command that uses `{message}` is skipped for events with nothing to say, such as
`departed`). Commands come from `--on-event "..."` (repeatable) and from the optional
file data/watch.json:

    {"on_event": [["openclaw", "message", "send", "--channel", "telegram",
                   "--target", "123456", "--message", "{message}"]],
     "fast_seconds": 5}

`setup` writes that file for a chat (once; it leaves an existing file alone unless
`--force`), and `setup --decline` records that the user does not want it, so they are
not asked again. `setup --show` says which of the three states it is in.

With no command set, messages go back to the chat the watch was started from: when an
OpenClaw agent runs `start` from a chat, OpenClaw tells the command which chat that is
(OPENCLAW_CHANNEL_CONTEXT), and the watch remembers it and sends with
`openclaw message send`. It needs the channel's name too: `--channel telegram` (or the
channel in that context, if OpenClaw gives one). `--target` names a chat explicitly.
With neither a command nor a chat, events are only kept in the watch's record (see `status`).

It is polite to the board, which is the undocumented service behind the MTA's own apps
(see check_track.py): it asks every `fast_seconds` only in the last 20 minutes before a
train without a track leaves, slowly otherwise, backs off when refused, and gives up
after three hours.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

from check_track import STATION_NAMES, fetch_arrivals, find_train, seconds_late

DATA = Path(__file__).resolve().parent.parent / "data"
WATCH_DIR = DATA / ".watch"
CONFIG = DATA / "watch.json"

FAST_SECONDS = 5          # while a track is about to post
DELAY_SECONDS = 15        # once the track is known, or when following only lateness
SLOW_SECONDS = 30         # more than PREPARE_MINUTES before it leaves
PREPARE_MINUTES = 20
TICK_EVERY = "20s"        # how often the scheduler runs a tick
BURST_SECONDS = 15        # how long one tick keeps looking before it exits
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
        if record and (not active_only or (not record.get("ended") and (record.get("job") or alive(record.get("pid"))))):
            record["path"] = str(path)
            found.append(record)
    return found


# ---------------------------------------------------------------- what changed

def clock(when):
    moment = time.localtime(when)
    return f"{moment.tm_hour % 12 or 12}:{moment.tm_min:02d} {'AM' if moment.tm_hour < 12 else 'PM'}"


def a_track(code):
    """The board's "scheduled track" when it is a track to tell a person: a number, as at
    a terminal. At a through station the board gives a code of its own (G3, R7), which
    is not a track and is left unsaid."""
    return str(code) if code and str(code).isdigit() else None


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
            "delay_min": record.get("delay_min"), "scheduled": record.get("scheduled"), "leaves": record.get("leaves"), "canceled": record.get("canceled", False),
        })

    if live:
        status = live.get("status") or {}
        behind = seconds_late(live)                      # None: the board has no live report of it
        if live.get("time"):
            # The board's time is the timetable's; a train ahead of it still leaves on time.
            record["scheduled"] = float(live["time"])
            record["leaves"] = float(live["time"]) + max(0, behind or 0)
        if live.get("sched_track"):
            record["sched_track"] = str(live["sched_track"])
        if behind is not None:
            record["delay_min"] = max(0, round(behind / 60))
        record["live"] = behind is not None
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
            expected = (f", expected on track {a_track(record.get('sched_track'))}"
                        if a_track(record.get("sched_track")) and not track else "")
            event("started", f"Following {name}: {how}{track_words}{expected}.")
        else:
            if track and track != record.get("track"):
                changed = record.get("track")
                record["track"] = track
                expected = record.get("sched_track")
                if changed:
                    note = f" (was {changed})"
                elif not expected or not (expected.isdigit() and track.isdigit()):
                    # At a through station the scheduled "track" is a code of its own
                    # (H1) and the posted one a platform (A): not comparable.
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


def origin_chat(environment=None):
    """The chat an OpenClaw agent was in when it ran this command, from the identity
    OpenClaw passes to commands it runs for a chat. Returns (channel, chat id); either
    may be None. The payload is small JSON; both flat and nested forms are read."""
    environment = os.environ if environment is None else environment
    try:
        context = json.loads(environment.get("OPENCLAW_CHANNEL_CONTEXT") or "{}")
    except ValueError:
        return None, None
    if not isinstance(context, dict):
        return None, None
    chat = context.get("chat") if isinstance(context.get("chat"), dict) else {}
    chat_id = context.get("chatId") or chat.get("id")
    channel = context.get("channel") or chat.get("channel") or context.get("provider")
    if isinstance(channel, dict):
        channel = channel.get("id") or channel.get("name")
    return (str(channel) if channel else None), (str(chat_id) if chat_id else None)


def reply_command(channel, target, thread=None):
    """The command that sends a message to a chat through OpenClaw; None if either part
    of the address is missing. `thread` is a forum topic within the chat, if any."""
    if not channel or not target:
        return None
    command = [openclaw_bin(), "message", "send",
               "--channel", str(channel).lower(), "--target", str(target)]
    if thread:
        command += ["--thread-id", str(thread)]
    return command + ["--message", "{message}"]


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
                           "delay_min": record.get("delay_min"), "scheduled": record.get("scheduled"), "leaves": record.get("leaves"),
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
                             "delay_min": record.get("delay_min"), "scheduled": record.get("scheduled"), "leaves": record.get("leaves"), "canceled": False}
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
                           "delay_min": record.get("delay_min"), "scheduled": record.get("scheduled"), "leaves": record.get("leaves"),
                           "canceled": record.get("canceled", False)})
        record["checked"] = now
        record["events"] = (record.get("events", []) + events)[-KEPT_EVENTS:]
        write_record(path, record)
        for event in events:
            deliver(event, commands, echo)
        if record.get("ended"):
            return
        wait(path, pause_for(record, now, fast), sleep)


# ---------------------------------------------------------------- under OpenClaw's scheduler

def openclaw_bin():
    """Where the openclaw command is. A program started by a service (and so this one)
    may not have it on its PATH, so the usual places are tried as well."""
    named = os.environ.get("OPENCLAW_BIN")
    if named:
        return named
    found = shutil.which("openclaw")
    if found:
        return found
    home = Path.home()
    for place in (home / ".npm-global/bin/openclaw", home / ".local/bin/openclaw", Path("/usr/local/bin/openclaw"),
                  Path("/opt/homebrew/bin/openclaw"), home / ".openclaw/bin/openclaw"):
        if place.exists():
            return str(place)
    return "openclaw"


def openclaw(*args, timeout=90):
    """Runs an openclaw command; returns (exit code, output)."""
    try:
        done = subprocess.run([openclaw_bin(), *args], capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout
    except Exception as error:
        return 1, str(error)


def ended_event(record, kind, message=None):
    return {"event": kind, "message": message, "at": time.time(), "train": record["train"],
            "station": record["station"], "system": record["system"], "label": record.get("label"),
            "track": record.get("track"), "sched_track": record.get("sched_track"),
            "delay_min": record.get("delay_min"), "scheduled": record.get("scheduled"), "leaves": record.get("leaves"),
            "canceled": record.get("canceled", False)}


def tick(args, fetch=fetch_arrivals, sleep=time.sleep, clock=time.time, run=openclaw, out=print):
    """One run under the scheduler: look at the board for a few seconds, report what is
    new, exit. Anything printed is delivered to the chat by the scheduler; NO_REPLY
    means there is nothing to say. Always exits 0, so that a quiet run is not counted as
    a failure."""
    path = Path(args.record)
    record = read_record(path)
    if not record:
        out("NO_REPLY")
        return 0
    if record.get("ended"):
        # The last word was delivered by the run before this one; now the job can go.
        # (A job removed from inside the run that prints loses that run's output.)
        if record.get("job") and not record.get("job_removed"):
            record["job_removed"] = True
            write_record(path, record)
            run("cron", "rm", record["job"])
        out("NO_REPLY")
        return 0
    config = load_config()
    commands = [list(command) for command in config.get("on_event", []) if isinstance(command, list) and command]
    fast = max(2, int(config.get("fast_seconds") or record.get("fast") or FAST_SECONDS))
    said = []
    began = clock()
    while True:
        now = clock()
        try:
            live = find_train(fetch(record["station"], record["system"]), record["train"])
            record["refused"] = 0
        except HTTPError as error:
            if error.code in (403, 429):
                record["refused"] = record.get("refused", 0) + 1
                if record["refused"] >= 6:
                    record["ended"] = "error"
                    event = ended_event(record, "error", f"Stopped following {describe(record)}: the station's board is refusing requests.")
                    record["events"] = (record.get("events", []) + [event])[-KEPT_EVENTS:]
                    deliver(event, commands)
                    said.append(event["message"])
            break
        except Exception:
            break
        events = step(record, live, now)
        if not record.get("ended") and now - record.get("began", now) > MAX_MINUTES * 60:
            record["ended"] = "stopped"
            events.append(ended_event(record, "stopped"))
        record["checked"] = now
        record["events"] = (record.get("events", []) + events)[-KEPT_EVENTS:]
        write_record(path, record)
        for event in events:
            deliver(event, commands)
            if event.get("message"):
                said.append(event["message"])
        # With something to say and no command to say it, say it now: print and exit.
        if record.get("ended") or (said and not commands):
            break
        pause = pause_for(record, now, fast)
        if now - began + pause > BURST_SECONDS:
            break
        sleep(pause)
    write_record(path, record)
    out("\n".join(said) if said and not commands else "NO_REPLY")
    return 0


def schedule(record, path, args, run=openclaw):
    """Adds the scheduler job for a watch. Returns the job id, or None with a reason."""
    here = Path(__file__).resolve()
    command = ["cron", "add", "--every", TICK_EVERY, "--name", f"Train watch: {describe(record)}"[:80],
               "--command-argv", json.dumps([sys.executable, str(here), "tick", "--record", str(path)]), "--json"]
    config = load_config()
    has_commands = any(isinstance(each, list) and each for each in config.get("on_event", []))
    if has_commands:
        command += ["--no-deliver"]                      # the commands do the telling
    else:
        command += ["--announce"]
        channel, chat = origin_chat()
        if args.channel or channel:
            command += ["--channel", str(args.channel or channel).lower()]
        if args.target or chat:
            command += ["--to", str(args.target or chat)]
        if args.thread_id:
            command += ["--thread-id", str(args.thread_id)]
    code, output = run(*command)
    try:
        return json.loads(output)["id"], None
    except Exception:
        return None, (output or "").strip()[-200:] or f"openclaw cron add exited {code}"


# ---------------------------------------------------------------- commands

def start(args):
    path = record_path(args.system, args.station, args.train)
    old = read_record(path)
    if old and not old.get("ended") and (old.get("job") or alive(old.get("pid"))):
        print(json.dumps({"ok": True, "already": True, "watch": public(old)}))
        return 0
    config = load_config()
    commands = [list(command) for command in config.get("on_event", []) if isinstance(command, list) and command]
    commands += [shlex.split(command) for command in args.on_event or []]
    fast = max(2, int(args.fast_seconds or config.get("fast_seconds") or FAST_SECONDS))
    # Nothing set up: answer in the chat this was started from, if that is known.
    reply = None
    if not commands:
        channel, chat = origin_chat()
        reply = reply_command(args.channel or channel, args.target or chat, args.thread_id)
        if reply:
            commands = [reply]
    record = {"train": str(args.train), "station": args.station.upper(), "system": args.system,
              "label": args.label, "arrives": args.arrives, "until": args.until, "began": time.time(),
              "pid": None, "events": [], "reply": reply}
    if args.foreground:
        record["pid"] = os.getpid()
        write_record(path, record)
        watch(path, commands, fast, echo=True)
        return 0
    # On-event commands given for this one watch only work with a process of its own.
    if not args.background and not args.on_event:
        record["fast"] = fast
        record["reply"] = None
        write_record(path, record)
        job, problem = schedule(record, path, args)
        if job:
            record["job"] = job
            write_record(path, record)
            # The scheduler's first run comes a full interval from now. With commands to
            # hand events to, take the first look straight away (the same short run the
            # scheduler does, started here and left to finish by itself), so a display
            # shows the train at once. Without commands the first message has to come
            # from a scheduler run, since that is what delivers it to the chat.
            if commands:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "tick", "--record", str(path)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            print(json.dumps({"ok": True, "watch": public(record), "how": "scheduler",
                              "note": "OpenClaw's scheduler is following it and will deliver what changes."}))
            return 0
        # No scheduler to be had (not an OpenClaw host, or the gateway is down): fall
        # back to a process of its own.
        record["note"] = f"scheduler unavailable: {problem}"
    write_record(path, record)
    command = [sys.executable, str(Path(__file__).resolve()), "run", "--record", str(path), "--fast-seconds", str(fast)]
    for extra in args.on_event or []:
        command += ["--on-event", extra]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    record["pid"] = child.pid
    write_record(path, record)
    answer = {"ok": True, "watch": public(record)}
    if reply:
        answer["note"] = "Messages will be sent to the chat this was started from."
    elif not commands:
        answer["note"] = ("Nothing will be sent anywhere: no on-event command is set and the chat is not known. "
                          "Start it again with --channel and --target, or use `status` to see what happens.")
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
    if not commands and record.get("reply"):
        commands = [record["reply"]]
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
                f"no track yet (expected {a_track(record.get('sched_track'))})" if a_track(record.get("sched_track")) else "no track yet")
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
        if record.get("job"):
            record["ended"] = "stopped"
            record["job_removed"] = True
            write_record(path, record)
            openclaw("cron", "rm", record["job"])
            config = load_config()
            deliver(ended_event(record, "stopped"),
                    [list(each) for each in config.get("on_event", []) if isinstance(each, list) and each])
        else:
            record["stop"] = True
            write_record(path, record)
        stopped.append(public(record))
    print(json.dumps({"ok": True, "stopped": stopped}))
    return 0


def setup_state():
    """"ready": events have somewhere to go. "declined": the user chose not to set it up.
    "missing": never set up."""
    if not CONFIG.exists():
        return "missing"
    config = load_config()
    if any(isinstance(command, list) and command for command in config.get("on_event", [])):
        return "ready"
    return "declined" if config.get("declined") else "missing"


def setup(args):
    state = setup_state()
    if args.show:
        print(json.dumps({"state": state}))
        return 0
    if state == "ready" and not args.force:
        print(json.dumps({"ok": True, "state": "ready", "note": "Already set up; left as it is. Use --force to replace it."}))
        return 0
    config = load_config()
    if args.decline:
        config.update({"on_event": [], "declined": True})
    else:
        command = reply_command(args.channel, args.target, args.thread_id)
        if not command:
            print(json.dumps({"error": "Give --channel and --target (and --thread-id for a forum topic), or --decline."}))
            return 1
        command[0] = "openclaw"                          # found on the PATH when it is run
        config["on_event"] = [command]
        config.pop("declined", None)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    print(json.dumps({"ok": True, "state": setup_state()}))
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
    begin.add_argument("--channel", default=None,
                       help="with no on-event command: the channel to answer on (e.g. telegram)")
    begin.add_argument("--target", default=None,
                       help="with no on-event command: the chat to answer in (default: the one this was started from)")
    begin.add_argument("--thread-id", default=None, help="with --target: the forum topic within that chat")
    begin.add_argument("--fast-seconds", type=int, default=None, help=f"how often to ask while a track is due (default {FAST_SECONDS}, least 2)")
    begin.add_argument("--background", action="store_true",
                       help="keep a process of its own running instead of using OpenClaw's scheduler")
    begin.add_argument("--foreground", action="store_true", help="watch in this process and print each message")
    begin.set_defaults(handler=start)

    ticker = commands.add_parser("tick")                  # internal: one run under the scheduler
    ticker.add_argument("--record", required=True)
    ticker.set_defaults(handler=tick)

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

    ready = commands.add_parser("setup", help="say where a followed train's news goes (once)")
    ready.add_argument("--channel", default=None, help="e.g. telegram")
    ready.add_argument("--target", default=None, help="the chat id to send to")
    ready.add_argument("--thread-id", default=None, help="the forum topic within that chat, if any")
    ready.add_argument("--decline", action="store_true", help="record that the user does not want this")
    ready.add_argument("--show", action="store_true", help="only say whether it is set up")
    ready.add_argument("--force", action="store_true", help="replace what is already set up")
    ready.set_defaults(handler=setup)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
