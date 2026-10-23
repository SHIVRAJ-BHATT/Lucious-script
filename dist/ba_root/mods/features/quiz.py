# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""Automatic chat quiz (trivia / maths / word scramble) with V2 coin rewards."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any

import babase
import bascenev1 as bs

# ============================================================================
# MESSAGES. Placeholders: {category} {question} {hint} {answer} {winner}
# {reward} {seconds} {streak} {bonus} {balance}
# ============================================================================
QUIZ_MESSAGES: dict[str, str] = {
    "question": "\ue047 QUIZ [{category}]  {question}",
    "hint": "\ue048 HINT: {hint}",
    "timeout": "\ue044 Time is up! Answer: {answer} \ue045",
    "correct_title": "\ue04c  CORRECT ANSWER!  \ue04d",
    "winner": "\ue047 {winner} answered in {seconds}s and won {reward} coins!",
    "streak": "\ue043 {winner} is on a {streak}x streak! (+{bonus} bonus)",
    "balance": "\ue048 New balance: {balance} coins.",
    "no_v2": "Correct! But coin rewards need a signed-in V2 account.",
}

# Optional setting.json -> quiz keys (defaults used if missing).
OPTIONAL_DEFAULTS: dict[str, Any] = {
    "hintEnabled": True,
    "streakBonusCoins": 1,
    "streakBonusCap": 5,
    "mathQuestions": True,
    "scrambleQuestions": True,
}
MIX_WEIGHTS = {"trivia": 6, "math": 2, "scramble": 2}

_LOG = logging.getLogger("Lucifer.Quiz")
_LOCK = threading.RLock()
_CONFIG_LOCK = threading.Lock()
_CONFIG_CACHE: dict[str, Any] | None = None
_CONFIG_CACHE_AT = 0.0
_STARTED = False
_CURRENT: dict[str, Any] | None = None
_SEQ = 0
_DECK: list[int] = []
_LAST_TRIVIA: int | None = None
_LAST_WINNER: str | None = None
_STREAK = 0

_STRIP = re.compile(r"[^a-z0-9]+")
_ARTICLES = re.compile(r"^(the|a|an)\s+")

# ----------------------------------------------------------------------------
# Easy trivia: category -> (question, answer, extra accepted answers)
# ----------------------------------------------------------------------------
_TRIVIA_BANK: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {
    "WORLD": [
        ("What is the capital of France?", "Paris", ()),
        ("What is the capital of India?", "New Delhi", ("delhi",)),
        ("What is the capital of Japan?", "Tokyo", ()),
        ("What is the currency of Japan?", "Yen", ()),
        ("Which continent is India in?", "Asia", ()),
        ("What is the largest ocean on Earth?", "Pacific", ("pacific ocean",)),
        ("Which country has the Eiffel Tower?", "France", ()),
    ],
    "SPACE": [
        ("Which planet is known as the Red Planet?", "Mars", ()),
        ("What is the largest planet in our Solar System?", "Jupiter", ()),
        ("Which star is closest to Earth?", "Sun", ()),
        ("What is our galaxy called?", "Milky Way", ()),
        ("Which planet has the famous rings?", "Saturn", ()),
        ("What does the Moon orbit?", "Earth", ()),
    ],
    "SCIENCE": [
        ("What is the chemical formula of water?", "H2O", ()),
        ("Which gas do humans breathe in to live?", "Oxygen", ()),
        ("Which gas makes up most of Earth's air?", "Nitrogen", ()),
        ("Water freezes at how many degrees Celsius?", "0", ("zero",)),
        ("How plants make food from sunlight?", "Photosynthesis", ()),
        ("How many bones does an adult human have?", "206", ()),
    ],
    "ANIMALS": [
        ("What is the largest mammal on Earth?", "Blue whale", ()),
        ("Which animal is the fastest on land?", "Cheetah", ()),
        ("What sweet food do bees make?", "Honey", ()),
        ("Which animal is called the King of the Jungle?", "Lion", ()),
        ("How many legs does a spider have?", "8", ("eight",)),
    ],
    "GENERAL": [
        ("How many sides does a hexagon have?", "6", ("six",)),
        ("How many days are in a leap year?", "366", ()),
        ("How many items are in a dozen?", "12", ("twelve",)),
        ("How many minutes are in one hour?", "60", ("sixty",)),
        ("How many squares are on a chessboard?", "64", ("sixty four",)),
        ("What is the first month of the year?", "January", ()),
        ("What is the opposite of north?", "South", ()),
        ("How many colours are in a rainbow?", "7", ("seven",)),
        ("Who wrote Hamlet?", "Shakespeare", ("william shakespeare",)),
        ("Which sport uses a shuttlecock?", "Badminton", ()),
        ("What does CPU stand for? (short: C...)", "CPU",
         ("central processing unit",)),
    ],
    "BOMBSQUAD": [
        ("Which powerup puts a protective bubble around you?", "Shield",
         ("energy shield",)),
        ("Which bomb type freezes players?", "Ice bomb", ("ice", "ice bombs")),
        ("Which powerup makes you explode after a countdown?", "Curse", ()),
        ("Which powerup gives you boxing gloves?", "Gloves",
         ("boxing gloves", "punch")),
        ("Which bomb sticks to players and walls?", "Sticky bomb",
         ("sticky", "sticky bombs")),
    ],
}
_TRIVIA = [(cat, q, a, v) for cat, items in _TRIVIA_BANK.items()
           for (q, a, v) in items]

_SCRAMBLE_WORDS = ("BOMB", "SHIELD", "CURSE", "GLOVES", "MINE", "STICKY",
                   "FREEZE", "PUNCH", "PLAYER", "VICTORY", "TEAM", "FLAG",
                   "HOCKEY", "NINJA", "BOXING", "IMPACT")


# ============================================================================
# Config
# ============================================================================
def _load_config() -> dict[str, Any]:
    result: dict[str, Any] = {
        "enable": False, "firstQuestionDelaySeconds": 5, "intervalSeconds": 30,
        "answerWindowSeconds": 10, "rewardCoins": 1, "announcerName": "",
        "messages": dict(QUIZ_MESSAGES), **OPTIONAL_DEFAULTS,
    }
    try:
        import setting
        supplied = setting.get_section("quiz")
        if not isinstance(supplied, dict):
            _LOG.error("Missing 'quiz' section in setting.json; quiz disabled.")
            return result
        required = ("enable", "firstQuestionDelaySeconds", "intervalSeconds",
                    "answerWindowSeconds", "rewardCoins", "announcerName")
        missing = [k for k in required if k not in supplied]
        if missing:
            _LOG.error("Missing quiz setting(s): %s; disabled.", ", ".join(missing))
            return result
        for key in required:
            result[key] = supplied[key]
        for key, default in OPTIONAL_DEFAULTS.items():
            value = supplied.get(key, default)
            if type(value) is type(default):
                result[key] = value
        economy = setting.get_section("economy")
        if isinstance(economy, dict) and economy.get("enable", True) is False:
            result["enable"] = False
    except Exception:
        _LOG.exception("Could not read quiz settings; quiz disabled.")
        return result

    if not isinstance(result["enable"], bool):
        result["enable"] = False
    if not isinstance(result["announcerName"], str):
        result["announcerName"] = ""
    bounds = {
        "firstQuestionDelaySeconds": (5, 3600, 5),
        "intervalSeconds": (30, 86400, 30),
        "answerWindowSeconds": (10, 120, 10),
        "rewardCoins": (1, 1_000_000, 1),
        "streakBonusCoins": (0, 1000, 1),
        "streakBonusCap": (0, 50, 5),
    }
    for key, (lo, hi, fallback) in bounds.items():
        v = result.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            _LOG.warning("Invalid quiz setting %s; using %s.", key, fallback)
            result[key] = fallback
    if result["answerWindowSeconds"] >= result["intervalSeconds"]:
        result["answerWindowSeconds"] = max(10, result["intervalSeconds"] - 5)
    return result


def _config() -> dict[str, Any]:
    global _CONFIG_CACHE, _CONFIG_CACHE_AT
    now = time.monotonic()
    with _CONFIG_LOCK:
        if _CONFIG_CACHE is None or now - _CONFIG_CACHE_AT >= 1.0:
            _CONFIG_CACHE, _CONFIG_CACHE_AT = _load_config(), now
        return dict(_CONFIG_CACHE)


def refresh_config_cache() -> dict[str, Any]:
    global _CONFIG_CACHE
    with _CONFIG_LOCK:
        _CONFIG_CACHE = None
    return _config()


# ============================================================================
# Helpers
# ============================================================================
def _normalise(value: str) -> str:
    text = " ".join(str(value).casefold().split())
    return _STRIP.sub("", _ARTICLES.sub("", text))


def _broadcast(lines: str) -> None:
    try:
        sender = str(_config().get("announcerName", "")).strip() or None
        for line in str(lines).splitlines():
            if line:
                bs.chatmessage(line, sender_override=sender)
    except Exception:
        _LOG.exception("Could not broadcast a quiz message.")


def _tell(client_id: int, text: str) -> None:
    try:
        bs.broadcastmessage(text, color=(1.0, 0.65, 0.3),
                            clients=[client_id], transient=True)
    except Exception:
        _LOG.debug("Private quiz notice failed.", exc_info=True)


def _has_players() -> bool:
    try:
        session = bs.get_foreground_host_session()
        return bool(session and session.sessionplayers)
    except Exception:
        return False


def _schedule(delay: float, callback, *args) -> None:
    def _run():
        try:
            callback(*args)
        except Exception:
            _LOG.exception("Quiz timer callback failed.")
    try:
        babase.apptimer(max(1.0, float(delay)), _run)
    except Exception:
        _LOG.exception("Could not schedule quiz timer.")


# ============================================================================
# Question generators
# ============================================================================
def _word_hint(answer: str) -> str:
    return "   ".join(w[0].upper() + " _" * (len(w) - 1) for w in answer.split())


def _make_trivia() -> dict[str, Any]:
    global _LAST_TRIVIA
    if not _DECK:
        _DECK.extend(range(len(_TRIVIA)))
        random.shuffle(_DECK)
        if len(_DECK) > 1 and _DECK[-1] == _LAST_TRIVIA:
            _DECK[0], _DECK[-1] = _DECK[-1], _DECK[0]
    _LAST_TRIVIA = _DECK.pop()
    cat, q, a, variants = _TRIVIA[_LAST_TRIVIA]
    return {"category": cat, "question": q, "answer": a,
            "accepted": (a, *variants), "hint": _word_hint(a)}


def _make_math() -> dict[str, Any]:
    op = random.choice("+-x")
    if op == "+":
        a, b = random.randint(2, 25), random.randint(2, 25)
        n = a + b
    elif op == "-":
        a = random.randint(10, 40)
        b = random.randint(1, a)
        n = a - b
    else:
        a, b = random.randint(2, 10), random.randint(2, 10)
        n = a * b
    return {"category": "MATHS", "question": f"What is {a} {op} {b} ?",
            "answer": str(n), "accepted": (str(n),),
            "hint": f"It is an {'even' if n % 2 == 0 else 'odd'} number."}


def _make_scramble() -> dict[str, Any]:
    word = random.choice(_SCRAMBLE_WORDS)
    letters = list(word)
    for _ in range(10):
        random.shuffle(letters)
        if "".join(letters) != word:
            break
    return {"category": "SCRAMBLE",
            "question": "Unscramble: " + " ".join(letters),
            "answer": word.title(), "accepted": (word,),
            "hint": _word_hint(word)}


def _new_question(cfg: dict[str, Any]) -> dict[str, Any]:
    kinds, weights = ["trivia"], [MIX_WEIGHTS["trivia"]]
    if cfg["mathQuestions"]:
        kinds.append("math"); weights.append(MIX_WEIGHTS["math"])
    if cfg["scrambleQuestions"]:
        kinds.append("scramble"); weights.append(MIX_WEIGHTS["scramble"])
    kind = random.choices(kinds, weights=weights)[0]
    return {"trivia": _make_trivia, "math": _make_math,
            "scramble": _make_scramble}[kind]()


# ============================================================================
# Quiz loop
# ============================================================================
def _schedule_next_question(delay: float) -> None:
    if _STARTED:
        _schedule(delay, _ask_question)


def _ask_question() -> None:
    global _CURRENT, _SEQ
    cfg = _config()
    if not _STARTED:
        return
    if not cfg["enable"] or not _has_players():
        _schedule_next_question(60.0)  # stay alive; resume when possible
        return

    q = _new_question(cfg)
    window = float(cfg["answerWindowSeconds"])
    with _LOCK:
        _SEQ += 1
        now = time.monotonic()
        q.update({
            "id": _SEQ, "asked_at": now, "expires_at": now + window,
            "accepted": {_normalise(x) for x in q["accepted"]},
            "notified": set(),
        })
        _CURRENT = q
        qid = _SEQ

    _broadcast(cfg["messages"]["question"].format(
        category=q["category"], question=q["question"]))
    if cfg["hintEnabled"] and q.get("hint"):
        _schedule(window / 2, _give_hint, qid)
    _schedule(window, _expire_question, qid)


def _give_hint(qid: int) -> None:
    with _LOCK:
        cur = _CURRENT
        hint = cur.get("hint") if cur and cur["id"] == qid else None
    if hint:
        _broadcast(_config()["messages"]["hint"].format(hint=hint))


def _expire_question(qid: int) -> None:
    global _CURRENT, _LAST_WINNER, _STREAK
    cfg = _config()
    with _LOCK:
        cur = _CURRENT
        if cur is None or cur["id"] != qid:
            return
        _CURRENT = None
        _LAST_WINNER, _STREAK = None, 0  # nobody won: streak broken
        answer = cur["answer"]
    _broadcast(cfg["messages"]["timeout"].format(answer=answer))
    _schedule_next_question(cfg["intervalSeconds"] - cfg["answerWindowSeconds"])


def _resolve_player(client_id: int) -> tuple[str | None, str]:
    try:
        session = bs.get_foreground_host_session()
        if session is None:
            return None, "Player"
        from tools.account_ids import get_v2_account_id, is_v2_account_id
        for sp in session.sessionplayers:
            try:
                if int(sp.inputdevice.client_id) != client_id:
                    continue
            except Exception:
                continue
            candidate = get_v2_account_id(sp)
            account_id = candidate if is_v2_account_id(candidate) else None
            try:
                name = sp.getname(full=False, icon=False)
            except Exception:
                name = "Player"
            name = " ".join(str(name).split())[:64] or "Player"
            return account_id, name
    except Exception:
        _LOG.exception("Could not resolve quiz winner identity.")
    return None, "Player"


def handle_answer(message: str, client_id: int) -> bool:
    """Award the first correct answer; True means the message was consumed."""
    global _CURRENT, _LAST_WINNER, _STREAK
    if (not isinstance(message, str) or not message.strip()
            or len(message) > 48 or message.startswith("/")
            or isinstance(client_id, bool)):
        return False
    try:
        client_id = int(client_id)
    except (TypeError, ValueError, OverflowError):
        return False
    if client_id < 0 or _CURRENT is None:  # cheap exit for normal chat
        return False

    cfg = _config()
    if not cfg["enable"]:
        return False
    guess = _normalise(message)
    now = time.monotonic()

    with _LOCK:
        cur = _CURRENT
        if cur is None or now > cur["expires_at"] or guess not in cur["accepted"]:
            return False
        account_id, name = _resolve_player(client_id)
        if not account_id:
            if client_id not in cur["notified"]:
                cur["notified"].add(client_id)
                _tell(client_id, cfg["messages"]["no_v2"])
            return False

        _STREAK = _STREAK + 1 if account_id == _LAST_WINNER else 1
        _LAST_WINNER = account_id
        bonus = min(_STREAK - 1, cfg["streakBonusCap"]) * cfg["streakBonusCoins"]
        reward = int(cfg["rewardCoins"]) + bonus
        try:
            from shop import coins
            balance = coins.add_coins(account_id, reward, name)
        except Exception:
            _LOG.exception("Could not credit quiz reward to %s.", account_id)
            return False
        _CURRENT = None
        streak = _STREAK

    seconds = f"{now - cur['asked_at']:.1f}"
    lines = [cfg["messages"]["correct_title"],
             cfg["messages"]["winner"].format(winner=name, reward=reward,
                                              seconds=seconds)]
    if bonus:
        lines.append(cfg["messages"]["streak"].format(
            winner=name, streak=streak, bonus=bonus))
    lines.append(cfg["messages"]["balance"].format(balance=balance))
    _broadcast("\n".join(lines))
    _LOG.info("Quiz %s won by %s (+%s coins).", cur["id"], name, reward)

    elapsed = now - cur["asked_at"]
    _schedule_next_question(cfg["intervalSeconds"] - elapsed)
    return True


def install() -> bool:
    global _STARTED
    if _STARTED:
        return True
    cfg = _config()
    if not cfg["enable"]:
        _LOG.info("Automatic quiz is disabled in setting.json.")
        return False
    _STARTED = True
    _schedule(cfg["firstQuestionDelaySeconds"], _ask_question)
    _LOG.info("Quiz enabled: every %ss, %ss window, %s coins.",
              cfg["intervalSeconds"], cfg["answerWindowSeconds"],
              cfg["rewardCoins"])
    return True
