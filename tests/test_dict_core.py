"""Offline tests for the GoldenDict program dictionaries and the Anki card templates.

Run from the project root:

    python -m unittest discover -s tests -v

No internet access is needed. Web services are replaced with fakes, and the
connection routes are tested against small servers and proxies on 127.0.0.1.
"""

import contextlib
import hashlib
import http.server
import io
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dict_core as dc  # noqa: E402

NOT_FOUND = object()

DICTIONARY_OK = [{
    "word": "hello",
    "phonetic": "/həˈloʊ/",
    "phonetics": [
        {"text": "/həˈloʊ/", "audio": "https://api.dictionaryapi.dev/media/pronunciations/en/hello-uk.mp3"},
        {"text": "/həˈloʊ/", "audio": "https://api.dictionaryapi.dev/media/pronunciations/en/hello-us.mp3"},
    ],
    "meanings": [
        {"partOfSpeech": "exclamation", "synonyms": ["hi"],
         "definitions": [{"definition": "Used as a greeting.", "example": "Hello there!",
                          "synonyms": ["hey", "hi"]}]},
        {"partOfSpeech": "noun",
         "definitions": [{"definition": "An utterance of hello.", "example": "Hello there!"},
                         {"definition": "A greeting, a hello."}]},
    ],
}]

MYMEMORY_OK = {"responseData": {"translatedText": "سلام", "match": 1}, "responseStatus": 200, "responseDetails": ""}
MYMEMORY_QUOTA = {
    "responseData": {"translatedText": "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS FOR TODAY. "
                                       "NEXT AVAILABLE IN 05 HOURS 12 MINUTES 03 SECONDS"},
    "responseStatus": 429,
}
DATAMUSE_OK = [{
    "word": "hello", "score": 3000,
    "defs": ["n\ta greeting (or response to a greeting)", "v\tsay hello; greet"],
    "tags": ["n", "ipa_pron:həˈloʊ"],
}]
DATAMUSE_NO_IPA = [{
    "word": "hello", "score": 3000,
    "defs": ["n\ta greeting (or response to a greeting)"],
    "tags": ["n", "pron:HH AH0 L OW1 "],  # ARPAbet only: not worth showing
}]
WIKTIONARY_OK = {
    "en": [
        {"partOfSpeech": "Interjection", "language": "English", "definitions": [
            {"definition": '<span>A greeting, used when meeting <a href="/wiki/somebody">somebody</a>.</span>',
             "examples": ["<i>Hello</i>, everyone."]},
        ]},
        {"partOfSpeech": "Noun", "language": "English", "definitions": [
            {"definition": "An utterance of <i>hello</i>; a greeting."},
        ]},
    ],
    "de": [{"partOfSpeech": "Interjektion", "language": "Deutsch", "definitions": [{"definition": "ignored"}]}],
}


def google_response(translation="سلام", source="en", extra=True):
    """A response shaped like the public gtx endpoint's (only the used indexes are real)."""
    data = [None] * 14
    data[0] = [[translation, "hello", None, None, 1]]
    data[2] = source
    if extra:
        data[11] = [["exclamation", [[["hi", "hey"], None]]]]
        data[12] = [
            ["exclamation", [["Used as a greeting.", None, None],
                             ["Used to attract attention.", None, None]]],
            ["noun", [["An utterance of hello.", None, None]]],
        ]
        data[13] = [[["Hello <b>there</b>!", None], ["Say <b>hello</b> to Ann", None]]]
    return data


def opts(**changes):
    values = dc.parse_argv([])[0]
    values.update(changes)
    return values


class FakeNet:
    """Stands in for dc.Network. answers maps a service name to a value, an exception
    to raise, NOT_FOUND, or a function of the url. A service that is not listed
    answers 'not found'."""

    def __init__(self, answers):
        self.answers = answers
        self.meta = {}
        self.lines = []
        self.calls = []

    def get_json(self, service, url, deadline):
        self.calls.append((service, url))
        if service not in self.answers:
            raise dc.NotFound(url)
        answer = self.answers[service]
        if callable(answer):
            answer = answer(url)
        if isinstance(answer, BaseException):
            raise answer
        if answer is NOT_FOUND:
            raise dc.NotFound(url)
        return answer

    def services(self):
        return [service for service, _ in self.calls]


def limited(retry_after=None):
    return dc.RouteFailure("HTTP 429", "direct: HTTP 429", retry_after)


def timed_out():
    return dc.RouteFailure("timeout", "direct: timeout")


class LookupCase(unittest.TestCase):
    """No network. The cache and the log live in a temporary folder and the time limits are short."""

    def setUp(self):
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.CRITICAL)
        self.addCleanup(root.setLevel, previous_level)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_file = Path(tmp.name) / "cache.json"
        patches = [mock.patch.object(dc, "cache_path", return_value=self.cache_file)]
        for name, value in (("TOTAL_BUDGET", 3.0), ("OPTIONAL_GRACE", 0.4), ("SHORT_GRACE", 0.1),
                            ("HEDGE_DELAY", 0.3), ("EXTRA_GRACE", 0.4), ("DISABLED_SERVICES", ()), ("SHOW_SOURCE_NOTES", True),
                            ("RECORDING_BUTTON", True)):
            patches.append(mock.patch.object(dc, name, value))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def hanging(self):
        """A fake answer that never arrives (until the test ends)."""
        gate = threading.Event()
        self.addCleanup(gate.set)

        def hang(url):
            gate.wait(30)
            raise dc.RouteFailure("timeout")

        return hang

    def lookup(self, text, net, translate=True, **changes):
        started = time.monotonic()
        out = dc.build_output(text, opts(translate=translate, **changes), net=net)
        return out, time.monotonic() - started

    def write_cache(self, entries=None, down=None, cool=None):
        data = {"entries": entries or {}, "meta": {"route": {}, "down": down or {}, "cool": cool or {}}}
        self.cache_file.write_text(json.dumps(data), encoding="utf-8")

    def read_cache(self):
        return json.loads(self.cache_file.read_text(encoding="utf-8"))

    @staticmethod
    def saved_google():
        return {"translation": "سلام", "entry": dc.google_entry(google_response())}

    @staticmethod
    def saved_dictionary():
        return {"translation": None, "entry": dc.parse_definition(DICTIONARY_OK)}


# ── Small functions ────────────────────────────────────────────────────────
class ArgumentTests(unittest.TestCase):
    def test_plain_query_uses_the_defaults(self):
        values, query = dc.parse_argv(["ice", "cream"])
        self.assertEqual(query, "ice cream")
        self.assertEqual(values["translate"], dc.ENABLE_TRANSLATION)
        self.assertEqual(values["define"], dc.ENABLE_DEFINITIONS)
        self.assertFalse(values["debug"])
        self.assertFalse(values["check"])

    def test_translate_default_can_be_changed_by_the_entry_script(self):
        self.assertFalse(dc.parse_argv(["run"], translate=False)[0]["translate"])

    def test_flags_are_removed_from_the_query(self):
        values, query = dc.parse_argv(["--no-translate", "--no-cache", "run"])
        self.assertFalse(values["translate"])
        self.assertFalse(values["cache"])
        self.assertTrue(values["define"])
        self.assertEqual(query, "run")

    def test_check_flag(self):
        values, query = dc.parse_argv(["--check"])
        self.assertTrue(values["check"])
        self.assertEqual(query, "")

    def test_double_dash_ends_the_flags(self):
        values, query = dc.parse_argv(["--", "--debug"])
        self.assertFalse(values["debug"])
        self.assertEqual(query, "--debug")

    def test_unknown_dash_word_is_a_query(self):
        _, query = dc.parse_argv(["-x"])
        self.assertEqual(query, "-x")


class NormalizeTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(dc.normalize_query("  hello \t  world\n"), "hello world")

    def test_strips_edge_punctuation_only(self):
        self.assertEqual(dc.normalize_query('"don\'t,"'), "don't")
        self.assertEqual(dc.normalize_query("well-known"), "well-known")

    def test_lone_surrogates_are_replaced(self):
        self.assertEqual(dc.normalize_query("ab\udc80cd"), "ab?cd")

    def test_persian_text_is_kept(self):
        self.assertEqual(dc.normalize_query(" سلام "), "سلام")

    def test_html_text(self):
        self.assertEqual(dc.html_text('<span>a <a href="x">b</a> &amp; c</span>'), "a b & c")
        self.assertEqual(dc.html_text(None), "")


class SafeUrlTests(unittest.TestCase):
    def test_accepts_http_and_protocol_relative_urls(self):
        self.assertEqual(dc.safe_url("//ssl.gstatic.com/x.mp3"), "https://ssl.gstatic.com/x.mp3")
        self.assertEqual(dc.safe_url("http://example.test/a.mp3"), "http://example.test/a.mp3")

    def test_rejects_other_schemes_and_junk(self):
        self.assertEqual(dc.safe_url("javascript:alert(1)"), "")
        self.assertEqual(dc.safe_url(None), "")
        self.assertEqual(dc.safe_url(42), "")


class ParseProxyTests(unittest.TestCase):
    def test_http_addresses(self):
        self.assertEqual(dc.parse_proxy("127.0.0.1:10809"), ("http", "127.0.0.1", 10809))
        self.assertEqual(dc.parse_proxy("http://127.0.0.1:10809"), ("http", "127.0.0.1", 10809))
        self.assertEqual(dc.parse_proxy("http://[::1]:8080"), ("http", "::1", 8080))

    def test_every_socks_version_means_socks5(self):
        self.assertEqual(dc.parse_proxy("socks5://127.0.0.1:10808"), ("socks5", "127.0.0.1", 10808))
        self.assertEqual(dc.parse_proxy("socks4://127.0.0.1:1080"), ("socks5", "127.0.0.1", 1080))
        self.assertEqual(dc.parse_proxy("socks5h://localhost:1080"), ("socks5", "localhost", 1080))

    def test_unusable_addresses(self):
        for value in ("", None, 42, "localhost", "http://127.0.0.1", "https://127.0.0.1:1", "ftp://h:1", "h:port"):
            with self.subTest(value=value):
                self.assertIsNone(dc.parse_proxy(value))

    def test_credentials_are_masked_for_display(self):
        self.assertEqual(dc.mask_credentials("http://user:secret@127.0.0.1:1"), "http://***@127.0.0.1:1")
        self.assertEqual(dc.mask_credentials("http://127.0.0.1:1"), "http://127.0.0.1:1")


class ErrorReasonTests(unittest.TestCase):
    def test_reasons(self):
        http_error = urllib.error.HTTPError("u", 403, "x", None, io.BytesIO(b""))
        self.assertEqual(dc.describe_error(http_error), "HTTP 403")
        self.assertEqual(dc.describe_error(socket.timeout()), "timeout")
        self.assertEqual(dc.describe_error(urllib.error.URLError(socket.timeout())), "timeout")
        self.assertEqual(dc.describe_error(urllib.error.URLError("refused")), "connection failed")
        self.assertEqual(dc.describe_error(ssl.SSLError("bad certificate")), "TLS error")
        self.assertEqual(dc.describe_error(ssl.SSLError("The handshake operation timed out")), "timeout")
        self.assertEqual(dc.describe_error(urllib.error.URLError(ssl.SSLError("x"))), "TLS error")
        self.assertEqual(dc.describe_error(ValueError("bad json")), "unexpected response")
        self.assertEqual(dc.describe_error(dc.RouteFailure("HTTP 500")), "HTTP 500")
        self.assertEqual(dc.describe_error(dc.NotFound("u")), "not found")

    def test_server_errors_are_described(self):
        self.assertEqual(dc.reason_text("HTTP 530"), "server error (HTTP 530)")
        self.assertEqual(dc.reason_text("HTTP 500"), "server error (HTTP 500)")
        self.assertEqual(dc.reason_text("HTTP 404"), "HTTP 404")

    def test_reasons_on_the_card(self):
        self.assertEqual(dc.reason_text("HTTP 429"), "rate limited (HTTP 429)")
        self.assertEqual(dc.reason_text("HTTP 403"), "refused (HTTP 403)")
        self.assertEqual(dc.reason_text("cooling down"), "skipped, it limited the requests recently")
        self.assertEqual(dc.reason_text("timeout"), "timeout")

    def test_retry_after(self):
        self.assertEqual(dc.parse_retry_after("120"), 120)
        self.assertEqual(dc.parse_retry_after(" 7 "), 7)
        for value in (None, "", "soon", "Wed, 21 Oct 2026 07:28:00 GMT", "-5"):
            with self.subTest(value=value):
                self.assertIsNone(dc.parse_retry_after(value))


# ── Parsers ────────────────────────────────────────────────────────────────
class ParseDefinitionTests(unittest.TestCase):
    def test_groups_examples_synonyms_and_phonetic(self):
        entry = dc.parse_definition(DICTIONARY_OK)
        self.assertEqual(entry["phonetic"], "/həˈloʊ/")
        self.assertEqual([g["pos"] for g in entry["groups"]], ["exclamation", "noun"])
        self.assertEqual(entry["groups"][0]["defs"], ["Used as a greeting."])
        self.assertEqual(entry["groups"][1]["defs"], ["An utterance of hello.", "A greeting, a hello."])
        self.assertEqual(entry["examples"], ["Hello there!"])
        self.assertEqual(entry["synonyms"], ["hi", "hey"])

    def test_american_recording_is_preferred(self):
        self.assertTrue(dc.parse_definition(DICTIONARY_OK)["audio"].endswith("hello-us.mp3"))

    def test_unsafe_audio_is_ignored(self):
        data = [{"phonetics": [{"audio": "javascript:alert(1)"}],
                 "meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "d"}]}]}]
        self.assertEqual(dc.parse_definition(data)["audio"], "")

    def test_same_part_of_speech_is_merged(self):
        data = [{"meanings": [
            {"partOfSpeech": "noun", "definitions": [{"definition": "A"}]},
            {"partOfSpeech": "noun", "definitions": [{"definition": "B"}]},
        ]}]
        entry = dc.parse_definition(data)
        self.assertEqual(len(entry["groups"]), 1)
        self.assertEqual(entry["groups"][0]["defs"], ["A", "B"])

    def test_limits_are_applied(self):
        defs = [{"definition": f"definition {i}"} for i in range(10)]
        entry = dc.parse_definition([{"meanings": [{"partOfSpeech": "noun", "definitions": defs}]}])
        self.assertEqual(len(entry["groups"][0]["defs"]), dc.MAX_DEFS_PER_POS)
        meanings = [{"partOfSpeech": f"part{i}", "definitions": [{"definition": "x"}]}
                    for i in range(dc.MAX_POS_GROUPS + 3)]
        entry = dc.parse_definition([{"meanings": meanings}])
        self.assertEqual(len(entry["groups"]), dc.MAX_POS_GROUPS)

    def test_malformed_or_empty_data_gives_none(self):
        self.assertIsNone(dc.parse_definition({"title": "No Definitions Found"}))
        self.assertIsNone(dc.parse_definition([]))
        self.assertIsNone(dc.parse_definition([{"meanings": "oops"}]))
        self.assertIsNone(dc.parse_definition([{"meanings": [{"partOfSpeech": "noun", "definitions": []}]}]))


class GoogleParseTests(unittest.TestCase):
    def test_translation_is_read(self):
        self.assertEqual(dc.parse_translation(google_response()), "سلام")

    def test_persian_source_gives_no_translation(self):
        self.assertEqual(dc.parse_translation(google_response(source="fa", extra=False)), "")

    def test_english_entry_is_read(self):
        entry = dc.google_entry(google_response())
        self.assertEqual([g["pos"] for g in entry["groups"]], ["exclamation", "noun"])
        self.assertEqual(entry["groups"][0]["defs"], ["Used as a greeting.", "Used to attract attention."])
        self.assertEqual(entry["synonyms"], ["hi", "hey"])
        self.assertEqual(entry["examples"], ["Hello there!", "Say hello to Ann"])
        self.assertEqual(entry["phonetic"], "")

    def test_missing_fields_give_no_entry(self):
        self.assertIsNone(dc.google_entry(google_response(extra=False)))

    def test_unusual_layout_is_ignored_not_crashed_on(self):
        data = google_response()
        data[12] = "oops"
        data[13] = [42]
        self.assertIsNone(dc.google_entry(data))
        self.assertIsNone(dc.google_entry({"not": "a list"}))

    def test_unknown_response_raises(self):
        with self.assertRaises(ValueError):
            dc.parse_translation({"error": 1})

    def test_one_request_asks_for_translation_and_dictionary_data(self):
        url = dc.google_url("hello", True)
        for part in ("tl=fa", "hl=en", "dt=t", "dt=md", "dt=ex", "dt=ss", "q=hello"):
            self.assertIn(part, url)
        self.assertNotIn("dt=md", dc.google_url("one two three", False))


class SpellingFormsTests(unittest.TestCase):
    def test_forms(self):
        cases = {
            "hello": ["hello"],
            "Hello": ["hello", "Hello"],      # only the first letter is a capital: a sentence start
            "Ice cream": ["ice cream", "Ice cream"],
            "NASA": ["NASA", "nasa"],
            "iPhone": ["iPhone", "iphone"],
            "New York": ["New York", "new york"],
            "McDonald": ["McDonald", "mcdonald"],
        }
        for text, forms in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dc._forms(text), forms)


class MyMemoryParseTests(unittest.TestCase):
    def test_translation(self):
        self.assertEqual(dc.parse_mymemory(MYMEMORY_OK, "hello"), "سلام")

    def test_html_entities_are_decoded(self):
        data = {"responseData": {"translatedText": "it&#39;s"}, "responseStatus": 200}
        self.assertEqual(dc.parse_mymemory(data, "x"), "it's")

    def test_the_original_text_is_not_a_translation(self):
        data = {"responseData": {"translatedText": "Hello"}, "responseStatus": 200}
        self.assertEqual(dc.parse_mymemory(data, "hello"), "")

    def test_a_used_up_quota_is_a_rate_limit(self):
        with self.assertRaises(dc.RouteFailure) as caught:
            dc.parse_mymemory(MYMEMORY_QUOTA, "hello")
        self.assertEqual(caught.exception.reason, "HTTP 429")
        quota_in_text_only = {"responseData": {"translatedText": "MYMEMORY WARNING: ..."}, "responseStatus": 200}
        with self.assertRaises(dc.RouteFailure):
            dc.parse_mymemory(quota_in_text_only, "hello")

    def test_other_statuses_are_reported(self):
        with self.assertRaises(dc.RouteFailure) as caught:
            dc.parse_mymemory({"responseData": {}, "responseStatus": "403", "responseDetails": "no"}, "hello")
        self.assertEqual(caught.exception.reason, "HTTP 403")

    def test_unknown_response_raises(self):
        with self.assertRaises(ValueError):
            dc.parse_mymemory([], "hello")

    def test_request(self):
        url = dc.mymemory_url("hello")
        self.assertIn("langpair=en%7Cfa", url)
        self.assertNotIn("de=", url)
        with mock.patch.object(dc, "MYMEMORY_EMAIL", "me@example.test"):
            self.assertIn("de=me%40example.test", dc.mymemory_url("hello"))


class DatamuseParseTests(unittest.TestCase):
    def test_definitions_by_part_of_speech(self):
        entry = dc.parse_datamuse(DATAMUSE_OK, "hello")
        self.assertEqual([g["pos"] for g in entry["groups"]], ["noun", "verb"])
        self.assertEqual(entry["groups"][0]["defs"], ["a greeting (or response to a greeting)"])
        self.assertEqual(entry["groups"][1]["defs"], ["say hello; greet"])
        self.assertEqual((entry["phonetic"], entry["audio"], entry["synonyms"], entry["examples"]),
                         ("/həˈloʊ/", "", [], []))

    def test_only_the_word_itself_is_used(self):
        data = [{"word": "hellos", "defs": ["n\tother"]}, {"word": "Hello", "defs": ["n\tgreeting"]}]
        self.assertEqual(dc.parse_datamuse(data, "hello")["groups"][0]["defs"], ["greeting"])
        self.assertIsNone(dc.parse_datamuse([{"word": "hellos", "defs": ["n\tother"]}], "hello"))

    def test_unknown_part_of_speech_and_junk(self):
        data = [{"word": "x", "defs": ["u\tsomething", "no tab at all", 5]}]
        entry = dc.parse_datamuse(data, "x")
        self.assertEqual([g["pos"] for g in entry["groups"]], [""])
        self.assertEqual(entry["groups"][0]["defs"], ["something"])

    def test_a_capitalised_word_gets_the_common_sense_not_the_proper_noun(self):
        data = [
            {"word": "Dispatched", "defs": ["n\ta Swedish melodic death metal band"], "tags": ["ipa_pron:dɪspˈætʃt"]},
            {"word": "dispatched", "defs": ["v\tsend off to a destination"], "tags": ["ipa_pron:dɪspˈætʃt"]},
        ]
        entry = dc.parse_datamuse(data, "Dispatched")
        self.assertEqual(entry["groups"], [{"pos": "verb", "defs": ["send off to a destination"]}])
        self.assertEqual(dc.parse_datamuse(data, "dispatched")["groups"][0]["defs"], ["send off to a destination"])

    def test_the_proper_noun_is_used_when_it_is_all_there_is(self):
        data = [{"word": "Dispatched", "defs": ["n\ta band"]}]
        self.assertEqual(dc.parse_datamuse(data, "Dispatched")["groups"][0]["defs"], ["a band"])

    def test_a_name_in_capitals_is_looked_up_as_typed(self):
        data = [{"word": "nasa", "defs": ["n\tnot it"]}, {"word": "NASA", "defs": ["n\tthe space agency"]}]
        self.assertEqual(dc.parse_datamuse(data, "NASA")["groups"][0]["defs"], ["the space agency"])

    def test_only_one_item_is_used_so_senses_are_not_mixed(self):
        data = [{"word": "a", "defs": ["n\tfirst"]}, {"word": "A", "defs": ["n\tsecond"]}]
        self.assertEqual(dc.parse_datamuse(data, "a")["groups"][0]["defs"], ["first"])

    def test_nothing_found(self):
        for data in ([], {"error": 1}, [{"word": "hello"}], [{"word": "hello", "defs": []}]):
            with self.subTest(data=data):
                self.assertIsNone(dc.parse_datamuse(data, "hello"))

    def test_request(self):
        url = dc.datamuse_url("ice cream")
        self.assertIn("sp=ice+cream", url)
        self.assertIn("md=dr", url)  # definitions and pronunciation
        self.assertIn("ipa=1", url)  # the pronunciation in IPA, not ARPAbet


class DatamusePhoneticTests(unittest.TestCase):
    def word(self, *tags, word="hello"):
        return [{"word": word, "score": 1, "tags": list(tags)}]

    def test_the_ipa_tag(self):
        self.assertEqual(dc.datamuse_phonetic(self.word("n", "ipa_pron:həˈloʊ"), "hello"), "/həˈloʊ/")

    def test_a_plain_pron_tag_is_used_when_it_is_ipa(self):
        self.assertEqual(dc.datamuse_phonetic(self.word("pron:həˈloʊ"), "hello"), "/həˈloʊ/")

    def test_slashes_are_not_doubled(self):
        self.assertEqual(dc.datamuse_phonetic(self.word("ipa_pron:/həˈloʊ/"), "hello"), "/həˈloʊ/")

    def test_arpabet_is_not_shown(self):
        for tag in ("pron:HH AH0 L OW1 ", "pron:K AW1 ", "pron:JH ER0 AE1 F"):
            with self.subTest(tag=tag):
                self.assertEqual(dc.datamuse_phonetic(self.word(tag), "hello"), "")

    def test_the_ipa_tag_wins_over_arpabet(self):
        self.assertEqual(dc.datamuse_phonetic(self.word("pron:HH AH0 L OW1 ", "ipa_pron:həˈloʊ"), "hello"), "/həˈloʊ/")

    def test_only_the_word_itself_counts(self):
        self.assertEqual(dc.datamuse_phonetic(self.word("ipa_pron:həˈloʊ", word="hellos"), "hello"), "")
        self.assertEqual(dc.datamuse_phonetic(self.word("ipa_pron:həˈloʊ", word="Hello"), "hello"), "/həˈloʊ/")

    def test_junk_gives_nothing(self):
        for data in ([], {"a": 1}, [{"word": "hello"}], [{"word": "hello", "tags": "x"}],
                     [{"word": "hello", "tags": [5, "ipa_pron:", "pron"]}]):
            with self.subTest(data=data):
                self.assertEqual(dc.datamuse_phonetic(data, "hello"), "")

    def test_a_word_without_definitions_still_has_its_phonetic_spelling(self):
        data = [{"word": "hello", "tags": ["ipa_pron:həˈloʊ"]}]
        self.assertIsNone(dc.parse_datamuse(data, "hello"))  # no entry to show ...
        self.assertEqual(dc.datamuse_phonetic(data, "hello"), "/həˈloʊ/")  # ... but the spelling is kept


class CompleteEntryTests(unittest.TestCase):
    def entry(self, **changes):
        base = {"phonetic": "", "audio": "", "groups": [{"pos": "noun", "defs": ["d"]}],
                "synonyms": [], "examples": []}
        base.update(changes)
        return base

    def test_a_missing_phonetic_spelling_is_taken_from_datamuse(self):
        values = {"wiktionary": {"translation": None, "entry": self.entry()},
                  "datamuse": {"translation": None, "entry": None, "phonetic": "/x/"}}
        merged, extras = dc.complete_entry(values["wiktionary"]["entry"], "wiktionary", values)
        self.assertEqual(merged["phonetic"], "/x/")
        self.assertEqual(extras, {"phonetic": "datamuse"})

    def test_dictionaryapi_is_preferred_over_datamuse_for_the_phonetic_spelling(self):
        values = {"google": {"translation": "", "entry": self.entry()},
                  "dictionaryapi": {"translation": None, "entry": None, "phonetic": "/a/"},
                  "datamuse": {"translation": None, "entry": None, "phonetic": "/b/"}}
        merged, extras = dc.complete_entry(values["google"]["entry"], "google", values)
        self.assertEqual((merged["phonetic"], extras), ("/a/", {"phonetic": "dictionaryapi"}))

    def test_a_phonetic_spelling_already_there_is_kept(self):
        values = {"datamuse": {"translation": None, "entry": self.entry(phonetic="/mine/"), "phonetic": "/mine/"}}
        merged, extras = dc.complete_entry(values["datamuse"]["entry"], "datamuse", values)
        self.assertEqual((merged["phonetic"], extras), ("/mine/", {}))

    def test_the_service_that_supplied_the_entry_is_not_named_for_its_own_phonetic_spelling(self):
        values = {"datamuse": {"translation": None, "entry": self.entry(phonetic="/p/"), "phonetic": "/p/"}}
        entry = self.entry()  # the same entry, seen without the phonetic spelling
        merged, extras = dc.complete_entry(entry, "datamuse", values)
        self.assertEqual((merged["phonetic"], extras), ("/p/", {}))

    def test_examples_and_synonyms_are_filled_up_from_the_other_entries(self):
        mine = self.entry(examples=["one"], synonyms=["a"])
        values = {
            "dictionaryapi": {"translation": None, "entry": mine},
            "google": {"translation": "", "entry": self.entry(examples=["one", "two", "three", "four"],
                                                              synonyms=["a", "b"])},
        }
        merged, extras = dc.complete_entry(mine, "dictionaryapi", values)
        self.assertEqual(merged["examples"], ["one", "two", "three"])  # up to MAX_EXAMPLES, no repeats
        self.assertEqual(merged["synonyms"], ["a", "b"])
        self.assertEqual(extras, {})  # only the phonetic spelling is worth a note

    def test_the_original_entry_is_not_changed(self):
        mine = self.entry(examples=["one"])
        values = {"google": {"translation": "", "entry": self.entry(examples=["two"])},
                  "datamuse": {"translation": None, "entry": None, "phonetic": "/x/"}}
        dc.complete_entry(mine, "dictionaryapi", values)
        self.assertEqual(mine["examples"], ["one"])
        self.assertEqual(mine["phonetic"], "")

    def test_without_any_other_answer_nothing_changes(self):
        mine = self.entry()
        merged, extras = dc.complete_entry(mine, "google", {"google": {"translation": "", "entry": mine}})
        self.assertEqual((merged, extras), (mine, {}))


class WiktionaryParseTests(unittest.TestCase):
    def test_entries_by_part_of_speech_with_the_html_removed(self):
        entry = dc.parse_wiktionary(WIKTIONARY_OK)
        self.assertEqual([g["pos"] for g in entry["groups"]], ["interjection", "noun"])
        self.assertEqual(entry["groups"][0]["defs"], ["A greeting, used when meeting somebody."])
        self.assertEqual(entry["groups"][1]["defs"], ["An utterance of hello; a greeting."])
        self.assertEqual(entry["examples"], ["Hello, everyone."])

    def test_other_languages_are_ignored(self):
        entry = dc.parse_wiktionary(WIKTIONARY_OK)
        self.assertNotIn("ignored", json.dumps(entry))

    def test_parsed_examples_are_read_too(self):
        data = {"en": [{"partOfSpeech": "Noun", "definitions": [
            {"definition": "d", "parsedExamples": [{"example": "<b>an</b> example"}, "junk"]}]}]}
        self.assertEqual(dc.parse_wiktionary(data)["examples"], ["an example"])

    def test_limits_and_empty_definitions(self):
        defs = [{"definition": f"<i>d{i}</i>"} for i in range(10)] + [{"definition": "  "}]
        entry = dc.parse_wiktionary({"en": [{"partOfSpeech": "Verb", "definitions": defs}]})
        self.assertEqual(len(entry["groups"][0]["defs"]), dc.MAX_DEFS_PER_POS)

    def test_nothing_usable(self):
        for data in ({}, [], {"en": []}, {"en": ["x"]}, {"en": [{"partOfSpeech": "Noun", "definitions": []}]},
                     {"type": "error", "title": "Not found."}):
            with self.subTest(data=data):
                self.assertIsNone(dc.parse_wiktionary(data))


class RenderTests(unittest.TestCase):
    def entry(self):
        return dc.parse_definition(DICTIONARY_OK)

    def test_full_entry_has_every_label_the_template_reads(self):
        audio = [("Recording", "https://example.test/a.mp3"), ("Google voice", "https://example.test/b")]
        out = dc.render("سلام", self.entry(), audio, ["note"])
        for text in ("❖ NOTE:", "❖ GOOGLE TRANSLATE:", "❖ PHONETIC:", "❖ POS: exclamation",
                     "❖ POS: noun", "❖ MEANINGS:", "❖ SYNONYMS:", "❖ EXAMPLES:"):
            self.assertIn(text, out)

    def test_text_is_escaped(self):
        entry = {"phonetic": "", "groups": [{"pos": "noun", "defs": ["<script>x</script> & y"]}],
                 "synonyms": [], "examples": [], "audio": ""}
        out = dc.render("", entry, [], [])
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;x&lt;/script&gt; &amp; y", out)

    def test_audio_buttons_are_escaped_and_numbered(self):
        out = dc.audio_buttons([("Recording", "https://example.test/a?x=1&y=2"), ("Google voice", "https://e/b")])
        self.assertIn('src="https://example.test/a?x=1&amp;y=2"', out)
        self.assertIn("getElementById('gd_audio_0')", out)
        self.assertIn("getElementById('gd_audio_1')", out)
        self.assertIn("▶ Google voice", out)

    def test_nothing_to_show_gives_an_empty_string(self):
        self.assertEqual(dc.render("", None, [], []), "")

    def test_notes_only(self):
        out = dc.render("", None, [], ["something failed"])
        self.assertIn("❖ NOTE: something failed", out)
        self.assertNotIn("GOOGLE TRANSLATE", out)


# ── Cache, memory about the network, and the plan ──────────────────────────
class CacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "cache.json"

    def test_fresh_stale_and_missing(self):
        now = time.time()
        key = f"v{dc.CACHE_VERSION}|x"
        cache = dc.Cache(self.path, True)
        cache.entries = {
            key + "1": {"t": now, "v": "fresh"},
            key + "2": {"t": now - dc.CACHE_TTL - 100, "v": "stale"},
            key + "3": {"t": now - dc.STALE_TTL - 100, "v": "ancient"},
        }
        self.assertEqual(cache.lookup(key + "1")[:2], ("fresh", "fresh"))
        self.assertEqual(cache.lookup(key + "2")[:2], ("stale", "stale"))
        self.assertEqual(cache.lookup(key + "3")[0], "missing")
        self.assertEqual(cache.lookup(key + "4")[0], "missing")

    def test_a_shorter_lifetime_can_be_asked_for(self):
        cache = dc.Cache(self.path, True)
        cache.entries = {"k": {"t": time.time() - 2 * 24 * 3600, "v": "x"}}
        self.assertEqual(cache.lookup("k")[0], "fresh")
        self.assertEqual(cache.lookup("k", dc.BACKUP_TTL)[0], "stale")

    def test_not_found_answers_are_fresh_for_a_day_and_never_stale(self):
        now = time.time()
        cache = dc.Cache(self.path, True)
        cache.entries = {
            "a": {"t": now, "v": None},
            "b": {"t": now - dc.NEGATIVE_CACHE_TTL - 60, "v": None},
        }
        self.assertEqual(cache.lookup("a")[0], "fresh")
        self.assertEqual(cache.lookup("b")[0], "missing")

    def test_round_trip_with_meta(self):
        cache = dc.Cache(self.path, True)
        cache.put(f"v{dc.CACHE_VERSION}|k", {"a": 1})
        cache.meta["route"]["google"] = "direct"
        cache.meta["cool"]["google"] = {"until": 5.0, "strikes": 2}
        cache.save()
        again = dc.Cache(self.path, True)
        self.assertEqual(again.lookup(f"v{dc.CACHE_VERSION}|k")[:2], ("fresh", {"a": 1}))
        self.assertEqual(again.meta["route"], {"google": "direct"})
        self.assertEqual(again.meta["cool"], {"google": {"until": 5.0, "strikes": 2}})

    def test_a_change_to_the_meta_alone_is_saved(self):
        cache = dc.Cache(self.path, True)
        cache.meta["down"]["google"] = 123.0
        cache.save()
        self.assertEqual(dc.Cache(self.path, True).meta["down"], {"google": 123.0})

    def test_nothing_is_written_when_nothing_changed(self):
        dc.Cache(self.path, True).save()
        self.assertFalse(self.path.exists())

    def test_old_layout_and_old_versions_are_dropped(self):
        self.path.write_text(json.dumps({"v2|dict|hello": {"t": time.time(), "v": "old"}}), encoding="utf-8")
        cache = dc.Cache(self.path, True)
        self.assertEqual(cache.entries, {})
        cache.put(f"v{dc.CACHE_VERSION}|new", 1)
        cache.entries["v1|ancient"] = {"t": time.time(), "v": 1}
        cache.save()
        self.assertEqual(list(json.loads(self.path.read_text(encoding="utf-8"))["entries"]), [f"v{dc.CACHE_VERSION}|new"])

    def test_corrupt_file_is_ignored(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(dc.Cache(self.path, True).entries, {})

    def test_disabled_cache_reads_and_writes_nothing(self):
        cache = dc.Cache(self.path, False)
        cache.put("k", 1)
        cache.save()
        self.assertFalse(self.path.exists())
        self.assertEqual(cache.lookup("k")[0], "missing")

    def test_two_scripts_saving_one_after_the_other_both_keep_their_results(self):
        # GoldenDict starts both programs at the same moment, so both loaded the same file
        first, second = dc.Cache(self.path, True), dc.Cache(self.path, True)
        key = f"v{dc.CACHE_VERSION}|"
        first.put(key + "one", 1)
        first.meta["down"]["dictionaryapi"] = 111.0
        second.put(key + "two", 2)
        second.meta["cool"]["google"] = {"until": 999.0, "strikes": 1}
        first.save()
        second.save()
        saved = dc.Cache(self.path, True)
        self.assertEqual(saved.lookup(key + "one")[:2], ("fresh", 1))
        self.assertEqual(saved.lookup(key + "two")[:2], ("fresh", 2))
        self.assertEqual(saved.meta["down"], {"dictionaryapi": 111.0})
        self.assertEqual(saved.meta["cool"], {"google": {"until": 999.0, "strikes": 1}})

    def test_a_rate_limit_mark_survives_the_other_scripts_save(self):
        # the English-only script knows nothing about the 429 that the full script just met
        full, english = dc.Cache(self.path, True), dc.Cache(self.path, True)
        dc.start_cooldown(full.meta, "google")
        full.save()
        english.put(f"v{dc.CACHE_VERSION}|x", 1)
        english.save()
        self.assertGreater(dc.cooldown_left(dc.Cache(self.path, True).meta, "google"), 500)

    def test_clearing_a_mark_is_saved_too(self):
        dc.Cache(self.path, True)
        seed = dc.Cache(self.path, True)
        seed.meta["down"]["google"] = 5.0
        seed.meta["down"]["mymemory"] = 6.0
        seed.save()
        script = dc.Cache(self.path, True)
        dc.mark_up(script.meta, "google")
        script.save()
        self.assertEqual(dc.Cache(self.path, True).meta["down"], {"mymemory": 6.0})

    def test_a_mark_set_by_another_script_is_not_cleared_by_one_that_never_saw_it(self):
        script = dc.Cache(self.path, True)
        other = dc.Cache(self.path, True)
        other.meta["down"]["google"] = 7.0
        other.save()
        script.put(f"v{dc.CACHE_VERSION}|y", 1)
        script.save()
        self.assertEqual(dc.Cache(self.path, True).meta["down"], {"google": 7.0})

    def test_a_saved_script_can_save_again(self):
        script = dc.Cache(self.path, True)
        script.put(f"v{dc.CACHE_VERSION}|a", 1)
        script.save()
        script.put(f"v{dc.CACHE_VERSION}|b", 2)
        script.save()
        saved = dc.Cache(self.path, True)
        self.assertEqual(saved.lookup(f"v{dc.CACHE_VERSION}|a")[1], 1)
        self.assertEqual(saved.lookup(f"v{dc.CACHE_VERSION}|b")[1], 2)

    def test_a_lock_left_by_a_dead_script_is_removed(self):
        lock = self.path.with_name(self.path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        old = time.time() - dc.LOCK_STALE - 5
        os.utime(lock, (old, old))
        script = dc.Cache(self.path, True)
        script.put(f"v{dc.CACHE_VERSION}|k", 1)
        started = time.monotonic()
        script.save()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(dc.Cache(self.path, True).lookup(f"v{dc.CACHE_VERSION}|k")[1], 1)
        self.assertFalse(lock.exists())

    def test_a_lock_that_stays_does_not_block_forever(self):
        lock = self.path.with_name(self.path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")  # a fresh lock, held by "another script"
        script = dc.Cache(self.path, True)
        script.put(f"v{dc.CACHE_VERSION}|k", 1)
        with mock.patch.object(dc, "LOCK_WAIT", 0.2):
            started = time.monotonic()
            script.save()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(dc.Cache(self.path, True).lookup(f"v{dc.CACHE_VERSION}|k")[1], 1)
        self.assertTrue(lock.exists())  # somebody else's lock is not ours to remove

    def test_log_keeps_only_the_last_blocks(self):
        log = self.path.with_name("network.log")
        for number in range(dc.LOG_BLOCKS + 5):
            dc.write_log(log, f"block {number}\n  detail")
        text = log.read_text(encoding="utf-8")
        self.assertNotIn("block 0\n", text)
        self.assertIn(f"block {dc.LOG_BLOCKS + 4}\n", text)
        self.assertEqual(text.count("block "), dc.LOG_BLOCKS)


class CooldownTests(unittest.TestCase):
    def test_the_first_cooldown_is_the_base_and_each_repeat_doubles_it_up_to_the_maximum(self):
        meta = {}
        dc.start_cooldown(meta, "google")
        self.assertAlmostEqual(dc.cooldown_left(meta, "google"), dc.COOLDOWN_BASE, delta=5)
        dc.start_cooldown(meta, "google")
        self.assertAlmostEqual(dc.cooldown_left(meta, "google"), 2 * dc.COOLDOWN_BASE, delta=5)
        for _ in range(12):
            dc.start_cooldown(meta, "google")
        self.assertAlmostEqual(dc.cooldown_left(meta, "google"), dc.COOLDOWN_MAX, delta=5)

    def test_a_longer_retry_after_wins_and_a_shorter_one_does_not_shorten_it(self):
        meta = {}
        dc.start_cooldown(meta, "google", retry_after=9000)
        self.assertAlmostEqual(dc.cooldown_left(meta, "google"), 9000, delta=5)
        dc.start_cooldown(meta, "mymemory", retry_after=5)
        self.assertAlmostEqual(dc.cooldown_left(meta, "mymemory"), dc.COOLDOWN_BASE, delta=5)

    def test_services_are_independent(self):
        meta = {}
        dc.start_cooldown(meta, "google")
        self.assertEqual(dc.cooldown_left(meta, "dictionaryapi"), 0.0)

    def test_an_answer_clears_the_cooldown_and_the_down_mark(self):
        meta = {}
        dc.start_cooldown(meta, "google")
        dc.mark_down(meta, "google")
        dc.mark_up(meta, "google")
        self.assertEqual(dc.cooldown_left(meta, "google"), 0.0)
        self.assertFalse(dc.is_down(meta, "google"))

    def test_nonsense_in_the_saved_memory_is_ignored(self):
        for meta in ({"cool": "x"}, {"cool": {"google": 5}}, {"cool": {"google": {"until": "soon"}}}, {}):
            with self.subTest(meta=meta):
                self.assertEqual(dc.cooldown_left(meta, "google"), 0.0)
        self.assertFalse(dc.is_down({"down": {"google": "x"}}, "google"))


class PlanTests(unittest.TestCase):
    def test_full_word(self):
        self.assertEqual(
            dc.plan("hello", True, True),
            {"google": 0, "mymemory": 1, "dictionaryapi": 0, "wiktionary": 1, "datamuse": 1},
        )

    def test_english_only_asks_the_same_services_for_the_entry(self):
        # so that both programs show the same entry
        self.assertEqual(
            dc.plan("hello", True, False),
            {"google": 0, "dictionaryapi": 0, "wiktionary": 1, "datamuse": 1},
        )

    def test_phrase(self):
        self.assertEqual(dc.plan("one two three", False, True), {"google": 0, "mymemory": 1})

    def test_text_that_mymemory_cannot_take_is_left_out(self):
        self.assertNotIn("mymemory", dc.plan("word " * 100, False, True))
        self.assertNotIn("mymemory", dc.plan("سلام دنیا", False, True))

    def test_disabled_services_are_left_out(self):
        with mock.patch.object(dc, "DISABLED_SERVICES", ("mymemory", "datamuse")):
            self.assertEqual(dc.plan("hello", True, True), {"google": 0, "dictionaryapi": 0, "wiktionary": 1})

    def test_capabilities(self):
        self.assertEqual(dc.capabilities("google", True, True), {"translation", "entry", "examples", "synonyms"})
        self.assertEqual(dc.capabilities("google", True, False), {"entry", "examples", "synonyms"})
        self.assertEqual(dc.capabilities("dictionaryapi", True, False), {"entry", "phonetic", "examples", "synonyms"})
        self.assertEqual(dc.capabilities("wiktionary", True, False), {"entry", "examples"})
        self.assertEqual(dc.capabilities("datamuse", True, False), {"entry", "phonetic"})
        self.assertEqual(dc.capabilities("mymemory", True, True), {"translation"})
        self.assertEqual(dc.capabilities("datamuse", False, True), set())
        self.assertEqual(dc.capabilities("google", False, True), {"translation"})


# ── Lookups ────────────────────────────────────────────────────────────────
class BuildOutputTests(LookupCase):
    """Persian translation and English entry (dict_full.py)."""

    def test_the_first_choices_are_enough_and_no_backup_is_asked(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertIn("▶ Recording", out)
        self.assertIn("▶ Google voice", out)
        self.assertNotIn("❖ NOTE:", out)
        self.assertEqual(sorted(set(net.services())), ["dictionaryapi", "google"])

    def test_google_alone_is_enough_when_dictionaryapi_hangs(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": self.hanging()})
        out, elapsed = self.lookup("hello", net)
        self.assertIn("❖ POS: exclamation", out)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("▶ Google voice", out)
        self.assertNotIn("▶ Recording", out)
        self.assertNotIn("❖ PHONETIC", out)
        self.assertNotIn("❖ NOTE:", out)
        self.assertGreaterEqual(elapsed, dc.OPTIONAL_GRACE - 0.05)
        self.assertLess(elapsed, dc.TOTAL_BUDGET / 2)
        # Datamuse is also asked, for the phonetic spelling that Google's entry lacks
        self.assertEqual(sorted(set(net.services())), ["datamuse", "dictionaryapi", "google"])
        self.assertIn("dictionaryapi", self.read_cache()["meta"]["down"])

    def test_the_phonetic_spelling_that_google_lacks_comes_from_datamuse(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": self.hanging(), "datamuse": DATAMUSE_OK})
        out, elapsed = self.lookup("hello", net)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertIn("❖ NOTE: English entry from Google, with the phonetic spelling from Datamuse.", out)
        self.assertIn("❖ POS: exclamation", out)  # the definitions are still Google's
        self.assertLess(elapsed, 1.5)

    def test_a_service_that_was_not_answering_is_waited_for_only_briefly(self):
        self.write_cache(down={"dictionaryapi": time.time()})
        net = FakeNet({"google": google_response(), "dictionaryapi": self.hanging()})
        with mock.patch.object(dc, "OPTIONAL_GRACE", 1.0):
            out, elapsed = self.lookup("hello", net)
        self.assertIn("❖ POS: exclamation", out)
        self.assertLess(elapsed, 0.6)

    def test_an_old_down_mark_is_ignored(self):
        self.write_cache(down={"dictionaryapi": time.time() - dc.DOWN_WINDOW - 5})
        net = FakeNet({"google": google_response(), "dictionaryapi": self.hanging()})
        with mock.patch.object(dc, "OPTIONAL_GRACE", 0.6):
            _, elapsed = self.lookup("hello", net)
        self.assertGreaterEqual(elapsed, 0.55)

    def test_dictionaryapi_failure_is_quiet_when_google_has_the_entry(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": timed_out()})
        out, _ = self.lookup("hello", net)
        self.assertIn("❖ POS: exclamation", out)
        self.assertNotIn("❖ NOTE:", out)
        self.assertIn("dictionaryapi", self.read_cache()["meta"]["down"])

    def test_a_working_service_clears_its_down_mark(self):
        self.write_cache(down={"dictionaryapi": time.time()})

        def not_instant(url):
            time.sleep(0.1)  # so that dictionaryapi is asked before Google has answered
            return google_response()

        net = FakeNet({"google": not_instant, "dictionaryapi": DICTIONARY_OK})
        self.lookup("hello", net)
        self.assertNotIn("dictionaryapi", self.read_cache()["meta"]["down"])

    def test_a_down_first_choice_is_not_asked_when_saved_answers_cover_the_need(self):
        self.write_cache({f"v{dc.CACHE_VERSION}|google|w|hello": {"t": time.time(), "v": self.saved_google()},
                          f"v{dc.CACHE_VERSION}|datamuse|w|hello": {
                              "t": time.time(), "v": {"translation": None, "entry": None, "phonetic": "/həˈloʊ/"}}},
                         down={"dictionaryapi": time.time()})
        net = FakeNet({})
        out, elapsed = self.lookup("hello", net)
        self.assertEqual(net.calls, [])
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("❖ POS: exclamation", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertLess(elapsed, 0.2)

    def test_a_down_service_is_not_asked_just_for_the_phonetic_spelling(self):
        self.write_cache({f"v{dc.CACHE_VERSION}|google|w|hello": {"t": time.time(), "v": self.saved_google()}},
                         down={"dictionaryapi": time.time()})
        net = FakeNet({"datamuse": DATAMUSE_OK})
        out, elapsed = self.lookup("hello", net)
        self.assertEqual(net.services(), ["datamuse"])  # not dictionaryapi, which is known to be down
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertLess(elapsed, 0.5)

    def test_a_first_choice_that_is_not_known_to_be_down_is_still_asked_for_an_upgrade(self):
        self.write_cache({f"v{dc.CACHE_VERSION}|google|w|hello": {"t": time.time(), "v": self.saved_google()}})
        net = FakeNet({"dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("hello", net)
        self.assertEqual(net.services(), ["dictionaryapi"])
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)

    def test_a_better_service_that_has_been_silent_for_long_is_not_given_a_fresh_grace(self):
        def slow_wiktionary(url):
            time.sleep(0.8)
            return WIKTIONARY_OK

        net = FakeNet({"google": limited(), "dictionaryapi": self.hanging(), "mymemory": MYMEMORY_OK,
                       "wiktionary": slow_wiktionary})
        with mock.patch.object(dc, "OPTIONAL_GRACE", 1.5):
            out, elapsed = self.lookup("hello", net)
        self.assertIn("English entry from Wiktionary (dictionaryapi.dev: no answer in time; Google: rate limited (HTTP 429)).", out)
        # Wiktionary answered at about 1.1 s. dictionaryapi.dev had been silent since the start, so
        # it is given up on at 1.5 s, not at 1.1 + 1.5 = 2.6 s.
        self.assertGreaterEqual(elapsed, 1.35)
        self.assertLess(elapsed, 2.1)

    # -- rate limits and backups
    def test_a_429_from_google_is_answered_by_mymemory_and_says_so(self):
        net = FakeNet({"google": limited(), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("❖ NOTE: Persian translation from MyMemory (Google: rate limited (HTTP 429)).", out)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("سلام", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertNotIn("Network details", out)
        self.assertGreater(self.read_cache()["meta"]["cool"]["google"]["until"], time.time() + 500)

    def test_google_is_left_alone_after_a_429(self):
        first = FakeNet({"google": limited(), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        self.lookup("hello", first)
        second = FakeNet({})  # asking anybody would fail the test
        out, _ = self.lookup("hello", second)
        self.assertEqual(second.calls, [])
        self.assertIn("Persian translation from MyMemory (Google: skipped, it limited the requests recently).", out)

    def test_a_longer_retry_after_is_respected(self):
        net = FakeNet({"google": limited(retry_after=5000), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        self.lookup("hello", net)
        self.assertGreater(self.read_cache()["meta"]["cool"]["google"]["until"], time.time() + 4900)

    def test_a_403_also_starts_a_cooldown(self):
        net = FakeNet({"google": dc.RouteFailure("HTTP 403"), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("Google: refused (HTTP 403)", out)
        self.assertIn("google", self.read_cache()["meta"]["cool"])

    def test_a_timeout_is_not_a_rate_limit(self):
        net = FakeNet({"google": timed_out(), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        self.lookup("hello", net)
        meta = self.read_cache()["meta"]
        self.assertEqual(meta["cool"], {})
        self.assertIn("google", meta["down"])

    def test_when_the_cooldown_is_over_google_is_asked_again_and_the_cooldown_is_cleared(self):
        self.write_cache(cool={"google": {"until": time.time() - 10, "strikes": 2}})
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("google", net.services())
        self.assertNotIn("❖ NOTE:", out)
        self.assertNotIn("google", self.read_cache()["meta"]["cool"])

    def test_a_quota_message_from_mymemory_starts_its_cooldown(self):
        net = FakeNet({"google": limited(), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_QUOTA})
        out, _ = self.lookup("hello", net)
        self.assertIn("Persian translation unavailable (Google: rate limited (HTTP 429); MyMemory: rate limited (HTTP 429)).", out)
        self.assertEqual(sorted(self.read_cache()["meta"]["cool"]), ["google", "mymemory"])

    def test_both_first_choices_failing_at_once_use_the_backups(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("Persian translation from MyMemory (Google: rate limited (HTTP 429)).", out)
        self.assertIn(
            "English entry from Wiktionary (dictionaryapi.dev: timeout; Google: rate limited (HTTP 429)), "
            "with the phonetic spelling from Datamuse.", out
        )
        self.assertIn("A greeting, used when meeting somebody.", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertNotIn("Network details", out)
        self.assertIn("▶ Google voice", out)
        self.assertNotIn("▶ Recording", out)

    def test_a_silent_first_choice_is_hedged_after_the_delay(self):
        net = FakeNet({"google": limited(), "dictionaryapi": self.hanging(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        out, elapsed = self.lookup("hello", net)
        self.assertIn(
            "English entry from Wiktionary (dictionaryapi.dev: no answer in time; Google: rate limited (HTTP 429)), "
            "with the phonetic spelling from Datamuse.", out
        )
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertGreaterEqual(elapsed, dc.HEDGE_DELAY - 0.05)
        self.assertLess(elapsed, 2.0)

    def test_a_dictionaryapi_entry_without_a_phonetic_spelling_gets_datamuses(self):
        entry = [{"word": "hello", "meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "A greeting."}]}]}]
        net = FakeNet({"google": google_response(), "dictionaryapi": entry, "datamuse": DATAMUSE_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertIn("❖ NOTE: English entry from dictionaryapi.dev, with the phonetic spelling from Datamuse.", out)

    def test_a_word_without_a_known_pronunciation_is_not_waited_for(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_NO_IPA})
        out, elapsed = self.lookup("hello", net)
        self.assertNotIn("❖ PHONETIC", out)
        self.assertIn("English entry from Wiktionary (dictionaryapi.dev: timeout; Google: rate limited (HTTP 429)).", out)
        self.assertNotIn("phonetic spelling from", out)
        self.assertLess(elapsed, 1.0)

    def test_a_silent_datamuse_is_not_waited_for_beyond_the_grace(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": self.hanging()})
        out, elapsed = self.lookup("hello", net)
        self.assertNotIn("❖ PHONETIC", out)
        self.assertIn("English entry from Wiktionary", out)
        self.assertGreaterEqual(elapsed, dc.OPTIONAL_GRACE - 0.1)
        self.assertLess(elapsed, 1.5)
        self.assertIn("datamuse", self.read_cache()["meta"]["down"])

    def test_the_phonetic_spelling_is_saved_with_the_datamuse_answer(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        self.lookup("hello", net)
        saved = self.read_cache()["entries"][f"v{dc.CACHE_VERSION}|datamuse|w|hello"]["v"]
        self.assertEqual(saved["phonetic"], "/həˈloʊ/")
        again = FakeNet({})
        out, _ = self.lookup("hello", again)
        self.assertEqual(again.calls, [])  # everything needed is saved
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)

    def test_datamuse_alone_supplies_the_entry_and_is_not_named_twice(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": NOT_FOUND, "datamuse": DATAMUSE_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("English entry from Datamuse (dictionaryapi.dev: timeout; Google: rate limited (HTTP 429); Wiktionary: no entry).", out)
        self.assertNotIn("with the phonetic spelling", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)

    def test_backups_are_not_asked_while_the_first_choice_still_has_time(self):
        def slow(url):
            time.sleep(0.15)  # well inside the hedge delay
            return google_response()

        net = FakeNet({"google": slow, "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        self.lookup("hello", net)
        self.assertNotIn("mymemory", net.services())

    def test_the_next_backup_supplies_the_entry_when_the_first_has_none(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": NOT_FOUND, "datamuse": DATAMUSE_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn(
            "English entry from Datamuse (dictionaryapi.dev: timeout; Google: rate limited (HTTP 429); Wiktionary: no entry).", out
        )
        self.assertIn("a greeting (or response to a greeting)", out)

    def test_the_better_backup_wins_even_when_the_other_answers_first(self):
        def slow_wiktionary(url):
            time.sleep(0.15)
            return WIKTIONARY_OK

        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": slow_wiktionary, "datamuse": DATAMUSE_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("English entry from Wiktionary", out)

    def test_a_hanging_google_is_replaced_by_mymemory_after_a_short_wait(self):
        net = FakeNet({"google": self.hanging(), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        out, elapsed = self.lookup("hello", net)
        self.assertIn("Persian translation from MyMemory (Google: no answer in time).", out)
        self.assertLess(elapsed, 2.0)

    def test_a_google_that_was_not_answering_has_a_short_grace(self):
        self.write_cache(down={"google": time.time()})
        net = FakeNet({"google": self.hanging(), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        out, elapsed = self.lookup("hello", net)
        self.assertLess(elapsed, 0.6)
        self.assertIn("Persian translation from MyMemory (Google: no answer in time).", out)

    def test_after_an_outage_nothing_is_cut_short_when_there_is_nothing_to_show(self):
        self.write_cache(down={"google": time.time(), "dictionaryapi": time.time()})

        def slow_google(url):
            time.sleep(0.8)
            return google_response()

        net = FakeNet({"google": slow_google, "dictionaryapi": self.hanging()})
        out, elapsed = self.lookup("hello", net)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertGreaterEqual(elapsed, 0.75)

    def test_everything_failing_gives_a_note_with_every_reason(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": timed_out(),
                       "wiktionary": timed_out(), "datamuse": timed_out()})
        out, elapsed = self.lookup("hello", net)
        self.assertLess(elapsed, 1.5)
        self.assertIn("Persian translation unavailable (Google: rate limited (HTTP 429); MyMemory: timeout).", out)
        self.assertIn(
            "English entry not available (dictionaryapi.dev: timeout; Google: rate limited (HTTP 429); "
            "Wiktionary: timeout; Datamuse: timeout).", out
        )
        self.assertIn("Network details:", out)
        self.assertIn("network.log", out)
        self.assertNotIn("❖ POS", out)

    def test_nothing_answering_ends_at_the_deadline(self):
        net = FakeNet({name: self.hanging() for name in ("google", "dictionaryapi", "mymemory", "wiktionary", "datamuse")})
        with mock.patch.object(dc, "TOTAL_BUDGET", 0.8):
            out, elapsed = self.lookup("hello", net)
        self.assertGreaterEqual(elapsed, 0.75)
        self.assertLess(elapsed, 2.0)
        self.assertIn("Persian translation unavailable (Google: no answer within 1 s; MyMemory: no answer within 1 s).", out)
        self.assertIn(
            "English entry not available (dictionaryapi.dev: no answer within 1 s; Google: no answer within 1 s; "
            "Wiktionary: no answer within 1 s; Datamuse: no answer within 1 s).", out
        )

    def test_services_that_are_switched_off_are_never_asked(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        with mock.patch.object(dc, "DISABLED_SERVICES", ("mymemory", "wiktionary", "datamuse")):
            out, _ = self.lookup("hello", net)
        self.assertEqual(sorted(set(net.services())), ["dictionaryapi", "google"])
        self.assertIn("Persian translation unavailable (Google: rate limited (HTTP 429)).", out)
        self.assertIn("English entry not available (dictionaryapi.dev: timeout; Google: rate limited (HTTP 429)).", out)

    # -- phrases and special inputs
    def test_a_phrase_gets_the_translation_only(self):
        net = FakeNet({"google": google_response(extra=False), "dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("one two three", net)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertNotIn("❖ POS", out)
        self.assertEqual(net.services(), ["google"])
        self.assertNotIn("dt=md", net.calls[0][1])

    def test_a_phrase_falls_back_to_mymemory(self):
        net = FakeNet({"google": limited(), "mymemory": MYMEMORY_OK})
        out, _ = self.lookup("one two three", net)
        self.assertIn("Persian translation from MyMemory (Google: rate limited (HTTP 429)).", out)
        self.assertEqual(net.services(), ["google", "mymemory"])

    def test_a_long_phrase_is_not_sent_to_mymemory(self):
        net = FakeNet({"google": limited(), "mymemory": MYMEMORY_OK})
        out, _ = self.lookup(("word " * 96).strip(), net)
        self.assertEqual(net.services(), ["google"])
        self.assertIn("Persian translation unavailable (Google: rate limited (HTTP 429)).", out)

    def test_a_persian_query_gives_no_output_and_asks_nobody_else(self):
        net = FakeNet({"google": google_response(source="fa", extra=False), "mymemory": MYMEMORY_OK})
        out, _ = self.lookup("سلام", net)
        self.assertEqual(out, "")
        self.assertEqual(net.services(), ["google"])

    def test_an_unknown_word_keeps_its_translation_without_a_network_note(self):
        net = FakeNet({"google": google_response(extra=False), "dictionaryapi": NOT_FOUND,
                       "wiktionary": NOT_FOUND, "datamuse": []})
        out, _ = self.lookup("xyzzy", net)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("No English entry found (", out)
        self.assertIn("dictionaryapi.dev: no entry", out)  # (a service that has not answered yet is not listed)
        self.assertNotIn("not available", out)
        self.assertNotIn("Network details", out)

    def test_a_capitalised_word_is_looked_up_in_lower_case_first(self):
        net = FakeNet({"google": google_response(extra=False), "dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("Hello", net)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertEqual([u.rsplit("/", 1)[1] for s, u in net.calls if s == "dictionaryapi"], ["hello"])

    def test_the_form_as_typed_is_tried_when_the_lower_case_one_is_unknown(self):
        def answer(url):
            if url.endswith("/hello"):
                raise dc.NotFound(url)
            return DICTIONARY_OK

        net = FakeNet({"google": google_response(extra=False), "dictionaryapi": answer})
        out, _ = self.lookup("Hello", net)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertEqual([u.rsplit("/", 1)[1] for s, u in net.calls if s == "dictionaryapi"], ["hello", "Hello"])

    def test_google_rejecting_the_extra_fields_falls_back_to_the_plain_translation(self):
        def answer(url):
            if "dt=md" in url:
                raise dc.RouteFailure("HTTP 400")
            return google_response(extra=False)

        net = FakeNet({"google": answer, "dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertNotIn("❖ NOTE:", out)

    def test_a_429_is_not_followed_by_a_second_google_request(self):
        net = FakeNet({"google": limited(), "dictionaryapi": DICTIONARY_OK, "mymemory": MYMEMORY_OK})
        self.lookup("hello", net)
        self.assertEqual(net.services().count("google"), 1)


class AudioButtonTests(LookupCase):
    """By default the card has one pronunciation button: Google voice."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(dc, "RECORDING_BUTTON", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_recording_button_is_off_by_default(self):
        source = (ROOT / "dict_core.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"(?m)^RECORDING_BUTTON = False\b")

    def test_an_entry_with_a_recording_still_gets_only_google_voice(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("▶ Google voice", out)
        self.assertNotIn("Recording", out)
        self.assertNotIn("hello-us.mp3", out)
        self.assertEqual(out.count("<audio"), 1)

    def test_the_recording_button_can_be_switched_on(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        with mock.patch.object(dc, "RECORDING_BUTTON", True):
            out, _ = self.lookup("hello", net)
        self.assertIn("▶ Recording", out)
        self.assertIn("▶ Google voice", out)
        self.assertIn("hello-us.mp3", out)

    def test_every_entry_has_the_same_single_button(self):
        for answers in (
            {"google": google_response(), "dictionaryapi": DICTIONARY_OK},
            {"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK, "wiktionary": WIKTIONARY_OK},
            {"google": limited(), "dictionaryapi": timed_out(), "mymemory": MYMEMORY_OK, "wiktionary": NOT_FOUND,
             "datamuse": DATAMUSE_OK},
        ):
            with self.subTest(services=sorted(answers)):
                self.write_cache()
                out, _ = self.lookup("hello", FakeNet(answers))
                self.assertEqual(out.count("<audio"), 1)
                self.assertIn("▶ Google voice", out)

    def test_the_voice_button_can_be_switched_off(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        with mock.patch.object(dc, "ENABLE_AUDIO", False):
            out, _ = self.lookup("hello", net)
        self.assertNotIn("<audio", out)


class QuietCardTests(LookupCase):
    """By default the card says nothing about which service supplied what. Only a missing part is reported."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(dc, "SHOW_SOURCE_NOTES", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_default_is_quiet(self):
        source = (ROOT / "dict_core.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"(?m)^SHOW_SOURCE_NOTES = False\b")

    def test_backups_and_a_completed_phonetic_spelling_leave_no_note(self):
        net = FakeNet({"google": limited(), "dictionaryapi": self.hanging(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        out, _ = self.lookup("hello", net)
        self.assertNotIn("❖ NOTE", out)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertIn("A greeting, used when meeting somebody.", out)

    def test_google_with_datamuses_phonetic_spelling_leaves_no_note(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": self.hanging(), "datamuse": DATAMUSE_OK})
        out, _ = self.lookup("hello", net)
        self.assertNotIn("❖ NOTE", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)

    def test_a_saved_copy_leaves_no_note(self):
        google_key = f"v{dc.CACHE_VERSION}|google|w|hello"
        dictionary_key = f"v{dc.CACHE_VERSION}|dictionaryapi|w|hello"
        old = time.time() - 60 * 24 * 3600
        self.write_cache({google_key: {"t": old, "v": self.saved_google()},
                          dictionary_key: {"t": old, "v": self.saved_dictionary()}})
        out, _ = self.lookup("hello", FakeNet({"google": timed_out(), "dictionaryapi": timed_out()}))
        self.assertNotIn("❖ NOTE", out)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)

    def test_a_missing_part_is_still_reported(self):
        net = FakeNet({"google": limited(), "dictionaryapi": timed_out(), "mymemory": timed_out(),
                       "wiktionary": timed_out(), "datamuse": timed_out()})
        out, _ = self.lookup("hello", net)
        self.assertIn("❖ NOTE: Persian translation unavailable (Google: rate limited (HTTP 429); MyMemory: timeout).", out)
        self.assertIn("❖ NOTE: English entry not available (", out)
        self.assertIn("Network details:", out)

    def test_only_the_part_that_is_missing_is_reported(self):
        net = FakeNet({"google": limited(), "dictionaryapi": DICTIONARY_OK, "mymemory": timed_out()})
        out, _ = self.lookup("hello", net)
        self.assertIn("Persian translation unavailable (Google: rate limited (HTTP 429); MyMemory: timeout).", out)
        self.assertNotIn("English entry", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)

    def test_a_word_that_has_no_entry_leaves_no_note(self):
        # dictionaryapi.dev's own server is down, and everybody else says there is no entry
        answers = {"google": google_response(extra=False), "dictionaryapi": dc.RouteFailure("HTTP 530"),
                   "mymemory": MYMEMORY_OK, "wiktionary": NOT_FOUND, "datamuse": []}
        out, _ = self.lookup("woolly hat", FakeNet(answers))
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertNotIn("❖ NOTE", out)
        self.assertNotIn("Network details", out)
        log = dc.log_path().read_text(encoding="utf-8")
        self.assertIn("note: No English entry found (", log)
        self.assertIn("dictionaryapi.dev: server error (HTTP 530)", log)
        self.assertIn("Wiktionary: no entry", log)

    def test_the_english_only_version_then_shows_nothing_at_all(self):
        answers = {"google": google_response(extra=False), "dictionaryapi": dc.RouteFailure("HTTP 530"),
                   "wiktionary": NOT_FOUND, "datamuse": []}
        out, _ = self.lookup("woolly hat", FakeNet(answers), translate=False)
        self.assertEqual(out, "")

    def test_wiktionary_and_datamuse_saying_no_entry_is_enough(self):
        answers = {"google": dc.RouteFailure("timeout"), "dictionaryapi": dc.RouteFailure("timeout"),
                   "wiktionary": NOT_FOUND, "datamuse": [], "mymemory": MYMEMORY_OK}
        out, _ = self.lookup("woolly hat", FakeNet(answers))
        self.assertNotIn("❖ NOTE", out)

    def test_a_word_without_an_entry_does_not_wait_for_a_silent_service(self):
        answers = {"google": google_response(extra=False), "dictionaryapi": self.hanging(),
                   "mymemory": MYMEMORY_OK, "wiktionary": NOT_FOUND, "datamuse": []}
        out, elapsed = self.lookup("woolly hat", FakeNet(answers))
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertNotIn("❖ NOTE", out)
        self.assertLess(elapsed, 1.0)  # not the whole time budget, and not the grace for dictionaryapi.dev either

    def test_the_english_only_version_does_not_wait_either(self):
        answers = {"google": google_response(extra=False), "dictionaryapi": self.hanging(),
                   "wiktionary": NOT_FOUND, "datamuse": []}
        out, elapsed = self.lookup("woolly hat", FakeNet(answers), translate=False)
        self.assertEqual(out, "")
        self.assertLess(elapsed, 1.0)

    def test_the_next_lookup_of_such_a_word_needs_no_network(self):
        answers = {"google": google_response(extra=False), "dictionaryapi": self.hanging(),
                   "mymemory": MYMEMORY_OK, "wiktionary": NOT_FOUND, "datamuse": []}
        self.lookup("woolly hat", FakeNet(answers))
        again = FakeNet({})
        out, elapsed = self.lookup("woolly hat", again)
        self.assertEqual(again.calls, [])
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertLess(elapsed, 0.3)

    def test_two_narrow_services_saying_no_entry_are_not_enough(self):
        # Google and Datamuse know only common words, so they cannot tell that a rare word has no entry
        answers = {"google": google_response(extra=False), "dictionaryapi": dc.RouteFailure("timeout"),
                   "wiktionary": dc.RouteFailure("timeout"), "datamuse": [], "mymemory": MYMEMORY_OK}
        out, _ = self.lookup("rarity", FakeNet(answers))
        self.assertIn("❖ NOTE: English entry not available (", out)
        self.assertIn("Network details:", out)

    def test_a_single_no_entry_answer_is_not_enough(self):
        answers = {"google": dc.RouteFailure("timeout"), "dictionaryapi": dc.RouteFailure("timeout"),
                   "wiktionary": NOT_FOUND, "datamuse": dc.RouteFailure("timeout"), "mymemory": MYMEMORY_OK}
        out, _ = self.lookup("rarity", FakeNet(answers))
        self.assertIn("❖ NOTE: English entry not available (", out)

    def test_a_phrase_limit_is_still_explained(self):
        out, _ = self.lookup("one two three", FakeNet({}), translate=False)
        self.assertIn("English entries are only looked up for one or two words.", out)

    def test_what_was_hidden_is_in_the_log(self):
        net = FakeNet({"google": limited(), "dictionaryapi": self.hanging(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        self.lookup("hello", net)
        log = dc.log_path().read_text(encoding="utf-8")
        self.assertIn("note: Persian translation from MyMemory (Google: rate limited (HTTP 429)).", log)
        self.assertIn("note: English entry from Wiktionary (", log)
        self.assertIn("with the phonetic spelling from Datamuse", log)


class EnglishOnlyTests(LookupCase):
    """English entry only (dict_eng_only.py)."""

    def test_dictionaryapi_and_google_are_asked_and_nobody_else(self):
        net = FakeNet({"dictionaryapi": DICTIONARY_OK, "google": google_response()})
        out, elapsed = self.lookup("hello", net, translate=False)
        self.assertEqual(sorted(set(net.services())), ["dictionaryapi", "google"])
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertNotIn("❖ NOTE:", out)
        self.assertNotIn("GOOGLE TRANSLATE", out)
        self.assertLess(elapsed, 0.3)

    def test_a_silent_dictionaryapi_is_backed_up(self):
        net = FakeNet({"dictionaryapi": self.hanging(), "google": limited(), "wiktionary": WIKTIONARY_OK,
                       "datamuse": DATAMUSE_OK})
        out, elapsed = self.lookup("hello", net, translate=False)
        self.assertIn(
            "English entry from Wiktionary (dictionaryapi.dev: no answer in time; Google: rate limited (HTTP 429)), "
            "with the phonetic spelling from Datamuse.", out
        )
        self.assertLess(elapsed, 2.0)

    def test_a_dictionaryapi_that_was_not_answering_is_backed_up_at_once(self):
        self.write_cache(down={"dictionaryapi": time.time()})
        net = FakeNet({"dictionaryapi": self.hanging(), "wiktionary": WIKTIONARY_OK})
        out, elapsed = self.lookup("hello", net, translate=False)
        self.assertIn("English entry from Wiktionary", out)
        self.assertLess(elapsed, 0.3 + dc.SHORT_GRACE)  # no hedge delay

    def test_google_supplies_the_entry_when_the_others_have_none(self):
        net = FakeNet({"dictionaryapi": NOT_FOUND, "wiktionary": NOT_FOUND, "datamuse": [],
                       "google": google_response()})
        out, _ = self.lookup("hello", net, translate=False)
        self.assertEqual(net.services().count("google"), 1)
        self.assertIn("❖ POS: exclamation", out)
        self.assertNotIn("❖ NOTE:", out)
        self.assertNotIn("GOOGLE TRANSLATE", out)

    def test_everything_failing_names_every_reason(self):
        net = FakeNet({"dictionaryapi": timed_out(), "wiktionary": timed_out(), "datamuse": timed_out(),
                       "google": limited()})
        out, _ = self.lookup("hello", net, translate=False)
        self.assertIn(
            "English entry not available (dictionaryapi.dev: timeout; Google: rate limited (HTTP 429); "
            "Wiktionary: timeout; Datamuse: timeout).", out
        )
        self.assertIn("Network details:", out)

    def test_a_phrase_gives_a_note_and_makes_no_request(self):
        net = FakeNet({})
        out, _ = self.lookup("one two three", net, translate=False)
        self.assertIn("English entries are only looked up for one or two words.", out)
        self.assertEqual(net.calls, [])


class ExtrasTests(LookupCase):
    """The phonetic spelling, examples and synonyms are filled in from other services when the best entry lacks them."""

    @staticmethod
    def google_without_examples():
        data = google_response()
        data[13] = None
        return data

    @staticmethod
    def dictionary_without_synonyms():
        return [{"word": "hello", "phonetic": "/x/", "meanings": [
            {"partOfSpeech": "noun", "definitions": [{"definition": "A greeting.", "example": "Hello there!"}]}]}]

    def test_examples_come_from_wiktionary_when_google_has_none(self):
        net = FakeNet({"google": self.google_without_examples(), "dictionaryapi": self.hanging(),
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        out, elapsed = self.lookup("hello", net)
        self.assertIn("❖ EXAMPLES:", out)
        self.assertIn("Hello, everyone.", out)       # Wiktionary's example
        self.assertIn("Used as a greeting.", out)     # the definitions are still Google's
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertLess(elapsed, 2.5)

    def test_synonyms_come_from_google_when_dictionaryapi_has_none(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": self.dictionary_without_synonyms()})
        out, _ = self.lookup("hello", net, translate=False)
        self.assertIn("❖ SYNONYMS:", out)
        self.assertIn("hi · hey", out)
        self.assertIn("A greeting.", out)  # the definitions are dictionaryapi's

    def test_a_slow_google_is_waited_for_its_synonyms(self):
        def slow_google(url):
            time.sleep(0.25)
            return google_response()

        net = FakeNet({"google": slow_google, "dictionaryapi": self.dictionary_without_synonyms()})
        out, elapsed = self.lookup("hello", net, translate=False)
        self.assertIn("hi · hey", out)
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 1.0)

    def test_a_google_that_limits_the_requests_is_not_waited_for(self):
        self.write_cache(cool={"google": {"until": time.time() + 600, "strikes": 1}})
        net = FakeNet({"dictionaryapi": self.dictionary_without_synonyms()})
        out, elapsed = self.lookup("hello", net, translate=False)
        self.assertNotIn("❖ SYNONYMS", out)
        self.assertEqual(net.services(), ["dictionaryapi"])
        self.assertLess(elapsed, 0.3)

    def test_a_silent_google_is_not_waited_for_longer_than_the_grace_for_extras(self):
        net = FakeNet({"google": self.hanging(), "dictionaryapi": self.dictionary_without_synonyms()})
        out, elapsed = self.lookup("hello", net, translate=False)
        self.assertNotIn("❖ SYNONYMS", out)
        self.assertGreaterEqual(elapsed, dc.EXTRA_GRACE - 0.1)
        self.assertLess(elapsed, 1.5)
        self.assertIn("google", self.read_cache()["meta"]["down"])


class SharedLookupTests(LookupCase):
    """GoldenDict runs both programs for every search at the same moment. They must agree, and ask each service once."""

    def setUp(self):
        super().setUp()
        for name, value in (("SHOW_SOURCE_NOTES", False), ("RECORDING_BUTTON", False)):
            patcher = mock.patch.object(dc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def lock_for(self, text):
        return self.cache_file.parent / f"lookup-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]}.lock"

    def run_both(self, net, text="hello"):
        results = {}

        def work(name, translate):
            results[name] = dc.build_output(text, opts(translate=translate), net=net)

        threads = [threading.Thread(target=work, args=("full", True)),
                   threading.Thread(target=work, args=("english", False))]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        return results["full"], results["english"], time.monotonic() - started

    def assert_the_same_entry(self, full, english):
        self.assertTrue(english)
        self.assertTrue(full.endswith(english), "the English part of the two programs differs")

    def test_both_programs_show_the_same_entry_and_each_service_is_asked_once(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        full, english, _ = self.run_both(net)
        self.assert_the_same_entry(full, english)
        self.assertIn("❖ GOOGLE TRANSLATE:", full)
        self.assertNotIn("GOOGLE TRANSLATE", english)
        self.assertEqual(sorted(net.services()), ["dictionaryapi", "google"])

    def test_the_same_entry_when_dictionaryapi_is_slow(self):
        def slow(url):
            time.sleep(0.25)
            return DICTIONARY_OK

        net = FakeNet({"google": google_response(), "dictionaryapi": slow})
        full, english, _ = self.run_both(net)
        self.assert_the_same_entry(full, english)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", english)
        self.assertEqual(sorted(net.services()), ["dictionaryapi", "google"])

    def test_the_same_entry_when_dictionaryapi_is_silent_and_google_limits(self):
        net = FakeNet({"google": limited(), "dictionaryapi": self.hanging(), "mymemory": MYMEMORY_OK,
                       "wiktionary": WIKTIONARY_OK, "datamuse": DATAMUSE_OK})
        full, english, elapsed = self.run_both(net)
        self.assert_the_same_entry(full, english)
        self.assertIn("A greeting, used when meeting somebody.", english)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", english)
        for service in ("google", "dictionaryapi", "mymemory", "wiktionary", "datamuse"):
            self.assertEqual(net.services().count(service), 1, service)
        self.assertLess(elapsed, 3.0)

    def test_the_same_entry_when_only_the_english_part_is_missing_a_phonetic_spelling(self):
        entry = [{"word": "hello", "meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "A greeting."}]}]}]
        net = FakeNet({"google": google_response(), "dictionaryapi": entry, "datamuse": DATAMUSE_OK})
        full, english, _ = self.run_both(net)
        self.assert_the_same_entry(full, english)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", english)
        self.assertEqual(net.services().count("datamuse"), 1)

    # -- the lock itself
    def test_a_lookup_waits_while_another_script_holds_the_word(self):
        lock = self.lock_for("hello")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        done = threading.Event()

        def work():
            dc.build_output("hello", opts(), net=net)
            done.set()

        threading.Thread(target=work, daemon=True).start()
        time.sleep(0.4)
        self.assertFalse(done.is_set())
        self.assertEqual(net.calls, [])
        lock.unlink()
        self.assertTrue(done.wait(3.0))
        self.assertFalse(lock.exists())  # and it let go again

    def test_a_lock_left_by_a_dead_script_is_ignored(self):
        lock = self.lock_for("hello")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        old = time.time() - dc.LOOKUP_LOCK_STALE - 5
        os.utime(lock, (old, old))
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        _, elapsed = self.lookup("hello", net)
        self.assertLess(elapsed, 0.5)
        self.assertFalse(lock.exists())

    def test_the_wait_is_limited_and_somebody_elses_lock_is_left_alone(self):
        lock = self.lock_for("hello")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        with mock.patch.object(dc, "LOOKUP_WAIT", 0.3):
            out, elapsed = self.lookup("hello", net)
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertLess(elapsed, 1.5)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)  # it went ahead on its own
        self.assertTrue(lock.exists())

    def test_the_wait_fits_inside_the_time_budget(self):
        lock = self.lock_for("hello")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        with mock.patch.object(dc, "TOTAL_BUDGET", 1.5):
            _, elapsed = self.lookup("hello", net)
        self.assertLess(elapsed, 1.5)  # LOOKUP_WAIT alone would have been 8 seconds

    def test_the_lock_is_released_after_an_error(self):
        net = FakeNet({})
        with mock.patch.object(dc, "_lookup", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                dc.build_output("hello", opts(), net=net)
        self.assertFalse(self.lock_for("hello").exists())

    def test_other_words_do_not_wait(self):
        lock = self.lock_for("hello")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        _, elapsed = self.lookup("world", net)
        self.assertLess(elapsed, 0.5)

    def test_without_a_cache_there_is_no_lock(self):
        lock = self.lock_for("hello")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        _, elapsed = self.lookup("hello", net, cache=False)
        self.assertLess(elapsed, 0.5)
        self.assertTrue(lock.exists())


class EntryAbsentTests(unittest.TestCase):
    @staticmethod
    def tasks(**states):
        """states: service -> 'none' (answered without an entry), 'entry', 'failed' or 'running'."""
        result = {}
        for name, state in states.items():
            task = dc.Task(name, "k", 0, dc.capabilities(name, True, False))
            task.launched = True
            if state == "none":
                task.done, task.value = True, {"translation": None, "entry": None}
            elif state == "entry":
                task.done, task.value = True, {"translation": None, "entry": {"groups": [1]}}
            elif state == "failed":
                task.done, task.error = True, "timeout"
            result[name] = task
        return result

    def test_two_answers_one_of_them_broad(self):
        for states in ({"wiktionary": "none", "datamuse": "none"}, {"dictionaryapi": "none", "google": "none"},
                       {"wiktionary": "none", "google": "none", "dictionaryapi": "failed"},
                       {"dictionaryapi": "none", "wiktionary": "none", "datamuse": "running"}):
            with self.subTest(states=states):
                self.assertTrue(dc.entry_absent(self.tasks(**states)))

    def test_not_enough(self):
        for states in ({"wiktionary": "none"}, {"google": "none", "datamuse": "none"},
                       {"wiktionary": "none", "dictionaryapi": "failed", "google": "failed", "datamuse": "failed"},
                       {"wiktionary": "none", "google": "running"}, {}):
            with self.subTest(states=states):
                self.assertFalse(dc.entry_absent(self.tasks(**states)))

    def test_an_entry_anywhere_means_the_word_has_one(self):
        self.assertFalse(dc.entry_absent(self.tasks(wiktionary="none", datamuse="none", google="entry")))

    def test_a_saved_not_found_answer_counts(self):
        saved = dc.Task.cached("wiktionary", "k", 1, dc.capabilities("wiktionary", True, False), None)
        other = dc.Task.cached("datamuse", "k", 1, dc.capabilities("datamuse", True, False), None)
        self.assertTrue(dc.entry_absent({"wiktionary": saved, "datamuse": other}))

    def test_services_asked_about_something_else_do_not_count(self):
        mymemory = dc.Task.cached("mymemory", "k", 1, dc.capabilities("mymemory", True, True), {"translation": "x", "entry": None})
        wiktionary = dc.Task.cached("wiktionary", "k", 1, dc.capabilities("wiktionary", True, True), None)
        self.assertFalse(dc.entry_absent({"mymemory": mymemory, "wiktionary": wiktionary}))


class SavedResultTests(LookupCase):
    def keys(self):
        return f"v{dc.CACHE_VERSION}|google|w|hello", f"v{dc.CACHE_VERSION}|dictionaryapi|w|hello"

    def test_results_are_saved_and_reused(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        first, _ = self.lookup("hello", net)
        calls = len(net.calls)
        second, _ = self.lookup("hello", net)
        self.assertEqual(first, second)
        self.assertEqual(len(net.calls), calls)

    def test_failures_are_not_saved(self):
        net = FakeNet({name: timed_out() for name in ("google", "dictionaryapi", "mymemory", "wiktionary", "datamuse")})
        self.lookup("hello", net)
        self.assertEqual(self.read_cache()["entries"], {})

    def test_nothing_found_is_saved_as_a_not_found_answer_that_lasts_a_day(self):
        net = FakeNet({"google": google_response(extra=False), "dictionaryapi": NOT_FOUND,
                       "wiktionary": NOT_FOUND, "datamuse": []})
        self.lookup("hello", net)
        entries = self.read_cache()["entries"]
        # The verdict "no entry" needs two answers, so a service that had not answered yet is not saved.
        self.assertIsNone(entries[f"v{dc.CACHE_VERSION}|dictionaryapi|w|hello"]["v"])
        for name in ("wiktionary", "datamuse"):
            key = f"v{dc.CACHE_VERSION}|{name}|w|hello"
            if key in entries:
                self.assertIsNone(entries[key]["v"], name)
        self.assertEqual(entries[f"v{dc.CACHE_VERSION}|google|w|hello"]["v"]["translation"], "سلام")

    def test_the_backups_that_found_nothing_are_saved_too(self):
        net = FakeNet({"google": limited(), "dictionaryapi": self.hanging(), "mymemory": MYMEMORY_OK,
                       "wiktionary": NOT_FOUND, "datamuse": []})
        self.lookup("hello", net)
        entries = self.read_cache()["entries"]
        for name in ("wiktionary", "datamuse"):
            self.assertIsNone(entries[f"v{dc.CACHE_VERSION}|{name}|w|hello"]["v"], name)

    def test_the_cache_can_be_switched_off(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        self.lookup("hello", net, cache=False)
        self.lookup("hello", net, cache=False)
        self.assertEqual(len(net.calls), 4)
        self.assertFalse(self.cache_file.exists())

    def test_a_corrupt_file_is_ignored(self):
        self.cache_file.write_text("{not json", encoding="utf-8")
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        out, _ = self.lookup("hello", net)
        self.assertIn("❖ POS: exclamation", out)

    def test_fresh_saved_results_need_no_network(self):
        google_key, dictionary_key = self.keys()
        now = time.time()
        self.write_cache({google_key: {"t": now, "v": self.saved_google()},
                          dictionary_key: {"t": now, "v": self.saved_dictionary()}})
        net = FakeNet({})
        out, _ = self.lookup("hello", net)
        self.assertEqual(net.calls, [])
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertNotIn("❖ NOTE:", out)

    def test_a_fresh_saved_backup_result_is_used_without_asking_it_again(self):
        mymemory_key = f"v{dc.CACHE_VERSION}|mymemory|w|hello"
        _, dictionary_key = self.keys()
        now = time.time()
        self.write_cache({mymemory_key: {"t": now, "v": {"translation": "سلام", "entry": None}},
                          dictionary_key: {"t": now, "v": self.saved_dictionary()}},
                         cool={"google": {"until": now + 600, "strikes": 1}})
        net = FakeNet({})
        out, _ = self.lookup("hello", net)
        self.assertEqual(net.calls, [])
        self.assertIn("Persian translation from MyMemory (Google: skipped, it limited the requests recently).", out)

    def test_a_backup_result_older_than_a_day_is_asked_again(self):
        old = time.time() - 2 * 24 * 3600
        self.write_cache({f"v{dc.CACHE_VERSION}|wiktionary|w|hello": {
            "t": old, "v": {"translation": None, "entry": dc.parse_wiktionary(WIKTIONARY_OK)}}})
        net = FakeNet({"dictionaryapi": NOT_FOUND, "wiktionary": WIKTIONARY_OK, "datamuse": []})
        out, _ = self.lookup("hello", net, translate=False)
        self.assertIn("wiktionary", net.services())
        self.assertIn("English entry from Wiktionary", out)
        self.assertNotIn("saved copies", out)

    def test_a_dictionaryapi_result_stays_fresh_for_a_month(self):
        old = time.time() - 20 * 24 * 3600
        self.write_cache({f"v{dc.CACHE_VERSION}|dictionaryapi|w|hello": {"t": old, "v": self.saved_dictionary()}})
        net = FakeNet({})
        out, _ = self.lookup("hello", net, translate=False)
        self.assertEqual(net.calls, [])
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)

    def test_a_fresh_not_found_answer_is_used(self):
        _, dictionary_key = self.keys()
        self.write_cache({dictionary_key: {"t": time.time(), "v": None}})
        net = FakeNet({"wiktionary": WIKTIONARY_OK})
        self.lookup("hello", net, translate=False)
        self.assertNotIn("dictionaryapi", net.services())

    def test_an_expired_not_found_answer_is_asked_again(self):
        _, dictionary_key = self.keys()
        self.write_cache({dictionary_key: {"t": time.time() - dc.NEGATIVE_CACHE_TTL - 60, "v": None}})
        net = FakeNet({"dictionaryapi": DICTIONARY_OK})
        self.lookup("hello", net, translate=False)
        self.assertIn("dictionaryapi", net.services())

    def test_a_saved_copy_is_used_when_the_network_fails(self):
        google_key, dictionary_key = self.keys()
        old = time.time() - 60 * 24 * 3600  # not fresh any more, but still usable
        self.write_cache({google_key: {"t": old, "v": self.saved_google()},
                          dictionary_key: {"t": old, "v": self.saved_dictionary()}})
        net = FakeNet({"google": timed_out(), "dictionaryapi": timed_out()})
        out, _ = self.lookup("hello", net)
        self.assertIn("saved copies from", out)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertNotIn("unavailable", out)
        self.assertNotIn("Network details", out)

    def test_a_saved_copy_is_used_when_the_network_is_too_slow(self):
        google_key, dictionary_key = self.keys()
        old = time.time() - 60 * 24 * 3600
        self.write_cache({google_key: {"t": old, "v": self.saved_google()},
                          dictionary_key: {"t": old, "v": self.saved_dictionary()}})
        net = FakeNet({"google": self.hanging(), "dictionaryapi": self.hanging()})
        with mock.patch.object(dc, "TOTAL_BUDGET", 0.6):
            out, _ = self.lookup("hello", net)
        self.assertIn("saved copies from", out)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)

    def test_a_result_older_than_a_year_is_not_used(self):
        google_key, dictionary_key = self.keys()
        ancient = time.time() - dc.STALE_TTL - 1000
        self.write_cache({google_key: {"t": ancient, "v": self.saved_google()},
                          dictionary_key: {"t": ancient, "v": self.saved_dictionary()}})
        net = FakeNet({"google": timed_out(), "dictionaryapi": timed_out()})
        out, _ = self.lookup("hello", net)
        self.assertNotIn("saved copies", out)
        self.assertIn("English entry not available", out)

    def test_every_lookup_leaves_a_line_in_the_log(self):
        net = FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK})
        self.lookup("hello", net)
        text = dc.log_path().read_text(encoding="utf-8")
        self.assertIn("lookup 'hello'", text)
        self.assertIn("=> google: ok", text)
        self.assertIn("=> dictionaryapi: ok", text)
        self.assertIn("=> mymemory: not needed", text)


# ── Connection routes ──────────────────────────────────────────────────────
class _Service(http.server.BaseHTTPRequestHandler):
    """A stand-in for the web services."""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        segment = path.split("/")[1] if "/" in path else ""
        self.server.hits[segment] = self.server.hits.get(segment, 0) + 1
        time.sleep(self.server.delays.get(segment, 0))
        if path.startswith("/hang"):
            time.sleep(30)  # a service that never answers
            return
        if path == "/missing":
            return self._send(404, {})
        if path == "/broken":
            return self._send(500, {})
        if path == "/limited":
            self.server.limited_hits += 1
            return self._send(429, {}, {"Retry-After": "7"})
        if path == "/flaky":
            self.server.flaky_hits += 1
            if self.server.flaky_hits == 1:
                return self._send(500, {})
        routes = {
            "/translate": google_response(),
            "/mymemory": MYMEMORY_OK,
            "/datamuse": DATAMUSE_OK,
        }
        if path in routes:
            return self._send(200, routes[path])
        if path.startswith("/entries/"):
            return self._send(200, DICTIONARY_OK)
        if path.startswith("/wiktionary/"):
            return self._send(200, WIKTIONARY_OK)
        self._send(200, {"ok": 1})

    def _send(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def start_service(tls_context=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Service)
    server.flaky_hits = 0
    server.limited_hits = 0
    server.hits = {}    # requests seen, by the first part of the path
    server.delays = {}  # seconds to wait before answering, by the first part of the path
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _pump(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class FakeProxy:
    """A small HTTP-CONNECT or SOCKS5 proxy on 127.0.0.1, standing in for a VPN app."""

    def __init__(self, mode):
        self.mode = mode
        self.hits = 0
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def address(self):
        return f"{'socks5' if self.mode == 'socks5' else 'http'}://127.0.0.1:{self.port}"

    def close(self):
        self.listener.close()

    def _serve(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        target = None
        try:
            target = self._http_connect(client) if self.mode == "http" else self._socks5(client)
            if target is None:
                return
            self.hits += 1
            threading.Thread(target=_pump, args=(target, client), daemon=True).start()
            _pump(client, target)
        except OSError:
            pass
        finally:
            for sock in (client, target):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _read(client, count):
        data = b""
        while len(data) < count:
            chunk = client.recv(count - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _http_connect(self, client):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                return None
            data += chunk
        method, address, _ = data.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
        if method != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return None
        host, _, port = address.rpartition(":")
        target = socket.create_connection((host, int(port)), timeout=5)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        return target

    def _socks5(self, client):
        if self._read(client, 3) != b"\x05\x01\x00":
            return None
        client.sendall(b"\x05\x00")
        head = self._read(client, 4)
        if head[3] != 3:
            return None
        name = self._read(client, self._read(client, 1)[0])
        port = int.from_bytes(self._read(client, 2), "big")
        target = socket.create_connection((name.decode("idna"), port), timeout=5)
        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        return target


class Blackhole:
    """An opener whose connection never gets an answer, like a blocked service."""

    def __init__(self, stop):
        self.stop = stop

    def open(self, request, timeout=None):
        self.stop.wait(timeout or 30)
        raise socket.timeout("timed out")


class BlockedDirect(dc.Network):
    """A network whose direct and IPv4 routes never get an answer."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop = threading.Event()

    def _direct(self):
        return dc.Route("direct", lambda: Blackhole(self.stop), group="direct")

    def _ipv4(self):
        return dc.Route("ipv4", lambda: Blackhole(self.stop), group="direct")


class OnlyIPv4(dc.Network):
    def routes(self, service):
        yield self._ipv4()


class DirectThenHang(dc.Network):
    """A real direct route followed by a route that never answers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop = threading.Event()

    def routes(self, service):
        yield self._direct()
        yield dc.Route("hang", lambda: Blackhole(self.stop), group="hang")


def make_certificate(folder):
    cert, key = Path(folder, "cert.pem"), Path(folder, "key.pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True,
    )
    return cert, key


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed to create a test certificate")
class TLSCase(unittest.TestCase):
    """A local HTTPS service with its own certificate."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cert, key = make_certificate(cls.folder.name)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert), str(key))
        cls.server = start_service(server_context)
        cls.context = ssl.create_default_context(cafile=str(cert))
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.folder.cleanup()

    def url(self, path):
        return f"https://localhost:{self.port}{path}"


class NetworkTests(TLSCase):
    def setUp(self):
        self.server.flaky_hits = 0
        self.server.limited_hits = 0
        patches = [mock.patch("urllib.request.getproxies", return_value={})]
        for name, value in (("ROUTE_TIMEOUT", 3.0), ("ROUTE_STAGGER", 0.3), ("AUTO_PROXY", False), ("PROXY", "")):
            patches.append(mock.patch.object(dc, name, value))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def network(self, blocked=False, meta=None):
        net = (BlockedDirect if blocked else dc.Network)(meta if meta is not None else {}, context=self.context)
        if blocked:
            self.addCleanup(net.stop.set)
        return net

    def proxy(self, mode):
        proxy = FakeProxy(mode)
        self.addCleanup(proxy.close)
        return proxy

    @staticmethod
    def deadline(seconds=10.0):
        return time.monotonic() + seconds

    def timed(self, net, path="/ok", seconds=10.0):
        started = time.monotonic()
        data = net.get_json("google", self.url(path), self.deadline(seconds))
        return data, time.monotonic() - started

    def test_a_direct_route_gets_the_answer_and_is_remembered(self):
        net = self.network()
        data, _ = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(net.meta["route"]["google"], "direct")

    def test_the_ipv4_route_gets_the_answer(self):
        net = OnlyIPv4({}, context=self.context)
        data, _ = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(net.meta["route"]["google"], "ipv4")

    def test_an_http_proxy_works_when_direct_hangs(self):
        proxy = self.proxy("http")
        net = self.network(blocked=True)
        with mock.patch.object(dc, "PROXY", proxy.address):
            data, elapsed = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertLess(elapsed, 2.0)  # well before the blocked route's own timeout
        self.assertEqual(proxy.hits, 1)
        self.assertEqual(net.meta["route"]["google"], proxy.address)

    def test_a_socks5_proxy_works_when_direct_hangs(self):
        proxy = self.proxy("socks5")
        net = self.network(blocked=True)
        with mock.patch.object(dc, "PROXY", proxy.address):
            data, elapsed = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertLess(elapsed, 2.0)
        self.assertEqual(proxy.hits, 1)

    def test_the_system_proxy_is_used(self):
        proxy = self.proxy("http")
        net = self.network(blocked=True)
        with mock.patch("urllib.request.getproxies", return_value={"http": proxy.address, "https": proxy.address}):
            data, _ = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(proxy.hits, 1)

    def test_a_local_vpn_proxy_is_found_without_any_setting(self):
        proxy = self.proxy("socks5")
        net = self.network(blocked=True)
        with mock.patch.object(dc, "AUTO_PROXY", True), \
                mock.patch.object(dc, "LOCAL_PROXY_PORTS", (closed_port(), proxy.port)):
            data, elapsed = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertLess(elapsed, 2.5)
        self.assertEqual(net.meta["route"]["google"], proxy.address)
        self.assertTrue(any("local proxy ports found" in line for line in net.lines))

    def test_the_remembered_route_is_tried_first(self):
        proxy = self.proxy("socks5")
        net = self.network(blocked=True, meta={"route": {"google": proxy.address}})
        with mock.patch.object(dc, "ROUTE_STAGGER", 1.0):
            data, elapsed = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertLess(elapsed, 0.9)

    def test_a_route_that_fails_at_once_lets_the_next_start_at_once(self):
        net = self.network(meta={"route": {"google": f"socks5://127.0.0.1:{closed_port()}"}})
        with mock.patch.object(dc, "ROUTE_STAGGER", 5.0):
            data, elapsed = self.timed(net)
        self.assertEqual(data, {"ok": 1})
        self.assertLess(elapsed, 2.0)
        self.assertEqual(net.meta["route"]["google"], "direct")

    def test_when_every_route_hangs_the_deadline_ends_the_wait(self):
        net = self.network(blocked=True)
        with mock.patch.object(dc, "ROUTE_TIMEOUT", 30.0):
            started = time.monotonic()
            with self.assertRaises(dc.RouteFailure) as caught:
                net.get_json("google", self.url("/ok"), self.deadline(0.8))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertIn("direct: timeout", caught.exception.detail)

    def test_a_fast_server_error_is_tried_again(self):
        net = self.network()
        with mock.patch.object(dc.time, "sleep"):
            data, _ = self.timed(net, "/flaky")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(self.server.flaky_hits, 2)

    def test_a_persistent_server_error_is_reported_with_its_status(self):
        net = self.network()
        with mock.patch.object(dc.time, "sleep"):
            with self.assertRaises(dc.RouteFailure) as caught:
                net.get_json("google", self.url("/broken"), self.deadline())
        self.assertEqual(caught.exception.reason, "HTTP 500")

    def test_a_404_is_not_found(self):
        with self.assertRaises(dc.NotFound):
            self.network().get_json("dictionaryapi", self.url("/missing"), self.deadline())

    def test_an_untrusted_certificate_is_reported_as_a_tls_error(self):
        net = dc.Network({}, context=ssl.create_default_context())
        with mock.patch.object(dc.time, "sleep"):
            with self.assertRaises(dc.RouteFailure) as caught:
                net.get_json("google", self.url("/ok"), self.deadline())
        self.assertEqual(caught.exception.reason, "TLS error")

    # -- rate limits
    def test_a_429_is_asked_once_not_retried_and_not_repeated_over_the_same_exit(self):
        net = self.network()  # routes: direct, then IPv4, which leaves through the same address
        with mock.patch.object(dc.time, "sleep"):
            with self.assertRaises(dc.RouteFailure) as caught:
                net.get_json("google", self.url("/limited"), self.deadline())
        self.assertEqual(caught.exception.reason, "HTTP 429")
        self.assertEqual(caught.exception.retry_after, 7)
        self.assertEqual(self.server.limited_hits, 1)

    def test_another_exit_is_still_tried_after_a_429(self):
        proxy = self.proxy("http")
        net = self.network()
        with mock.patch.object(dc, "PROXY", proxy.address), mock.patch.object(dc.time, "sleep"):
            with self.assertRaises(dc.RouteFailure):
                net.get_json("google", self.url("/limited"), self.deadline())
        self.assertEqual(self.server.limited_hits, 2)  # direct and through the proxy, but not over IPv4
        self.assertEqual(proxy.hits, 1)

    def test_the_most_telling_reason_wins_when_routes_disagree(self):
        net = DirectThenHang({}, context=self.context)
        self.addCleanup(net.stop.set)
        with mock.patch.object(dc, "ROUTE_TIMEOUT", 30.0):
            with self.assertRaises(dc.RouteFailure) as caught:
                net.get_json("google", self.url("/limited"), self.deadline(1.0))
        self.assertEqual(caught.exception.reason, "HTTP 429")

    def test_a_full_lookup_works_through_a_vpn_proxy_when_direct_is_blocked(self):
        proxy = self.proxy("socks5")
        net = BlockedDirect({}, context=self.context)
        self.addCleanup(net.stop.set)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(dc, "PROXY", proxy.address), \
                mock.patch.object(dc, "cache_path", return_value=Path(tmp.name) / "cache.json"), \
                mock.patch.object(dc, "TRANSLATE_URL", self.url("/translate")), \
                mock.patch.object(dc, "DICTIONARY_URL", self.url("/entries/{}")):
            out = dc.build_output("hello", opts(), net=net)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", out)
        self.assertNotIn("❖ NOTE:", out)


class DiscoveryTests(unittest.TestCase):
    def test_http_and_socks5_ports_are_told_apart_and_closed_ports_ignored(self):
        http_proxy, socks_proxy = FakeProxy("http"), FakeProxy("socks5")
        self.addCleanup(http_proxy.close)
        self.addCleanup(socks_proxy.close)
        gone = closed_port()
        found = dc.discover_local_proxies((http_proxy.port, gone, socks_proxy.port), budget=3.0)
        self.assertEqual(found, [http_proxy.address, socks_proxy.address])

    def test_probe_results(self):
        socks_proxy = FakeProxy("socks5")
        self.addCleanup(socks_proxy.close)
        self.assertEqual(dc.probe_local_port(socks_proxy.port), "socks5")
        self.assertIsNone(dc.probe_local_port(closed_port()))

    def test_nothing_listening_means_no_routes_and_no_delay(self):
        started = time.monotonic()
        self.assertEqual(dc.discover_local_proxies((closed_port(), closed_port()), budget=3.0), [])
        self.assertLess(time.monotonic() - started, 1.5)

    def test_discovery_can_be_switched_off(self):
        proxy = FakeProxy("socks5")
        self.addCleanup(proxy.close)
        with mock.patch.object(dc, "AUTO_PROXY", False), mock.patch.object(dc, "LOCAL_PROXY_PORTS", (proxy.port,)):
            self.assertEqual(dc.Network().discovered_proxies(), [])


class RouteOrderTests(unittest.TestCase):
    def names(self, net, service="google", system=None, proxy="", discovered=()):
        with mock.patch("urllib.request.getproxies", return_value=system or {}), \
                mock.patch.object(dc, "PROXY", proxy), \
                mock.patch.object(dc, "discover_local_proxies", return_value=list(discovered)):
            return [route.name for route in net.routes(service)]

    def test_default_order(self):
        self.assertEqual(self.names(dc.Network()), ["direct", "ipv4"])

    def test_order_with_every_kind_of_proxy(self):
        names = self.names(
            dc.Network(), system={"https": "http://127.0.0.1:1"},
            discovered=["socks5://127.0.0.1:2", "http://127.0.0.1:1"],
        )
        self.assertEqual(names, ["direct", "http://127.0.0.1:1", "socks5://127.0.0.1:2", "ipv4"])

    def test_the_configured_proxy_replaces_the_system_proxy(self):
        names = self.names(dc.Network(), system={"https": "http://127.0.0.1:1"}, proxy="socks5://127.0.0.1:9")
        self.assertEqual(names, ["direct", "socks5://127.0.0.1:9", "ipv4"])

    def test_the_remembered_route_comes_first_and_is_not_repeated(self):
        net = dc.Network({"route": {"google": "ipv4"}})
        self.assertEqual(self.names(net), ["ipv4", "direct"])
        net = dc.Network({"route": {"google": "socks5://127.0.0.1:2"}})
        self.assertEqual(
            self.names(net, discovered=["socks5://127.0.0.1:2"]), ["socks5://127.0.0.1:2", "direct", "ipv4"]
        )

    def test_a_remembered_route_belongs_to_one_service(self):
        net = dc.Network({"route": {"dictionaryapi": "ipv4"}})
        self.assertEqual(self.names(net, "google"), ["direct", "ipv4"])

    def test_nonsense_in_the_saved_memory_is_ignored(self):
        for meta in ({"route": "x"}, {"route": {"google": 5}}, {"route": {"google": "nonsense"}}, {}):
            with self.subTest(meta=meta):
                self.assertEqual(self.names(dc.Network(meta)), ["direct", "ipv4"])

    def test_direct_and_ipv4_leave_through_the_same_exit_and_each_proxy_through_its_own(self):
        net = dc.Network()
        self.assertEqual(net._direct().group, net._ipv4().group)
        self.assertNotEqual(net._proxy("http", "127.0.0.1", 1).group, net._proxy("http", "127.0.0.1", 2).group)
        self.assertNotEqual(net._direct().group, net._proxy("http", "127.0.0.1", 1).group)


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_service()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def patch_services(self, **changes):
        urls = {
            "DICTIONARY_URL": self.base + "/entries/{}",
            "TRANSLATE_URL": self.base + "/translate",
            "MYMEMORY_URL": self.base + "/mymemory",
            "DATAMUSE_URL": self.base + "/datamuse",
            "WIKTIONARY_URL": self.base + "/wiktionary/{}",
        }
        urls.update(changes)
        patches = [mock.patch.object(dc, name, value) for name, value in urls.items()]
        patches += [mock.patch.object(dc, "AUTO_PROXY", False), mock.patch("urllib.request.getproxies", return_value={})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_report_covers_every_service_over_every_route(self):
        self.patch_services()
        report = dc.run_checks()
        report.encode("ascii")  # safe to print on any console
        lines = report.splitlines()
        self.assertTrue(lines[0].startswith("python "))
        self.assertTrue(any(line.startswith("local proxy ports found: none") for line in lines))
        rows = [line for line in lines
                if line.startswith(("dictionaryapi.dev", "Google", "MyMemory", "Datamuse", "Wiktionary"))]
        self.assertEqual(len(rows), 5 * 2)  # five services over direct and ipv4
        self.assertEqual(sum(" ok " in line for line in rows), 10)
        self.assertEqual(report.count("reachable via direct, ipv4"), 5)

    def test_a_service_that_cannot_be_reached_is_reported(self):
        self.patch_services(DICTIONARY_URL=f"http://127.0.0.1:{closed_port()}/entries/{{}}")
        report = dc.run_checks()
        self.assertIn("NOT reachable by any route", report)
        self.assertIn("connection failed", report)

    def test_a_service_that_limits_the_requests_is_reported_as_such(self):
        self.patch_services(TRANSLATE_URL=self.base + "/limited")
        report = dc.run_checks()
        self.assertIn("rate limited (HTTP 429)", report)
        self.assertIn("LIMITING the requests (HTTP 429): wait, or try another VPN server", report)

    def test_google_without_definitions_is_reported_as_such(self):
        self.patch_services(TRANSLATE_URL=self.base + "/mymemory")  # a reply that is not a Google reply
        report = dc.run_checks()
        self.assertNotIn("Google             direct                        ok", report)


# ── Entry points ───────────────────────────────────────────────────────────
class MainTests(LookupCase):
    def run_main(self, argv, net=None, translate=True):
        stdout = mock.Mock(buffer=io.BytesIO())
        factory = (lambda meta=None, context=None: net) if net is not None else dc.Network
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(dc, "Network", factory):
            code = dc.main(argv, translate)
        return code, stdout.buffer.getvalue().decode("utf-8")

    def test_a_word_is_printed_as_html(self):
        code, out = self.run_main(["hello"], FakeNet({"google": google_response(), "dictionaryapi": DICTIONARY_OK}))
        self.assertEqual(code, 0)
        self.assertIn("❖ GOOGLE TRANSLATE:", out)

    def test_numbers_print_nothing_and_make_no_request(self):
        net = FakeNet({})
        self.assertEqual(self.run_main(["12345"], net), (0, ""))
        self.assertEqual(net.calls, [])

    def test_an_unreplaced_placeholder_is_logged_not_printed(self):
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.run_main(["%GDWORD%"], FakeNet({})), (0, ""))

    def test_an_unexpected_error_prints_a_note_not_a_traceback(self):
        with mock.patch.object(dc, "build_output", side_effect=RuntimeError("boom")):
            with self.assertLogs(level="ERROR"):
                code, out = self.run_main(["hello"], FakeNet({}))
        self.assertEqual(code, 0)
        self.assertIn("❖ NOTE: Unexpected error", out)
        self.assertNotIn("Traceback", out)

    def test_the_check_option_prints_the_report(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.object(dc, "run_checks", return_value="the report"):
            self.assertEqual(dc.main(["--check"]), 0)
        self.assertEqual(buffer.getvalue(), "the report\n")


class EntryScriptTests(unittest.TestCase):
    """The GoldenDict program scripts must start, and must exit on time."""

    def run_script(self, name, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / name), *args],
            capture_output=True, cwd=str(ROOT), timeout=60,
        )

    def test_scripts_print_nothing_for_numbers(self):
        for name in ("dict_full.py", "dict_eng_only.py", "dict_lookup.py"):
            with self.subTest(script=name):
                result = self.run_script(name, "12345")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"")

    def test_english_only_script_refuses_phrases_without_any_request(self):
        result = self.run_script("dict_eng_only.py", "one", "two", "three")
        out = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("English entries are only looked up for one or two words.", out)
        self.assertNotIn("GOOGLE TRANSLATE", out)

    def test_the_process_ends_on_time_even_when_every_request_hangs(self):
        server = start_service()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        code = textwrap.dedent(f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(ROOT)!r})
            import dict_core as dc
            dc.TOTAL_BUDGET = 2.0
            dc.HEDGE_DELAY = 0.5
            dc.ROUTE_TIMEOUT = 30.0
            dc.ROUTE_STAGGER = 0.5
            dc.AUTO_PROXY = False
            dc.PROXY = ""
            dc.DICTIONARY_URL = {base!r} + "/hang/{{}}"
            dc.WIKTIONARY_URL = {base!r} + "/hang/{{}}"
            dc.TRANSLATE_URL = {base!r} + "/hang"
            dc.MYMEMORY_URL = {base!r} + "/hang"
            dc.DATAMUSE_URL = {base!r} + "/hang"
            dc.cache_path = lambda: Path({tmp.name!r}) / "cache.json"
            dc.run(True)
        """)
        started = time.monotonic()
        result = subprocess.run([sys.executable, "-c", code, "hello"], capture_output=True, timeout=30,
                                env={"PATH": "", "SYSTEMROOT": ""})
        elapsed = time.monotonic() - started
        out = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 8.0)  # the requests would have hung for 30 seconds
        self.assertIn("❖ NOTE: Persian translation unavailable (Google: no answer within 2 s", out)
        self.assertIn("English entry not available", out)
        self.assertTrue(Path(tmp.name, "network.log").exists())


class PreviewTests(unittest.TestCase):
    """tools/make_preview.py builds a page that shows the card at phone widths."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_preview", ROOT / "tools" / "make_preview.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)
        cls.folder = tempfile.TemporaryDirectory()
        cls.path = Path(cls.folder.name) / "preview.html"
        assert cls.tool.main([str(cls.path)]) == 0
        cls.page = cls.path.read_text(encoding="utf-8")

        from html.parser import HTMLParser

        class Frames(HTMLParser):
            def __init__(self):
                super().__init__()
                self.frames = []

            def handle_starttag(self, tag, attrs):
                if tag == "iframe":
                    self.frames.append(dict(attrs))

        parser = Frames()
        parser.feed(cls.page)
        cls.frames = parser.frames

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_every_sample_is_shown_in_four_frames(self):
        self.assertEqual(len(self.frames), 4 * len(self.tool.samples()))
        self.assertEqual({frame["width"] for frame in self.frames}, {"360", "412", "820"})

    def test_the_frames_are_whole_cards_with_the_real_styling(self):
        css = (ROOT / "anki" / "style.css").read_text(encoding="utf-8")
        for frame in self.frames:
            self.assertIn(css, frame["srcdoc"])
            self.assertIn("viewport", frame["srcdoc"])

    def test_nothing_of_the_template_is_left_unfilled(self):
        for frame in self.frames:
            self.assertNotIn("{{", frame["srcdoc"])

    def test_the_fronts_and_backs_are_the_real_templates(self):
        words = [word for _, word, _ in self.tool.samples()]
        fronts = [frame for frame in self.frames if 'id="raw-data"' not in frame["srcdoc"]]
        backs = [frame for frame in self.frames if 'id="raw-data"' in frame["srcdoc"]]
        self.assertEqual((len(fronts), len(backs)), (4, 12))
        for word, frame in zip(words, fronts):
            self.assertIn(f'<div class="my-word">{word}</div>', frame["srcdoc"])
        for frame in backs:
            self.assertIn("<script>", frame["srcdoc"])  # the real back template builds the layout with a script
        self.assertTrue(all('class="replay-button soundLink"' in frame["srcdoc"] for frame in self.frames))

    def test_the_samples_cover_the_awkward_cases(self):
        text = " ".join(meaning for _, _, meaning in self.tool.samples())
        self.assertIn("must-wrap-not-overflow", text)   # a very long unbroken word
        self.assertIn("❖ NOTE:", text)                   # a problem note
        self.assertIn("❖ GOOGLE TRANSLATE:", text)       # the Persian button
        for title, _, _ in self.tool.samples():
            self.assertIn(title.replace("&", "&amp;"), self.page)

    def test_the_preview_is_not_committed(self):
        self.assertIn("preview.html", (ROOT / ".gitignore").read_text(encoding="utf-8").split())


class TwoProgramsTests(unittest.TestCase):
    """What GoldenDict does: both programs for one search, at the same moment, as separate processes."""

    def test_two_real_programs_ask_each_service_once_and_show_the_same_entry(self):
        server = start_service()
        server.delays["entries"] = 0.6  # a dictionaryapi.dev that is slow, as it often is
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        def program(full):
            return textwrap.dedent(f"""
                import sys
                from pathlib import Path
                sys.path.insert(0, {str(ROOT)!r})
                import dict_core as dc
                dc.TOTAL_BUDGET = 8.0
                dc.HEDGE_DELAY = 1.0
                dc.OPTIONAL_GRACE = 2.0
                dc.EXTRA_GRACE = 1.0
                dc.ROUTE_TIMEOUT = 5.0
                dc.AUTO_PROXY = False
                dc.PROXY = ""
                dc.DICTIONARY_URL = {base!r} + "/entries/{{}}"
                dc.WIKTIONARY_URL = {base!r} + "/wiktionary/{{}}"
                dc.TRANSLATE_URL = {base!r} + "/translate"
                dc.MYMEMORY_URL = {base!r} + "/mymemory"
                dc.DATAMUSE_URL = {base!r} + "/datamuse"
                dc.cache_path = lambda: Path({tmp.name!r}) / "cache.json"
                dc.run({full!r})
            """)

        env = {"PATH": "", "SYSTEMROOT": ""}
        started = time.monotonic()
        processes = [subprocess.Popen([sys.executable, "-c", program(full), "hello"], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env) for full in (True, False)]
        outputs = [process.communicate(timeout=30) for process in processes]
        elapsed = time.monotonic() - started
        full, english = (out.decode("utf-8") for out, _ in outputs)
        for process, (_, err) in zip(processes, outputs):
            self.assertEqual(process.returncode, 0, err)
        self.assertIn("❖ GOOGLE TRANSLATE:", full)
        self.assertNotIn("GOOGLE TRANSLATE", english)
        self.assertIn("❖ PHONETIC: /həˈloʊ/", english)
        self.assertTrue(full.endswith(english), "the English part of the two programs differs")
        self.assertEqual(server.hits.get("entries"), 1)    # asked once, not once per program
        self.assertEqual(server.hits.get("translate"), 1)
        for backup in ("mymemory", "wiktionary", "datamuse"):
            self.assertNotIn(backup, server.hits)          # nothing was missing, so no backup was asked
        self.assertLess(elapsed, 6.0)


@unittest.skipUnless(shutil.which("node"), "node is needed to run the template's script")
class BackTemplateTests(unittest.TestCase):
    """The back template of the Anki card, run for real with tests/run_back_template.js."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_preview", ROOT / "tools" / "make_preview.py")
        cls.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.tool)

    # -- helpers
    def run_back(self, meaning, mode=None):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "meaning.html")
            path.write_text(meaning, encoding="utf-8")
            command = ["node", str(ROOT / "tests" / "run_back_template.js"), str(ROOT / "anki" / "back.html"), str(path)]
            result = subprocess.run(command + ([mode] if mode else []), capture_output=True, text=True,
                                    encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def card(self, meaning):
        """The page the script builds, as a tree."""
        return self.tree(self.run_back(meaning)["ui"])

    @staticmethod
    def tree(markup):
        from html.parser import HTMLParser

        class Node:
            def __init__(self, tag, attrs):
                self.tag, self.attrs, self.children = tag, dict(attrs), []
                self.classes = set((self.attrs.get("class") or "").split())

            def text(self):
                return "".join(c if isinstance(c, str) else c.text() for c in self.children)

            def find(self, name):
                found = [self] if name in self.classes else []
                for child in self.children:
                    if not isinstance(child, str):
                        found += child.find(name)
                return found

        class Builder(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.root = Node("root", [])
                self.stack = [self.root]
                self.tags = []

            def handle_starttag(self, tag, attrs):
                node = Node(tag, attrs)
                self.tags.append((tag, dict(attrs)))
                self.stack[-1].children.append(node)
                self.stack.append(node)

            def handle_endtag(self, tag):
                for index in range(len(self.stack) - 1, 0, -1):
                    if self.stack[index].tag == tag:
                        del self.stack[index:]
                        return

            def handle_data(self, data):
                self.stack[-1].children.append(data)

        builder = Builder()
        builder.feed(markup)
        builder.root.tags = builder.tags
        return builder.root

    def entry(self, **changes):
        values = dict(phonetic="/ˈhɛləʊ/", groups=[("noun", ["A greeting."])], synonyms=[], examples=[])
        values.update(changes)
        return self.tool.entry(values["phonetic"], values["groups"], values["synonyms"], values["examples"])

    @staticmethod
    def render(translation="سلام", entry=None, notes=()):
        return dc.render(translation, entry, [("Google voice", dc.tts_url("hello"))] if entry else [], list(notes))

    # -- a normal card
    def test_a_full_entry_is_laid_out(self):
        entry = self.entry(
            groups=[("noun", ["First.", "Second.", "Third."]), ("verb", ["Fourth.", "Fifth."])],
            synonyms=["hi", "hey", "hello"], examples=["Hello there!", "Say hello."])
        result = self.run_back(self.render("سلام", entry))
        page = self.tree(result["ui"])
        self.assertEqual([n.text() for n in page.find("my-pos-pill")], ["noun", "verb"])
        rows = page.find("my-pos-row")
        self.assertEqual(len(rows), 2)
        self.assertEqual([n.text() for n in rows[0].find("my-phonetic")], ["/ˈhɛləʊ/"])  # on the first row only
        self.assertEqual(rows[1].find("my-phonetic"), [])
        self.assertEqual([n.text() for n in page.find("my-def-num")], ["1", "2", "3", "1", "2"])
        self.assertEqual(len(page.find("my-def-item")), 5)
        self.assertEqual(len(page.find("my-fa-btn")), 1)
        self.assertIn("سلام", page.find("my-fa-box")[0].text())
        self.assertIn("hi · hey · hello", page.find("my-sec-syn")[0].text())
        self.assertEqual(len(page.find("my-sec-ex")[0].find("my-ex-text")[0].children), 2)
        self.assertEqual((result["raw"], result["rawDisplay"], result["hasToggle"]), ("", "none", True))

    def test_the_phonetic_spelling_is_on_the_right_of_the_first_part_of_speech(self):
        page = self.card(self.render("", self.entry()))
        row = page.find("my-pos-row")[0]
        self.assertEqual([c.attrs["class"] for c in row.children], ["my-pos-pill", "my-phonetic"])

    # -- the bug in the screenshot
    def test_text_left_behind_by_the_play_buttons_is_not_shown(self):
        meaning = self.render("حاشیه", self.entry()) + "<div>▶ Google voice</div><div>▶ Recording</div><div>▶ Play</div>"
        text = self.card(meaning).text()
        for leftover in ("Google voice", "Recording", "Play", "▶"):
            self.assertNotIn(leftover, text)
        self.assertIn("A greeting.", text)

    def test_play_button_text_is_not_taken_for_a_synonym_or_a_translation(self):
        # An empty section followed by the text a play button left behind. The labels the template
        # knows (Google voice, Recording, Play) are removed early; any other label is caught by the
        # general rule that a line starting with the play sign is never content.
        page = self.card("<div><b>❖ GOOGLE TRANSLATE:</b><br>▶ Listen</div>"
                         "<div><b>❖ POS: noun</b></div><div><b>❖ MEANINGS:</b><br>• a greeting</div>"
                         "<div><b>❖ SYNONYMS:</b><br>▶ Pronounce</div>"
                         "<div><b>❖ EXAMPLES:</b><br>▶ Hear it</div>")
        self.assertEqual(page.find("my-fa-btn"), [])
        self.assertEqual(page.find("my-sec-syn"), [])
        self.assertEqual(page.find("my-sec-ex"), [])
        self.assertNotIn("▶", page.text())
        self.assertEqual([n.text()[1:] for n in page.find("my-def-item")], ["a greeting"])

    def test_the_play_buttons_themselves_are_not_shown_either(self):
        text = self.card(self.render("سلام", self.entry())).text()
        self.assertNotIn("Google voice", text)

    # -- notes and missing parts
    def test_notes_are_shown_in_boxes(self):
        notes = ["Persian translation unavailable (Google: rate limited (HTTP 429)).", "Network details: C:\\x\\network.log"]
        page = self.card(self.render("", None, notes))
        self.assertEqual([n.text() for n in page.find("my-note")], notes)
        self.assertEqual(page.find("my-pos-pill"), [])
        self.assertEqual(page.find("my-fa-btn"), [])

    def test_a_word_with_no_entry_shows_only_the_translation(self):
        page = self.card(self.render("کلاه پشمی", None))
        self.assertEqual(len(page.find("my-fa-btn")), 1)
        self.assertIn("کلاه پشمی", page.find("my-fa-box")[0].text())
        self.assertEqual(page.find("my-def-item"), [])
        self.assertEqual(page.find("my-note"), [])

    # -- entries as the backup services give them
    def test_meanings_without_a_part_of_speech_are_kept(self):
        page = self.card("<div><b>❖ MEANINGS:</b><br>• a Swedish band<br>• another meaning</div>")
        self.assertEqual(page.find("my-pos-pill"), [])
        self.assertEqual([n.text() for n in page.find("my-def-item")], ["1a Swedish band", "2another meaning"])

    def test_a_second_block_of_meanings_gets_its_own_group(self):
        page = self.card("<div><b>❖ POS: noun</b></div><div><b>❖ MEANINGS:</b><br>• first</div>"
                         "<div><b>❖ MEANINGS:</b><br>• second</div>")
        self.assertEqual([n.text() for n in page.find("my-pos-pill")], ["noun"])
        self.assertEqual(len(page.find("my-defs")), 2)
        self.assertEqual(len(page.find("my-def-item")), 2)

    def test_a_repeated_part_of_speech_is_merged_and_nothing_is_shown_twice(self):
        page = self.card("<div><b>❖ POS: noun</b></div><div><b>❖ MEANINGS:</b><br>• a<br>• b</div>"
                         "<div><b>❖ POS: noun</b></div><div><b>❖ MEANINGS:</b><br>• b<br>• c</div>"
                         "<div><b>❖ SYNONYMS:</b><br>x · y</div><div><b>❖ SYNONYMS:</b><br>y · z</div>"
                         "<div><b>❖ EXAMPLES:</b><br>• one<br>• one<br>• two</div>")
        self.assertEqual([n.text() for n in page.find("my-pos-pill")], ["noun"])
        self.assertEqual([n.text()[1:] for n in page.find("my-def-item")], ["a", "b", "c"])
        self.assertIn("x · y · z", page.find("my-sec-syn")[0].text())
        self.assertEqual(len(page.find("my-ex-text")[0].children), 2)

    def test_the_phonetic_spelling_has_a_row_even_without_a_part_of_speech(self):
        with_meanings = self.card("<div><b>❖ PHONETIC: /x/</b></div><div><b>❖ MEANINGS:</b><br>• a</div>")
        self.assertEqual([n.text() for n in with_meanings.find("my-phonetic")], ["/x/"])
        self.assertEqual(with_meanings.find("my-pos-pill"), [])
        alone = self.card("<div><b>❖ PHONETIC: /x/</b></div>")
        self.assertEqual([n.text() for n in alone.find("my-phonetic")], ["/x/"])

    # -- notes made by the older scripts
    OLD_NOTE = (
        "<div style='direction:rtl; text-align:right;'><b>❖ GOOGLE TRANSLATE:</b><br>سلام</div>"
        "<div style='font-family:Arial,sans-serif; font-size:15px;'>"
        "<div style='text-align:center; color:#8b949e;'>/həˈloʊ/</div>"
        "<div style='margin-bottom:12px;'><b style='color:#d2a8ff;'>❖ POS: exclamation</b>"
        "<audio id='gd_audio' src='https://x.test/a'></audio>"
        "<a href='javascript:void(0);' onclick=\"document.getElementById('gd_audio').play();\">▶ Play</a></div>"
        "<div style='margin-top:10px;'><b>❖ SYNONYMS:</b><br>hi · hey</div>"
        "<div style='margin-top:10px;'><b>❖ MEANINGS:</b><br><div>• Used as a greeting.</div></div>"
        "<div style='margin-top:12px;'><b>❖ EXAMPLES:</b><br>• Hello there!</div></div>"
    )

    def test_notes_made_by_the_older_scripts_still_work(self):
        page = self.card(self.OLD_NOTE)
        self.assertEqual([n.text() for n in page.find("my-pos-pill")], ["exclamation"])
        self.assertEqual([n.text() for n in page.find("my-pos-row")[0].find("my-phonetic")], ["/həˈloʊ/"])
        self.assertIn("سلام", page.find("my-fa-box")[0].text())
        self.assertIn("hi · hey", page.find("my-sec-syn")[0].text())
        self.assertEqual([n.text() for n in page.find("my-def-item")], ["1Used as a greeting."])
        self.assertEqual(len(page.find("my-ex-text")[0].children), 1)
        self.assertNotIn("Play", page.text())

    def test_a_dictionary_name_in_the_field_is_not_taken_for_the_phonetic_spelling(self):
        old_without_phonetic = self.OLD_NOTE.replace("<div style='text-align:center; color:#8b949e;'>/həˈloʊ/</div>", "")
        page = self.card("dict english only<br>" + old_without_phonetic)
        self.assertEqual(page.find("my-phonetic"), [])
        self.assertNotIn("dict english only", page.text())
        page = self.card("<div>dict english only</div>" + self.render("", self.entry()))
        self.assertEqual([n.text() for n in page.find("my-phonetic")], ["/ˈhɛləʊ/"])

    # -- safety
    def test_text_from_a_dictionary_cannot_become_markup(self):
        hostile = "<img src=x onerror=alert(1)> & <script>alert(2)</script>"
        entry = self.entry(phonetic="/" + hostile + "/", groups=[("noun" + hostile, [hostile])],
                           synonyms=[hostile], examples=[hostile])
        markup = self.run_back(self.render(hostile, entry, [hostile]))["ui"]
        page = self.tree(markup)
        allowed = {"div", "span", "button", "p"}
        self.assertEqual({tag for tag, _ in page.tags} - allowed, set())
        for _, attrs in page.tags:
            self.assertEqual(set(attrs) - {"class", "id", "style", "onclick"}, set())
        self.assertEqual([attrs["onclick"] for tag, attrs in page.tags if "onclick" in attrs], ["myToggleFa()"])
        text = page.text()
        self.assertEqual(text.count(hostile), 7)  # the pos, phonetic, meaning, synonyms, example, translation and note

    def test_broken_icon_characters_are_removed(self):
        text = self.card(self.render("", self.entry(groups=[("noun", ["A\ufffc greeting\u2318."])]))).text()
        self.assertIn("A greeting.", text)
        self.assertNotIn("\ufffc", text)
        self.assertNotIn("\u2318", text)

    def test_a_note_that_is_not_from_the_dictionary_scripts_is_shown_as_it_is(self):
        result = self.run_back("<div>just my own text</div>")
        self.assertIn("just my own text", result["ui"])
        self.assertEqual(result["raw"], "")

    def test_an_empty_field_shows_nothing(self):
        for empty in ("", "   ", "<div><br></div>"):
            with self.subTest(field=empty):
                self.assertEqual(self.run_back(empty)["ui"], "")

    def test_an_error_never_leaves_the_card_empty(self):
        result = self.run_back(self.render("سلام", self.entry()), mode="break")
        self.assertIn("A greeting.", result["uiText"])
        self.assertEqual(result["rawDisplay"], "none")

    def test_the_translation_button_opens_and_closes(self):
        result = self.run_back(self.render("سلام", self.entry()), mode="toggle")
        opened, closed = result["toggle"]
        self.assertEqual((opened["boxDisplay"], opened["open"], opened["button"]), ("block", True, "🙈 پنهان کردن ترجمه"))
        self.assertEqual((closed["boxDisplay"], closed["open"], closed["button"]), ("none", False, "👁 نمایش ترجمه فارسی"))

    # -- the preview's samples
    def test_every_sample_of_the_preview_is_laid_out(self):
        for title, word, meaning in self.tool.samples():
            with self.subTest(sample=title):
                result = self.run_back(meaning)
                self.assertTrue(result["ui"].strip())
                page = self.tree(result["ui"])
                self.assertEqual(len(page.find("my-note")), meaning.count("❖ NOTE:"))
                self.assertNotIn("Google voice", page.text())
        long_word = self.card([m for t, w, m in self.tool.samples() if w == "responsibilities"][0])
        self.assertIn("must-wrap-not-overflow", long_word.text())


class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.front = (ROOT / "anki" / "front.html").read_text(encoding="utf-8")
        cls.back = (ROOT / "anki" / "back.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "anki" / "style.css").read_text(encoding="utf-8")
        cls.script_source = (ROOT / "dict_core.py").read_text(encoding="utf-8")

    def test_fields_are_used(self):
        self.assertIn("{{Word}}", self.front)
        self.assertIn("{{Word}}", self.back)
        self.assertIn("{{Meaning}}", self.back)
        self.assertIn("AwesomeTTS:Word", self.back)

    def test_back_template_knows_every_label_the_script_prints(self):
        labels = set(re.findall(r"❖ ([A-Z][A-Z ]*[A-Z]):", self.script_source))
        self.assertIn("NOTE", labels)
        self.assertIn("GOOGLE TRANSLATE", labels)
        for label in labels:
            self.assertIn(label, self.back)

    def test_back_template_never_uses_the_risky_calls(self):
        for banned in ("outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
            self.assertNotIn(banned, self.back)

    def test_back_template_escapes_the_text_it_puts_into_markup(self):
        self.assertIn("function esc(", self.back)
        self.assertGreaterEqual(self.back.count("esc("), 8)

    def test_every_class_used_by_the_templates_is_styled(self):
        text = self.front + self.back
        used = set()
        for value in re.findall(r'class="([^"]+)"', text) + re.findall(r"classList\.add\('([^']+)'\)", text):
            used.update(name for name in value.split() if name.startswith("my-"))
        self.assertIn("my-note", used)
        self.assertIn("my-open", used)
        for name in sorted(used):
            self.assertRegex(self.css, r"\." + re.escape(name) + r"\b", msg=name)

    def test_the_preview_uses_only_classes_that_are_styled(self):
        # (the preview tool puts the same templates on a page)
        for name in ("my-pos-row", "my-phonetic", "my-sec-syn", "my-sec-ex"):
            self.assertIn(name, self.back)
            self.assertRegex(self.css, r"\." + name + r"\b")

    def test_comments_are_in_english(self):
        persian = re.compile(r"[\u0600-\u06FF]")
        for name, text in (("front.html", self.front), ("back.html", self.back), ("style.css", self.css)):
            comments = re.findall(r"/\*.*?\*/|<!--.*?-->", text, re.S)
            comments += re.findall(r"(?m)(?<![:'\"])//\s.*$", text)
            for comment in comments:
                self.assertIsNone(persian.search(comment), f"{name}: {comment[:60]}")

    # -- the layout on a phone
    def block(self, header, text=None):
        """The text of the rule that starts with header, braces included."""
        text = self.css if text is None else text
        start = text.index(header)
        depth, end = 0, start
        for position in range(text.index("{", start), len(text)):
            depth += {"{": 1, "}": -1}.get(text[position], 0)
            if depth == 0:
                end = position
                break
        return text[start:end + 1]

    @staticmethod
    def pixels(text, name):
        match = re.search(re.escape(name) + r":\s*(\d+(?:\.\d+)?)px", text)
        return float(match.group(1)) if match else None

    def test_the_styling_is_well_formed_and_has_no_template_text(self):
        self.assertEqual(self.css.count("{"), self.css.count("}"))
        self.assertNotIn("{{", self.css)

    def test_the_card_adapts_to_the_screen(self):
        for header in ("@media (max-width: 600px)", "@media (max-width: 360px)", "@media (pointer: coarse)",
                       "@supports (font-size: clamp("):
            self.assertIn(header, self.css)

    def test_text_is_big_enough_on_a_phone(self):
        phone = self.block("@media (max-width: 600px)")
        minimum = {"--fs-body": 16, "--fs-small": 14, "--fs-label": 11, "--fs-phonetic": 16, "--fs-fa": 18}
        for name, smallest in minimum.items():
            self.assertGreaterEqual(self.pixels(phone, name), smallest, name)

    def test_the_phone_text_is_not_smaller_than_the_computer_text(self):
        base = self.block(".my-wrap {")
        phone = self.block("@media (max-width: 600px)")
        for name in ("--fs-body", "--fs-small", "--fs-label", "--fs-phonetic", "--fs-fa"):
            self.assertGreaterEqual(self.pixels(phone, name), self.pixels(base, name), name)

    def test_things_to_press_are_big_enough(self):
        self.assertGreaterEqual(self.pixels(self.block(".my-fa-btn {"), "min-height"), 44)
        coarse = self.block("@media (pointer: coarse)")
        self.assertGreaterEqual(self.pixels(self.block(".my-fa-btn {", coarse), "min-height"), 44)
        self.assertGreaterEqual(self.pixels(self.block(".replay-button {", coarse), "min-height"), 44)

    def test_long_words_wrap_instead_of_being_cut_off(self):
        for selector in (".my-word {", ".my-def-item > span:last-child {", ".my-syn-text {", ".my-ex-text {", ".my-fa-box {"):
            self.assertIn("overflow-wrap: anywhere", self.block(selector), selector)

    def test_every_size_has_a_fallback_for_browsers_without_variables(self):
        lines = [line.strip() for line in self.css.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line.startswith("font-size: var("):
                self.assertRegex(lines[index - 1], r"^font-size: \d+px;$", line)

    def test_the_card_fits_the_screen(self):
        wrap = self.block(".my-wrap {")
        for rule in ("width: 100%", "max-width:", "box-sizing: border-box"):
            self.assertIn(rule, wrap)


if __name__ == "__main__":
    unittest.main()
