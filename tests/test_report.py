from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

import pytest
from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
from PyQt6.QtGui import QDesktopServices

from snipux import __version__, platform, report


def _body(url: str) -> str:
    return parse_qs(urlparse(url).query)["body"][0]


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", lambda url: urls.append(bytes(url.toEncoded()).decode())
    )
    return urls


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(platform, "is_linux", lambda: False)
    monkeypatch.setattr(platform, "os_name", lambda: "Windows")


@pytest.fixture
def on_linux(monkeypatch):
    monkeypatch.setattr(platform, "is_linux", lambda: True)
    monkeypatch.setattr(platform, "os_name", lambda: "Linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")


class TestIssueUrl:
    def test_it_opens_a_new_issue_on_the_snipux_repository(self):
        url = report.issue_url("env\n")

        parts = urlparse(url)
        assert (parts.scheme, parts.netloc, parts.path) == (
            "https", "github.com", "/CydoEntis/snipux/issues/new"
        )
        assert set(parse_qs(parts.query)) == {"title", "body"}

    def test_the_body_round_trips_through_the_encoding(self):
        env = "### Environment\n\n- path: C:\\Users\\a b & c=d #e +f ü 🐛\n"

        body = _body(report.issue_url(env))

        assert body.endswith(env)
        assert "### What happened" in body
        assert "### What you expected" in body
        assert "### Steps to reproduce" in body

    def test_nothing_unencoded_reaches_the_query(self):
        query = urlparse(report.issue_url("a b&c=d#e\n")).query

        assert " " not in query and "#" not in query and "\n" not in query
        assert query.count("&") == 1


def _no_os_release():
    raise OSError("no os-release here")


class TestEnvironment:
    def test_windows_has_no_session_line(self, on_windows):
        env = report.environment()

        assert f"- Snipux: {__version__}" in env
        assert "- OS: Windows " in env
        assert f"- Qt: {QT_VERSION_STR}, PyQt6: {PYQT_VERSION_STR}" in env
        assert "Session" not in env

    def test_windows_names_its_release_beside_the_build(self, on_windows, monkeypatch):
        monkeypatch.setattr(report.stdlib_platform, "freedesktop_os_release", _no_os_release)
        monkeypatch.setattr(report.stdlib_platform, "release", lambda: "11")
        monkeypatch.setattr(report.stdlib_platform, "version", lambda: "10.0.26300")

        assert "- OS: Windows 11 (10.0.26300)" in report.environment()

    def test_linux_names_the_session_and_desktop(self, on_linux):
        env = report.environment()

        assert "- OS: Linux " in env
        assert "- Session: wayland, desktop: ubuntu:GNOME" in env

    def test_a_bundle_says_so(self, on_windows, monkeypatch):
        monkeypatch.setattr(report.sys, "frozen", True, raising=False)

        assert "- Installed as: bundled build" in report.environment()

    def test_anything_else_is_a_python_package(self, on_windows, monkeypatch):
        monkeypatch.delattr(report.sys, "frozen", raising=False)

        assert "- Installed as: Python package" in report.environment()


class TestLogTail:
    def test_it_keeps_whole_lines_from_the_end(self):
        text = "first line\nsecond line\nthird line\n"

        tail = report.log_tail(text, len(quote("second line\nthird line\n", safe="")))

        assert tail == "second line\nthird line\n"

    def test_a_line_that_does_not_fit_is_dropped_not_cut(self):
        tail = report.log_tail("aaaa\nbb\n", len("bb\n") + 2)

        assert tail == "bb\n"

    def test_a_log_with_no_trailing_newline_still_ends_on_one(self):
        assert report.log_tail("one\ntwo", 1000) == "one\ntwo\n"

    def test_no_room_means_no_tail(self):
        assert report.log_tail("too long for this\n", 3) == ""


class TestUrlWithLog:
    def test_a_long_log_stays_inside_the_budget(self):
        log = "".join(f"Traceback line {i} in C:\\Users\\cody\\x.py\n" for i in range(2000))

        url = report.issue_url("env\n", log)

        assert len(url) <= report.URL_BUDGET
        assert "Traceback line 1999 in" in _body(url)
        assert "Traceback line 0 in" not in _body(url)

    def test_the_log_sits_in_a_fenced_block_of_whole_lines(self):
        log = "".join(f"line {i}\n" for i in range(5000))

        body = _body(report.issue_url("env\n", log))

        fenced = body.split("```\n")[1]
        assert fenced.startswith("line ") and fenced.endswith("line 4999\n")
        assert body.endswith("```\n")

    def test_a_short_log_goes_in_whole(self):
        body = _body(report.issue_url("env\n", "only line\n"))

        assert "### crash.log (most recent part)\n\n```\nonly line\n```\n" in body


class TestOpenReport:
    def test_no_log_opens_without_asking(self, tmp_path, opened, monkeypatch):
        monkeypatch.setattr(report, "ask_to_include_log", lambda: pytest.fail("asked"))

        url = report.open_report(tmp_path / "crash.log")

        assert opened == [url]
        assert "crash.log" not in _body(url)

    def test_an_empty_log_opens_without_asking(self, tmp_path, opened, monkeypatch):
        (tmp_path / "crash.log").write_text("\n  \n", encoding="utf-8")
        monkeypatch.setattr(report, "ask_to_include_log", lambda: pytest.fail("asked"))

        report.open_report(tmp_path / "crash.log")

        assert len(opened) == 1

    def test_yes_includes_the_log(self, tmp_path, opened, monkeypatch):
        (tmp_path / "crash.log").write_text("--- boom\nValueError\n", encoding="utf-8")
        monkeypatch.setattr(report, "ask_to_include_log", lambda: True)

        url = report.open_report(tmp_path / "crash.log")

        assert "ValueError" in _body(url)
        assert opened == [url]

    def test_no_leaves_the_log_out(self, tmp_path, opened, monkeypatch):
        (tmp_path / "crash.log").write_text("--- boom\nValueError\n", encoding="utf-8")
        monkeypatch.setattr(report, "ask_to_include_log", lambda: False)

        url = report.open_report(tmp_path / "crash.log")

        assert "ValueError" not in _body(url)
        assert opened == [url]

    def test_the_question_mentions_the_user_name(self, monkeypatch):
        seen = {}

        def question(_parent, _title, text, *_args):
            seen["text"] = text
            return report.QMessageBox.StandardButton.No

        monkeypatch.setattr(report.QMessageBox, "question", question)

        assert report.ask_to_include_log() is False
        assert "user name" in seen["text"]
