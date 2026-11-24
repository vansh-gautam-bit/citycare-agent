"""Deterministic safety checks that run in code, not in a prompt.

  detect_emergency   keyword layer, English / Hindi (Devanagari) / Hinglish (romanised Hindi)
  detect_language    en | hi | hinglish
  classify_reply     is the patient's message a clear yes to a read-back, a no, or something else
  guard_reply        last-line filter against medical advice in model output

The emergency detector deliberately errs on the side of raising the alarm. It ignores a symptom
only when it is plainly negated ("no chest pain") or in the past ("years ago").
"""

from __future__ import annotations

import re
import unicodedata

Lang = str  # "en" | "hi" | "hinglish"


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFC", text).lower()
    t = t.replace("’", "'").replace("`", "'")
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------ emergencies

_BREATH_EN = r"(can'?t|cannot|can not|unable to|not able to|trouble|difficulty|hard to|struggling to|short(ness)? of|gasping for)"
_EMERGENCY: list[tuple[str, str]] = [
    # --- English
    ("chest", r"\bchest\b.{0,20}\b(pain|pains|hurts?|hurting|tight|tightness|pressure|heavy|crushing)\b"),
    ("chest", r"\b(pain|pressure|tightness)\b.{0,15}\bchest\b"),
    ("chest", r"\bheart attack\b"),
    ("breath", _BREATH_EN + r"\s*(to\s+)?(breathe|breathing|a breath|breath)\b"),
    ("breath", r"\b(breathless|breathlessness|gasping|choking)\b"),
    ("bleed", r"\b(heavy|severe|profuse|a lot of|too much|lots of)\s+(bleeding|blood loss)\b"),
    ("bleed", r"\bbleeding\s+(heavily|a lot|badly|profusely|non-?stop)\b"),
    ("bleed", r"\bbleeding\b.{0,25}\b(won'?t|not|doesn'?t|will not|cannot|can'?t) stop"),
    ("bleed", r"\bblood\b.{0,10}\b(pouring|gushing|everywhere)\b"),
    ("faint", r"\b(fainted|fainting|passed out|blacked out|unconscious|unresponsive|collapsed|not responding|not waking up|won'?t wake up)\b"),
    ("stroke", r"\b(face|mouth)\b.{0,20}\b(droop|drooping|drooped|crooked|twisted)\b"),
    ("stroke", r"\b(slurred speech|slurring|can'?t speak|unable to speak|trouble speaking)\b"),
    ("stroke", r"\b(sudden|suddenly)\b.{0,30}\b(weakness|numb|numbness|paralys\w*|can'?t move|lost vision|can'?t see)\b"),
    ("stroke", r"\bstroke\b"),
    ("seizure", r"\b(seizure|seizures|convulsion|convulsions|convulsing|having a fit|epileptic fit)\b"),
    ("injury", r"\b(severe|serious|major|critical)\s+(injury|injuries|accident|burn|burns|fracture|head injury)\b"),
    ("injury", r"\b(head injury|road accident|in an accident|met with an accident|had an accident|run over|stabbed|gunshot|bone (is )?sticking out)\b"),
    ("poison", r"\b(poison|poisoned|poisoning|overdose|overdosed)\b"),
    ("poison", r"\b(swallowed|drank|ate|consumed)\b.{0,15}\b(acid|bleach|pesticide|phenyl|kerosene|poison|rat killer)\b"),
    ("selfharm", r"(kill(ing)? myself|end my life|end it all|suicid(e|al)|want to die|wish i (was|were) dead|self.?harm|hurt myself|harm myself|cut myself|don'?t want to (live|be alive))"),
    # --- Hinglish (romanised Hindi)
    ("chest", r"(seene|seena|sine|chhati|chhaati|chaati|chati)\s*(mein|me|main|m)?\s*.{0,15}(dard|dukh|jalan|bhaari|bhari|jakad)"),
    ("chest", r"\bdard\b.{0,12}\b(seene|seena|chhati|chaati|chati)\b"),
    ("chest", r"\bdil\b.{0,12}\b(dard|ghabra|dhadak\w*)\b"),
    ("breath", r"\b(saans|sans|saas)\b.{0,20}\b(taklif|takleef|dikkat|nahi|nhi|phool\w*|ruk\w*|na aa|ukhad\w*|tez)\b"),
    ("breath", r"\bdam ghut\w*"),
    ("bleed", r"\b(khoon|khun)\b.{0,25}\b(bahut|beh|bah|nahi ruk|nhi ruk|ruk nahi|zyada|jyada)\b"),
    ("bleed", r"\bbahut\s+(khoon|khun)\b"),
    ("faint", r"\b(behosh|behoshi|hosh nahi|hosh nhi|hosh kho|chakkar khaa?ke gir)\w*"),
    ("seizure", r"\b(daura|dauraa|daure|mirgi|jhatke|jhatka)\b"),
    ("stroke", r"\b(lakwa|laqwa|lakva|paralysis)\b"),
    ("stroke", r"\b(chehra|chehre)\b.{0,12}\b(tedha|latak\w*)\b"),
    ("injury", r"\b(bahut|gehri|gahri|gambhir)\s+chot\b"),
    ("injury", r"\b(haadsa|hadsa|haddi toot\w*)\b"),
    ("poison", r"\b(zeher|jahar|zahar|vish)\b"),
    ("selfharm", r"(khudkushi|khud kushi|aatmhatya|atmahatya|mar jaa?na chaht\w*|marna chaht\w*|jeena (nahi|nhi) chaht\w*|jaan de\w*|khatam kar (lun|loon|du|dun))"),
    # --- Hindi (Devanagari)
    ("chest", r"(सीने|छाती).{0,15}(दर्द|जकड़न|भारीपन|जलन)"),
    ("chest", r"दर्द.{0,10}(सीने|छाती)"),
    ("breath", r"सा[ंँ]स.{0,20}(तकलीफ|दिक्कत|नहीं|फूल|रुक|उखड़)"),
    ("bleed", r"खून.{0,25}(बहुत|बह|नहीं रु|ज्यादा|ज़्यादा)"),
    ("faint", r"बेहोश|होश नहीं|चक्कर.{0,10}गिर"),
    ("seizure", r"दौरा|मिर्गी|झटके"),
    ("stroke", r"लकवा|पैरालिसिस|चेहरा.{0,10}(टेढ़ा|लटक)|जु[बब़]ान.{0,10}(लड़खड़|अटक)"),
    ("injury", r"गहरी चोट|बहुत चोट|एक्सीडेंट|हादसा|हड्डी टूट"),
    ("poison", r"ज़हर|जहर|विष"),
    ("selfharm", r"खुदकुशी|आत्महत्या|मरना चाहत|जीना नहीं चाहत|जान दे"),
]
_EMERGENCY_RE = [(k, re.compile(p)) for k, p in _EMERGENCY]
_NEGATION_BEFORE = re.compile(r"\b(no|not|without|never|nahi|nahin|nhi|koi|don'?t have|do not have|dont have|didn'?t have)\b[^.!?]{0,15}$")
_PAST = re.compile(r"\b(last|past) (year|month)s?\b|\b(years?|months?) ago\b|\bpichle (saal|mahine)\b|\bsaal pa(he|h)le\b|साल पहले|पिछले साल")


def detect_emergency(text: str) -> str | None:
    """Return the kind of emergency ("chest", "breath", ..., "selfharm") or None."""
    t = _norm(text)
    if _PAST.search(t):
        return None
    for kind, rx in _EMERGENCY_RE:
        for m in rx.finditer(t):
            if _NEGATION_BEFORE.search(t[max(0, m.start() - 25):m.start()]):
                continue
            return kind
    return None


# ------------------------------------------------------------------ language

_HINGLISH = set("""mujhe mera meri mere hai hain nahi nahin nhi kal aaj chahiye karo kar kijiye kariye kardo karna dard bukhar
kya aap ho raha rahi rahe tha thi haan han theek thik ji dena batao bataiye sakta sakte sakti kitna kab kahan kaise
mein bhi aur lekin toh abhi bilkul dijiye lena milega milegi appointment_ka ka ki ke ko se par""".split())
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def detect_language(text: str) -> Lang | None:
    """en | hi | hinglish, or None when the message is too short to tell ("Yes.")."""
    dev = len(_DEVANAGARI.findall(text))
    letters = sum(c.isalpha() for c in text) or 1
    if dev / letters > 0.3:
        return "hi"
    words = re.findall(r"[a-z']+", _norm(text))
    hits = sum(w in _HINGLISH for w in words)
    if hits >= 2 or (hits == 1 and len(words) <= 3):
        return "hinglish"
    return "en" if len(words) >= 4 else None


def update_language(state: dict, text: str, router_lang: str | None = None) -> Lang:
    lang = detect_language(text)
    if lang is None and router_lang in ("en", "hi", "hinglish") and len(text.split()) >= 4:
        lang = router_lang
    if lang:
        state["lang"] = lang
    return state.get("lang", "en")


# ------------------------------------------------------------------ confirmations

_STRONG_YES = re.compile(
    r"(?<![\w'])(yes|yeah|yep|yup|yea|y|ok|okay|k|sure|confirm|confirmed|correct|proceed|please do|go ahead|do it|"
    r"book it|send it|cancel it|move it|haan|haa|han|ji|ji haan|theek hai|thik hai|kar do|kardo|bilkul|done)(?![\w'])"
    r"|हाँ|हां|जी|ठीक है|कर दो|करदो|बिल्कुल")
_WEAK_YES = re.compile(r"(?<![\w'])(fine|works|sounds good|that'?s fine|that is fine|theek|thik)(?![\w'])")
_HARD_NO = re.compile(
    r"(?<![\w'])(no|nope|nah|not|don'?t|dont|do not|stop|wait|hold on|hold|never|nahi|nahin|nhi|mat|ruko|rukiye)(?![\w'])"
    r"|नहीं|मत|रुक")
_CHANGES = re.compile(  # the patient is adding or changing something, so the read-back no longer matches
    r"(?<![\w'])(change|instead|actually|but|however|rather|different|another|other|also|except|wrong|later|"
    r"any|anything|anyone|earliest|latest|whichever|koi bhi)(?![\w'])|बदल|लेकिन|पर")


def classify_reply(text: str) -> str:
    """"yes" only for a short, unambiguous agreement with no new details (no digits, no 'but', no 'any').

    A refusal word always wins ("no", "don't", "wait"), even next to a yes word. A "yes" that also
    changes or opens up the request ("yes but 6pm", "any doctor is fine") is deliberately NOT a yes:
    the read-back no longer matches what the patient wants, so the agent must propose again.
    """
    t = _norm(text)
    words = len(t.split())
    if _HARD_NO.search(t):
        return "no"
    agrees = bool(_STRONG_YES.search(t)) or (words <= 4 and bool(_WEAK_YES.search(t)))
    if agrees and not _CHANGES.search(t) and not re.search(r"\d", t) and words <= 15:
        return "yes"
    return "other"


# ------------------------------------------------------------------ medical-advice guard

_MEDICAL = re.compile(
    r"\b(paracetamol|acetaminophen|crocin|dolo|ibuprofen|brufen|aspirin|disprin|amoxicillin|azithromycin|antibiotics?|"
    r"cetirizine|pantoprazole|antacid|painkillers?|cough syrup|dosage|mg tablet)\b"
    r"|\btake (a |an |some |one |two )?(tablet|pill|medicine|medication|dose|capsule)s?\b"
    r"|\b(probably|likely|just) (nothing|a cold|a virus|gas|acidity|a minor)\b|\bnothing (serious|to worry)\b"
    r"|\byou (should|can|could|may) (take|apply|try|drink) \w+", re.I)


def guard_reply(text: str) -> bool:
    """True if the reply looks like medical advice and must be replaced."""
    return bool(_MEDICAL.search(text))
