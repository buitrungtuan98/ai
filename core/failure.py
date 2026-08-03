"""One classification of render failures, shared by everything that reads them (ADR-068/069).

The web layer uses it to say the same cause + fix on the episode page, the dashboard triage row and
the alert bell; the autopilot uses it to decide whether retrying can help at all. Keeping it in one
table is the point: when the scheduler had its own keyword list, "what counts as a quota error" was
already two definitions drifting apart.

It is a fixed pattern table rather than an AI call because it must work when the AI is exactly what
is broken — and it returns None rather than guessing: a confident wrong cause is worse than a stack
trace.
"""
from __future__ import annotations

# (match words, cause, fix, href, action, transient) — matched lowercase, first match wins, so keep
# the specific classes (credential, quota) ahead of the generic network one: a "429 … timed out"
# message must classify as quota, not as a connection blip. `transient` answers the autopilot's
# question — can a plain retry of the SAME episode succeed? A missing key can't be retried into
# existence, a spent quota is fixed by the reset (the scheduler already waits for it), and a safety
# block is deterministic for the same script — only a reject/re-render (new script) clears it.
PATTERNS: tuple[tuple[tuple[str, ...], str, str, str, str, bool], ...] = (
    # Pre-render quality gate (ADR-079). NOT transient: the autopilot's retry regenerates against
    # the same recent episodes with the same avoid-notes, so it would spend AI calls to fail the
    # same way — this one needs a human to adjust the topic, the blacklist, or judge the draft.
    # A compilation with too few library masters heals by PUBLISHING more episodes, not by
    # retrying the concat — the same result would come back every time (ADR-082).
    (("compilable episode",),
     "Not enough episodes in the library to build a compilation yet",
     "Masters are retained into the library as episodes publish (only from after this feature "
     "shipped). Let the campaign publish a few more episodes, then approve a new compile proposal "
     "— retrying now would just count the same library again.",
     "/campaigns", "Open campaigns", False),
    (("failed the quality gate",),
     "The script was judged too repetitive or generic to be worth rendering",
     "Two drafts in a row failed the pre-render quality gate (details in the raw error) — the "
     "campaign may be running out of fresh angles on this topic. Adjust the topic or persona, "
     "trim the blacklist in Settings, or Retry for a fresh attempt when you disagree.",
     "/settings", "Review settings", False),
    # Facebook's own OAuth wording, ahead of the generic key class because the FIX IS SOMEWHERE ELSE:
    # an API key lives on /credentials, a Page token lives on the channel. These phrases only ever
    # come from Graph. Before this, a dead Page token classified as a generic failure and the
    # autopilot burned its whole retry cap re-uploading with the same dead credential (ADR-072).
    (("error validating access token", "session has expired", "page access token", "oauthexception",
      "oauth error"),
     "The Facebook Page token is no longer valid",
     "Facebook refused the Page Access Token — it expired, was revoked, or the Page's permissions "
     "changed. The channel is marked expired; open it and paste a fresh permanent Page Access Token, "
     "then retry this episode.", "/channels", "Fix the channel", False),
    (("api key", "api_key", "invalid key", "unauthorized", "401", "403 forbidden"),
     "A provider rejected the key",
     "The AI or footage provider refused the credentials this render used. Check the key is present "
     "and still valid, then retry.", "/credentials", "Check credentials", False),
    (("quota", "429", "rate limit", "resource_exhausted", "exceeded"),
     "A free-tier quota ran out",
     "The daily or per-minute allowance for the AI model is spent. It resets on its own — retry "
     "later, or put a bigger-quota model first in the model chain.", "/credentials", "Model chain",
     False),
    # Ahead of the network class on purpose: the watchdog's and the reaper's own wording contains
    # BOTH "stalled/worker" and "timeout" ("…no progress for N minutes, past this job's own
    # timeout…"), and matching "timeout" first told the operator a provider was unreachable when the
    # truth was that this box's worker had wedged. Same retry verdict either way, wrong explanation.
    (("stalled", "wedged", "worker"),
     "The worker stopped making progress",
     "The render was abandoned because it stopped reporting progress. The worker recovers itself; "
     "check it is running, then retry this episode — it resumes from the scenes already drawn.",
     "/operations", "Worker status", True),
    (("no space", "disk", "enospc"),
     "The box ran out of disk",
     "Rendering needs working space. Old renders are cleaned automatically, but a stuck job can fill "
     "the disk — check the disk reading and free space, then retry.", "/operations", "Operations",
     True),  # the sweeper runs before every autopilot pass, so a retry may find room again
    (("ffmpeg", "codec", "invalid data", "moov atom"),
     "ffmpeg could not build the video",
     "One of the source clips or the audio track was unusable. A retry usually picks different "
     "footage and succeeds; if it repeats, try a different visual source on the campaign.",
     "", "", True),
    (("timed out", "timeout", "connection", "network", "temporarily unavailable", "502", "503"),
     "A provider was unreachable",
     "This is almost always transient — the render reached out and got nothing back. Retry it; an "
     "interrupted render resumes from the scenes it already finished.",
     "", "", True),
    (("safety", "blocked", "policy", "profanity"),
     "The safety filter blocked the content",
     "The generated script tripped the brand-safety filter. Discard & re-render writes a fresh "
     "script (a plain Retry reuses the same one); if it keeps happening, soften the campaign's "
     "topic or persona.", "", "", False),
)


def diagnose(message: str | None) -> dict | None:
    """Turn a recorded render error into a cause, a fix and somewhere to go (ADR-068).

    A failed episode used to show the raw exception text and a single Retry button: true, unreadable,
    and a dead end when retrying was not the answer (a spent quota, a missing key, a full disk).
    Returns None when nothing matches — the raw message is always shown either way."""
    if not message:
        return None
    low = message.lower()
    for words, cause, fix, href, action, _transient in PATTERNS:
        if any(w in low for w in words):
            return {"cause": cause, "fix": fix, "href": href, "action": action}
    return None


def is_transient(message: str | None) -> bool:
    """Can an automatic retry of the SAME episode plausibly succeed? Unknown errors say yes — the
    retry cap bounds the cost of optimism, while a wrong "no" would strand a recoverable episode."""
    if not message:
        return True
    low = message.lower()
    for words, *_rest, transient in PATTERNS:
        if any(w in low for w in words):
            return transient
    return True
