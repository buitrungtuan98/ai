"""The campaign's creative brief as data — which settings reach the scriptwriter, and how to tell
that the operator has changed them (ADR-089).

Why this module exists: the pre-render quality gate fails an episode after two blocked drafts, and
`core.failure` therefore classifies that failure as NOT transient — no automatic retry, because
re-generating against the same brief produces the same draft and fails the same way (ADR-079). The
reasoning is right; its premise is not permanent. Operators *do* change the brief — a new topic
angle, a rewritten persona, a retired catchphrase — and nothing recorded which brief an episode had
died under, so every gate-failed episode stayed dead until somebody remembered it and pressed Retry
by hand. A brief version answers both halves of that: "is this still the same brief?" and "what
did they change?".

What is stored is a digest per field, never the field. The persona and system prompt are the
operator's own words, so a version can be recorded on a Task and in the audit log without copying
prompt text into two more places.
"""
from __future__ import annotations

import hashlib
import json

# The campaign settings that reach the scriptwriter or the gate that judges its output. Everything
# else (voice, music, captions, footage, scheduling, watermark) shapes the VIDEO built around an
# accepted script: nudging the music volume is not a reason to re-run a failed gate, and treating it
# as one would spend an AI call per unrelated edit.
CONFIG_KEYS: tuple[str, ...] = (
    "language", "system_prompt", "persona", "style_examples", "content_style", "continuity",
    "script_depth", "video_format", "duration_min_s", "duration_max_s", "rate_pct",
    "self_critique", "script_judge",
    "catchphrase_open", "catchphrase_close", "catchphrase_open_on", "catchphrase_close_on",
)


def _digest(value) -> str:
    """A short, stable hash of one field — the value itself never travels."""
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def active_catchphrases(cfg: dict | None) -> tuple[str, ...]:
    """The phrases this campaign forces into every episode — text that is saved but toggled off is
    not one of them (the per-campaign flags default on for pre-flag campaigns).

    ONE definition, because three places have to agree about it: generation orders the model to open
    and close with these lines, the similarity gate must not read them as self-repetition, and the
    AI judge must not be allowed to demand their removal. While they disagreed, a campaign with
    catchphrases could face a gate that no regeneration was able to pass.
    """
    cfg = cfg or {}
    out: list[str] = []
    for text_key, flag_key in (("catchphrase_open", "catchphrase_open_on"),
                               ("catchphrase_close", "catchphrase_close_on")):
        phrase = (cfg.get(text_key) or "").strip()
        if phrase and cfg.get(flag_key, True):
            out.append(phrase)
    return tuple(out)


def key_digests(campaign, channel=None, user=None) -> dict[str, str]:
    """One digest per creative input of this campaign. `channel`/`user` are optional so a caller
    that only has the campaign still gets a usable (narrower) version."""
    cfg = campaign.config_json or {}
    digests = {"topic_name": _digest(campaign.topic_name),
               "total_episodes": _digest(campaign.total_episodes)}
    for key in CONFIG_KEYS:
        digests[key] = _digest(cfg.get(key))
    if channel is not None:
        # The script judge's reject threshold lives on the channel, and raising it is the single
        # most effective way to turn a passing script into a failing one — so it belongs to the
        # brief even though it is not a campaign field.
        from core import autopilot

        digests["judge_threshold"] = _digest(autopilot.script_judge_reject_max(channel))
    if user is not None:
        digests["slop_blacklist"] = _digest((user.settings_json or {}).get("slop_blacklist"))
    return digests


def fingerprint(digests: dict[str, str] | None) -> str:
    """One short id for a whole brief version — equality is all the retry decision needs."""
    return _digest(sorted((digests or {}).items()))


def changed(before: dict | None, after: dict | None) -> list[str]:
    """The creative keys whose digests differ, in stable order — what the operator actually edited.
    A key present on one side only counts as changed: a brief recorded before that field existed is
    genuinely not the brief in front of us now."""
    before, after = before or {}, after or {}
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def edited_since_gate_failure(task, campaign, channel=None, user=None) -> list[str]:
    """The creative keys edited since this episode's script was refused by the gate.

    Empty when nothing changed, when the episode never reached the gate, or when the stored record
    cannot be read — every caller (the regenerating prompt, the retry sweep, the episode page) wants
    "nothing known changed" rather than an exception from a bookkeeping field."""
    record = (getattr(task, "render_json", None) or {}).get("gate_failure") or {}
    if not record.get("brief"):
        return []
    try:
        return changed(record["brief"], key_digests(campaign, channel, user))
    except Exception:  # noqa: BLE001 — an unreadable record is not a reason to fail a render
        return []


def describe(keys: list[str], limit: int = 3) -> str:
    """"persona, catchphrase_open (+2 more)" — a log/UI phrase naming what changed, bounded so an
    audit summary stays inside its column."""
    if not keys:
        return "nothing"
    head = ", ".join(keys[:limit])
    rest = len(keys) - limit
    return f"{head} (+{rest} more)" if rest > 0 else head
