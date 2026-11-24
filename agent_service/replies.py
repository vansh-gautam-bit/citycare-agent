"""Fixed reply texts, in English, Hindi and Hinglish.

Anything safety-critical is written here, in code, so it does not depend on what a model says:
the emergency reply, the read-back before any change, and every handoff message.
Facts come from hospital_info.md (front desk number and hours, emergency department).
"""

from __future__ import annotations

from datetime import datetime

FRONT_DESK = "020-4000-1234"
LANGS = ("en", "hi", "hinglish")


def _pick(table: dict[str, str], lang: str) -> str:
    return table.get(lang) or table["en"]


# ------------------------------------------------------------------ emergency

_EMERGENCY = {
    "en": "This sounds like an emergency. Please call 108 right now, or go to the nearest emergency room. "
          "Our Emergency department (12 MG Road, Camp, main entrance) is open 24 hours. "
          "I have alerted our staff. Please don't wait for an appointment.",
    "hinglish": "Yeh emergency lag rahi hai. Please abhi 108 par call kariye, ya sabse nazdeeki emergency room jaiye. "
                "Hamara Emergency department (12 MG Road, Camp, main entrance) 24 ghante khula hai. "
                "Maine hamare staff ko bata diya hai. Appointment ka wait mat kariye.",
    "hi": "यह इमरजेंसी लग रही है। कृपया अभी 108 पर कॉल करें, या सबसे नज़दीकी इमरजेंसी रूम में जाएँ। "
          "हमारा इमरजेंसी विभाग (12 MG Road, Camp, मुख्य प्रवेश द्वार) 24 घंटे खुला है। "
          "मैंने हमारे स्टाफ को सूचित कर दिया है। अपॉइंटमेंट का इंतज़ार न करें।",
}
_SELFHARM_EXTRA = {
    "en": " You are not alone, and help is available right now.",
    "hinglish": " Aap akele nahi hain, madad abhi uplabdh hai.",
    "hi": " आप अकेले नहीं हैं, मदद अभी उपलब्ध है।",
}


def emergency_reply(lang: str, kind: str | None = None) -> str:
    text = _pick(_EMERGENCY, lang)
    return text + (_pick(_SELFHARM_EXTRA, lang) if kind == "selfharm" else "")


# ------------------------------------------------------------------ handoffs

HANDOFF = {
    "generic": {
        "en": f"I'm connecting you with our staff, who will help you from here. You can also call the front desk on {FRONT_DESK} (Mon to Sat, 9am to 8pm).",
        "hinglish": f"Main aapko hamare staff se connect kar raha hoon, woh aage madad karenge. Aap front desk ko {FRONT_DESK} par bhi call kar sakte hain (Mon se Sat, subah 9 se shaam 8).",
        "hi": f"मैं आपको हमारे स्टाफ से जोड़ रहा हूँ, वे आगे मदद करेंगे। आप फ्रंट डेस्क को {FRONT_DESK} पर भी कॉल कर सकते हैं (सोम से शनि, सुबह 9 से शाम 8)।",
    },
    "within_4h": {
        "en": f"Your appointment is less than 4 hours away, so I can't change or cancel it here. I've asked our staff to help you. You can also call the front desk on {FRONT_DESK}.",
        "hinglish": f"Aapka appointment 4 ghante se kam door hai, isliye main ise yahan change ya cancel nahi kar sakta. Maine staff se madad ke liye bola hai. Aap front desk ko {FRONT_DESK} par bhi call kar sakte hain.",
        "hi": f"आपका अपॉइंटमेंट 4 घंटे से कम दूर है, इसलिए मैं इसे यहाँ बदल या रद्द नहीं कर सकता। मैंने स्टाफ से मदद के लिए कहा है। आप फ्रंट डेस्क को {FRONT_DESK} पर भी कॉल कर सकते हैं।",
    },
    "locked": {
        "en": f"Our staff have this chat and will reply shortly. For anything urgent, please call {FRONT_DESK}.",
        "hinglish": f"Hamare staff ke paas aapki chat hai, woh jaldi reply karenge. Kuch urgent ho to {FRONT_DESK} par call kariye.",
        "hi": f"आपकी चैट हमारे स्टाफ के पास है, वे जल्द जवाब देंगे। कुछ जरूरी हो तो {FRONT_DESK} पर कॉल करें।",
    },
    "trouble": {
        "en": f"Sorry, I'm having trouble with our systems right now, and I don't want to give you wrong information. I've asked our staff to help you. You can also call {FRONT_DESK}.",
        "hinglish": f"Sorry, abhi hamare system mein dikkat aa rahi hai aur main galat jaankari nahi dena chahta. Maine staff se madad ke liye bola hai. Aap {FRONT_DESK} par bhi call kar sakte hain.",
        "hi": f"क्षमा करें, अभी हमारे सिस्टम में दिक्कत है और मैं गलत जानकारी नहीं देना चाहता। मैंने स्टाफ से मदद के लिए कहा है। आप {FRONT_DESK} पर भी कॉल कर सकते हैं।",
    },
    "unknown_outcome": {
        "en": f"I couldn't confirm whether that went through, so please don't assume it did. I've asked our staff to check it for you. You can also call {FRONT_DESK}.",
        "hinglish": f"Main confirm nahi kar paaya ki woh ho gaya ya nahi, isliye ise ho gaya na maanein. Maine staff se check karne ko bola hai. Aap {FRONT_DESK} par bhi call kar sakte hain.",
        "hi": f"मैं पुष्टि नहीं कर सका कि वह हुआ या नहीं, इसलिए इसे हुआ न मानें। मैंने स्टाफ से जाँच के लिए कहा है। आप {FRONT_DESK} पर भी कॉल कर सकते हैं।",
    },
    "bad_phone": {
        "en": f"I can't verify your phone number on this chat, so I can't access appointments or bills. I've asked our staff to help. You can also call {FRONT_DESK}.",
        "hinglish": f"Main is chat par aapka phone number verify nahi kar paa raha, isliye appointments ya bills nahi dekh sakta. Maine staff se madad ke liye bola hai. Aap {FRONT_DESK} par call kar sakte hain.",
        "hi": f"मैं इस चैट पर आपका फ़ोन नंबर सत्यापित नहीं कर पा रहा, इसलिए अपॉइंटमेंट या बिल नहीं देख सकता। मैंने स्टाफ से मदद के लिए कहा है। आप {FRONT_DESK} पर कॉल कर सकते हैं।",
    },
}


def handoff_reply(kind: str, lang: str) -> str:
    return _pick(HANDOFF[kind], lang)


RETRY = {
    "en": "Sorry, something went wrong on my side. Could you please send that again?",
    "hinglish": "Sorry, meri taraf se kuch gadbad ho gayi. Please woh dobara bhej dijiye.",
    "hi": "क्षमा करें, मेरी ओर से कुछ गड़बड़ हो गई। कृपया उसे दोबारा भेजें।",
}

NO_ADVICE = {
    "en": "I'm sorry, I can't give medical advice. I can book you with the right doctor if you'd like, or connect you with our staff.",
    "hinglish": "Sorry, main medical salah nahi de sakta. Aap chahein to main sahi doctor ke saath appointment book kar sakta hoon, ya staff se connect kar sakta hoon.",
    "hi": "क्षमा करें, मैं चिकित्सा सलाह नहीं दे सकता। आप चाहें तो मैं सही डॉक्टर के साथ अपॉइंटमेंट बुक कर सकता हूँ, या स्टाफ से जोड़ सकता हूँ।",
}

SLOT_TAKEN_NOTICE = {
    "en": "Sorry, that time was just taken by someone else.\n\n",
    "hinglish": "Sorry, woh time abhi kisi aur ne le liya.\n\n",
    "hi": "क्षमा करें, वह समय अभी किसी और ने ले लिया।\n\n",
}


# ------------------------------------------------------------------ read-backs (before any change)

def when(iso: str) -> str:
    """'Friday, 4 Dec 2026 at 5:00 PM' from an ISO timestamp (the hospital's local time)."""
    d = datetime.fromisoformat(iso)
    return f"{d.strftime('%A')}, {d.day} {d.strftime('%b %Y')} at {d.strftime('%I:%M %p').lstrip('0')}"


_ASK = {
    "en": ("Shall I {verb}? Reply YES to confirm.",),
    "hinglish": ("Kya main {verb}? Confirm karne ke liye YES likhiye.",),
    "hi": ("क्या मैं {verb}? पुष्टि के लिए हाँ लिखें।",),
}
_VERB = {
    "book": {"en": "book it", "hinglish": "book kar doon", "hi": "बुक कर दूँ"},
    "reschedule": {"en": "go ahead and move it", "hinglish": "isse move kar doon", "hi": "इसे बदल दूँ"},
    "cancel": {"en": "cancel it", "hinglish": "cancel kar doon", "hi": "रद्द कर दूँ"},
    "payment_link": {"en": "send the link", "hinglish": "link bhej doon", "hi": "लिंक भेज दूँ"},
}
_HEAD = {
    "book": {"en": "Please confirm this booking:", "hinglish": "Please yeh booking confirm kariye:", "hi": "कृपया यह बुकिंग कन्फर्म करें:"},
    "reschedule": {"en": "Please confirm this change:", "hinglish": "Please yeh change confirm kariye:", "hi": "कृपया यह बदलाव कन्फर्म करें:"},
    "cancel": {"en": "Please confirm this cancellation:", "hinglish": "Please yeh cancellation confirm kariye:", "hi": "कृपया यह रद्दीकरण कन्फर्म करें:"},
    "payment_link": {"en": "Please confirm:", "hinglish": "Please confirm kariye:", "hi": "कृपया कन्फर्म करें:"},
}
_LBL = {
    "patient": {"en": "Patient", "hinglish": "Patient", "hi": "मरीज़"},
    "from": {"en": "From", "hinglish": "Se", "hi": "से"},
    "to": {"en": "To", "hinglish": "Ko", "hi": "को"},
    "bill": {"en": "Payment link for bill", "hinglish": "Payment link, bill", "hi": "भुगतान लिंक, बिल"},
}


def readback(kind: str, lang: str, f: dict) -> str:
    """The exact details the patient is asked to confirm. Built from verified data, never from model text."""
    lines: list[str]
    if kind == "book":
        lines = [f"{f['doctor']} ({f['specialty']})", when(f["start"]), f"{_pick(_LBL['patient'], lang)}: {f['name']}"]
    elif kind == "reschedule":
        lines = [f"{f['doctor']}", f"{_pick(_LBL['from'], lang)}: {when(f['old_start'])}", f"{_pick(_LBL['to'], lang)}: {when(f['start'])}"]
    elif kind == "cancel":
        lines = [f"{f['doctor']}", when(f["start"])]
    else:  # payment_link
        lines = [f"{_pick(_LBL['bill'], lang)} {f['bill_id']}: Rs {f['amount']} ({f['description']})"]
    ask = _pick({k: v[0] for k, v in _ASK.items()}, lang).format(verb=_pick(_VERB[kind], lang))
    return "\n".join([_pick(_HEAD[kind], lang)] + [f"• {ln}" for ln in lines] + [ask])
