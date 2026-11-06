#!/usr/bin/env python3
"""Build preview.html: the Anki card as it looks at the width of a small phone, a large
phone and a tablet or computer, so the layout can be checked in any browser without
syncing a phone.

    python tools/make_preview.py            writes preview.html in the project folder
    python tools/make_preview.py out.html   writes the file you name

Open the file in a browser. Each sample is shown as the front and the back of the card, in
frames of 360, 412 and 820 pixels. A frame has the width of a screen of that size, so the
@media rules of anki/style.css apply as they do on a device.

This is an approximation made with the browser's own engine. It is not AnkiDroid or
AnkiMobile, which can differ a little (their own margins and text-size settings).

Standard library only.
"""

import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dict_core as dc  # noqa: E402

# What Anki writes for the text-to-speech tag: a play button that the card styling then restyles.
TTS_TAG = "{{tts en_US voices=AwesomeTTS:Word}}"
REPLAY_BUTTON = (
    '<a class="replay-button soundLink" href="#">'
    '<svg viewBox="0 0 64 64" width="16" height="16"><path d="M8 20v24h12l16 14V6L20 20z"/></svg></a>'
)

# (what the frame stands for, width in pixels, height in pixels)
SCREENS = (("small phone", 360, 780), ("large phone", 412, 820), ("tablet or computer", 820, 720))


def entry(phonetic, groups, synonyms=(), examples=()):
    return {
        "phonetic": phonetic,
        "audio": "",
        "groups": [{"pos": pos, "defs": list(defs)} for pos, defs in groups],
        "synonyms": list(synonyms),
        "examples": list(examples),
    }


def samples():
    """(title, word, the HTML for the Meaning field) for each sample card."""

    def voice(word):
        return [("Google voice", dc.tts_url(word))]

    network = entry(
        "/nɛtwɜːk/",
        [("noun", ["A fabric or structure of fibrous elements attached to each other at regular intervals.",
                   "Any interconnected group or system.",
                   "A directory of people maintained for their advancement."]),
         ("verb", ["To interact socially for the purpose of getting connections or personal advancement.",
                   "To connect two or more computers or other computerized devices."])],
        ["cardboard", "pasteboard", "board", "stiff paper", "greetings card", "postcard", "Christmas card", "birthday card"],
        ["A network of roads crisscrossed the country.",
         "To get a job in today's economy, it is important to have a strong network.",
         "The copy machine is connected to the network so it can now serve as a printer."],
    )
    long_word = entry(
        "/rɪˌspɒnsɪˈbɪlɪtiz/ /rɪˌspɑnsəˈbɪlətiz/",
        [("noun", ["The state or fact of having a duty to deal with something or of having control over someone.",
                   "Supercalifragilisticexpialidocious-and-other-extremely-long-unbroken-strings-must-wrap-not-overflow."])],
        ["duties", "obligations", "burdens", "commitments"],
        ["Parents have a responsibility for the safety of their children."],
    )
    return [
        ("A normal entry", "network", dc.render("شبکه", network, voice("network"), [])),
        ("A long word and long text", "responsibilities",
         dc.render("مسئولیت‌ها و وظایف", long_word, voice("responsibilities"), [])),
        ("Only a translation (the word has no entry)", "woolly hat", dc.render("کلاه پشمی", None, [], [])),
        ("A problem note", "rarity", dc.render("", None, [], [
            "Persian translation unavailable (Google: rate limited (HTTP 429); MyMemory: timeout).",
            "English entry not available (dictionaryapi.dev: timeout; Google: connection failed).",
            "Network details: C:\\Users\\you\\AppData\\Local\\goldendict-anki\\network.log",
        ])),
    ]


def fill(template, word, meaning):
    """The template as Anki would fill it in."""
    return (
        template.replace("{{Word}}", html.escape(word))
        .replace("{{Meaning}}", meaning)
        .replace(TTS_TAG, REPLAY_BUTTON)
    )


def document(css, body):
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{css}</style></head><body class=\"card mobile\">{body}</body></html>"
    )


PAGE_HEAD = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>goldendict-anki card preview</title>
<style>
  body { background: #1b1f27; color: #c9d1d9; font-family: Segoe UI, Roboto, Arial, sans-serif; padding: 20px; }
  h1 { font-size: 22px; } h2 { font-size: 18px; margin: 34px 0 12px; } h2 small { color: #8b949e; font-weight: normal; }
  p { color: #8b949e; max-width: 760px; line-height: 1.5; }
  .row { display: flex; flex-wrap: wrap; gap: 22px; align-items: flex-start; }
  figure { margin: 0; } figcaption { font-size: 13px; color: #8b949e; margin-bottom: 6px; }
  iframe { border: 1px solid #3a4150; background: #090d14; display: block; }
</style></head><body>
<h1>goldendict-anki: card preview</h1>
<p>Each frame has the width of a screen, so the phone rules of <code>anki/style.css</code> apply as they do on a device.
This is the browser's engine, not AnkiDroid or AnkiMobile, which can differ a little.</p>
"""


def build(front, back, css):
    parts = [PAGE_HEAD]
    for title, word, meaning in samples():
        parts.append(f"<h2>{html.escape(title)} <small>({html.escape(word)})</small></h2>\n<div class=\"row\">\n")
        frames = [("front", "small phone", 360, 340)]
        frames += [("back", name, width, height) for name, width, height in SCREENS]
        for side, name, width, height in frames:
            template = front if side == "front" else back
            doc = document(css, fill(template, word, meaning))
            parts.append(
                f'<figure><figcaption>{side}, {name} ({width} px)</figcaption>'
                f'<iframe width="{width}" height="{height}" srcdoc="{html.escape(doc, quote=True)}"></iframe></figure>\n'
            )
        parts.append("</div>\n")
    parts.append("</body></html>\n")
    return "".join(parts)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    out = Path(args[0]) if args else ROOT / "preview.html"
    anki = ROOT / "anki"
    page = build(
        (anki / "front.html").read_text(encoding="utf-8"),
        (anki / "back.html").read_text(encoding="utf-8"),
        (anki / "style.css").read_text(encoding="utf-8"),
    )
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
