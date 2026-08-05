"""Ruger in languages that are not English (§16).

    .venv/bin/python scratch/test_languages.py

Around a hundred cases across Hindi, Hinglish, Marathi, Bengali, Tamil, Arabic,
Chinese, Japanese, Korean, Thai, Russian, Spanish, German, French and Turkish,
plus the mixed-script and mixed-direction text that real meetings actually
contain.

Every one of these was written after three bugs turned up in an hour of looking:

  1. `verify_quote` counted space-separated words, so a Chinese or Thai sentence
     counted as ONE word and every commitment from such a meeting was dropped as
     `too_short`. Silently.
  2. `normalise_text` treated Devanagari vowel signs as punctuation, reducing
     "मैं डेक भेजूंगा" to its consonants and scoring two unrelated Hindi tasks at
     0.86 — over the merge threshold.
  3. Slugs were ASCII-only, so every non-Latin title collapsed to the same
     fallback and two meetings on one day wrote to ONE filename. The second
     overwrote the first, and a meeting was gone.

No model call and no network: this is about the code around the model.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unicodedata
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
os.environ.update(PKM_DB=str(TMP / "t.db"), PKM_INBOX=str(TMP / "inbox"),
                  PKM_TRASH=str(TMP / "trash"), PKM_ENV_FILE=str(TMP / ".env"),
                  PKM_ME="Alex, अलेक्स")
for _k in ("PKM_PROVIDER", "PKM_MODEL", "PKM_API_KEY", "PKM_BASE_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(ROOT))

from pkm import capture, config, db, dedup, episodes, extract, notes  # noqa: E402
from pkm.connectors import inbox as inbox_mod, notion, wispr  # noqa: E402

config.ENV_FILE = TMP / ".env"
config.reload()

PASSES = {"n": 0}
FAILED: list[str] = []


def check(label, actual, expected):
    ok = actual == expected
    PASSES["n"] += 1
    if not ok:
        FAILED.append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}"
          + ("" if ok else f" != {expected!r}"))


# --- the corpus ---------------------------------------------------------------
# (language, transcript, a quote that really is in it, a task, an owner)

CORPUS = [
    ("hindi", "राहुल: मैं कल तक डेक भेज दूंगा। प्रिया: मैं इनवॉइस देख लूंगी।",
     "मैं कल तक डेक भेज दूंगा।", "डेक भेजना", "राहुल"),
    ("hindi-long", "अलेक्स: मैं शुक्रवार तक सभी प्रोफाइल ऑडिट कर दूंगा।",
     "मैं शुक्रवार तक सभी प्रोफाइल ऑडिट कर दूंगा।", "प्रोफाइल ऑडिट करना", "अलेक्स"),
    ("hinglish", "Rahul: main kal tak deck bhej dunga, invoice bhi dekh lunga.",
     "main kal tak deck bhej dunga", "Send the deck", "Rahul"),
    ("marathi", "सचिन: मी उद्या अहवाल पाठवीन.", "मी उद्या अहवाल पाठवीन.",
     "अहवाल पाठवणे", "सचिन"),
    ("bengali", "রাহুল: আমি কাল রিপোর্ট পাঠাবো।", "আমি কাল রিপোর্ট পাঠাবো।",
     "রিপোর্ট পাঠানো", "রাহুল"),
    ("tamil", "ராஜா: நான் நாளை அனுப்புகிறேன்.", "நான் நாளை அனுப்புகிறேன்.",
     "அனுப்ப வேண்டும்", "ராஜா"),
    ("telugu", "రవి: నేను రేపు పంపుతాను.", "నేను రేపు పంపుతాను.", "పంపాలి", "రవి"),
    ("gujarati", "અમિત: હું કાલે મોકલીશ.", "હું કાલે મોકલીશ.", "મોકલવું", "અમિત"),
    ("arabic", "علي: سأرسل العرض غدا.", "سأرسل العرض غدا.", "إرسال العرض", "علي"),
    ("hebrew", "דני: אשלח את המצגת מחר.", "אשלח את המצגת מחר.", "לשלוח מצגת", "דני"),
    ("chinese", "李雷：我明天会把资料发给你。", "我明天会把资料发给你", "发送资料", "李雷"),
    ("japanese", "田中：明日資料を送ります。", "明日資料を送ります", "資料を送る", "田中"),
    ("korean", "지민: 내일 자료를 보내겠습니다.", "내일 자료를 보내겠습니다",
     "자료 보내기", "지민"),
    ("thai", "สมชาย: ฉันจะส่งเอกสารพรุ่งนี้", "ฉันจะส่งเอกสารพรุ่งนี้",
     "ส่งเอกสาร", "สมชาย"),
    ("russian", "Иван: я отправлю презентацию завтра.", "я отправлю презентацию завтра",
     "Отправить презентацию", "Иван"),
    ("spanish", "Ana: enviaré la presentación mañana.", "enviaré la presentación mañana",
     "Enviar la presentación", "Ana"),
    ("german", "Jonas: Ich schicke die Unterlagen morgen.",
     "Ich schicke die Unterlagen morgen", "Unterlagen schicken", "Jonas"),
    ("french", "Camille : j'enverrai le dossier demain.", "j'enverrai le dossier demain",
     "Envoyer le dossier", "Camille"),
    ("turkish", "Ayşe: yarın sunumu göndereceğim.", "yarın sunumu göndereceğim",
     "Sunumu göndermek", "Ayşe"),
    ("vietnamese", "Minh: tôi sẽ gửi tài liệu vào ngày mai.",
     "tôi sẽ gửi tài liệu vào ngày mai", "Gửi tài liệu", "Minh"),
]


def episode_for(transcript, kind="meeting"):
    return {"transcript": transcript, "started_at": "2026-08-06", "kind": kind}


def main() -> None:
    print("1. the quote check accepts a real sentence in every script")
    for lang, transcript, quote, _task, _owner in CORPUS:
        ok, how = extract.verify_quote(quote, transcript)
        check(f"{lang}: {len(quote.split())}w/{len(quote)}c", (ok, how), (True, "exact"))

    print("\n2. and still refuses a quote that is not in the note")
    for lang, transcript, quote, _t, _o in CORPUS[:8]:
        check(f"{lang}: invented quote",
              extract.verify_quote(quote + "XYZ不存在", transcript)[0], False)

    print("\n3. a fragment too small to corroborate anything is still refused")
    tiny = [("english", "ok sure", "ok sure thing, fine"),
            ("chinese", "好的", "李雷：好的，我明天发。"),
            ("japanese", "はい", "田中：はい、明日送ります。"),
            ("hindi", "हाँ", "राहुल: हाँ, मैं कल भेजूंगा।"),
            ("arabic", "نعم", "علي: نعم، سأرسل غدا.")]
    for lang, quote, transcript in tiny:
        check(f"{lang}: {quote!r} too short",
              extract.verify_quote(quote, transcript), (False, "too_short"))

    print("\n4. dense scripts are measured in characters, not in spaces")
    check("a dense string is recognised", extract._dense("我明天会把资料发给你"), True)
    check("a spaced one is not", extract._dense("main kal bhej dunga"), False)
    # The trap: "no spaces" is not "dense script". Calling every single English
    # word dense let "audit" and "YouTube" clear the floor as if they were
    # sentences, which two older tests caught the moment it shipped.
    check("a lone latin word is NOT dense", extract._dense("Sendline"), False)
    check("and is therefore still too short",
          extract.verify_quote("audit", "Alex: I will audit the profiles.")[1],
          "too_short")
    check("korean is spaced, so it is not dense",
          extract._dense("내일 자료를 보내겠습니다"), False)
    check("japanese kana and kanji are dense", extract._dense("明日資料を送ります"), True)
    check("thai is dense", extract._dense("ฉันจะส่งเอกสาร"), True)
    check("a number is not a script", extract._dense("12345"), False)
    check("dense floor is lower for a capture",
          extract.verify_quote("发资料", "李雷：发资料。", min_words=2, min_chars=6,
                               min_dense_chars=3)[0], True)
    check("a capture episode asks for all three floors",
          extract.quote_floor({"kind": "capture"}), (2, 6, 3))
    check("a meeting asks for its own", extract.quote_floor({"kind": "meeting"}),
          (3, 12, 5))

    print("\n5. normalisation keeps the vowels that carry the meaning")
    pairs = [
        ("hindi", "मैं डेक भेजूंगा", "मैं इनवॉइस भेजूंगा", False),
        ("hindi same", "मैं डेक भेजूंगा", "मैं डेक भेजूंगा", True),
        ("tamil", "நான் அனுப்புகிறேன்", "நான் வாங்குகிறேன்", False),
        ("arabic", "إرسال العرض", "إرسال الفاتورة", False),
        ("thai", "ส่งเอกสาร", "ซื้อของ", False),
        ("korean", "자료 보내기", "송장 보내기", False),
    ]
    for lang, a, b, should_merge in pairs:
        na, nb = dedup.normalise_text(a), dedup.normalise_text(b)
        merged = dedup.jaccard(na, nb) >= config.DEDUP_THRESHOLD
        check(f"{lang}: merge={merged}", merged, should_merge)

    check("devanagari survives normalisation",
          "ै" in unicodedata.normalize("NFC", dedup.normalise_text("मैं")), True)
    check("marks are not treated as punctuation",
          len(dedup.normalise_text("मैं").split()), 1)
    check("accents no longer fold, which is the accepted cost",
          dedup.normalise_text("café") == dedup.normalise_text("cafe"), False)
    check("latin still lowercases", dedup.normalise_text("SEND The Deck"), "send deck")

    print("\n6. owners in other scripts")
    check("a devanagari alias folds to me", dedup.normalise_owner("अलेक्स"), "me")
    check("a different devanagari name does not",
          dedup.normalise_owner("राहुल"), "राहुल")
    check("mine always wins regardless of script",
          dedup.normalise_owner("李雷", "mine"), "me")
    check("an arabic name is preserved", dedup.normalise_owner("علي"), "علي")
    check("case folds for cyrillic", dedup.normalise_owner("ИВАН"), "иван")
    check("an owner of only punctuation is empty", dedup.normalise_owner("!!!"), "")

    print("\n7. filenames keep their own script, and stay distinct")
    titles = ["राहुल के साथ बैठक", "प्रिया के साथ बैठक", "李雷会议", "王芳会议",
              "اجتماع الفريق", "اجتماع المبيعات", "Weekly with Maya"]
    slugs = [notes.slugify(t) for t in titles]
    check("no two titles collide", len(set(slugs)), len(titles))
    check("none fell back to the default", "note" in slugs, False)
    wslugs = [wispr.slug(t) for t in titles]
    check("the wispr slug agrees", len(set(wslugs)), len(titles))
    check("a title of only punctuation still falls back", notes.slugify("!!! ???"), "note")
    check("separators never survive", "/" in notes.slugify("a/b/c"), False)
    check("nor do dots that could climb", ".." in notes.slugify("../../etc"), False)
    check("length is capped", len(notes.slugify("क" * 200)) <= 60, True)

    print("\n8. a note in another language survives the whole ingest path")
    inbox_dir = TMP / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    for lang, transcript, quote, _t, _o in CORPUS:
        (inbox_dir / f"{lang}.md").write_text(
            f"---\ntitle: {lang} बैठक\ndate: 2026-08-06\nid: {lang}\n---\n\n{transcript}\n",
            encoding="utf-8")

    with closing(db.connect()) as conn:
        stats = episodes.ingest_inbox(conn, inbox_dir)
        check("every note ingested", stats["events_new"], len(CORPUS))
        check("no complaints", stats["problems"], [])

        rows = {r["external_id"]: r for r in conn.execute("SELECT * FROM episodes")}
        for lang, transcript, quote, _t, _o in CORPUS:
            stored = rows[lang]["transcript"]
            check(f"{lang}: stored byte for byte", quote in stored, True)

        print("\n  and the quote check still matches against what was stored:")
        for lang, transcript, quote, task, owner in CORPUS:
            episode = dict(rows[lang])
            kept, dropped = extract.validate({"commitments": [{
                "task": task, "direction": "theirs", "owner": owner,
                "due_date": None, "quote": quote, "speaker": owner}]}, episode)
            check(f"{lang}: kept", len(kept), 1)

    print("\n9. dates and frontmatter in other scripts")
    check("an iso date still parses", inbox_mod.parse_date("2026-08-06"), "2026-08-06")
    check("a devanagari digit date is refused rather than guessed",
          inbox_mod.parse_date("२०२६-०८-०६"), None)
    fields, body = inbox_mod.parse_frontmatter(
        "---\ntitle: राहुल के साथ बैठक\ndate: 2026-08-06\nparticipants: [राहुल, प्रिया]\n---\n\nनोट\n")
    check("a non-latin title is read", fields["title"], "राहुल के साथ बैठक")
    check("and non-latin participants", fields["participants"], ["राहुल", "प्रिया"])
    check("the body is untouched", body.strip(), "नोट")

    print("\n10. what goes to Notion survives the trip")
    for lang, _tr, quote, task, owner in CORPUS[:10]:
        props = notion.content_properties({
            "id": 1, "task": task, "direction": "theirs", "owner": owner,
            "due_date": None, "mention_count": 1, "quote": quote, "speaker": owner,
            "meeting": f"{lang} बैठक", "meeting_date": "2026-08-06", "origin": "extracted"})
        title = props["Task"]["title"][0]["text"]["content"]
        check(f"{lang}: title intact", title, task)
        check(f"{lang}: evidence carries the quote",
              quote[:12] in props["Evidence"]["rich_text"][0]["text"]["content"], True)

    print("\n11. a capture in another language")
    with closing(db.connect(TMP / "cap.db")) as conn:
        for lang, text in [("hindi", "कल राहुल को डेक भेजना है"),
                           ("chinese", "明天把资料发给李雷"),
                           ("arabic", "أرسل العرض غدا إلى علي"),
                           ("hinglish", "kal rahul ko deck bhejna hai")]:
            def fake(episode, _text=text, _lang=lang):
                kept, dropped = extract.validate({"commitments": [{
                    "task": _text, "direction": "mine", "owner": "me",
                    "due_date": None, "quote": _text, "speaker": None}]}, episode)
                return {"kept": kept, "dropped": dropped, "usage": {"model": "canned"}}

            r = capture.run(text, conn=conn, push=False, inbox=TMP / "capin",
                            extract_fn=fake, mode="create")
            check(f"{lang}: captured", len(r["tasks"]), 1)
            check(f"{lang}: title is not the fallback",
                  Path(r["file"]).name.endswith("-note.md"), False)

    print("\n12. mixed and hostile scripts")
    mixed = "Rahul: main kal deck bhej dunga और invoice भी देख लूंगा, ok? 好的"
    check("code-switched line matches",
          extract.verify_quote("main kal deck bhej dunga और invoice भी देख लूंगा", mixed)[0],
          True)
    rtl_in_ltr = "Alex: send the عرض تقديمي to Ali tomorrow"
    check("bidi text matches", extract.verify_quote("send the عرض تقديمي to Ali", rtl_in_ltr)[0], True)
    check("zero-width joiners do not break a match",
          extract.verify_quote("send the deck", "Alex: send the​deck tomorrow")[0], False)
    check("nbsp folds to a normal space",
          extract.verify_quote("send the deck", "Alex: send the deck tomorrow")[0],
          True)
    check("full-width punctuation normalises",
          extract.verify_quote("我明天发资料", "李雷：我明天发资料。")[0], True)
    check("an emoji inside a quote is fine",
          extract.verify_quote("ship the 🚀 deck today", "Alex: ship the 🚀 deck today.")[0],
          True)
    check("combining marks are not silently dropped",
          extract.verify_quote("परीक्षा की तैयारी करनी है",
                               "राहुल: परीक्षा की तैयारी करनी है।")[0], True)

    print(f"\n{PASSES['n']} checks, {len(FAILED)} failed")
    for f in FAILED:
        print(f"  FAIL {f}")
    if FAILED:
        raise SystemExit(1)
    print("\nOK — Ruger reads meetings that are not in English.")


if __name__ == "__main__":
    main()
