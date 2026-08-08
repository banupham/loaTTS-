from __future__ import annotations

import re

import uvicorn

import app as core


# Runtime text pipeline:
# 1) remove exact custom blocked phrases,
# 2) normalize known abbreviations,
# 3) spell suspicious/meaningless tokens character by character.
_original_normalize_abbreviations = core._normalize_abbreviations

LETTER_NAMES = {
    "a": "a", "b": "bê", "c": "xê", "d": "dê", "e": "e",
    "f": "ép", "g": "gờ", "h": "hát", "i": "i", "j": "giây",
    "k": "ca", "l": "lờ", "m": "mờ", "n": "nờ", "o": "ô",
    "p": "pê", "q": "quy", "r": "rờ", "s": "ét", "t": "tê",
    "u": "u", "v": "vê", "w": "vê kép", "x": "ích", "y": "i dài",
    "z": "dét",
}
DIGIT_NAMES = {
    "0": "không", "1": "một", "2": "hai", "3": "ba", "4": "bốn",
    "5": "năm", "6": "sáu", "7": "bảy", "8": "tám", "9": "chín",
}

# Common words/tokens that should be left for VieNeu instead of being spelled.
COMMON_PLAIN_TOKENS = {
    "live", "shop", "size", "sale", "game", "app", "web", "link", "phone",
    "video", "comment", "tiktok", "facebook", "youtube", "iphone", "android",
    "windows", "wifi", "bluetooth", "hello", "hi", "ok", "okay", "online",
    "offline", "admin", "mod", "stream", "server", "model", "voice", "gift",
    "follow", "like", "share", "inbox", "chat", "code", "qr", "usb", "cpu",
    "gpu", "ram", "ssd", "api", "ip", "id",
}
TOKEN_RE = re.compile(r"(?<!\w)([A-Za-z0-9]{2,16})(?!\w)")
ASCII_VOWELS = set("aeiouy")
RARE_VIETNAMESE_ASCII = set("fjwz")


def _remove_exact_blocked_phrases(
    text: str,
    phrases: list[str],
) -> tuple[str, list[str], int]:
    output = text
    matched: list[str] = []
    total = 0

    for phrase in sorted(phrases, key=len, reverse=True):
        source = str(phrase or "").strip()
        if not source:
            continue

        # Exact phrase boundary, accent-sensitive, case-insensitive.
        pattern = re.compile(rf"(?<!\w){re.escape(source)}(?!\w)", re.IGNORECASE)
        output, count = pattern.subn("", output)
        if count:
            matched.append(source)
            total += count

    if total:
        output = re.sub(r"\s+", " ", output)
        output = re.sub(r"\s+([,.;:!?])", r"\1", output)
        output = re.sub(r"^[\s,.;:!?…|/\\\-–—]+", "", output)
        output = re.sub(r"[\s,.;:!?…|/\\\-–—]+$", "", output)
        output = output.strip()

    return output, matched, total


def _should_spell_token(token: str, cfg: core.SpeakerSettings) -> bool:
    value = token.strip()
    lower = value.casefold()

    if not value or value.isdigit() or lower in COMMON_PLAIN_TOKENS:
        return False

    # If user created an explicit replacement rule, that rule wins.
    custom_keys = {str(k).casefold() for k in cfg.custom_replacements}
    default_keys = {str(k).casefold() for k in core.DEFAULT_ABBREVIATIONS}
    if lower in custom_keys or lower in default_keys:
        return False

    # Keep common compact values such as 10k, 5g, 128gb, 500mah.
    if re.fullmatch(r"\d+(?:k|m|g|gb|mb|tb|kg|cm|mm|v|w|a|mah)", lower):
        return False

    has_alpha = any(ch.isalpha() for ch in value)
    has_digit = any(ch.isdigit() for ch in value)

    # Mixed IDs/codes with at least two letters are usually safer to spell.
    if has_alpha and has_digit and sum(ch.isalpha() for ch in value) >= 2:
        return True

    if not value.isalpha():
        return False

    # Upper-case acronyms/codes such as QWR, SKU, ABC are spelled.
    if value.isupper() and 2 <= len(value) <= 8:
        return True

    # Lower-case consonant clusters such as xjsk/qwr are unlikely normal words.
    letters = lower
    if not any(ch in ASCII_VOWELS for ch in letters):
        return True

    # ASCII letters uncommon in native Vietnamese are a strong signal of junk/code.
    if any(ch in RARE_VIETNAMESE_ASCII for ch in letters) and lower not in COMMON_PLAIN_TOKENS:
        return True

    return False


def _spell_token(token: str) -> str:
    spoken: list[str] = []
    for ch in token:
        if ch.isdigit():
            spoken.append(DIGIT_NAMES.get(ch, ch))
        elif ch.isalpha():
            spoken.append(LETTER_NAMES.get(ch.casefold(), ch))
        else:
            spoken.append(ch)
    return " ".join(spoken)


def _spell_unknown_tokens(
    text: str,
    cfg: core.SpeakerSettings,
) -> tuple[str, list[tuple[str, str]]]:
    changed: list[tuple[str, str]] = []

    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if not _should_spell_token(token, cfg):
            return token
        spoken = _spell_token(token)
        changed.append((token, spoken))
        return spoken

    output = TOKEN_RE.sub(repl, text)
    output = re.sub(r"\s+", " ", output).strip()
    return output, changed


def _patched_normalize_abbreviations(
    text: str,
    cfg: core.SpeakerSettings,
) -> tuple[str, int]:
    cleaned, matched, removed_count = _remove_exact_blocked_phrases(
        text,
        cfg.blocked_phrases,
    )

    if removed_count:
        core.stats["comments_filtered_custom"] += 1
        print(
            f"[FILTER] Xóa chính xác {removed_count} cụm {matched}: "
            f"{text} -> {cleaned or '[RỖNG]'}"
        )

    normalized, abbreviation_count = _original_normalize_abbreviations(cleaned, cfg)
    spelled, changed = _spell_unknown_tokens(normalized, cfg)
    if changed:
        core.stats["comments_spelled_unknown"] = core.stats.get("comments_spelled_unknown", 0) + 1
        print(f"[SPELL] Đọc từng ký tự: {changed} | {normalized} -> {spelled}")

    return spelled, abbreviation_count


# Disable old custom phrase behavior that dropped the whole comment.
core._is_custom_blocked = lambda text, cfg: None
core._normalize_abbreviations = _patched_normalize_abbreviations
core.SERVER_VERSION = "2.4"
core.stats.setdefault("comments_spelled_unknown", 0)


# Update web help text at runtime without duplicating the whole HTML file.
try:
    html = core.WEB_FILE.read_text(encoding="utf-8")
    html = html.replace(
        "Cụm từ không đọc: mỗi dòng một cụm",
        "Cụm từ cần xóa chính xác: mỗi dòng một cụm",
    )
    html = html.replace(
        "Chỉ cần comment chứa một trong các cụm này thì sẽ bị bỏ trước khi vào queue.",
        "Chỉ xóa đúng cụm khớp; phần comment còn lại vẫn được đọc. Có dấu phải khớp dấu, không khớp một phần của từ/số dài hơn.",
    )
    marker = "Bộ phổ biến có sẵn. Quy tắc tự thêm sẽ ghi đè bộ mặc định nếu trùng khóa."
    html = html.replace(
        marker,
        marker + " Token lạ/code không có quy tắc sẽ tự đọc từng ký tự (ví dụ xjsk, QWR, abc123).",
    )
    runtime_web = core.BASE_DIR / ".runtime_index.html"
    runtime_web.write_text(html, encoding="utf-8")
    core.WEB_FILE = runtime_web
except Exception as exc:
    print(f"[WEB] Không cập nhật được hướng dẫn bộ lọc: {exc}")


# Startup guards against over-broad matching/spelling.
assert _remove_exact_blocked_phrases("hạng 2 có không chị", ["hạng 2"])[0] == "có không chị"
assert _remove_exact_blocked_phrases("hang 2 có không chị", ["hạng 2"])[0] == "hang 2 có không chị"
assert _remove_exact_blocked_phrases("hạng 20 có không chị", ["hạng 2"])[0] == "hạng 20 có không chị"
assert _remove_exact_blocked_phrases("HẠNG 2, có không chị", ["hạng 2"])[0] == "có không chị"

_test_cfg = core.SpeakerSettings()
assert _spell_unknown_tokens("xjsk", _test_cfg)[0] == "ích giây ét ca"
assert _spell_unknown_tokens("QWR", _test_cfg)[0] == "quy vê kép rờ"
assert _spell_unknown_tokens("abc123", _test_cfg)[0] == "a bê xê một hai ba"
assert _spell_unknown_tokens("10k", _test_cfg)[0] == "10k"
assert _spell_unknown_tokens("chị không có size này", _test_cfg)[0] == "chị không có size này"


if __name__ == "__main__":
    print("[FILTER] Exact phrase mode: ON (xóa đúng cụm, giữ phần comment còn lại)")
    print("[SPELL] Unknown token mode: ON (token lạ/code -> đọc từng ký tự)")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
