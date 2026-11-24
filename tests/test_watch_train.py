"""Tests for watch_train (no network, no waiting)."""
import json
import os
import sys
from urllib.error import HTTPError

import pytest

import watch_train
from watch_train import step, pause_for

NOW = 1_800_000_000.0


def record(**extra):
    base = {"train": "1582", "station": "NYK", "system": "lirr", "label": "5:22 PM to Huntington", "until": "departure"}
    base.update(extra)
    return base


def live(track=None, late=0, minutes=15, sched="19", canceled=False):
    """The train as the board lists it. `late`: seconds behind its timetable (below zero:
    ahead of it), None for no live report. The board's own figure, `otp`, counts the
    other way round: below zero is late."""
    status = {"canceled": canceled}
    if late is not None:
        status["otp"] = -late
    return {"train_num": "1582", "time": NOW + minutes * 60, "track": track, "sched_track": sched, "status": status}


def kinds(events):
    return [event["event"] for event in events]


class TestStep:
    def test_first_look_says_it_is_followed(self):
        r = record()
        events = step(r, live(), NOW)
        assert kinds(events) == ["started"]
        assert events[0]["message"] == "Following 5:22 PM to Huntington: on time, expected on track 19."
        assert r["delay_min"] == 0 and r["leaves"] == NOW + 15 * 60

    def test_a_through_stations_code_is_not_said_as_a_track(self):
        # A through station's board schedules trains on codes of its own (G3, R7).
        r = record()
        events = step(r, live(sched="G3"), NOW)
        assert events[0]["message"] == "Following 5:22 PM to Huntington: on time."
        assert events[0]["sched_track"] == "G3"          # still reported as the board has it

    def test_nothing_new_says_nothing(self):
        r = record()
        step(r, live(), NOW)
        assert step(r, live(), NOW + 5) == []

    def test_small_change_is_a_quiet_update(self):
        r = record()
        step(r, live(), NOW)
        events = step(r, live(late=60), NOW + 5)
        assert kinds(events) == ["update"]
        assert events[0]["message"] is None and events[0]["delay_min"] == 1
        assert step(r, live(late=60), NOW + 10) == []

    def test_messages_start_with_a_capital(self):
        r = record(label="the 5:22 PM to Huntington")
        step(r, live(), NOW)
        events = step(r, live(late=300), NOW + 5)
        assert events[0]["message"].startswith("The 5:22 PM to Huntington is now 5 min late")

    def test_track_posted_as_expected(self):
        r = record()
        step(r, live(), NOW)
        events = step(r, live(track="19", minutes=10), NOW)
        assert kinds(events) == ["track"]
        assert events[0]["message"] == "Track 19, as expected: 5:22 PM to Huntington, leaving in 10 min."
        assert events[0]["track"] == "19"

    def test_track_message_gives_the_arrival_when_known_and_on_time(self):
        r = record(arrives="6:14 PM")
        step(r, live(), NOW)
        events = step(r, live(track="19", minutes=10), NOW)
        assert events[0]["message"] == "Track 19, as expected: 5:22 PM to Huntington, leaving in 10 min, arrives 6:14 PM."

    def test_arrival_is_left_out_when_late(self):
        r = record(arrives="6:14 PM")
        step(r, live(late=300), NOW)
        events = step(r, live(track="19", late=300), NOW)
        assert "arrives" not in events[0]["message"]

    def test_arriving_is_said_once(self):
        r = record()
        step(r, live(track="19"), NOW)
        pulling_in = dict(live(track="19"), stop_status="ARRIVING")
        events = step(r, pulling_in, NOW + 5)
        assert kinds(events) == ["arriving"]
        assert events[0]["message"] == "5:22 PM to Huntington is arriving now, track 19."
        assert step(r, pulling_in, NOW + 10) == []

    def test_track_differs_from_expected(self):
        r = record()
        step(r, live(), NOW)
        events = step(r, live(track="17"), NOW)
        assert "Track 17, expected 19" in events[0]["message"]

    def test_platform_letter_is_not_compared_with_a_scheduled_code(self):
        r = record()
        step(r, live(sched="H1"), NOW)
        events = step(r, live(track="A", sched="H1"), NOW)
        assert events[0]["message"].startswith("Track A: 5:22 PM to Huntington")

    def test_track_is_said_once(self):
        r = record()
        step(r, live(), NOW)
        step(r, live(track="19"), NOW)
        assert step(r, live(track="19"), NOW + 5) == []

    def test_track_change_is_said(self):
        r = record()
        step(r, live(), NOW)
        step(r, live(track="19"), NOW)
        events = step(r, live(track="20"), NOW + 5)
        assert "Track 20 (was 19)" in events[0]["message"]

    def test_until_track_ends_the_watch(self):
        r = record(until="track")
        step(r, live(), NOW)
        step(r, live(track="19"), NOW)
        assert r["ended"] == "track"

    def test_small_delay_change_is_not_news(self):
        r = record()
        step(r, live(), NOW)
        assert all(event["message"] is None for event in step(r, live(late=60), NOW + 5))

    def test_delay_change_is_news(self):
        r = record()
        step(r, live(), NOW)
        events = step(r, live(late=300), NOW + 5)
        assert kinds(events) == ["delay"]
        assert "now 5 min late (was 0)" in events[0]["message"]
        assert events[0]["delay_min"] == 5

    def test_back_on_time(self):
        r = record()
        step(r, live(late=300), NOW)
        events = step(r, live(late=0), NOW + 5)
        assert events[0]["message"].startswith("5:22 PM to Huntington is back on time")

    def test_started_late(self):
        r = record()
        events = step(r, live(late=240), NOW)
        assert "4 min late" in events[0]["message"]
        assert r["leaves"] == NOW + 15 * 60 + 240

    def test_late_is_below_zero_on_the_board(self):
        # As the New Hyde Park board had the 8:13 on 2026-11-24: due at its timetable
        # time, 369 seconds behind. Read the other way round it was "on time".
        r = record()
        board = {"train_num": "1582", "time": NOW + 300, "track": "3", "sched_track": "G3",
                 "status": {"canceled": False, "otp": -369, "otp_location": "NA1"}}
        events = step(r, board, NOW)
        assert "6 min late" in events[0]["message"] and "on time" not in events[0]["message"]
        assert events[0]["delay_min"] == 6 and events[0]["leaves"] == NOW + 300 + 369
        assert events[0]["scheduled"] == NOW + 300       # the timetable's time, for "was 8:13"
        # ... and it has not left three minutes after its timetable time
        assert kinds(step(r, board, NOW + 300 + 200)) == []
        assert kinds(step(r, board, NOW + 300 + 369 + watch_train.GONE_SECONDS + 1)) == ["departed"]

    def test_ahead_of_time_is_on_time(self):
        r = record()
        board = {"train_num": "1582", "time": NOW + 300, "track": None, "sched_track": "19",
                 "status": {"canceled": False, "otp": 185}}
        events = step(r, board, NOW)
        assert "on time" in events[0]["message"]
        assert r["delay_min"] == 0 and r["leaves"] == NOW + 300

    def test_no_live_report_yet(self):
        r = record()
        events = step(r, live(late=None), NOW)
        assert "no live report on it yet" in events[0]["message"]
        # the first report afterwards is news, even "on time"
        events = step(r, live(late=0), NOW + 5)
        assert kinds(events) == ["delay"] and "is on time" in events[0]["message"]

    def test_canceled_ends_the_watch(self):
        r = record()
        step(r, live(), NOW)
        events = step(r, live(canceled=True), NOW + 5)
        assert kinds(events) == ["canceled"]
        assert r["ended"] == "canceled"

    def test_departed_a_few_minutes_after_its_time(self):
        r = record()
        step(r, live(minutes=1), NOW)
        assert step(r, None, NOW + 60 + 100) == []
        events = step(r, None, NOW + 60 + 200)
        assert kinds(events) == ["departed"]
        assert events[0]["message"] is None
        assert r["ended"] == "departed"

    def test_not_on_the_board_yet_waits(self):
        r = record()
        assert step(r, None, NOW) == []
        assert "ended" not in r

    def test_default_name_without_a_label(self):
        r = record(label=None)
        events = step(r, live(), NOW)
        assert events[0]["message"].startswith("Following train 1582 at Penn Station")


class TestPause:
    def test_slow_when_far_off_or_unknown(self):
        assert pause_for({}, NOW) == watch_train.SLOW_SECONDS
        assert pause_for({"leaves": NOW + 40 * 60}, NOW) == watch_train.SLOW_SECONDS

    def test_fast_when_a_track_is_due(self):
        assert pause_for({"leaves": NOW + 10 * 60}, NOW) == watch_train.FAST_SECONDS
        assert pause_for({"leaves": NOW + 10 * 60}, NOW, fast=2) == 2

    def test_slower_once_the_track_is_known(self):
        assert pause_for({"leaves": NOW + 10 * 60, "track": "19"}, NOW) == watch_train.DELAY_SECONDS


class TestDeliver:
    def test_command_gets_the_message_and_the_event(self, tmp_path):
        out = tmp_path / "out.txt"
        script = tmp_path / "hook.py"
        script.write_text("import sys, json\nopen(sys.argv[1], 'w').write(sys.argv[2] + '|' + json.load(sys.stdin)['event'])\n")
        watch_train.deliver({"event": "track", "message": "Track 19"}, [[sys.executable, str(script), str(out), "{message}"]])
        assert out.read_text() == "Track 19|track"

    def test_message_command_is_skipped_when_there_is_nothing_to_say(self, tmp_path):
        out = tmp_path / "out.txt"
        script = tmp_path / "hook.py"
        script.write_text("import sys\nopen(sys.argv[1], 'w').write('ran')\n")
        watch_train.deliver({"event": "departed", "message": None}, [[sys.executable, str(script), str(out), "{message}"]])
        assert not out.exists()
        watch_train.deliver({"event": "departed", "message": None}, [[sys.executable, str(script), str(out)]])
        assert out.read_text() == "ran"

    def test_a_failing_command_does_not_raise(self):
        watch_train.deliver({"event": "track", "message": "x"}, [["/no/such/program", "{message}"]])


class TestWatch:
    """The loop, with the board and the waiting replaced."""

    def _run(self, tmp_path, boards, record_extra=None, monkeypatch=None):
        path = tmp_path / "watch.json"
        rec = record(pid=os.getpid(), events=[], **(record_extra or {}))
        watch_train.write_record(path, rec)
        boards = iter(boards)
        said = []

        def fetch(station, system):
            board = next(boards)
            if isinstance(board, Exception):
                raise board
            return {"arrivals": [board] if board else []}

        monkeypatch.setattr(watch_train, "deliver", lambda event, commands, echo=False: said.append(event))
        watch_train.watch(path, [], 5, sleep=lambda seconds: None, fetch=fetch)
        return said, watch_train.read_record(path)

    def test_follows_to_the_track_and_ends(self, tmp_path, monkeypatch):
        now = watch_train.time.time()
        def entry(track=None):
            return {"train_num": "1582", "time": now + 600, "track": track, "sched_track": "19", "status": {"canceled": False, "otp": 0}}
        said, rec = self._run(tmp_path, [entry(), entry(), entry("19")], {"until": "track"}, monkeypatch)
        said = [event for event in said if event["event"] != "update"]
        assert kinds(said) == ["started", "track"]
        assert rec["ended"] == "track" and rec["track"] == "19"

    def test_gives_up_when_the_board_refuses(self, tmp_path, monkeypatch):
        refused = HTTPError("u", 429, "Too Many Requests", {}, None)
        said, rec = self._run(tmp_path, [refused, refused, refused], None, monkeypatch)
        assert kinds(said) == ["error"]
        assert rec["ended"] == "error"

    def test_stops_when_asked(self, tmp_path, monkeypatch):
        path = tmp_path / "watch.json"
        watch_train.write_record(path, record(pid=os.getpid(), events=[], stop=True))
        said = []
        monkeypatch.setattr(watch_train, "deliver", lambda event, commands, echo=False: said.append(event))
        watch_train.watch(path, [], 5, sleep=lambda seconds: None, fetch=lambda *a: pytest.fail("should not ask the board"))
        assert kinds(said) == ["stopped"]


class TestCli:
    def test_status_with_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(watch_train, "WATCH_DIR", tmp_path / "none")
        assert watch_train.main(["status", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == {"watching": []}

    def test_status_lists_a_running_watch_and_stop_marks_it(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(watch_train, "WATCH_DIR", tmp_path)
        path = tmp_path / "lirr-NYK-1582.json"
        watch_train.write_record(path, record(pid=os.getpid(), track="19", events=[{"event": "track", "message": "Track 19"}]))
        watch_train.main(["status", "--json"])
        watching = json.loads(capsys.readouterr().out)["watching"]
        assert watching[0]["train"] == "1582" and watching[0]["track"] == "19" and watching[0]["last_said"] == "Track 19"
        watch_train.main(["stop", "--train", "1582"])
        assert json.loads(capsys.readouterr().out)["stopped"][0]["train"] == "1582"
        assert watch_train.read_record(path)["stop"] is True

    def test_finished_watches_are_not_listed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(watch_train, "WATCH_DIR", tmp_path)
        watch_train.write_record(tmp_path / "lirr-NYK-1.json", record(train="1", pid=os.getpid(), ended="departed"))
        watch_train.main(["status", "--json"])
        assert json.loads(capsys.readouterr().out) == {"watching": []}


class TestOriginChat:
    def test_reads_the_chat_openclaw_passes(self):
        env = {"OPENCLAW_CHANNEL_CONTEXT": json.dumps({"senderId": "42", "chatId": "12345"})}
        assert watch_train.origin_chat(env) == (None, "12345")

    def test_reads_the_nested_form_and_a_channel(self):
        env = {"OPENCLAW_CHANNEL_CONTEXT": json.dumps({"channel": "telegram", "chat": {"id": -100123}})}
        assert watch_train.origin_chat(env) == ("telegram", "-100123")

    def test_missing_or_broken_is_nothing(self):
        assert watch_train.origin_chat({}) == (None, None)
        assert watch_train.origin_chat({"OPENCLAW_CHANNEL_CONTEXT": "not json"}) == (None, None)
        assert watch_train.origin_chat({"OPENCLAW_CHANNEL_CONTEXT": "[1]"}) == (None, None)

    def test_reply_command_needs_both_parts(self):
        assert watch_train.reply_command("telegram", None) is None
        assert watch_train.reply_command(None, "12345") is None
        command = watch_train.reply_command("telegram", "12345")
        assert command[1:] == ["message", "send", "--channel", "telegram", "--target", "12345", "--message", "{message}"]
        assert "--thread-id" in watch_train.reply_command("telegram", "12345", "7")


class TestStartDefaults:
    """What `start` decides to do with messages, without starting a watcher."""

    def _start(self, tmp_path, monkeypatch, capsys, argv, env=None, config=None):
        monkeypatch.setattr(watch_train, "WATCH_DIR", tmp_path / "watch")
        monkeypatch.setattr(watch_train, "CONFIG", tmp_path / "watch.json")
        if config is not None:
            (tmp_path / "watch.json").write_text(json.dumps(config))
        monkeypatch.delenv("OPENCLAW_CHANNEL_CONTEXT", raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)

        class Child:
            pid = os.getpid()
        monkeypatch.setattr(watch_train.subprocess, "Popen", lambda *a, **k: Child())
        watch_train.main(["start", "--train", "1582", "--background", *argv])
        answer = json.loads(capsys.readouterr().out)
        return answer, watch_train.read_record(watch_train.record_path("lirr", "NYK", "1582"))

    def test_answers_in_the_chat_it_was_started_from(self, tmp_path, monkeypatch, capsys):
        env = {"OPENCLAW_CHANNEL_CONTEXT": json.dumps({"chatId": "12345"})}
        answer, rec = self._start(tmp_path, monkeypatch, capsys, ["--channel", "telegram"], env)
        assert "chat this was started from" in answer["note"]
        assert rec["reply"][-6:] == ["--channel", "telegram", "--target", "12345", "--message", "{message}"]

    def test_a_configured_command_wins(self, tmp_path, monkeypatch, capsys):
        env = {"OPENCLAW_CHANNEL_CONTEXT": json.dumps({"chatId": "12345"})}
        answer, rec = self._start(tmp_path, monkeypatch, capsys, ["--channel", "telegram"], env, {"on_event": [["echo", "{message}"]]})
        assert rec["reply"] is None and "note" not in answer

    def test_says_so_when_there_is_nowhere_to_send(self, tmp_path, monkeypatch, capsys):
        answer, rec = self._start(tmp_path, monkeypatch, capsys, [])
        assert rec["reply"] is None and "Nothing will be sent" in answer["note"]


class TestSetup:
    def _run(self, tmp_path, monkeypatch, capsys, argv):
        monkeypatch.setattr(watch_train, "CONFIG", tmp_path / "watch.json")
        code = watch_train.main(["setup", *argv])
        return code, json.loads(capsys.readouterr().out)

    def test_missing_until_set_up(self, tmp_path, monkeypatch, capsys):
        assert self._run(tmp_path, monkeypatch, capsys, ["--show"])[1] == {"state": "missing"}

    def test_writes_the_send_command_for_a_chat_and_topic(self, tmp_path, monkeypatch, capsys):
        code, answer = self._run(tmp_path, monkeypatch, capsys, ["--channel", "Telegram", "--target", "-100123", "--thread-id", "7"])
        assert code == 0 and answer["state"] == "ready"
        command = json.loads((tmp_path / "watch.json").read_text())["on_event"][0]
        assert command == ["openclaw", "message", "send", "--channel", "telegram", "--target", "-100123",
                           "--thread-id", "7", "--message", "{message}"]

    def test_leaves_an_existing_setup_alone(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "watch.json").write_text(json.dumps({"on_event": [["mine", "{message}"]], "fast_seconds": 2}))
        code, answer = self._run(tmp_path, monkeypatch, capsys, ["--channel", "telegram", "--target", "1"])
        assert "left as it is" in answer["note"]
        assert json.loads((tmp_path / "watch.json").read_text())["on_event"] == [["mine", "{message}"]]

    def test_force_replaces_but_keeps_other_settings(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "watch.json").write_text(json.dumps({"on_event": [["mine"]], "fast_seconds": 2}))
        self._run(tmp_path, monkeypatch, capsys, ["--channel", "telegram", "--target", "1", "--force"])
        config = json.loads((tmp_path / "watch.json").read_text())
        assert config["on_event"][0][0] == "openclaw" and config["fast_seconds"] == 2

    def test_decline_is_remembered(self, tmp_path, monkeypatch, capsys):
        assert self._run(tmp_path, monkeypatch, capsys, ["--decline"])[1]["state"] == "declined"
        assert self._run(tmp_path, monkeypatch, capsys, ["--show"])[1] == {"state": "declined"}

    def test_needs_an_address(self, tmp_path, monkeypatch, capsys):
        code, answer = self._run(tmp_path, monkeypatch, capsys, ["--channel", "telegram"])
        assert code == 1 and "error" in answer
        assert not (tmp_path / "watch.json").exists()


class TestTick:
    """One run under the scheduler, with the board, the clock and openclaw replaced."""

    def _tick(self, tmp_path, monkeypatch, boards, rec=None, config=None):
        monkeypatch.setattr(watch_train, "CONFIG", tmp_path / "watch.json")
        if config is not None:
            (tmp_path / "watch.json").write_text(json.dumps(config))
        path = tmp_path / "lirr-NYK-1582.json"
        watch_train.write_record(path, rec or record(began=NOW, events=[], job="job-1"))
        boards = iter(boards)
        clock = {"now": NOW}
        printed, ran, handed = [], [], []

        def fetch(station, system):
            board = next(boards)
            if isinstance(board, Exception):
                raise board
            return {"arrivals": [board] if board else []}

        def sleep(seconds):
            clock["now"] += seconds

        monkeypatch.setattr(watch_train, "deliver", lambda event, commands, echo=False: handed.append((event["event"], bool(commands))))
        import argparse
        code = watch_train.tick(argparse.Namespace(record=str(path)), fetch=fetch, sleep=sleep, clock=lambda: clock["now"],
                                run=lambda *a, **k: (ran.append(a), (0, ""))[1], out=printed.append)
        return code, printed, ran, watch_train.read_record(path), handed

    def test_quiet_run_prints_no_reply(self, tmp_path, monkeypatch):
        rec = record(began=NOW, events=[], job="job-1", said_started=True, said_delay=0, shown=[None, 0, round((NOW + 40 * 60) / 30)])
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [live(minutes=40)], rec)
        assert code == 0 and printed == ["NO_REPLY"] and ran == []

    def test_first_run_says_it_is_followed_and_exits(self, tmp_path, monkeypatch):
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [live(), live()])
        assert printed == ["Following 5:22 PM to Huntington: on time, expected on track 19."]
        assert rec["said_started"] is True

    def test_looks_several_times_when_a_track_is_due_and_reports_it(self, tmp_path, monkeypatch):
        rec = record(began=NOW, events=[], job="job-1")
        watch_train.step(rec, live(), NOW)
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [live(), live(), live(track="19")], rec)
        assert printed and printed[0].startswith("Track 19, as expected")
        assert rec["track"] == "19"

    def test_a_burst_ends_by_itself(self, tmp_path, monkeypatch):
        rec = record(began=NOW, events=[], job="job-1")
        watch_train.step(rec, live(), NOW)
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [live()] * 50, rec)
        assert printed == ["NO_REPLY"]

    def test_the_run_after_the_end_removes_the_job_quietly(self, tmp_path, monkeypatch):
        rec = record(began=NOW, events=[], job="job-1", ended="departed")
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [], rec)
        assert printed == ["NO_REPLY"] and ran == [("cron", "rm", "job-1")] and rec["job_removed"] is True
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [], rec)
        assert ran == []

    def test_with_commands_set_it_hands_events_over_and_prints_nothing(self, tmp_path, monkeypatch):
        code, printed, ran, rec, handed = self._tick(tmp_path, monkeypatch, [live(), live()], config={"on_event": [["mine"]]})
        assert printed == ["NO_REPLY"] and ("started", True) in handed

    def test_a_refusing_board_ends_it_after_several_runs(self, tmp_path, monkeypatch):
        refused = HTTPError("u", 429, "Too Many Requests", {}, None)
        rec = record(began=NOW, events=[], job="job-1", refused=5)
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [refused], rec)
        assert rec["ended"] == "error" and "refusing requests" in printed[0]

    def test_a_board_that_cannot_be_reached_is_just_a_quiet_run(self, tmp_path, monkeypatch):
        code, printed, ran, rec, _ = self._tick(tmp_path, monkeypatch, [OSError("down")])
        assert code == 0 and printed == ["NO_REPLY"] and "ended" not in rec


class TestSchedule:
    def _schedule(self, tmp_path, monkeypatch, argv, config=None):
        monkeypatch.setattr(watch_train, "CONFIG", tmp_path / "watch.json")
        if config is not None:
            (tmp_path / "watch.json").write_text(json.dumps(config))
        monkeypatch.delenv("OPENCLAW_CHANNEL_CONTEXT", raising=False)
        import argparse
        args = argparse.Namespace(channel=None, target=None, thread_id=None)
        for key, value in zip(argv[::2], argv[1::2]):
            setattr(args, key, value)
        ran = []
        job, problem = watch_train.schedule(record(), tmp_path / "r.json", args,
                                            run=lambda *a, **k: (ran.append(list(a)), (0, json.dumps({"id": "job-9"})))[1])
        return job, ran[0]

    def test_adds_a_job_that_announces_to_the_chat(self, tmp_path, monkeypatch):
        job, command = self._schedule(tmp_path, monkeypatch, ["channel", "Telegram", "target", "-100123", "thread_id", "7"])
        assert job == "job-9"
        assert command[:4] == ["cron", "add", "--every", "20s"]
        assert "--announce" in command and command[command.index("--channel") + 1] == "telegram"
        assert command[command.index("--to") + 1] == "-100123" and command[command.index("--thread-id") + 1] == "7"
        argv = json.loads(command[command.index("--command-argv") + 1])
        assert argv[1].endswith("watch_train.py") and argv[2:4] == ["tick", "--record"]

    def test_leaves_the_destination_to_openclaw_when_none_is_given(self, tmp_path, monkeypatch):
        job, command = self._schedule(tmp_path, monkeypatch, [])
        assert "--announce" in command and "--to" not in command

    def test_does_not_announce_when_commands_do_the_telling(self, tmp_path, monkeypatch):
        job, command = self._schedule(tmp_path, monkeypatch, [], {"on_event": [["mine"]]})
        assert "--no-deliver" in command and "--announce" not in command

    def test_a_failed_add_gives_the_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watch_train, "CONFIG", tmp_path / "watch.json")
        import argparse
        job, problem = watch_train.schedule(record(), tmp_path / "r.json", argparse.Namespace(channel=None, target=None, thread_id=None),
                                            run=lambda *a, **k: (1, "gateway not running"))
        assert job is None and "gateway not running" in problem


class TestStartUnderTheScheduler:
    def _start(self, tmp_path, monkeypatch, capsys, config):
        monkeypatch.setattr(watch_train, "WATCH_DIR", tmp_path / "watch")
        monkeypatch.setattr(watch_train, "CONFIG", tmp_path / "watch.json")
        if config is not None:
            (tmp_path / "watch.json").write_text(json.dumps(config))
        monkeypatch.delenv("OPENCLAW_CHANNEL_CONTEXT", raising=False)
        monkeypatch.setattr(watch_train, "schedule", lambda record, path, args, run=None: ("job-5", None))
        started = []
        monkeypatch.setattr(watch_train.subprocess, "Popen", lambda argv, **k: started.append(argv))
        watch_train.main(["start", "--train", "1582"])
        return json.loads(capsys.readouterr().out), started

    def test_with_commands_it_takes_the_first_look_at_once(self, tmp_path, monkeypatch, capsys):
        answer, started = self._start(tmp_path, monkeypatch, capsys, {"on_event": [["mine"]]})
        assert answer["how"] == "scheduler"
        assert len(started) == 1 and started[0][2:4] == ["tick", "--record"]

    def test_without_commands_the_scheduler_delivers_the_first_message(self, tmp_path, monkeypatch, capsys):
        answer, started = self._start(tmp_path, monkeypatch, capsys, None)
        assert answer["how"] == "scheduler" and started == []
