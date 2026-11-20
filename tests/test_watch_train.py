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


def live(track=None, otp=0, minutes=15, sched="19", canceled=False):
    status = {"canceled": canceled}
    if otp is not None:
        status["otp"] = otp
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

    def test_nothing_new_says_nothing(self):
        r = record()
        step(r, live(), NOW)
        assert step(r, live(), NOW + 5) == []

    def test_small_change_is_a_quiet_update(self):
        r = record()
        step(r, live(), NOW)
        events = step(r, live(otp=60), NOW + 5)
        assert kinds(events) == ["update"]
        assert events[0]["message"] is None and events[0]["delay_min"] == 1
        assert step(r, live(otp=60), NOW + 10) == []

    def test_messages_start_with_a_capital(self):
        r = record(label="the 5:22 PM to Huntington")
        step(r, live(), NOW)
        events = step(r, live(otp=300), NOW + 5)
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
        step(r, live(otp=300), NOW)
        events = step(r, live(track="19", otp=300), NOW)
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
        assert all(event["message"] is None for event in step(r, live(otp=60), NOW + 5))

    def test_delay_change_is_news(self):
        r = record()
        step(r, live(), NOW)
        events = step(r, live(otp=300), NOW + 5)
        assert kinds(events) == ["delay"]
        assert "now 5 min late (was 0)" in events[0]["message"]
        assert events[0]["delay_min"] == 5

    def test_back_on_time(self):
        r = record()
        step(r, live(otp=300), NOW)
        events = step(r, live(otp=0), NOW + 5)
        assert events[0]["message"].startswith("5:22 PM to Huntington is back on time")

    def test_started_late(self):
        r = record()
        events = step(r, live(otp=240), NOW)
        assert "4 min late" in events[0]["message"]
        assert r["leaves"] == NOW + 15 * 60 + 240

    def test_no_live_report_yet(self):
        r = record()
        events = step(r, live(otp=None), NOW)
        assert "no live report on it yet" in events[0]["message"]
        # the first report afterwards is news, even "on time"
        events = step(r, live(otp=0), NOW + 5)
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
