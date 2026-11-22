"""`updates.py`: the only part of Snipux that talks to the internet.

Every test here injects its own opener. Nothing in this module may reach
the network: a suite that asks GitHub what the newest release is would be
answering a different question on every run, would fail on a machine with
no connection, and would fail again the day a release is cut.
"""

import hashlib
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

        payload = json.dumps(
            {
                "tag_name": "v9.9.9",
                "assets": [{"name": "snipux.exe", "browser_download_url": "https://x/y"}],
            }
        ).encode()
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

        # A Release, not a bare string: the app reads `.version` and the
        # asset URLs off whatever this signal carries.
        assert seen == [updates.Release("9.9.9", {"snipux.exe": "https://x/y"})]

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


    def test_a_reply_cut_off_midway_still_reports(self):
        # IncompleteRead is not an OSError. Escaping the worker, it would
        # leave the tray on "Checking for updates..." with nothing after.
        import http.client
        from PyQt6.QtCore import QCoreApplication, QEventLoop

        def cut_off(_url):
            raise http.client.IncompleteRead(b"{\"tag_")

        check = updates.UpdateCheck(cut_off)
        seen = []
        check.finished.connect(seen.append)

        check.start()
        loop_guard = 0
        while not seen and loop_guard < 200:
            QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
            loop_guard += 1

        assert seen == [None]


class TestTheReleaseItself:
    def test_it_carries_every_asset_by_name(self):
        payload = json.dumps({
            "tag_name": "v1.2.0",
            "assets": [
                {"name": "snipux-setup-1.2.0.exe",
                 "browser_download_url": "https://example/setup.exe"},
                {"name": "snipux_1.2.0_amd64.deb",
                 "browser_download_url": "https://example/deb"},
            ],
        }).encode()

        release = updates.fetch_latest_release(lambda _url: payload)

        assert release.version == "1.2.0"
        assert release.asset("snipux-setup-1.2.0.exe") == "https://example/setup.exe"
        assert release.asset("nothing-like-this") is None

    def test_one_malformed_asset_does_not_lose_the_rest(self):
        payload = json.dumps({
            "tag_name": "v1.2.0",
            "assets": [
                {"name": "good", "browser_download_url": "https://example/good"},
                {"name": "no url here"},
            ],
        }).encode()

        release = updates.fetch_latest_release(lambda _url: payload)

        assert release.asset("good") == "https://example/good"

    def test_a_release_with_no_assets_is_still_a_release(self):
        payload = json.dumps({"tag_name": "v1.2.0"}).encode()

        assert updates.fetch_latest_release(lambda _url: payload).assets == {}

    def test_a_failed_request_is_an_answer(self):
        def refuse(_url):
            raise OSError("no route to host")

        assert updates.fetch_latest_release(refuse) is None


class TestDownloading:
    def test_it_writes_the_file_and_says_so(self, tmp_path):
        target = tmp_path / "setup.exe"

        result = updates.download("https://example/x", target, lambda _url: b"payload")

        assert result is updates.DownloadResult.WRITTEN
        assert target.read_bytes() == b"payload"

    def test_a_cut_download_leaves_nothing_that_looks_finished(self, tmp_path):
        # Written to a .part and renamed only once it is all there: half an
        # installer that looks like an installer is worse than none.
        # A folder of its own: the failure is also noted in snipux.log,
        # which the suite keeps under tmp_path.
        folder = tmp_path / "download"
        folder.mkdir()
        target = folder / "setup.exe"

        def die_midway(_url):
            raise OSError("connection reset")

        result = updates.download("https://example/x", target, die_midway)

        assert result is updates.DownloadResult.FAILED
        assert not target.exists()
        assert list(folder.iterdir()) == []

    def test_it_reports_rather_than_raises(self, tmp_path, capsys):
        def refuse(_url):
            raise OSError("connection reset")

        updates.download("https://example/x", tmp_path / "setup.exe", refuse)

        assert "could not download the update" in capsys.readouterr().out.lower()


class TestTheDownloadIsTheFileThatWasPublished:
    """The installer is run silently with the user's rights, so it is only
    run if it hashes to what the release says it should."""

    PAYLOAD = b"the real installer"
    SHA256 = hashlib.sha256(PAYLOAD).hexdigest()

    def _release(self, digest):
        asset = {"name": "snipux-setup-1.2.0.exe",
                 "browser_download_url": "https://example/setup.exe"}
        if digest is not None:
            asset["digest"] = digest
        payload = json.dumps({"tag_name": "v1.2.0", "assets": [asset]}).encode()
        return updates.fetch_latest_release(lambda _url: payload)

    def test_the_release_carries_the_published_sha256(self):
        release = self._release(f"sha256:{self.SHA256}")

        assert release.sha256("snipux-setup-1.2.0.exe") == self.SHA256

    @pytest.mark.parametrize("digest", [None, "", "md5:abc", "sha256:not-hex", 12345])
    def test_a_missing_or_unreadable_digest_is_no_digest(self, digest):
        assert self._release(digest).sha256("snipux-setup-1.2.0.exe") is None

    def test_a_download_that_matches_is_written(self, tmp_path):
        target = tmp_path / "setup.exe"

        result = updates.download(
            "https://example/x", target, lambda _url: self.PAYLOAD, sha256=self.SHA256
        )

        assert result is updates.DownloadResult.WRITTEN
        assert target.read_bytes() == self.PAYLOAD

    def test_a_download_that_does_not_match_is_never_written(self, tmp_path, capsys):
        folder = tmp_path / "download"
        folder.mkdir()

        result = updates.download(
            "https://example/x", folder / "setup.exe",
            lambda _url: b"something else", sha256=self.SHA256,
        )

        assert result is updates.DownloadResult.MISMATCHED
        assert list(folder.iterdir()) == []
        assert "not the file the release published" in capsys.readouterr().out

    def test_the_thread_reports_a_mismatch_as_no_file(self, tmp_path):
        download = updates.Download(
            "https://example/x", tmp_path / "setup.exe",
            lambda _url: b"something else", sha256=self.SHA256,
        )
        seen = []
        download.finished.connect(seen.append)

        download._run()

        assert seen == [None]
        assert download.result is updates.DownloadResult.MISMATCHED


class TestWhereDownloadsGo:
    def test_each_download_gets_a_folder_of_its_own(self, tmp_path):
        # A fixed name in the shared temp directory is one anybody can
        # guess, and put a file at first.
        first = updates.new_download_dir(tmp_path)
        second = updates.new_download_dir(tmp_path)

        assert first != second
        assert first.is_dir() and first.parent == tmp_path
        assert list(first.iterdir()) == []

    def test_old_downloads_are_removed(self, tmp_path):
        old = updates.new_download_dir(tmp_path)
        (old / "snipux-setup-1.0.0.exe").write_bytes(b"old")
        legacy = tmp_path / "snipux-setup-0.9.0.exe"
        legacy.write_bytes(b"older")
        cut_short = tmp_path / "snipux-setup-0.9.1.exe.part"
        cut_short.write_bytes(b"half")

        updates.remove_old_downloads(tmp_path)

        assert list(tmp_path.iterdir()) == []

    def test_nothing_else_in_the_temp_directory_is_touched(self, tmp_path):
        other = tmp_path / "someone-elses.exe"
        other.write_bytes(b"not ours")
        folder = tmp_path / "another-app"
        folder.mkdir()

        updates.remove_old_downloads(tmp_path)

        assert sorted(path.name for path in tmp_path.iterdir()) == [
            "another-app", "someone-elses.exe"
        ]


class TestDownloadingOverABrokenConnection:
    def test_a_garbled_reply_fails_the_download_instead_of_raising(self, tmp_path):
        import http.client

        def garbled(_url):
            raise http.client.BadStatusLine("HTTP/1.1 ???")

        folder = tmp_path / "download"
        folder.mkdir()

        result = updates.download("https://example/x", folder / "setup.exe", garbled)

        assert result is updates.DownloadResult.FAILED
        assert list(folder.iterdir()) == []
