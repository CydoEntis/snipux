"""The rules that decide what automatic hiding covers.

Every positive case below is text Windows OCR actually returned when reading
rendered screen text -- misspellings, dropped dots and split words included
-- because those are the inputs these rules exist to survive.
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QRectF

from snipux.sensitive import Finding, RecognizedWord, custom_list, find_sensitive

# Fake credentials in this file are written in two joined pieces so that
# no complete token-shaped string appears in the source: GitHub's push
# protection scans for them and cannot tell a test fixture from a leak.

WORD_HEIGHT = 16
WORD_GAP = 6
CHAR_WIDTH = 8


def line(*texts: str, y: float = 0.0) -> list[RecognizedWord]:
    """Words laid out left to right the way OCR reports them."""
    words, x = [], 10.0
    for text in texts:
        width = len(text) * CHAR_WIDTH
        words.append(RecognizedWord(text, QRectF(x, y, width, WORD_HEIGHT)))
        x += width + WORD_GAP
    return words


def kinds(*lines) -> list[str]:
    return [finding.kind for finding in find_sensitive(list(lines))]


class TestEmails:
    @pytest.mark.parametrize("text", [
        "bob.smith@gmail.com",
        "bob.smlth@gmall.com",       # i read as l
        "dev-team@cublx-test.lo",    # i read as l, dot dropped
        "bob.smith@gmailcom",        # dot dropped entirely
    ])
    def test_found_despite_misreads(self, text):
        assert kinds(line("Email:", text)) == ["email"]

    def test_the_label_is_not_covered(self):
        words = line("Email:", "bob.smith@gmail.com")

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[1].image_rect


class TestKeys:
    @pytest.mark.parametrize("text", [
        "sk-ant-" + "api03-Xq7pL2mNvB9cR4tY8wZ1",
        "ghp_" + "8fK2mQgxL4vN7c83pR6tY1wZ5hJ0dS",
        "AKIA" + "lOSFODNN7EXAMPLE",
        "AKIA" + "10SFODNN7EXAMPLE",
        "xoxb-" + "2048-8812-AbCdEf",
        "eyJ" + "hbGciOiJIUzI1NiJ9",
    ])
    def test_known_prefixes(self, text):
        # After a word that is not a label, so the prefix alone has to find it.
        assert kinds(line("Copied", text)) == ["key"]

    def test_a_key_after_a_key_label_is_still_covered(self):
        # The label rule claims it first; what matters is that it is hidden.
        words = line("Key:", "sk-ant-" + "api03-Xq7pL2mNvB9cR4tY8wZ1")

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[1].image_rect

    def test_a_key_ocr_split_in_two_is_one_finding_covering_both(self):
        words = line("Copied", "sk-ant-" + "api03-Xq7", "pL2mNvB9cR4tY8wZ1")

        [finding] = find_sensitive([words])

        assert finding.kind == "key"
        assert finding.image_rect == words[1].image_rect.united(words[2].image_rect)

    def test_a_split_tail_with_no_digit_still_joins(self):
        # `hJOdS`: OCR's reading of the last five characters of a GitHub token.
        words = line("Kev.", "ghp_" + "8fK2mQgxL4vN7c83pR6tY1", "hJOdS")

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[1].image_rect.united(words[2].image_rect)

    def test_a_key_does_not_swallow_the_sentence_after_it(self):
        words = line("sk-ant-" + "api03-Xq7pL2mNvB9cR4tY8wZ1", "Deploy", "finished", "today")

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[0].image_rect

    @pytest.mark.parametrize("after", [
        ("192.168.14.201",),
        ("bob.smith@gmail.com",),
        ("4539", "1488", "0343", "6467"),
        ("v0.5.0",),
    ])
    def test_a_key_stops_at_the_next_value_on_the_line(self, after):
        # A digit alone made these read as more of the key, so one box
        # covered both -- and the value after it was never classified.
        words = line("sk-ant-" + "api03-Xq7pL2mNvB9cR4tY8wZ1", *after)

        findings = find_sensitive([words])

        assert findings[0].kind == "key"
        assert findings[0].image_rect == words[0].image_rect

    def test_a_bare_prefix_is_not_a_key(self):
        assert kinds(line("the", "sk-", "prefix")) == []


class TestLabelledValues:
    """A password has no shape, so the only way to find one is by what it is
    called. The first two cases are the exact words Windows OCR returned
    for a real screenshot where both values were missed."""

    def test_the_api_key_from_the_bug_report(self):
        # `API_KEY=api-123445534534_asdasdasdasdas`, split by OCR at the
        # underscore.
        words = line("API", "KEY=api-123445534534_asdasdasdasdas")

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[1].image_rect

    def test_the_password_from_the_bug_report(self):
        words = line("password", "Test123123")

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[1].image_rect

    @pytest.mark.parametrize("word", [
        "API_KEY=abc123",
        "DB_PASSWORD=hunter",
        "password:hunter2",
        "client_secret=xyz",
        "GITHUB_TOKEN=whatever",
        "AWS_SECRET_ACCESS_KEY=abc",
    ])
    def test_label_and_value_in_one_word_covers_the_word(self, word):
        words = line(word)

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[0].image_rect

    @pytest.mark.parametrize("words", [
        ("password:", "hunter"),
        ("Password:", "correct"),
        ("API_KEY=", "abc"),
        ("token", ":", "plainword"),
        ("key:", "value"),
        ("Secret:", "shh"),
    ])
    def test_a_separator_means_the_next_word_is_hidden_whatever_it_is(self, words):
        laid_out = line(*words)

        [finding] = find_sensitive([laid_out])

        assert finding.image_rect == laid_out[-1].image_rect

    @pytest.mark.parametrize("words", [
        ("password", "Test123123"),
        ("PIN", "4821"),
        ("pwd", "P@ssw0rd"),
        ("Bearer", "abcDEF.ghi"),
    ])
    def test_a_bare_label_hides_a_value_that_looks_like_a_credential(self, words):
        laid_out = line(*words)

        [finding] = find_sensitive([laid_out])

        assert finding.image_rect == laid_out[-1].image_rect

    def test_only_the_value_is_covered_not_the_rest_of_the_sentence(self):
        words = line("password", "Test123123", "is", "wrong")

        [finding] = find_sensitive([words])

        assert finding.image_rect == words[1].image_rect

    @pytest.mark.parametrize("words", [
        ("Press", "any", "key", "to", "continue"),
        ("Forgot", "your", "password", "below"),
        ("Enter", "your", "password"),
        ("Reset", "password", "Reset"),
        ("token", "count", "is", "low"),
        ("keyboard:", "US"),
        ("Time:", "12:30"),
        ("https://example.com/login",),
        ("passage:", "text"),
    ])
    def test_ordinary_sentences_with_label_words_are_left_alone(self, words):
        assert kinds(line(*words)) == []


class TestSecrets:
    def test_a_long_random_token_with_no_known_prefix(self):
        assert kinds(line("token", "Hq8ZpL2mNvB9cR4tY8wZ1kd")) == ["secret"]

    @pytest.mark.parametrize("text", [
        "https://github.com/CydoEntis/snipux/releases/tag/v0.5.0",
        "snipux-backup-before-rewrite-2026-09-13.bundle",
        "2760b4aa-15d7-497a-ad9b-9eaceae121a8",          # UUID: no upper case
        "571a0a9b28cb826e2542dbd63b4385cc88b71330",       # commit hash
        "C:\\Users\\Someone\\AppData\\Local\\Programs2",
        "SupercalifragilisticExpialidocious",             # no digit
    ])
    def test_ordinary_long_tokens_are_left_alone(self, text):
        assert kinds(line(text)) == []


class TestCards:
    @pytest.mark.parametrize("words", [
        ("4539", "1488", "0343", "6467"),
        ("4916-3385-0608-2774",),
        ("4539148803436475",),
        ("3782", "822463", "10005"),       # Amex grouping
        ("5500-0000-0000-0004",),
    ])
    def test_found(self, words):
        assert kinds(line("Card", "number", *words)) == ["card"]

    def test_a_card_in_several_words_is_one_finding_covering_all_of_them(self):
        words = line("Card:", "4539", "1488", "0343", "6467")

        [finding] = find_sensitive([words])

        expected = words[1].image_rect
        for word in words[2:]:
            expected = expected.united(word.image_rect)
        assert finding.image_rect == expected

    def test_a_digit_misread_as_a_letter_is_still_a_card(self):
        # A grouped 4-4-4-4 number with one digit OCR read as `g` no longer
        # passes Luhn, and is still exactly the shape of a card.
        assert kinds(line("Card:", "4539", "1488", "0343", "646g")) == ["card"]


class TestPhones:
    @pytest.mark.parametrize("words", [
        ("(555)", "867-5309"),
        ("+1", "415", "555", "0132"),
        ("555.867.5309",),
        ("+44", "20", "7946", "0958"),
    ])
    def test_found(self, words):
        assert kinds(line("Phone:", *words)) == ["phone"]


class TestIPs:
    def test_found(self):
        assert kinds(line("Ip:", "192.168.14.201")) == ["ip"]

    def test_octets_over_255_are_not_an_address(self):
        assert kinds(line("build", "10.0.26200.1234")) == []


class TestOrdinaryText:
    @pytest.mark.parametrize("words", [
        ("Deploy", "finished", "in", "42s", "with", "0", "warnings."),
        ("Invoice", "paid", "on", "the", "3rd,", "thanks", "for", "the", "quick", "turnaround."),
        ("Released", "2026-09-13"),
        ("snipux", "v0.5.0"),
        ("1802", "passed,", "13", "skipped"),
        ("Copyright", "2026"),
        ("Updated", "at", "12:30:45", "on", "2026-09-13"),
        ("is", "so", "Is", "lol"),
        ("2026-09-13", "2026-09-14"),
        ("Windows", "10.0.26200"),
    ])
    def test_nothing_is_flagged(self, words):
        assert kinds(line(*words)) == []


def at(text: str, x: float, y: float, height: float = WORD_HEIGHT) -> RecognizedWord:
    """One word at an exact position, for rules that read where lines sit
    relative to each other."""
    return RecognizedWord(text, QRectF(x, y, len(text) * CHAR_WIDTH, height))


def hidden_texts(lines) -> set[str]:
    """Every word whose centre some finding covers."""
    findings = find_sensitive(lines)
    return {
        word.text
        for line in lines
        for word in line
        if any(f.image_rect.contains(word.image_rect.center()) for f in findings)
    }


class TestFormFields:
    """OCR returns a form's labels as lines of their own, with the value in
    a box beside or beneath. The positions below are the ones Windows OCR
    reported for a rendered form: labels above boxes on the left, labels
    beside boxes on the right, and a masked password box with nothing in
    it."""

    FORM = [
        [at("Username", 40, 42)], [at("codysmith", 52, 74)],
        [at("Password", 40, 132)],
        [at("Password", 40, 222)], [at("Hunter22", 52, 254)],
        [at("Social", 40, 312), at("Security", 96, 312), at("Number", 168, 312)],
        [at("812-44-9921", 52, 344)],
        [at("Password", 480, 75)], [at("Tr0ub4dor", 622, 74)],
        [at("Email", 480, 165)], [at("bob@gmail.com", 622, 164)],
        [at("PIN", 480, 255)], [at("4821", 622, 254)],
        [at("Address", 480, 345)], [at("Maple", 622, 344), at("Street", 668, 344), at("12", 722, 344)],
    ]

    def test_the_whole_form(self):
        assert hidden_texts(self.FORM) == {
            "Hunter22", "812-44-9921", "Tr0ub4dor", "bob@gmail.com", "4821",
            "Maple", "Street", "12",
        }

    def test_labels_themselves_stay_visible(self):
        hidden = hidden_texts(self.FORM)

        assert not hidden & {"Password", "PIN", "Address", "Social", "Security", "Number"}

    def test_a_username_is_not_treated_as_secret(self):
        assert "codysmith" not in hidden_texts(self.FORM)

    def test_the_same_form_read_twice_still_hides_every_value(self):
        # Recognition reads small images at two sizes; the second reading's
        # lines land a pixel or so from the first's.
        shifted = [[at(w.text, w.image_rect.x() + 1, w.image_rect.y() + 1) for w in line] for line in self.FORM]

        assert hidden_texts(self.FORM + shifted) >= {"Hunter22", "Tr0ub4dor", "4821"}

    def test_qualified_and_required_labels(self):
        lines = [
            [at("Confirm", 40, 40), at("password", 104, 40), at("*", 176, 40)], [at("Hunter22", 52, 70)],
            [at("Date", 40, 140), at("of", 80, 140), at("birth", 100, 140), at("(required)", 144, 140)],
            [at("01/02/1990", 52, 170)],
        ]

        assert hidden_texts(lines) == {"Hunter22", "01/02/1990"}

    def test_a_sentence_mentioning_a_password_is_not_a_label(self):
        lines = [
            [at("Your", 40, 40), at("password", 80, 40), at("must", 152, 40), at("be", 192, 40), at("long", 212, 40)],
            [at("Hunter22", 52, 70)],
        ]

        assert hidden_texts(lines) == set()

    def test_a_value_far_below_its_label_is_left_alone(self):
        lines = [[at("Password", 40, 40)], [at("Hunter22", 52, 400)]]

        assert hidden_texts(lines) == set()

    def test_another_label_is_never_taken_as_the_value(self):
        lines = [[at("Password", 40, 40)], [at("Confirm", 40, 66), at("password", 104, 66)]]

        assert hidden_texts(lines) == set()

    def test_text_on_the_same_row_but_to_the_left_is_not_the_value(self):
        lines = [[at("Hunter22", 40, 40)], [at("PIN", 400, 40)]]

        assert hidden_texts(lines) == set()


class TestFieldLabelsOnTheSameLine:
    @pytest.mark.parametrize("words,expected", [
        (("CVV:", "123"), {"123"}),
        (("Security", "code", "123"), {"123"}),
        (("Date", "of", "birth:", "January", "5,", "1990"), {"January", "5,", "1990"}),
        (("Routing", "number", "021000021"), {"021000021"}),
        (("Address:", "12", "Maple", "Street"), {"12", "Maple", "Street"}),
        (("Passport", "number:", "X1234567"), {"X1234567"}),
        (("Expiry", "12/27"), {"12/27"}),
    ])
    def test_value_after_a_field_label(self, words, expected):
        assert hidden_texts([line(*words)]) == expected

    def test_a_value_stops_at_the_next_label(self):
        found = hidden_texts([line("Account", "number:", "12345678", "Routing", "number:", "021000021")])

        assert found == {"12345678", "021000021"}

    def test_a_value_stops_at_a_wide_gap(self):
        words = [at("Address:", 10, 0), at("12", 90, 0), at("Maple", 114, 0), at("Help", 700, 0)]

        assert hidden_texts([words]) == {"12", "Maple"}

    @pytest.mark.parametrize("words", [
        ("Account", "number", "is", "shown", "below"),
        ("Change", "password"),
        ("zip", "it", "up"),
        ("Your", "address", "is", "private"),
    ])
    def test_sentences_with_label_words_are_left_alone(self, words):
        assert hidden_texts([line(*words)]) == set()


class TestIdentityAndFinance:
    def test_us_social_security_number(self):
        assert kinds(line("SSN", "812-44-9921")) == ["national_id"]

    @pytest.mark.parametrize("text", ["000-12-3456", "666-12-3456", "812-00-9921"])
    def test_impossible_social_security_numbers(self, text):
        assert kinds(line("ref", text)) == []

    def test_uk_national_insurance_number_in_groups(self):
        assert kinds(line("NI:", "AB", "12", "34", "56", "C")) == ["national_id"]

    def test_the_official_specimen_prefix_is_not_a_real_number(self):
        # `QQ 12 34 56 C` is HMRC's published example: Q is never issued as
        # a first letter, which is exactly why it is safe to print.
        assert kinds(line("e.g.", "QQ", "12", "34", "56", "C")) == []

    @pytest.mark.parametrize("words", [
        ("GB82", "WEST", "1234", "5698", "7654", "32"),
        ("DE89370400440532013000",),
    ])
    def test_iban_confirmed_by_its_check_digits(self, words):
        assert kinds(line("Pay", "to", *words)) == ["iban"]

    def test_iban_with_wrong_check_digits_is_not_an_iban(self):
        assert "iban" not in kinds(line("Pay", "GB82", "WEST", "1234", "5698", "7654", "33"))

    def test_an_iban_does_not_swallow_a_word_after_it(self):
        words = line("DE89", "3704", "0044", "0532", "0130", "00", "PAID")

        assert "PAID" not in hidden_texts([words])


class TestNetworkAndCrypto:
    @pytest.mark.parametrize("text,kind", [
        ("2001:db8:85a3::8a2e:370:7334", "ip"),
        ("fe80::1ff:fe23:4567:890a", "ip"),
        ("00:1A:2B:3C:4D:5E", "mac"),
        ("0x" + "a1B2c3D4e5" * 4, "crypto"),
        ("bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", "crypto"),
    ])
    def test_found(self, text, kind):
        assert kinds(line("addr", text)) == [kind]

    @pytest.mark.parametrize("text", ["std::vector", "12:30:45", "a::b", "0x1F"])
    def test_look_alikes_are_left_alone(self, text):
        assert kinds(line(text)) == []


class TestMoreCredentials:
    @pytest.mark.parametrize("text", [
        "glpat-" + "AbCd1234EfGh5678IjKl",
        "hf_" + "AbCdEfGh1234567890",
        "npm_" + "AbCdEfGh1234567890",
        "xai-" + "AbCd1234EfGh5678",
        "whsec_" + "AbCd1234EfGh5678",
        "123456789:" + "ABCdefGhIJKlmNoPQRsTUVwxyZ123456789",
        "MTA4NjI3NzI1NjkxMzU0MTE0Mg." + "GhJkLm." + "abcdefghijklmnopqrstuvwxyz12345",
        "AC" + "0123456789abcdef" * 2,
    ])
    def test_service_tokens(self, text):
        assert kinds(line("Copied", text)) == ["key"]

    def test_a_prefix_on_an_ordinary_word_is_not_a_key(self):
        assert kinds(line("see", "key-bindings-for-vim")) == []

    @pytest.mark.parametrize("url", [
        "https://hooks.slack.com/" + "services/T000/B000/XXXX",
        "https://discord.com/api/" + "webhooks/123/abc",
        "https://example.com/reset?token=abc",
        "https://api.example.com/v1?api_key=abc&x=1",
    ])
    def test_urls_that_carry_a_credential(self, url):
        assert kinds(line(url)) == ["secret"]

    def test_an_ordinary_url_is_left_alone(self):
        assert kinds(line("https://example.com/docs?page=2")) == []

    def test_a_private_key_block_is_hidden_top_to_bottom(self):
        lines = [
            [at("-----BEGIN", 10, 0), at("OPENSSH", 100, 0), at("PRIVATE", 170, 0), at("KEY-----", 240, 0)],
            [at("b3BlbnNzaC1rZXktdjEAAAAABG5vbmU", 10, 20)],
            [at("AAAABAAAAMwAAAAtzc2gtZWQyNTUxOQ", 10, 40)],
            [at("-----END", 10, 60), at("OPENSSH", 90, 60), at("PRIVATE", 160, 60), at("KEY-----", 230, 60)],
            [at("Next", 10, 120), at("paragraph", 50, 120)],
        ]

        hidden = hidden_texts(lines)

        assert {"-----BEGIN", "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU", "AAAABAAAAMwAAAAtzc2gtZWQyNTUxOQ", "-----END"} <= hidden
        assert not hidden & {"Next", "paragraph"}

    def test_a_private_key_block_with_its_end_line_lost_stops_at_the_gap(self):
        lines = [
            [at("-----BEGIN", 10, 0), at("RSA", 100, 0), at("PRIVATE", 140, 0), at("KEY-----", 210, 0)],
            [at("MIIEowIBAAKCAQEAv2x", 10, 20)],
            [at("Unrelated", 10, 200)],
        ]

        hidden = hidden_texts(lines)

        assert "MIIEowIBAAKCAQEAv2x" in hidden
        assert "Unrelated" not in hidden


class TestValuesOcrSplitApart:
    """OCR drops underscores and splits tokens there, leaving a gap the
    width of a space. Every split below is one Windows OCR produced."""

    def test_the_tail_of_the_api_key_from_the_bug_report(self):
        # The second report: the box stopped at `...534`, `_asdasdasdasdas`
        # stayed visible.
        hidden = hidden_texts([line("API", "KEY=api-123445534534", "asdasdasdasdas")])

        assert hidden == {"KEY=api-123445534534", "asdasdasdasdas"}

    @pytest.mark.parametrize("words,expected", [
        (("SECRET", "TOKEN=abc", "defghij", "klmnop"), {"TOKEN=abc", "defghij", "klmnop"}),
        (("DB", "PASS=hunter", "two"), {"PASS=hunter", "two"}),
        (("password:", "Testl", "23123"), {"Testl", "23123"}),
        (("API_KEY", "=", "abc", "def"), {"abc", "def"}),
    ])
    def test_a_tied_value_is_covered_to_the_end_of_the_line(self, words, expected):
        assert hidden_texts([line(*words)]) == expected

    @pytest.mark.parametrize("words,expected", [
        (("password", "Testl", "23123"), {"Testl", "23123"}),
        (("password", "Test", "123123"), {"Test", "123123"}),
        (("password", "Testl", "23123", "is", "wrong"), {"Testl", "23123"}),
    ])
    def test_a_bare_labels_split_value_is_glued_back_together(self, words, expected):
        assert hidden_texts([line(*words)]) == expected

    def test_prose_after_a_tied_value_is_covered_too(self):
        # The deliberate trade: a split token and a value followed by words
        # look the same, and the extra box costs a click.
        assert hidden_texts([line("API_KEY=abc123", "is", "here")]) == {"API_KEY=abc123", "is", "here"}

    def test_a_tied_value_stops_at_a_column_gap(self):
        words = [at("API_KEY=abc123", 10, 0), at("docs", 400, 0)]

        assert hidden_texts([words]) == {"API_KEY=abc123"}

    def test_a_tied_value_stops_at_the_next_pair(self):
        hidden = hidden_texts([line("PASSWORD=hunter", "HOST=db.internal", "PORT=5432")])

        assert hidden == {"PASSWORD=hunter"}

    def test_a_tied_value_stops_at_a_field_label(self):
        hidden = hidden_texts([line("Password:", "hunter", "Account", "number:", "1234")])

        assert "Account" not in hidden and "number:" not in hidden
        assert {"hunter", "1234"} <= hidden

    def test_a_bare_label_before_plain_words_still_hides_nothing(self):
        assert hidden_texts([line("password", "is", "incorrect")]) == set()


def mine(words=(), labels=(), patterns=()):
    return {"words": list(words), "labels": list(labels), "patterns": list(patterns)}


def hidden_with(lines, custom) -> set[str]:
    findings = find_sensitive(lines, custom)
    return {
        word.text
        for line in lines
        for word in line
        if any(f.image_rect.contains(word.image_rect.center()) for f in findings)
    }


class TestTheUsersOwnWords:
    """Things only this user knows are sensitive: an employer, an address,
    a codename. Matched the same tolerant way as everything else, because
    OCR misreads a character or so per word on small text."""

    def test_a_word_entry_is_hidden_wherever_it_appears(self):
        lines = [line("Employer:", "Acme"), line("about", "Acme", "today", y=30)]

        assert hidden_with(lines, mine(words=["Acme"])) == {"Acme"}
        assert len(find_sensitive(lines, mine(words=["Acme"]))) == 2

    def test_a_phrase_spanning_two_words(self):
        words = line("Sold", "by", "Acme", "Corporation", "today")

        assert hidden_with([words], mine(words=["Acme Corporation"])) == {"Acme", "Corporation"}

    @pytest.mark.parametrize("read_as", [
        ("Acrne", "Corporation"),      # rn read as m
        ("Acme", "Corporatlon"),       # i read as l
        ("ACME", "CORPORATION"),       # different case
        ("Acme", "Corporation,"),      # trailing punctuation
    ])
    def test_misreads_of_the_entry_still_match(self, read_as):
        words = line("Sold", "by", *read_as)

        assert hidden_with([words], mine(words=["Acme Corporation"])) == set(read_as)

    def test_a_phrase_ocr_joined_into_one_word(self):
        words = line("Sold", "by", "AcmeCorporation")

        assert hidden_with([words], mine(words=["Acme Corporation"])) == {"AcmeCorporation"}

    def test_a_phrase_ocr_split_into_three(self):
        words = line("Acme", "Corp", "oration", "sells")

        assert hidden_with([words], mine(words=["Acme Corporation"])) == {"Acme", "Corp", "oration"}

    def test_an_unrelated_word_is_not_hidden(self):
        words = line("Acne", "treatment", "advice")

        assert hidden_with([words], mine(words=["Acme Corporation"])) == set()

    def test_each_occurrence_is_its_own_finding(self):
        words = line("Acme", "and", "Acme")

        findings = find_sensitive([words], mine(words=["Acme"]))

        assert len(findings) == 2

    def test_the_kind_says_it_came_from_the_users_list(self):
        [finding] = find_sensitive([line("Acme", "sells")], mine(words=["Acme"]))

        assert finding.kind == "custom"


class TestTheUsersOwnLabels:
    def test_a_value_after_the_users_label(self):
        words = line("Employee", "ID:", "44821")

        assert hidden_with([words], mine(labels=["Employee ID"])) == {"44821"}

    def test_a_value_in_the_box_beneath_the_users_label(self):
        lines = [[at("Employee", 40, 40), at("ID", 120, 40)], [at("E-44821", 52, 70)]]

        assert hidden_with(lines, mine(labels=["Employee ID"])) == {"E-44821"}

    def test_the_label_itself_stays_readable(self):
        words = line("Employee", "ID:", "44821")

        assert "Employee" not in hidden_with([words], mine(labels=["Employee ID"]))

    def test_without_the_entry_nothing_is_hidden(self):
        words = line("Employee", "ID:", "44821")

        assert hidden_with([words], mine()) == set()


class TestTheUsersOwnPatterns:
    def test_a_pattern_hides_what_it_matches(self):
        words = line("Badge", "ACME-123456", "issued")

        assert hidden_with([words], mine(patterns=[r"ACME-\d{6}"])) == {"ACME-123456"}

    def test_a_pattern_covers_every_word_it_touches(self):
        words = line("Badge", "ACME", "123456")

        assert hidden_with([words], mine(patterns=[r"ACME \d{6}"])) == {"ACME", "123456"}

    def test_a_pattern_that_cannot_compile_is_skipped_not_raised(self):
        words = line("Badge", "ACME-123456")

        assert hidden_with([words], mine(patterns=["ACME-[0-9", r"ACME-\d{6}"])) == {"ACME-123456"}

    def test_patterns_ignore_case(self):
        words = line("badge", "acme-123456")

        assert hidden_with([words], mine(patterns=[r"ACME-\d{6}"])) == {"acme-123456"}


class TestTheUsersListAlongsideTheBuiltInRules:
    def test_no_custom_list_behaves_exactly_as_before(self):
        words = line("Deploy", "finished", "in", "42s")

        assert find_sensitive([words]) == find_sensitive([words], mine())

    def test_a_built_in_finding_is_not_doubled_by_a_users_word(self):
        words = line("bob.smith@gmail.com")

        findings = find_sensitive([words], mine(words=["bob.smith@gmail.com"]))

        assert len(findings) == 1

    def test_the_users_entries_and_the_built_in_rules_both_apply(self):
        words = line("Acme", "Corporation", "bob.smith@gmail.com")

        assert hidden_with([words], mine(words=["Acme Corporation"])) == {
            "Acme", "Corporation", "bob.smith@gmail.com"
        }

    def test_a_prepared_list_can_be_reused_across_lines(self):
        prepared = custom_list(mine(words=["Acme Corporation"]))

        first = find_sensitive([line("Acme", "Corporation")], prepared)
        second = find_sensitive([line("Acme", "Corporation")], prepared)

        assert len(first) == len(second) == 1

    def test_an_empty_or_missing_list_is_harmless(self):
        words = line("Acme", "Corporation")

        assert find_sensitive([words], None) == find_sensitive([words], custom_list(None)) == []


class TestShape:
    def test_values_on_separate_lines_are_separate_findings(self):
        found = kinds(
            line("Email:", "bob.smith@gmail.com", y=0),
            line("Phone:", "(555)", "867-5309", y=30),
        )

        assert found == ["email", "phone"]

    def test_several_values_on_one_line(self):
        found = kinds(line("bob.smith@gmail.com", "or", "(555)", "867-5309"))

        assert sorted(found) == ["email", "phone"]

    def test_no_lines_no_findings(self):
        assert find_sensitive([]) == []
        assert find_sensitive([[]]) == []

    def test_findings_are_value_objects(self):
        assert Finding("email", QRectF(0, 0, 1, 1)) == Finding("email", QRectF(0, 0, 1, 1))
