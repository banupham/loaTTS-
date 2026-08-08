from __future__ import annotations

import re

import uvicorn

import app as core


# Runtime patch for custom blocked phrases.
# Rules are intentionally strict:
# - accent-sensitive: "hạng" != "hang"
# - case-insensitive: "HẠNG 2" matches "hạng 2"
# - whole phrase boundaries: "hạng 2" does not match "hạng 20"
# - only the matched phrase is removed; the rest of the comment is still read.
_original_normalize_abbreviations = core._normalize_abbreviations


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

        pattern = re.compile(rf"(?<!\w){re.escape(source)}(?!\w)", re.IGNORECASE)
        output, count = pattern.subn("", output)
        if count:
            matched.append(source)
            total += count

    if total:
        # Clean spaces/punctuation left behind after removing the exact phrase.
        output = re.sub(r"\s+", " ", output)
        output = re.sub(r"\s+([,.;:!?])", r"\1", output)
        output = re.sub(r"^[\s,.;:!?…|/\\\-–—]+", "", output)
        output = re.sub(r"[\s,.;:!?…|/\\\-–—]+$", "", output)
        output = output.strip()

    return output, matched, total


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

    # Keep the existing abbreviation normalization after phrase removal.
    return _original_normalize_abbreviations(cleaned, cfg)


# Disable the old behavior that dropped the whole comment as soon as a phrase matched.
core._is_custom_blocked = lambda text, cfg: None
core._normalize_abbreviations = _patched_normalize_abbreviations
core.SERVER_VERSION = "2.3"


# Update the existing web help text without duplicating the whole HTML file here.
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
    runtime_web = core.BASE_DIR / ".runtime_index.html"
    runtime_web.write_text(html, encoding="utf-8")
    core.WEB_FILE = runtime_web
except Exception as exc:
    print(f"[WEB] Không cập nhật được hướng dẫn bộ lọc: {exc}")


# Small startup guard against over-broad matching.
assert _remove_exact_blocked_phrases("hạng 2 có không chị", ["hạng 2"])[0] == "có không chị"
assert _remove_exact_blocked_phrases("hang 2 có không chị", ["hạng 2"])[0] == "hang 2 có không chị"
assert _remove_exact_blocked_phrases("hạng 20 có không chị", ["hạng 2"])[0] == "hạng 20 có không chị"
assert _remove_exact_blocked_phrases("HẠNG 2, có không chị", ["hạng 2"])[0] == "có không chị"


if __name__ == "__main__":
    print("[FILTER] Exact phrase mode: ON (xóa đúng cụm, giữ phần comment còn lại)")
    uvicorn.run(core.app, host=core.HOST, port=core.PORT, log_level="info")
