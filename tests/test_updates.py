"""`updates.py`: the only part of Snipux that talks to the internet.

Every test here injects its own opener. Nothing in this module may reach
the network: a suite that asks GitHub what the newest release is would be
answering a different question on every run, would fail on a machine with
no connection, and would fail again the day a release is cut.
"""

import json

import pytest
from PyQt6.QtWidgets import QApplication

from snipux import updates


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestParseVersion:
    def test_a_plain_version(self):
        assert updates.parse_version("1.0.2") == (1, 0, 2)

    def test_the_tag_form_github_returns(self):
        assert updates.parse_version("v1.0.2") == (1, 0, 2)

    def test_a_prerelease_suffix_is_ignored_rather_than_refused(self):
        # Comparing a release candidate equal to its final version is a
        # better answer than raising at someone who only asked whether
        # there is an update.
        assert updates.parse_version("1.1.0-rc1") == (1, 1, 0)

    def test_something_that_is_not_a_version_at_all(self):
        assert updates.parse_version("nightly") is None


class TestIsNewer:
    @pytest.mark.parametrize(
        "candidate, current, expected",
        [
            ("1.0.2", "1.0.1", True),
            ("1.1.0", "1.0.9", True),
            ("2.0.0", "1.9.9", True),
            ("1.0.1", "1.0.1", False),
            ("1.0.0", "1.0.1", False),
            # Numeric, not lexical: "10" is bigger than "9" even though it
            # sorts before it as text.
            ("1.10.0", "1.9.0", True),
            # Zero-padded, so a shorter version is not treated as smaller.
            ("1.1", "1.1.0", False),
            ("1.1.1", "1.1", True),
        ],
    )
    def test_comparisons(self, candidate, current, expected):
        assert updates.is_newer(candidate, current) is expected

    def test_an_unreadable_version_is_never_newer(self):
        # Telling someone to go and download something on the strength of a
        # string nobody could parse is worse than saying nothing.
        assert updates.is_newer("nightly", "1.0.1") is False
        assert updates.is_newer("1.0.2", "unknown") is False


class TestFetchLatestVersion:
    def test_it_reads_the_tag_github_reports(self):
        payload = json.dumps({"tag_name": "v1.2.3"}).encode()

        assert updates.fetch_latest_version(lambda _url: payload) == "1.2.3"

    def test_it_asks_the_releases_api(self):
        asked = []

        def opener(url):
            asked.append(url)
            return json.dumps({"tag_name": "v1.0.1"}).encode()

        updates.fetch_latest_version(opener)

        assert asked == [updates.LATEST_RELEASE_URL]

    def test_a_network_failure_is_an_answer_not_an_error(self):
        def refuse(_url):
            raise OSError("getaddrinfo failed")

        assert updates.fetch_latest_version(refuse) is None

    def test_a_response_that_is_not_json(self):
        assert updates.fetch_latest_version(lambda _url: b"<html>rate limited</html>") is None

    def test_a_json_shape_that_changed(self):
        payload = json.dumps({"name": "1.2.3"}).encode()

        assert updates.fetch_latest_version(lambda _url: payload) is None

    def test_an_empty_tag(self):
        payload = json.dumps({"tag_name": "v"}).encode()

        assert updates.fetch_latest_version(lambda _url: payload) is None


class TestUpdateCheckRunsOffTheCallingThread:
    def test_the_result_arrives_through_the_signal(self):
        from PyQt6.QtCore import QCoreApplication, QEventLoop

        payload = json.dumps({"tag_name": "v9.9.9"}).encode()
        check = updates.UpdateCheck(lambda _url: payload)
        seen = []
        check.finished.connect(seen.append)

        check.start()
        # The worker is a real thread, so pump until it reports -- with a
        # ceiling, so a broken implementation fails the test rather than
        # hanging the suite.
        loop_guard = 0
        while not seen and loop_guard < 200:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
            loop_guard += 1

        assert seen == ["9.9.9"]

    def test_a_failed_check_reports_none(self):
        from PyQt6.QtCore import QCoreApplication, QEventLoop

        def refuse(_url):
            raise OSError("no route to host")

        check = updates.UpdateCheck(refuse)
        seen = []
        check.finished.connect(seen.append)

        check.start()
        loop_guard = 0
        while not seen and loop_guard < 200:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
            loop_guard += 1

        assert seen == [None]
