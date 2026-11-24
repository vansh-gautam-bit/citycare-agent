"""Emergency detection, language, confirmation parsing, advice guard, read-backs."""

import pytest

from agent_service import replies, safety

EMERGENCIES = [
    # English
    "I have chest pain and it's spreading to my arm",
    "My husband says there is pressure in his chest",
    "I can't breathe properly",
    "she has trouble breathing",
    "He is bleeding heavily from his leg",
    "bleeding won't stop",
    "my father fainted and is unconscious",
    "her face is drooping and speech is slurred",
    "My son is having a seizure",
    "he had a serious accident on the highway",
    "my child drank phenyl by mistake",
    "I want to end my life",
    "I feel like I want to die",
    "I took an overdose of tablets",
    # Hinglish
    "Mujhe seene mein bahut dard ho raha hai aur saans lene mein taklif hai. Kal ka appointment chahiye.",
    "papa ko seene me dard hai",
    "meri maa behosh ho gayi hain",
    "bahut khoon beh raha hai",
    "saans nahi aa rahi",
    "bacche ko daura pad raha hai",
    "unhe lakwa maar gaya lagta hai",
    "usne zeher kha liya",
    "mujhe khudkushi ke vichar aa rahe hain",
    "mujhe marna chahta hoon",
    # Hindi (Devanagari)
    "मुझे सीने में तेज दर्द हो रहा है",
    "सांस लेने में तकलीफ है",
    "वो बेहोश हो गए हैं",
    "बहुत खून बह रहा है",
    "मुझे आत्महत्या के विचार आ रहे हैं",
    "बच्चे को दौरा पड़ा है",
]

NOT_EMERGENCIES = [
    "I need a skin doctor on Friday evening",
    "Book me with Dr. Iyer tomorrow at 10am",
    "Do you accept Star Health insurance?",
    "What are the OPD timings?",
    "I have no chest pain, just a mild cough, please book a check-up",
    "My father had chest pain last year, can I get his old reports sent?",
    "How much do I owe the hospital?",
    "Mujhe kal ka appointment chahiye Dr. Sharma ke saath",
    "मुझे कल का अपॉइंटमेंट चाहिए",
    "Is the pharmacy open on Sunday?",
    "Yes, please book it.",
]


@pytest.mark.parametrize("msg", EMERGENCIES)
def test_emergency_detected(msg):
    assert safety.detect_emergency(msg), msg


@pytest.mark.parametrize("msg", NOT_EMERGENCIES)
def test_normal_messages_are_not_emergencies(msg):
    assert safety.detect_emergency(msg) is None, msg


def test_self_harm_kind_and_emergency_reply_has_108_in_every_language():
    assert safety.detect_emergency("I want to end my life") == "selfharm"
    for lang in replies.LANGS:
        assert "108" in replies.emergency_reply(lang)
        assert "108" in replies.emergency_reply(lang, "selfharm")


def test_emergency_reply_gives_no_medical_advice():
    for lang in replies.LANGS:
        assert not safety.guard_reply(replies.emergency_reply(lang, "chest"))


@pytest.mark.parametrize("msg,expected", [
    ("Yes", "yes"), ("Yes, please book it.", "yes"), ("Okay book whatever", "yes"), ("haan kar do", "yes"),
    ("Yes, send it.", "yes"), ("हाँ", "yes"),
    ("Ok, the next free time that day is fine. Yes, book that one.", "yes"),
    ("Yes but make it 6pm", "other"), ("yes, 5pm works", "other"), ("Yes. Also book Dr. Rao", "other"),
    ("No", "no"), ("no don't book it", "no"), ("nahi rehne do", "no"), ("wait, not that one", "no"),
    ("Any doctor is fine, the earliest evening slot works for me.", "other"),
    ("Which doctor is it?", "other"),
])
def test_classify_reply(msg, expected):
    assert safety.classify_reply(msg) == expected


def test_language_detection_and_stickiness():
    assert safety.detect_language("मुझे कल का अपॉइंटमेंट चाहिए") == "hi"
    assert safety.detect_language("Mujhe kal ka appointment chahiye") == "hinglish"
    assert safety.detect_language("I need a skin doctor on Friday evening") == "en"
    assert safety.detect_language("Yes.") is None
    state = {}
    safety.update_language(state, "Mujhe kal ka appointment chahiye")
    assert safety.update_language(state, "Yes.") == "hinglish"  # short reply keeps the patient's language


@pytest.mark.parametrize("text,bad", [
    ("You can take a paracetamol and rest.", True), ("It's probably nothing to worry about.", True),
    ("Take a tablet twice a day", True), ("You should try drinking warm water", True),
    ("Dr. Rao is free at 5pm. Shall I book it?", False),
    ("For fasting tests, no food for 10 to 12 hours before; water is fine.", False),
    ("Our pharmacy on the ground floor is open 8am to 10pm.", False),
])
def test_guard_reply(text, bad):
    assert safety.guard_reply(text) is bad


def test_readbacks_contain_every_detail_the_patient_must_check():
    f = {"doctor": "Dr. Vikram Rao", "specialty": "Dermatology", "start": "2026-12-04T17:00:00+05:30", "name": "Rohan Gupta"}
    for lang in replies.LANGS:
        text = replies.readback("book", lang, f)
        for part in ("Dr. Vikram Rao", "Friday, 4 Dec 2026 at 5:00 PM", "Rohan Gupta", "YES"):
            assert part in text or "हाँ" in text
    r = replies.readback("reschedule", "en", {"doctor": "Dr. Imran Khan", "old_start": "2026-12-03T17:30:00+05:30",
                                              "start": "2026-12-05T10:00:00+05:30"})
    assert "Thursday, 3 Dec 2026 at 5:30 PM" in r and "Saturday, 5 Dec 2026 at 10:00 AM" in r
    p = replies.readback("payment_link", "en", {"bill_id": "B1", "amount": 500, "description": "Consultation"})
    assert "B1" in p and "Rs 500" in p
