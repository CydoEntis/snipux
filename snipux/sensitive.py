"""Find sensitive values in recognised text, and say where they are.

Pure text work: words come in with their boxes, findings go out with
theirs. No OCR, no platform code and no widgets live here, so every rule
can be exercised headless against the strings real OCR actually produces.

**Everything is judged for position, not spelling.** OCR misreads screen
text in consistent ways -- `gmail` as `gmall`, `9` as `g`, `I` as `l`, a
space dropped into the middle of a key, `API_KEY` split at its underscore
-- and none of that matters, because the only thing a caller does with a
finding is cover it. So the rules are deliberately tolerant: a box over
something that was not sensitive costs the user one click to remove, and a
missed value costs them the value.

Three kinds of rule, run in this order:

1. **Shape** -- a value recognised by what it looks like: an email, a card
   number that passes Luhn, an IBAN that passes mod-97, a key with a
   service's prefix, a long random token.
2. **Label, same line** -- a value recognised by what it is called:
   `API_KEY=...`, `password: hunter2`, `CVV 123`, `Date of birth: 1990-01-02`.
   A password has no shape at all, so a label is the only way one is found.
3. **Label, nearby line** -- a web form, where OCR returns every label as
   a line of its own and the value sits in a box beside or beneath it.
   Rects are what connect the two.

Labels match the words *inside* a name -- `OPENAI_API_KEY`, `MY_APP_SECRET`
and `stripe-token` all carry one -- because nobody can know what a variable
will be called. What no rule can do is find a short password with no label
anywhere near it: `hunter22` alone in a paragraph is indistinguishable from
an ordinary word.

**Coordinates.** Every rect here is in the pixel space of the image that
was recognised, and nothing in this module converts it. The caller knows
how that image relates to what is on screen; this module does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PyQt6.QtCore import QRectF


@dataclass(frozen=True)
class RecognizedWord:
    """One word OCR returned. `image_rect` is in recognised-image pixels."""

    text: str
    image_rect: QRectF


@dataclass(frozen=True)
class Finding:
    """A value to hide. `image_rect` is in recognised-image pixels and
    covers every word the value spans."""

    kind: str
    image_rect: QRectF


# ---------------------------------------------------------------- shapes

# Prefixes of credentials issued by services whose keys end up in
# screenshots: OpenAI/Anthropic (`sk-`), Stripe (`sk_`, `rk_`, `pk_live_`,
# `whsec_`), GitHub (`ghp_` and family, `github_pat_`), GitLab (`glpat-`),
# Slack (`xox?-`, `xapp-`), AWS (`AKIA`, `ASIA`), Google (`AIza`, `ya29.`,
# `GOCSPX-`), SendGrid (`SG.`), Mailgun (`key-`), npm, PyPI, Hugging Face,
# Shopify, DigitalOcean, Square, Supabase, Linear, Notion (`secret_`),
# Atlassian (`ATATT`), Replicate, Groq, xAI, Grafana, and JWTs (`eyJ`, the
# base64 of `{"`). Case-insensitive, because OCR is not reliable about case.
_KEY_PREFIX = re.compile(
    r"^(sk-|sk_|rk_|pk_live_|whsec_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|gldt-|"
    r"xox[abposr]-|xapp-|akia|asia|aiza|ya29\.|gocspx-|sg\.|key-|npm_|pypi-|hf_|"
    r"shpat_|shpss_|shpca_|shppa_|dop_v1_|doo_v1_|sq0atp-|sq0csp-|sbp_|lin_api_|"
    r"secret_|atatt|r8_|gsk_|xai-|glc_|eyj)",
    re.IGNORECASE,
)
_KEY_MIN_LENGTH = 12

_TELEGRAM_BOT_TOKEN = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{35}$")
_DISCORD_BOT_TOKEN = re.compile(r"^[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6,7}\.[A-Za-z\d_-]{27,40}$")
_TWILIO_SID = re.compile(r"^(AC|SK)[0-9a-fA-F]{32}$")
# URLs that are credentials in themselves.
_SECRET_URL = re.compile(
    r"(hooks\.slack\.com/|discord(app)?\.com/api/webhooks/|webhook\.office\.com/|"
    r"[?&][\w.-]*(token|key|secret|password|passwd|pwd|sig|signature|auth|session|sessionid|code)=)",
    re.IGNORECASE,
)
_ETHEREUM = re.compile(r"^0x([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_BITCOIN_BECH32 = re.compile(r"^(bc1|tb1)[02-9ac-hj-np-z]{25,87}$")
_MAC = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_EMAIL = re.compile(r"\w@\w")
_IP = re.compile(r"^\(?(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\)?[,.;:]?$")
_US_SSN = re.compile(r"(?<![\w-])(?!000|666|9\d\d)\d{3}[- ](?!00)\d{2}[- ](?!0000)\d{4}(?![\w-])")
_UK_NI = re.compile(r"(?<!\w)(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D](?!\w)")
_IBAN_START = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]*$")
_IBAN_GROUP = re.compile(r"^[A-Z0-9]{1,4}$")
_CARD_TEXT = re.compile(r"^[\d -]+$")
_CARD_GROUPED = re.compile(r"^\d{4}(?:[ -]?\d{4}){2}[ -]?\d{1,7}$")
_PHONE = (
    # North American: (555) 867-5309, 555.867.5309, +1 415 555 0132
    re.compile(r"^(?:\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}$"),
    # Written with a leading + or a trunk 0: +44 20 7946 0958, 0412 345 678
    re.compile(r"^(?:\+|0)\d[\d ().-]{7,16}\d$"),
)

# Characters Windows OCR was measured substituting for digits in screen
# fonts. Kept to those: every extra mapping turns more ordinary words into
# "numbers" (`so` would read as `50`). Applied only when judging whether a
# word is a number, never to the text of anything else.
_AS_DIGITS = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "g": "9", "B": "8"})
_PUNCTUATION = ".,;:()[]{}\"'"
_SECRET_MIN_LENGTH = 20

# ---------------------------------------------------------------- labels

# Parts of a *name* -- a variable, a config key, a query parameter -- that
# mark its value as a credential. When a `:` or `=` ties the name to a
# value, the value is hidden whatever it looks like and however short. With
# only a space between, the value must also look like a credential,
# because "forgot your password below" is a sentence.
_STRONG_NAME_PARTS = frozenset({
    "password", "passwd", "pwd", "passcode", "passphrase", "secret", "token",
    "apikey", "privatekey", "pin", "otp", "totp", "mfa", "2fa", "bearer",
    "cvv", "cvc", "ssn", "iban", "session", "sessionid", "cookie", "csrf", "jwt",
})
# Too common as words to trust without a `:` or `=` ("press any key"), and
# only as the last part of a name, so `KEY` in `API_KEY` counts and
# `keyboard` does not.
_WEAK_NAME_PARTS = frozenset({"key", "pass", "auth", "credential", "credentials", "cred", "sig", "signature"})
_NAME_AND_VALUE = re.compile(r"^(?P<label>[A-Za-z][A-Za-z0-9_.-]*?)(?P<sep>[:=])(?P<value>.*)$")

# Field labels as people write them on forms and pages. A *token* label's
# value is one word (a password has no spaces); a *text* label's value runs
# on (an address, a name, a date written out, a grouped account number).
_TOKEN_LABELS = (
    "password", "passcode", "passphrase", "pin", "pin code", "pwd", "secret",
    "secret key", "api key", "api token", "api secret", "access token", "access key",
    "auth token", "token", "private key", "client secret", "recovery code",
    "recovery codes", "backup code", "backup codes", "verification code",
    "security code", "one-time code", "one-time password", "one time code", "otp",
    "2fa code", "mfa code", "authenticator code", "wifi password", "wi-fi password",
    "network key", "network password", "license key", "product key", "activation key",
    "seed phrase", "recovery phrase", "mnemonic", "session id", "session token",
    "cookie", "cvv", "cvc", "cvv2", "cvc2", "csc",
)
_TEXT_LABELS = (
    "card number", "credit card number", "debit card number", "card no", "expiry",
    "expiry date", "expiration", "expiration date", "exp date", "valid thru",
    "valid through", "cardholder name", "name on card", "account number",
    "bank account number", "account no", "routing number", "sort code", "iban",
    "bsb", "tax id", "tax number", "social security number", "ssn",
    "social insurance number", "national insurance number", "ni number",
    "national id", "national id number", "id number", "passport number",
    "passport no", "driver's license", "drivers license", "driver license",
    "driver's license number", "license number", "date of birth", "birth date",
    "birthdate", "birthday", "dob", "place of birth", "mother's maiden name",
    "maiden name", "security answer", "secret answer", "medical record number",
    "health card number", "health insurance number", "insurance number",
    "policy number", "member id", "medicare number", "nhs number", "full name",
    "first name", "last name", "legal name", "middle name", "surname", "address",
    "street address", "home address", "billing address", "shipping address",
    "mailing address", "address line 1", "address line 2", "postal code",
    "zip code", "zip", "postcode", "phone number", "mobile number", "cell number",
    "telephone", "email address", "ip address", "vin",
)
_LABELS: dict[tuple[str, ...], str] = {
    **{tuple(label.split()): "token" for label in _TOKEN_LABELS},
    **{tuple(label.split()): "text" for label in _TEXT_LABELS},
}
_LABEL_LENGTHS = sorted({len(label) for label in _LABELS}, reverse=True)
# Words that may come before a label on a form without changing what the
# field holds: "Confirm password", "Enter your PIN", "Current password".
_LABEL_QUALIFIERS = frozenset({
    "new", "confirm", "current", "old", "repeat", "re-enter", "reenter", "retype",
    "your", "enter", "verify", "primary", "secondary", "admin", "master", "root",
    "user", "account",
})
_LABEL_TRAILERS = frozenset({"required", "optional"})

# Form geometry, in multiples of the label's own text height -- a scale
# that holds at every zoom level and display density.
_BESIDE_MAX_GAP = 25.0      # label to the value in the box to its right
_BESIDE_ROW_SLOP = 0.6      # how far apart two centres on one row may be
_BELOW_MAX_GAP = 3.0        # label to the value in the box beneath it
_BELOW_LEFT_SLOP = 3.0      # the box may start a little left of its label
_BELOW_LEFT_REACH = 15.0    # ...or indented this far right of it
_SAME_VALUE_GAP = 3.0       # a wider gap inside a line ends a text value


def find_sensitive(lines: list[list[RecognizedWord]]) -> list[Finding]:
    """Every sensitive value in `lines`.

    `lines` is OCR's own grouping: each inner list is one line of text in
    reading order. Lines may come from more than one reading of the same
    image; nothing here assumes they are unique or sorted.
    """
    lines = [list(line) for line in lines if line]
    taken: set[tuple[int, int]] = set()
    findings: list[Finding] = []

    def claim(kind: str, line_index: int, indexes) -> None:
        indexes = [i for i in indexes if (line_index, i) not in taken]
        if not indexes:
            return
        taken.update((line_index, i) for i in indexes)
        rect = QRectF(lines[line_index][indexes[0]].image_rect)
        for i in indexes[1:]:
            rect = rect.united(lines[line_index][i].image_rect)
        findings.append(Finding(kind, rect))

    for line_index, line in enumerate(lines):
        local: set[int] = set()
        for rule in (_tied_values_in_line, _shapes_in_line, _labels_in_line):
            for kind, indexes in rule(line, local):
                local.update(indexes)
                claim(kind, line_index, indexes)

    for kind, line_index, indexes in _private_key_blocks(lines, taken):
        claim(kind, line_index, indexes)
    for kind, line_index, indexes in _form_fields(lines, taken):
        claim(kind, line_index, indexes)
    return findings


# ------------------------------------------------------- tied values

# A tied value ends at a gap wider than this many of the previous word's
# own characters. Measured on Windows OCR: a dropped underscore inside a
# token and a real space both leave 0.5-1.4 characters, so no gap can tell
# them apart -- only a column-sized gap reliably ends a value.
_TIED_VALUE_MAX_GAP = 2.5
# Adjacent words glued back together when a bare label's value was split.
_SPLIT_VALUE_MAX_GAP = 1.5
_SPLIT_VALUE_MAX_WORDS = 3


def _tied_values_in_line(words: list[RecognizedWord], taken: set[int]):
    """Values a `:` or `=` ties to a credential name -- `API_KEY=...`,
    `password: ...`, `DB_PASS = ...` -- taken whole, before any shape rule
    can claim a fragment of one.

    Whole means to the end of the line. OCR drops underscores and splits
    tokens there (`KEY=api-123445534534` + `asdasdasdasdas`), and the gap it
    leaves is the width of a space, so a split token and a value followed
    by words look identical. Config and `.env` lines end with the value,
    and where prose does follow one, covering it costs a click; leaving the
    tail of a key showing costs the key. A value still ends at a
    column-sized gap or at the next `NAME=` pair.
    """
    texts = [word.text for word in words]
    normalised = [_normalise(text) for text in texts]
    local = set(taken)
    for index in range(len(texts)):
        if index in local:
            continue
        text = texts[index].strip("\"'`,;()[]{}")
        match = _NAME_AND_VALUE.match(text)
        if match and _name_strength(match["label"]):
            start = index if match["value"].strip("\"'` ") else index + 1
        elif _name_strength(text.strip("?!")) and index + 1 < len(texts) and texts[index + 1].strip() in (":", "="):
            start = index + 2
        else:
            continue
        if start >= len(texts) or start in local:
            continue
        indexes = _rest_of_value(words, [start], local, normalised)
        local.update(indexes)
        yield "labelled", indexes


def _rest_of_value(words: list[RecognizedWord], indexes: list[int], taken: set[int], normalised: list[str]) -> list[int]:
    run = list(indexes)
    while True:
        last, following = run[-1], run[-1] + 1
        if following >= len(words) or following in taken:
            break
        gap = words[following].image_rect.left() - words[last].image_rect.right()
        if gap > _TIED_VALUE_MAX_GAP * _char_width(words[last]):
            break
        if _starts_new_pair(words[following].text) or _label_at(normalised, following):
            break
        run.append(following)
    return run


def _shortest_credential_run(words: list[RecognizedWord], start: int, taken: set[int]) -> list[int] | None:
    """The fewest adjacent words from `start` that, glued together, look
    like a credential -- `Testl` + `23123` is how OCR read `Test123123` --
    plus whatever code-like words continue it. None when nothing within
    reach does, so a sentence after a label is left alone."""
    texts = [word.text for word in words]
    run: list[int] = []
    for index in range(start, min(start + _SPLIT_VALUE_MAX_WORDS, len(words))):
        if index in taken:
            break
        if run:
            gap = words[index].image_rect.left() - words[run[-1]].image_rect.right()
            if gap > _SPLIT_VALUE_MAX_GAP * _char_width(words[run[-1]]):
                break
        run.append(index)
        if _looks_like_credential("".join(texts[i] for i in run)):
            return _with_continuations(texts, run, taken)
    return None


def _starts_new_pair(text: str) -> bool:
    match = _NAME_AND_VALUE.match(text.strip("\"'`,;()[]{}"))
    return bool(match) and len(match["label"]) >= 2


def _char_width(word: RecognizedWord) -> float:
    return word.image_rect.width() / max(1, len(word.text))


# ------------------------------------------------------------ shape rules

def _shapes_in_line(words: list[RecognizedWord], taken: set[int]):
    texts = [word.text for word in words]
    local = set(taken)

    def free(indexes) -> bool:
        return not any(i in local for i in indexes)

    # Several-word shapes first, matched across the whole line, so a
    # national insurance number's groups are not first read as numbers.
    for pattern, kind in ((_US_SSN, "national_id"), (_UK_NI, "national_id")):
        for indexes in _pattern_words(texts, pattern):
            if free(indexes):
                local.update(indexes)
                yield kind, indexes
    for indexes in _ibans(texts, local):
        local.update(indexes)
        yield "iban", indexes

    for index, text in enumerate(texts):
        if index in local:
            continue
        kind = _single_word_kind(text)
        if kind is None:
            continue
        indexes = [index]
        if kind == "key":
            indexes = _with_continuations(texts, indexes, local)
        local.update(indexes)
        yield kind, indexes

    for kind, indexes in _number_runs(texts, local):
        local.update(indexes)
        yield kind, indexes


def _single_word_kind(text: str) -> str | None:
    bare = text.strip(_PUNCTUATION)
    if _SECRET_URL.search(text):
        return "secret"
    if _EMAIL.search(text):
        return "email"
    if _KEY_PREFIX.match(bare) and len(bare) >= _KEY_MIN_LENGTH and _has_randomness(bare):
        return "key"
    if _TELEGRAM_BOT_TOKEN.match(bare) or _DISCORD_BOT_TOKEN.match(bare) or _TWILIO_SID.match(bare):
        return "key"
    if _ETHEREUM.match(bare) or _BITCOIN_BECH32.match(bare):
        return "crypto"
    if _MAC.match(bare):
        return "mac"
    if _is_ipv6(bare):
        return "ip"
    if _looks_like_secret(bare):
        return "secret"
    if _is_ip(text):
        return "ip"
    return None


def _pattern_words(texts: list[str], pattern: re.Pattern):
    """Word indexes covered by each match of `pattern` over the line's text,
    rebuilt with single spaces. A word only partly inside a match is
    covered whole -- OCR gives no character positions to cut it at."""
    offsets, position = [], 0
    for text in texts:
        offsets.append((position, position + len(text)))
        position += len(text) + 1
    joined = " ".join(texts)
    for match in pattern.finditer(joined):
        indexes = [i for i, (start, end) in enumerate(offsets) if start < match.end() and end > match.start()]
        if indexes:
            yield indexes


def _ibans(texts: list[str], taken: set[int]):
    """IBANs, written whole or in groups of four, confirmed by their own
    mod-97 check digits -- which is what lets an IBAN be found by shape
    alone without flagging every capitalised code on screen."""
    index = 0
    while index < len(texts):
        if index in taken or not _IBAN_START.match(texts[index]):
            index += 1
            continue
        found = None
        last = index
        while last + 1 < len(texts) and last + 1 not in taken and last - index < 9 and _IBAN_GROUP.match(texts[last + 1]):
            last += 1
        for stop in range(last, index - 1, -1):
            compact = "".join(texts[index:stop + 1])
            if 15 <= len(compact) <= 34 and _iban_checks(compact):
                found = list(range(index, stop + 1))
                break
        if found:
            yield found
            index = found[-1] + 1
        else:
            index += 1


def _iban_checks(compact: str) -> bool:
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


def _is_ipv6(bare: str) -> bool:
    address = bare.strip("[]").split("%")[0].split("/")[0]
    if not re.fullmatch(r"[0-9a-fA-F:]+", address) or address.count(":") < 2:
        return False
    if "::" not in address and address.count(":") != 7:
        return False
    groups = [group for group in address.split(":") if group]
    return (
        len(groups) >= 2
        and all(len(group) <= 4 for group in groups)
        and any(len(group) >= 3 for group in groups)
    )


def _number_runs(texts: list[str], taken: set[int]):
    """Cards and phone numbers, which OCR returns as several words.

    Groups consecutive number-like words, then tries the longest stretch of
    each group first so `4539 1488 0343 6467` is one card rather than four
    fragments that match nothing.
    """
    index = 0
    while index < len(texts):
        if index in taken or not _is_numberish(texts[index]):
            index += 1
            continue
        end = index
        while end + 1 < len(texts) and end + 1 not in taken and _is_numberish(texts[end + 1]):
            end += 1

        start = index
        while start <= end:
            match = None
            for stop in range(end, start - 1, -1):
                kind = _number_kind(texts[start:stop + 1])
                if kind:
                    match = (kind, list(range(start, stop + 1)))
                    break
            if match:
                yield match
                start = match[1][-1] + 1
            else:
                start += 1
        index = end + 1


def _number_kind(run: list[str]) -> str | None:
    joined = " ".join(run).translate(_AS_DIGITS)
    digits = re.sub(r"\D", "", joined)
    if _CARD_TEXT.match(joined) and 13 <= len(digits) <= 19:
        if _luhn(digits) or _CARD_GROUPED.match(joined):
            return "card"
    if 10 <= len(digits) <= 15 and any(pattern.match(joined) for pattern in _PHONE):
        return "phone"
    return None


def _is_numberish(text: str) -> bool:
    """Mostly digits once OCR's usual substitutions are undone, and made
    only of what a card or phone number is written with.

    At least one real digit is required: a substitution can repair a
    misread number, but a word with no digit at all is a word.
    """
    if not any(char.isdigit() for char in text):
        return False
    mapped = text.translate(_AS_DIGITS)
    if not re.fullmatch(r"[\d()+ .-]+", mapped):
        return False
    alphanumeric = [char for char in text if char.isalnum()]
    return bool(alphanumeric) and sum(c.isdigit() for c in mapped) / len(alphanumeric) >= 0.7


def _is_ip(text: str) -> bool:
    match = _IP.match(text.translate(_AS_DIGITS))
    return bool(match) and all(int(octet) <= 255 for octet in match.groups())


def _has_randomness(bare: str) -> bool:
    """Real issued keys carry digits or mixed case; `key-bindings` does not."""
    return any(char.isdigit() for char in bare) or bool(re.search(r"[a-z][A-Z]|[A-Z][a-z].*[A-Z]", bare))


def _looks_like_secret(bare: str) -> bool:
    """A long token with no known prefix that reads as random: upper case,
    lower case and digits all present.

    Words containing `/`, `.`, `\\`, `:` or `@` are paths, URLs, file names
    and addresses, which clear the same bar without being secrets. Mixed
    case is required so that lowercase hex -- commit hashes, UUIDs -- is
    left alone.
    """
    if len(bare) < _SECRET_MIN_LENGTH:
        return False
    if any(char in bare for char in "/.\\:@"):
        return False
    return (
        any(char.isupper() for char in bare)
        and any(char.islower() for char in bare)
        and any(char.isdigit() for char in bare)
    )


def _luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


# ------------------------------------------------------ same-line labels

def _labels_in_line(words: list[RecognizedWord], taken: set[int]):
    texts = [word.text for word in words]
    local = set(taken)

    for index in range(len(texts)):
        indexes = _named_value(words, index, local)
        if indexes:
            local.update(indexes)
            yield "labelled", indexes

    normalised = [_normalise(text) for text in texts]
    height = max(word.image_rect.height() for word in words)
    index = 0
    while index < len(texts):
        match = _label_at(normalised, index)
        if not match or index in local:
            index += 1
            continue
        length, category = match
        last = index + length - 1
        explicit = texts[last].rstrip().endswith((":", "="))
        value = last + 1
        if value < len(texts) and texts[value].strip() in (":", "=", "-", "–"):
            explicit, value = True, value + 1
        if value >= len(texts) or value in local:
            index = last + 1
            continue
        if category == "token":
            indexes = (
                _rest_of_value(words, [value], local, normalised) if explicit
                else _shortest_credential_run(words, value, local)
            )
            if indexes:
                local.update(indexes)
                yield "labelled", indexes
        elif explicit or _looks_like_value(texts[value]):
            end = value
            while (
                end + 1 < len(texts)
                and end + 1 not in local
                and not _label_at(normalised, end + 1)
                and words[end + 1].image_rect.left() - words[end].image_rect.right() <= _SAME_VALUE_GAP * height
            ):
                end += 1
            indexes = list(range(value, end + 1))
            local.update(indexes)
            yield "labelled", indexes
        index = last + 1


def _named_value(words: list[RecognizedWord], index: int, taken: set[int]) -> list[int] | None:
    """The value after a bare credential name with no `:` or `=` --
    `password Test123123`, `PIN 4821` -- when what follows looks like a
    credential. Tied names are `_tied_values_in_line`'s.
    """
    if index in taken:
        return None
    text = words[index].text.strip("\"'`,;()[]{}")
    if _NAME_AND_VALUE.match(text) or _name_strength(text.strip("?!")) != "strong":
        return None
    following = index + 1
    if following >= len(words) or following in taken or words[following].text.strip() in (":", "="):
        return None
    return _shortest_credential_run(words, following, taken)


def _name_strength(label: str) -> str | None:
    """`"strong"`, `"weak"` or None for a name that might mark a credential.

    Names are split at `_`, `-` and `.`, so `DB_PASSWORD`, `client-secret`
    and `aws.secret.key` all count, and joined again so `api_key` reads as
    `apikey`.
    """
    parts = [part.lower() for part in re.split(r"[_.\-]+", label) if part]
    if not parts:
        return None
    if "".join(parts) in _STRONG_NAME_PARTS or any(part in _STRONG_NAME_PARTS for part in parts):
        return "strong"
    if parts[-1] in _WEAK_NAME_PARTS:
        return "weak"
    return None


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9'-]", "", text.lower().replace("’", "'"))


def _label_at(normalised: list[str], index: int) -> tuple[int, str] | None:
    """(word count, category) of the longest field label starting at
    `index`, or None."""
    for length in _LABEL_LENGTHS:
        category = _LABELS.get(tuple(normalised[index:index + length]))
        if category and index + length <= len(normalised):
            return length, category
    return None


def _looks_like_credential(text: str) -> bool:
    """Could this word, sitting after a bare label, be a password? It has
    a digit, a symbol, or a capital after a lowercase letter -- which
    `Reset` and `below` do not. Short is fine: PINs are four digits."""
    bare = text.strip(_PUNCTUATION + "?!")
    if len(bare) < 3:
        return False
    return (
        any(char.isdigit() for char in bare)
        or any(char in "!#$%^&*_-+=~@" for char in bare)
        or bool(re.search(r"[a-z][A-Z]", bare))
    )


def _looks_like_value(text: str) -> bool:
    """A text label's value, with no `:` to say so, has to start with
    something no sentence starts with: a digit, an `@`, or a credential."""
    return any(char.isdigit() for char in text) or "@" in text or _looks_like_credential(text)


def _with_continuations(texts: list[str], indexes: list[int], taken: set[int]) -> list[int]:
    """`indexes` plus whatever words straight after it are more of the same
    value. OCR splits long keys where the font leaves a slightly wider gap
    between two glyphs, so a value keeps the code-like words that follow."""
    run = list(indexes)
    follower = run[-1] + 1
    while follower < len(texts) and follower not in taken and _continues_key(texts[follower]):
        run.append(follower)
        follower += 1
    return run


def _continues_key(text: str) -> bool:
    """Whether a word after a key is more of that key rather than the next
    thing on the line. Code-like, and not a value of its own: an address,
    a number or anything with a dot or `@` in it starts something new."""
    if any(char in text for char in ".@") or _is_numberish(text):
        return False
    return _looks_like_code(text)


def _looks_like_code(text: str) -> bool:
    """A fragment of a split key: has a digit, an underscore or dash, or a
    capital after a lowercase letter. Plain words -- including a capitalised
    one like `Deploy` -- are not."""
    bare = text.strip(_PUNCTUATION)
    if len(bare) < 4:
        return False
    return (
        any(char.isdigit() for char in bare)
        or "_" in bare or "-" in bare
        or bool(re.search(r"[a-z][A-Z]", bare))
    )


# ------------------------------------------------------- cross-line rules

def _line_rect(line: list[RecognizedWord]) -> QRectF:
    rect = QRectF(line[0].image_rect)
    for word in line[1:]:
        rect = rect.united(word.image_rect)
    return rect


def _line_height(line: list[RecognizedWord]) -> float:
    return max(word.image_rect.height() for word in line)


def _free_indexes(lines, line_index, taken) -> list[int]:
    return [i for i in range(len(lines[line_index])) if (line_index, i) not in taken]


def _private_key_blocks(lines: list[list[RecognizedWord]], taken):
    """Everything from a `-----BEGIN ... PRIVATE KEY-----` line down to its
    `-----END` line, or, when OCR lost the END line, down to the end of the
    column of lines beneath it. A private key is dozens of lines of base64
    no other rule would ever connect."""
    texts = [" ".join(word.text for word in line).upper() for line in lines]
    rects = [_line_rect(line) for line in lines]
    order = sorted(range(len(lines)), key=lambda i: rects[i].top())
    for position, begin in enumerate(order):
        if "BEGIN" not in texts[begin] or "PRIVATE" not in texts[begin]:
            continue
        height = _line_height(lines[begin])
        left_limit = rects[begin].left() - 6 * height
        bottom = rects[begin].bottom()
        for following in order[position + 1:]:
            rect = rects[following]
            if rect.left() < left_limit:
                continue
            if rect.top() - bottom > 1.5 * height:
                break
            bottom = max(bottom, rect.bottom())
            if "END" in texts[following] and "PRIVATE" in texts[following]:
                break
        for line_index in range(len(lines)):
            rect = rects[line_index]
            if rects[begin].top() <= rect.center().y() <= bottom and rect.left() >= left_limit:
                indexes = _free_indexes(lines, line_index, taken)
                if indexes:
                    yield "private_key", line_index, indexes


def _form_fields(lines: list[list[RecognizedWord]], taken):
    """Values in boxes beside or beneath a field label that OCR returned
    as a line of its own -- the shape of nearly every web form."""
    normalised = [[_normalise(word.text) for word in line] for line in lines]
    is_label = [_is_label_line(words) for words in normalised]
    rects = [_line_rect(line) for line in lines]

    for label_index, line in enumerate(lines):
        if not is_label[label_index]:
            continue
        label = rects[label_index]
        height = _line_height(line)
        beside, below = None, None
        for index, rect in enumerate(rects):
            if index == label_index or is_label[index] or not _free_indexes(lines, index, taken):
                continue
            if _label_at([w for w in normalised[index] if w], 0):
                continue
            if abs(rect.center().y() - label.center().y()) <= _BESIDE_ROW_SLOP * height:
                gap = rect.left() - label.right()
                if -0.5 * height <= gap <= _BESIDE_MAX_GAP * height and (beside is None or gap < beside[0]):
                    beside = (gap, index)
            gap = rect.top() - label.bottom()
            if (
                -0.3 * height <= gap <= _BELOW_MAX_GAP * height
                and label.left() - _BELOW_LEFT_SLOP * height <= rect.left() <= label.left() + _BELOW_LEFT_REACH * height
                and (below is None or gap < below[0])
            ):
                below = (gap, index)
        chosen = beside or below
        if chosen:
            value_index = chosen[1]
            yield "labelled", value_index, _free_indexes(lines, value_index, taken)


def _is_label_line(normalised: list[str]) -> bool:
    """Whether a whole line is a field label and nothing else:
    `Password`, `Confirm password *`, `Date of birth (required)`."""
    words = [word for word in normalised if word]
    while words and words[-1] in _LABEL_TRAILERS:
        words.pop()
    while len(words) > 1 and words[0] in _LABEL_QUALIFIERS and tuple(words) not in _LABELS:
        words.pop(0)
    return bool(words) and tuple(words) in _LABELS
