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

import re
from datetime import datetime, timezone

# (match words, cause, fix, href, action, transient) — matched lowercase against the CLASSIFICATION
# TEXT (see `_classification_text`: for a stored traceback that is the trailing exception summary,
# never the frame lines — a path like `workers/video_worker.py` inside a trace used to match the
# bare word "worker" and misdiagnose almost every stored failure as a worker stall, R22). First
# match wins, so keep the specific classes (credential, quota) ahead of the generic network one: a
# "429 … timed out" message must classify as quota, not as a connection blip. `transient` answers
# the autopilot's question — can a plain retry of the SAME episode succeed? A missing key can't be
# retried into existence, a spent quota is fixed by the reset (the scheduler already waits for it),
# and a safety block is deterministic for the same script — only a reject/re-render (new script)
# clears it. Purely-numeric words match on digit boundaries so an id/size can't false-positive.
# Named so `quota_reset_since` can recognise its own class by identity, not by re-matching words.
# "exceeded"/"rate limit" are deliberately NOT quota words (R22): requests' canonical "Max retries
# exceeded with url" and Graph's "rate limit / transient" are NETWORK-shaped — classifying them as
# quota deferred perfectly retryable uploads to the next US-Pacific midnight.
_QUOTA_WORDS = ("quota", "429", "resource_exhausted")

PATTERNS: tuple[tuple[tuple[str, ...], str, str, str, str, bool], ...] = (
    # A reviewer's decision comes first so free-text reject reasons ("intro timed out", "blocked
    # shot") can never fall through to a provider class and contradict the operator's own call.
    (("rejected in review",),
     "Rejected in review",
     "A reviewer (you or the auto-reviewer) rejected this render; the reason is recorded below and "
     "already steers the next script. Use Discard & re-render on the episode page for a fresh take.",
     "", "", False),
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
    # The AI judge's rejection, ahead of the deterministic gate's because the two need DIFFERENT
    # advice and only this one names a threshold the operator can move (ADR-089). Sending a judge
    # rejection to the gate's copy told an operator whose script was fine ("7/10, two style notes")
    # that it was "too repetitive or generic" and to go trim a blacklist that cannot block anything.
    (("script judge scored it",),
     "The AI script judge scored two drafts below this channel's bar",
     "This is a judgement about the WRITING, not about repetition: the exact objections are on the "
     "episode page under “What the render reported”. If the channel's “Reject at QC ≤” is set high, "
     "lower it — that dial also gates scripts. Otherwise soften the topic or persona, turn the "
     "script judge off for this campaign, or Retry for a fresh draft when you disagree.",
     "/channels", "Channel settings", False),
    # Fix copy is read on surfaces that don't show the raw error (the dashboard triage row, R21),
    # so it must point at things the reader can see from anywhere — "details in the raw error"
    # named an element that only exists collapsed on the episode page.
    (("failed the quality gate",),
     "The script repeated an earlier episode too closely to be worth rendering",
     "Two drafts in a row reused a recent episode's phrasing or its exact title — the campaign may "
     "be running out of fresh angles on this topic. What matched is on the episode page under “What "
     "the render reported”. Adjust the topic or persona so the next draft covers new ground; editing "
     "the campaign re-queues this episode by itself.",
     "/campaigns", "Open campaigns", False),
    # A Page token that is ALIVE but was minted without `pages_manage_posts` (Graph error #200).
    # Its own class because every other reading of it is wrong (ADR-089): the token is not expired,
    # so re-verification passes and the channel is never retired; the error carries none of the OAuth
    # words above; and an unmatched message defaults to transient=True, so this — a permission that
    # no amount of waiting grants — was retried by the autopilot and re-published at every posting
    # slot, failing identically each time while the page advised "fix the cause, then Retry".
    # Graph's OWN wording only, deliberately narrow: "insufficient permission" is what YouTube says
    # for an unrelated 403, and this row's fix talks about Page tokens.
    (("pages_manage_posts", "does not have permission to post",
      "subject does not have permission"),
     "The Facebook Page token cannot post to this Page",
     "Facebook accepted this token for reading the Page but it was generated without the "
     "pages_manage_posts permission, so publishing is refused — retrying cannot grant it. Generate a "
     "new Page Access Token with pages_manage_posts, pages_read_engagement and pages_show_list, "
     "paste it on the Channels page, then use Publish now: the rendered video is safe and needs no "
     "re-render.", "/channels", "Fix the channel", False),
    # Facebook's own OAuth wording, ahead of the generic key class because the FIX IS SOMEWHERE ELSE:
    # an API key lives on /credentials, a Page token lives on the channel. These phrases only ever
    # come from Graph. Before this, a dead Page token classified as a generic failure and the
    # autopilot burned its whole retry cap re-uploading with the same dead credential (ADR-072).
    (("error validating access token", "session has expired", "page access token", "oauthexception",
      "oauth error", "expired access token"),
     "The Facebook Page token is no longer valid",
     "Facebook refused the Page Access Token — it expired, was revoked, or the Page's permissions "
     "changed. The channel is marked expired; open it and paste a fresh permanent Page Access Token, "
     "then retry this episode.", "/channels", "Fix the channel", False),
    # YouTube's auth death has its own wording (google-auth RefreshError / the reconnect hint) and
    # its own fix location — before this row it fell through to the stall class and the operator
    # looped on Approve & publish against a dead credential (R22).
    (("invalid_grant", "reconnect the account"),
     "The YouTube connection is no longer valid",
     "Google refused the stored refresh token — it expired or was revoked (an OAuth app left in "
     "Testing status revokes tokens after 7 days). Reconnect the Google account on the Channels "
     "page; rendered episodes are safe in the buffer and publish once it works again.",
     "/channels", "Fix the channel", False),
    (("api key", "api_key", "invalid key", "unauthorized", "401", "403 forbidden"),
     "A provider rejected the key",
     "The AI or footage provider refused the credentials this render used. Check the key is present "
     "and still valid, then retry.", "/credentials", "Check credentials", False),
    (_QUOTA_WORDS,
     "A free-tier quota ran out",
     "The daily or per-minute allowance for the AI model is spent. It resets on its own — retry "
     "later, or put a bigger-quota model first in the model chain.", "/credentials", "Model chain",
     False),
    # A retired/renamed model 404s deterministically (ai_engine fails FAST on it) — retrying is
    # waste, and the remediation lives in the message this row now surfaces instead of hiding.
    (("model not found", "update gemini_model"),
     "The configured AI model was retired or renamed",
     "Google no longer serves this model name. Pick a current model on the Credentials page "
     "(model chain) or update GEMINI_MODEL in .env, then retry.",
     "/credentials", "Model chain", False),
    # Both stall rows sit ahead of the network class on purpose: their wording contains BOTH
    # "no progress" and "timeout" ("…no progress for N minutes, past this job's own timeout…"),
    # and matching "timeout" first told the operator a provider was unreachable when the truth was
    # that this box's worker had wedged. Upload before render: the upload message contains
    # "no progress for" too, and retrying an upload must not talk about re-drawing scenes.
    (("upload stalled", "upload was interrupted"),
     "An upload was interrupted",
     "The worker stopped mid-upload. The video is already rendered and safe in the buffer — Retry "
     "re-attempts the upload only (the platform is checked for a duplicate first); no re-render "
     "is needed.", "/operations", "Worker status", True),
    # Phrase-specific on purpose (R22): these are the watchdog's/reaper's/boot-recovery's OWN
    # sentences. The old bare words ("worker") also lived inside every stored traceback's file
    # paths, which shadowed every row below this one for every exception the worker recorded.
    (("render stalled", "no progress for", "wedged", "worker crashed", "worker restarted",
      "stopped making progress"),
     "The worker stopped making progress",
     "The render was abandoned because it stopped reporting progress. The worker recovers itself; "
     "check it is running, then retry this episode — it resumes from the scenes already drawn.",
     "/operations", "Worker status", True),
    # The expiry sweeper threw finished work away. NOT transient: an automatic re-render would
    # re-enter the same backlog that expired it and could loop the render slot forever.
    (("expired before its posting slot", "aged out before it could publish"),
     "A rendered episode aged out before it could publish",
     "The finished video waited too long (for review, a posting slot, or worker capacity) and was "
     "cleaned up. Retry re-renders it; if this repeats, add posting slots or lower the "
     "render-ahead buffer.", "/calendar", "Calendar", False),
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
    # "rate limit"/"exceeded" moved here from the quota class (R22): per-minute throttles and
    # requests' "Max retries exceeded with url" recover in minutes, not at the Pacific reset.
    (("timed out", "timeout", "connection", "network", "temporarily unavailable", "rate limit",
      "max retries exceeded", "502", "503"),
     "A provider was unreachable",
     "This is almost always transient — the render reached out and got nothing back. Retry it; an "
     "interrupted render resumes from the scenes it already finished.",
     "", "", True),
    (("safety", "blocked", "policy", "profanity", "gemini blocked", "prompt_feedback",
      "recitation", "no candidates"),
     "The safety filter blocked the content",
     "The generated script tripped the brand-safety filter. Open the episode and use Discard & "
     "re-render — it writes a fresh script, where a plain Retry reuses the blocked one. If it "
     "keeps happening, soften the campaign's topic or persona.", "", "", False),
)


def _classification_text(message: str) -> str:
    """What to classify: the exception summary, not the scaffolding around it.

    `_fail_task` stores full tracebacks, whose frame lines carry file paths and library names —
    haystack noise that used to decide the class ("workers/video_worker.py" matched the bare word
    "worker" on every stored failure, R22). A traceback's trailing non-indented block is exactly
    the "ExcType: message" summary (multi-line exception text included); everything indented above
    it is frames and source lines. Non-traceback messages classify whole, as before."""
    if "Traceback (most recent call last)" not in message:
        return message
    lines = [ln for ln in message.splitlines() if ln.strip()]
    tail: list[str] = []
    for line in reversed(lines):
        if line.startswith(" "):  # frame/source lines are indented; the summary block is not
            break
        tail.append(line)
    return "\n".join(reversed(tail)) if tail else message


def _word_hit(word: str, low: str) -> bool:
    """Substring match, except purely-numeric words ("429", "503") match on digit boundaries so a
    task id or byte size inside a message can never claim an HTTP-status class."""
    if word.isdigit():
        return re.search(rf"(?<!\d){re.escape(word)}(?!\d)", low) is not None
    return word in low


def _match(message: str):
    """The first PATTERNS row whose words appear in the classification text (lowercased), or None —
    the single matching pass `diagnose`, `is_transient` and `quota_reset_since` all share (first
    match wins, same as it always did)."""
    low = _classification_text(message).lower()
    for row in PATTERNS:
        if any(_word_hit(w, low) for w in row[0]):
            return row
    return None


def diagnose(message: str | None) -> dict | None:
    """Turn a recorded render error into a cause, a fix and somewhere to go (ADR-068).

    A failed episode used to show the raw exception text and a single Retry button: true, unreadable,
    and a dead end when retrying was not the answer (a spent quota, a missing key, a full disk).
    Returns None when nothing matches — the raw message is always shown either way."""
    if not message:
        return None
    row = _match(message)
    if row is None:
        return None
    _words, cause, fix, href, action, _transient = row
    return {"cause": cause, "fix": fix, "href": href, "action": action}


def is_transient(message: str | None) -> bool:
    """Can an automatic retry of the SAME episode plausibly succeed? Unknown errors say yes — the
    retry cap bounds the cost of optimism, while a wrong "no" would strand a recoverable episode."""
    if not message:
        return True
    row = _match(message)
    return row[-1] if row is not None else True


def is_quota(message: str | None) -> bool:
    """Does this failure classify as the quota class — the one that heals by pure waiting?"""
    if not message:
        return False
    row = _match(message)
    return row is not None and row[0] is _QUOTA_WORDS


# Causes that blame the box, not the campaign's content/config. The circuit breaker skips them:
# a wedged worker or an expired buffer is not evidence that the campaign is broken (R22, and the
# contract watchdog._notify_stall always claimed).
_INFRA_CAUSES = frozenset({
    "The worker stopped making progress",
    "An upload was interrupted",
    "A rendered episode aged out before it could publish",
})


def is_infrastructure(message: str | None) -> bool:
    """Does this failure blame the machine rather than the episode? Unknown errors say no."""
    if not message:
        return False
    row = _match(message)
    return row is not None and row[1] in _INFRA_CAUSES


def is_reject(message: str | None) -> bool:
    """Was this FAILED row written by a review rejection (human or auto)?"""
    return bool(message) and message.lower().startswith("rejected in review")


def is_human_reject(message: str | None) -> bool:
    """A HUMAN's rejection — the one decision no automatic retry may override. Structural, not
    substring-anywhere: a human reason merely mentioning "auto-review" must not read as the bot's."""
    if not is_reject(message):
        return False
    first = message.splitlines()[0].lower()
    return "(auto-review)" not in first and not first.startswith("rejected in review: auto-review:")


def quota_reset_since(message: str | None, failed_at: datetime | None,
                      now: datetime | None = None) -> bool:
    """A spent quota is the one non-transient failure that heals by pure waiting: Google's free
    tier resets at midnight US-Pacific. True when this failure classifies as quota-class AND at
    least one Pacific midnight has passed since it was recorded — the autopilot may then retry it
    (still inside its own retry cap). `failed_at`/`now` are naive-UTC DB timestamps."""
    if failed_at is None or not is_quota(message):
        return False
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    failed_day = failed_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
    now_day = ((now or datetime.utcnow()).replace(tzinfo=timezone.utc).astimezone(tz)).date()
    return now_day > failed_day
