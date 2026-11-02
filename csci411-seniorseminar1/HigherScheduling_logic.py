"""
AI Planner Editing: Redesigning the intial design as a skeleton before logic 
implementation.

Changed, not just added:
  Calendar.remove_event() -> Calendar.hide_event(). Nothing in the app
  destroys an event any more. Deleting one hides it, because past planners
  refer to events by id and a destroyed event leaves a finished week pointing
  at nothing. (design note 2c on StudyBlock)

  ClassSession.is_recurring and ExtracurricularEvent.is_recurring ->
  Event.recurrence, a Recurrence rule. The bools recorded THAT something
  repeated without saying how, so nothing could act on them; a rule can be
  expanded into the weeks it covers. (design note 1 on the Event hierarchy)

Removed:
  UserPreferences.class_schedule. It duplicated ClassSessions that were
  already on the Calendar, which is now the single source of truth for them.
  Preferences hold recurring habits as clock times; anything with a datetime
  is an Event. (design note 3)

Added:
  Event               is_complete, is_hidden, hidden_at; mark_complete(),
                      hide(), unhide(), is_expired(). is_flexible() is also
                      now @abstractmethod, which is what makes the class
                      genuinely abstract.
  StudyBlock          source_event_id - the link back to the Event a block was
                      made for, which the diagram has no equivalent of at all;
                      plus completes_source, is_complete, is_hidden,
                      hidden_at, mark_complete(), hide(), unhide() and
                      is_expired(). (design note 2)
  CompletionRecord    is_rated(), and the module-level UNRATED / MIN_RATING /
                      MAX_RATING that give the 1-5 scale a name. (design note 1)
  Calendar            get_event(), get_events_by_type(), unhide_event(),
                      purge_hidden_events(); get_events() takes include_hidden
  Planner             get_block(), get_blocks_for_event(),
                      hide_blocks_for_event(), needs_hidden_block(),
                      restore_blocks_for_event(), get_referenced_event_ids(),
                      purge_hidden_blocks()
  User                hide_event(), unhide_event(), purge_hidden(),
                      complete_study_block(), complete_assignment() - the
                      operations that span the calendar and the planners, and
                      so can't live on either one alone
  ScheduleItem        a new class, with the ScheduleItemKind enum: one row of
                      a planner's week view, so the planner can show the
                      user's commitments alongside the AI's study blocks
                      instead of blocks with no context. Built on demand from
                      the Calendar, never stored. Event and StudyBlock each
                      gain to_schedule_item(); Planner gains
                      get_schedule_items(); User gains get_week_view().
  Recurrence          a new class, with the RecurrenceFrequency and
                      RecurrenceScope enums: a repeat rule stored once on an
                      Event and expanded into dates on demand, so the AI can
                      see next week's classes instead of an empty calendar.
                      Event gains recurrence and occurrence_overrides, plus
                      set_recurrence(), is_recurring(), expand() and the
                      per-date methods; hide_event() and unhide_event() take
                      a scope on both Calendar and User; get_events() expands
                      and get_event() resolves occurrence ids.
                      (design note 1 on the Event hierarchy)
  OccurrenceOverride  one date of a series differing from the rest - skipped,
                      moved or renamed. The only per-date state that persists;
                      everything else about an occurrence is regenerated.
                      Carries hidden_at, so a skipped date expires like a
                      hidden event instead of accumulating forever.
  PurgeResult         the three counts one purge run destroyed - events,
                      blocks, overrides - named rather than a bare tuple
  module level        HIDDEN_RETENTION_DAYS = 14, the window a hidden record
                      is kept before it may be destroyed; occurrence_id() and
                      parse_occurrence_id(), which name one date of a series
                      so a StudyBlock can point at it; overlaps() and
                      clipped_duration(), which fix every window in the app
                      as half-open [start, end) and decide membership by
                      overlap rather than containment (Calendar design
                      note 1)

Three policies the diagram doesn't express, all argued in design note 2c:
deleting means hiding; unhiding restores the event but not its study time;
and hidden records are purged after 14 days unless something still refers to
them, which a past planner's blocks always do.

The through-line: the diagram drew Calendar and Planner as separate stores,
but a study block is scheduled FOR an event, and that relationship needs both
a field to carry it and an owner for the operations that cross it.
"""

# from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import closing
from copy import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
import hashlib
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Callable, Optional
import uuid
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


EMAIL_VERIFICATION_TTL = timedelta(hours=24)
EMAIL_VERIFICATION_RESEND_DELAY = timedelta(seconds=60)
PASSWORD_SETUP_TTL = timedelta(minutes=30)
PENDING_ACCOUNT_TTL = timedelta(days=7)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_DURATION = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 12


def new_id() -> str:
    """Generate a short unique id for any entity."""
    return str(uuid.uuid4())[:8]


def normalize_email(email: str) -> str:
    """Validate a conventional email address and return its comparison form."""
    if not isinstance(email, str):
        raise TypeError("email must be a string")
    normalized = email.strip().casefold()
    if not normalized:
        raise ValueError("email must not be empty")
    if len(normalized) > 254 or normalized.count("@") != 1:
        raise ValueError("email must be a valid address")

    local_part, domain = normalized.split("@")
    if (
        len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local_part) is None
    ):
        raise ValueError("email must be a valid address")

    labels = domain.split(".")
    if (
        len(labels) < 2
        or len(labels[-1]) < 2
        or any(
            len(label) > 63
            or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
            for label in labels
        )
    ):
        raise ValueError("email must include a valid domain name")
    return normalized


# Occurrence ids: how one date of a repeating event is named.
#
# A recurring event is stored ONCE, as a series, and the Calendar generates
# its individual dates on demand. Those generated occurrences still need ids,
# because StudyBlock.source_event_id points at one - and the id has to mean
# the same thing on every call, or a block made for the 14th would resolve to
# a different class next time the week was drawn.
#
# So an occurrence id is DERIVED, not random: the series id and the
# occurrence's start, joined. Two properties matter, and new_id() has neither:
#   - stable. Expanding the same series twice produces the same ids, so a
#     block's link keeps pointing at the date it was made for.
#   - reversible. Given an occurrence id you can recover which series and
#     which date, which is how Calendar.get_event() resolves one without
#     anything having been stored.
#
# new_id() produces 8 hex characters and can never contain "@", so the
# separator is unambiguous.

OCCURRENCE_SEPARATOR = "@"


def occurrence_id(series_event_id: str, start: datetime) -> str:
    """
    Build the id for one date of a repeating event, e.g. "a3f9c1d2@2026-09-15T10:00:00".

    Deterministic: same series and same start always give the same id.

    `start` is the CANONICAL start - the one the recurrence rule produces -
    never the actual start after an OccurrenceOverride has moved it. An
    occurrence's identity is the slot in the series, not where it ended up.
    Keying on the moved time instead would change an occurrence's id the
    moment it was rescheduled, which would orphan the study blocks pointing
    at it and hide the very override that moved it, since the override is
    stored under that id.
    """
    if not series_event_id or OCCURRENCE_SEPARATOR in series_event_id:
        raise ValueError("series_event_id must be non-empty and cannot contain '@'")
    if not isinstance(start, datetime):
        raise TypeError("start must be a datetime")
    return f"{series_event_id}{OCCURRENCE_SEPARATOR}{start.isoformat()}"


def parse_occurrence_id(event_id: str) -> Optional[tuple[str, datetime]]:
    """
    Split an occurrence id back into (series_event_id, start), or None if this
    isn't an occurrence id at all.

    Returning None for a plain event id is the point - callers use this to ask
    "is this an occurrence?" and handle both kinds, rather than having to know
    in advance which they were given.
    """
    if not isinstance(event_id, str):
        return None
    series_event_id, separator, start_text = event_id.partition(OCCURRENCE_SEPARATOR)
    if not separator or not series_event_id or not start_text:
        return None
    try:
        start = datetime.fromisoformat(start_text)
    except ValueError:
        return None
    return series_event_id, start


# How long a hidden event or study block is kept before the system is allowed
# to destroy it for real.
#
# Hiding is this app's "delete" (design note 2c on StudyBlock), and hidden
# things would otherwise pile up forever. After this many days a hidden record
# is EXPIRED - eligible for purging, not automatically purged. Expiry is only
# half the test; the other half is that nothing still refers to it, and a
# reference always wins over the clock. See User.purge_hidden().
#
# 14 days is a retention window, not a technical limit: long enough that a user
# who deleted something by mistake has a fortnight to notice, short enough that
# the calendar doesn't accumulate dead weight indefinitely.
HIDDEN_RETENTION_DAYS = 14


# Time windows: how "this event is in that week" is decided.
#
# Every window in this app is HALF-OPEN - [start, end), start included, end
# excluded. A week Sunday through Saturday runs from Sunday 00:00 up to, but
# not including, the NEXT Sunday 00:00.
#
# Half-open is what makes adjacent windows tile without a seam. With an
# inclusive end you have to write 23:59:59 and hope nothing happens in the
# last second of the day; two such weeks both contain that midnight, and an
# event sitting exactly on the boundary lands in both. With a half-open
# bound, consecutive weeks meet exactly and every instant belongs to one.
#
# The test for membership is OVERLAP, not containment: an event counts if it
# occupies any part of the window, even if it started before or ends after.
# Keeping only events entirely inside drops a Saturday 23:00 to Sunday 03:00
# shift out of BOTH weeks it touches, which is how a scheduler ends up
# booking study time on top of a night shift.
#
# See design note 1 under Calendar for the full argument.


def overlaps(start: datetime, end: datetime,
             window_start: datetime, window_end: datetime) -> bool:
    """
    Does [start, end) occupy any part of [window_start, window_end)?

    True when the event begins before the window ends AND finishes after the
    window begins. Both comparisons are strict, which is what makes the
    boundary behave: an event ending exactly at window_start does not overlap
    (it is over), and one starting exactly at window_end does not either (it
    hasn't begun). Each belongs to the neighbouring window instead.

    A zero-length event would never overlap anything under this rule, but
    Event.__init__ rejects an end_time not after start_time, so there aren't
    any.
    """
    return start < window_end and end > window_start


def clipped_duration(start: datetime, end: datetime,
                     window_start: datetime, window_end: datetime) -> timedelta:
    """
    How much of [start, end) actually falls inside [window_start, window_end).

    The companion overlaps() needs. Use it whenever the question is "how many
    hours does this take up in this week" rather than "is it in this week at
    all". A Saturday 23:00 to Sunday 03:00 shift overlaps two weeks and
    Event.get_duration() reports the full 4 hours to each; this reports the 1
    hour the first week really owns and the 3 the second does.

    Using get_duration() instead is the double-count in design note 1a under
    Calendar - it makes both weeks look fuller than they are, and the AI
    reads that as a user over-scheduling themselves.

    Returns a zero timedelta when there is no overlap, so a caller can sum
    over a list without filtering it first.
    """
    return max(timedelta(0), min(end, window_end) - max(start, window_start))


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

# An Enum is a fixed set of named choices. Using one instead of plain strings
# means a typo like "aproved" becomes an error instead of silently doing nothing,
# and the editor can autocomplete the valid options.
# The members themselves ARE the definition, not logic, so they stay in the skeleton.


class PlannerStatus(Enum):
    """
    The lifecycle stage of a Planner (one week's plan).

    DRAFT    - the AI has generated it but the user hasn't signed off yet.
    APPROVED - the user accepted it; this is the plan they're currently living by.
    PAST     - the week is over; kept for history and for the AI to learn from.

    Flow: DRAFT -> APPROVED -> PAST
    """

    DRAFT = "draft"
    APPROVED = "approved"
    PAST = "past"


class Priority(Enum):
    """
    How important a task is, used to decide what gets scheduled first
    when there isn't enough free time for everything.

    The values are numbers (1/2/3) rather than strings so they can be
    compared or sorted, e.g. highest priority first.
    """

    LOW = 1
    MEDIUM = 2
    HIGH = 3


class OffDayScope(Enum):
    """
    Who an OffDay applies to.

    CAMPUS   - a school-wide closure (holiday, break) that affects everyone.
    PERSONAL - a day the individual user blocked off for themselves.

    The distinction matters because a campus off-day should cancel classes,
    while a personal one only clears that user's own schedule.
    """

    CAMPUS = "campus"
    PERSONAL = "personal"


class RecurrenceFrequency(Enum):
    """
    How often a repeating event repeats.

    DAILY  - every day, or every Nth day with Recurrence.interval
    WEEKLY - every week on the same weekday. The case this app is really for:
             classes, shifts and club meetings all land on a weekly timetable.

    No MONTHLY or YEARLY. A student's term runs on weeks, and every pattern
    this app has to describe fits in the two above. Adding a frequency later
    means one member here and one branch in the expansion; inventing a
    general-purpose recurrence engine now means writing rules nobody needs.
    """

    DAILY = "daily"
    WEEKLY = "weekly"


class RecurrenceScope(Enum):
    """
    When an action touches a repeating event, does it mean this one or all of
    them?

    THIS_OCCURRENCE - just the single date. "I'm skipping Tuesday's lecture."
    ENTIRE_SERIES   - the whole repeating event. "I dropped the course."

    The two are genuinely different intentions, and no default can guess
    which - so anything acting on a recurring event takes one of these and
    the UI asks the user. The same distinction every calendar app puts in a
    dialog box when you delete a repeating meeting.
    """

    THIS_OCCURRENCE = "this_occurrence"
    ENTIRE_SERIES = "entire_series"


class ScheduleItemKind(Enum):
    """
    Where a thing on the week view came from.

    COMMITMENT - an Event off the Calendar. Already agreed to; the planner
                 draws it so the user can see what surrounds their study time.
    STUDY      - a StudyBlock the AI put in this planner. The suggestion.

    The user sees one timeline, but the two halves behave differently -
    rejecting a plan throws the STUDY items away and leaves every COMMITMENT
    untouched - so the view has to keep saying which is which.
    """

    COMMITMENT = "commitment"
    STUDY = "study"


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------


@dataclass
class Recurrence:
    """
    A repeat rule: how often an event happens after its first date.

    Lives on Event.recurrence. None there means the event happens once, which
    is why there's no "never" frequency - absence says it.

    Replaces the is_recurring booleans ClassSession and ExtracurricularEvent
    used to carry. Those recorded that something repeated without saying how,
    so nothing could act on them (design note 1 on the Event hierarchy).

    Fields
    ------
    frequency - DAILY or WEEKLY
    interval  - every Nth. 1 is every week, 2 every other week. Must be at
                least 1; 0 would mean an event that recurs without time
                passing, which the expansion would spin on forever.
    until     - the last date the series can produce, or None for open-ended.
                Optional because a job with no end date is a real thing, and
                expansion is bounded by the requested window anyway, so an
                open series can't run away. A course should still set it to
                the end of term - otherwise the timetable keeps generating
                lectures through the summer, and the AI plans around classes
                that aren't happening.
    """

    frequency: RecurrenceFrequency
    interval: int = 1
    until: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, RecurrenceFrequency):
            raise TypeError("frequency must be a RecurrenceFrequency")
        if isinstance(self.interval, bool) or not isinstance(self.interval, int):
            raise TypeError("interval must be an integer")
        if self.interval < 1:
            raise ValueError("interval must be at least 1")
        if self.until is not None and not isinstance(self.until, datetime):
            raise TypeError("until must be a datetime or None")

    def occurrence_starts(self, first_start: datetime,
                          window_start: datetime,
                          window_end: datetime) -> list[datetime]:
        """
        The start times this rule produces inside a window.

        Walks from first_start by interval units of frequency, keeping the
        dates that fall in window_start..window_end and stopping at whichever
        comes first: the end of the window, or `until`.

        Bounded by the window on purpose - that is what makes an open-ended
        series safe to expand. Never generate "all occurrences" without one.

        Returns them in order, empty if the series doesn't reach the window.
        """
        if not all(isinstance(value, datetime)
                   for value in (first_start, window_start, window_end)):
            raise TypeError("first_start and window bounds must be datetimes")
        if window_start >= window_end:
            raise ValueError("window_start must be earlier than window_end")

        days_per_interval = self.interval * (
            1 if self.frequency is RecurrenceFrequency.DAILY else 7
        )
        step = timedelta(days=days_per_interval)
        if self.until is not None and self.until.date() < first_start.date():
            return []
        if window_end <= first_start:
            return []

        # Jump close to the requested window rather than walking from the
        # beginning of a long-running series on every query.
        first_index = 0
        if window_start > first_start:
            elapsed = window_start - first_start
            first_index = max(0, elapsed.days // days_per_interval)
            candidate = first_start + step * first_index
            if candidate < window_start:
                first_index += 1

        starts: list[datetime] = []
        current = first_start + step * first_index
        while (
            current < window_end
            and (self.until is None or current.date() <= self.until.date())
        ):
            starts.append(current)
            current += step
        return starts


@dataclass
class OccurrenceOverride:
    """
    One date of a repeating event behaving differently from the rest of it.

    A series generates its dates from a rule, so by default every occurrence
    is identical apart from when it happens. This is where the exceptions
    live: the Tuesday the user skipped, the week the seminar ran an hour
    late. Event.occurrence_overrides maps an occurrence id to one of these,
    and expand() applies it as it generates that date.

    Overrides are the ONLY per-date state that persists. Everything else
    about an occurrence is regenerated from the series each time and
    discarded, which is what keeps a term's worth of classes a single object.

    Every field is Optional and None means "inherit from the series", so an
    override says only what differs. An entry with nothing set is meaningless
    and should be dropped rather than stored.

    Fields
    ------
    is_hidden   - the user dropped this one date. Distinct from Event.is_hidden,
                  which drops the whole series.
    hidden_at   - when that happened, giving a single skipped date the same
                  retention clock a hidden event has, so it can eventually be
                  purged instead of accumulating for the life of the series.
    start_time  - moved to a different time, or None to keep the rule's.
    end_time    - same. Set both when moving an occurrence; setting one alone
                  changes its duration, which is legal but rarely meant.
    title       - renamed for this date only, e.g. "Midterm review".

    What is NOT overridable: fields that belong to a subclass rather than to
    Event - a ClassSession's location, a JobShift's employer. Covering them
    means either one override class per subclass, or a loose dict of field
    names, and a loose dict is the silent-typo problem design note 9 on
    AIAssistant already argues against. So "this week we're in a different
    room" isn't expressible yet. Worth revisiting if it comes up; it needs a
    decision about which of those two shapes to take, not just a field.
    """

    is_hidden: bool = False
    hidden_at: Optional[datetime] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    title: Optional[str] = None

    def is_empty(self) -> bool:
        """
        Does this override say anything at all? True when every field is at
        its default.

        Used to decide when an entry should be dropped from
        occurrence_overrides rather than left behind - un-hiding a date the
        user never otherwise changed should leave no trace, not an empty
        record that makes the series look edited.
        """
        return not (
            self.is_hidden
            or self.hidden_at is not None
            or self.start_time is not None
            or self.end_time is not None
            or self.title is not None
        )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """
        Is this a hidden date that has outlived HIDDEN_RETENTION_DAYS?

        True only when is_hidden and hidden_at is more than that many days
        before now. Same rule and same wording as Event.is_expired(), so the
        retention window means one thing everywhere.

        An override that isn't hidden never expires however old it is: a
        moved seminar is a fact about the schedule, not a deletion waiting to
        be tidied away.
        """
        if not self.is_hidden or self.hidden_at is None:
            return False
        if now is None:
            now = datetime.now(tz=self.hidden_at.tzinfo)
        return now - self.hidden_at > timedelta(days=HIDDEN_RETENTION_DAYS)


# ---------------------------------------------------------------------------
# Event hierarchy
# ---------------------------------------------------------------------------

# Everything that takes up time on the calendar is an Event. The base class holds
# what they ALL share (a title, a start, an end); each subclass adds only the
# extra fields that are specific to it.
#
# The payoff: Calendar can store one mixed list of Events and treat them
# uniformly (e.g. call get_duration() on any of them), while the AI can still
# check "is this an Assignment?" when it needs type-specific behavior.


class Event(ABC):
    """
    Abstract base class for anything that occupies time on the calendar.

    "Abstract" (ABC) means this is a template, not a real thing you put on a
    calendar - you never create a bare Event, you create an Assignment or a
    ClassSession. That rule is enforced by is_flexible() below: because it is
    marked @abstractmethod, Python refuses to construct a bare Event at all,
    and refuses to construct any future subclass that forgets to answer it.

    Attributes
    ----------
    event_id          - unique id, auto-generated so events can be found/removed by id
    title             - human-readable name shown to the user
    start_time        - when it begins
    end_time          - when it ends
    is_complete       - the plain fact "this is done", False until marked.
                        Kept separate from completion_record because the two
                        answer different questions: is_complete is WHETHER the
                        work is finished, completion_record is HOW it went. The
                        record is optional detail the user may never fill in,
                        so "done" can't be inferred from its presence alone.
    is_hidden         - the user got rid of this event. It stays on the
                        calendar but drops out of every normal view and out of
                        the AI's planning. This is what "delete" means in this
                        app: nothing is destroyed immediately, because a past
                        planner that referred to it has to keep making sense.
                        See Calendar.hide_event() and design note 2c.
    hidden_at         - WHEN it was hidden, or None if it isn't. Starts the
                        HIDDEN_RETENTION_DAYS clock after which the record may
                        be purged for real. Cleared by unhide(), so restoring
                        something resets the clock rather than leaving it to
                        expire on the old timestamp.
    recurrence        - a Recurrence rule, or None for a one-off event. When
                        set, this object is the SERIES: start_time/end_time
                        describe its first date, and the Calendar generates
                        the rest on demand. See the series/occurrence note
                        below.
    occurrence_overrides
                      - per-date exceptions to the series, as
                        {occurrence_id: OccurrenceOverride}. Covers both the
                        Tuesday the user skipped and the week the class moved.
                        Empty for anything that doesn't repeat. Distinct from
                        is_hidden, which drops the entire series.

    Series and occurrences
    ----------------------
    A recurring event is stored ONCE. Calendar.get_events() expands it into
    per-date copies for the window asked for, and those copies are transient -
    generated, drawn, discarded. Nothing stores them, so there is no second
    list of classes to fall out of step with this one, which is the same rule
    design note 3 applied to UserPreferences.

    Each occurrence carries its own event_id, built by occurrence_id() from
    the series id and that date's start. Those ids are stable across calls, so
    a StudyBlock made for the 14th still resolves to the 14th a week later,
    and Calendar.get_event() can turn one back into an occurrence without
    anything having been saved.

    State the user sets per date rather than per series can't be transient,
    so it lives in occurrence_overrides and is applied as each date is
    generated. That is the whole exception mechanism: a date is the rule plus
    its override, if it has one.
    completion_record - filled in AFTER the event happens (None until then);
                        this is what the AI later learns from
    """

    event_id: str
    start_time: datetime
    end_time: datetime

    def __init__(self, title: str, start_time: datetime, end_time: datetime):
        """
        Set up the fields every event has.

        Should reject an end_time that is not after start_time - a zero-length
        or backwards event is always a mistake, and catching it here means no
        subclass has to check it. Raises ValueError.

        Also assigns event_id via new_id(), starts completion_record,
        hidden_at and recurrence at None, is_complete and is_hidden at False,
        and occurrence_overrides as an empty dict.

        recurrence is not a constructor argument, and set_recurrence() adds it
        afterwards. Threading it through would mean adding a parameter to all
        seven hand-written subclass __init__s and forwarding it in each - a
        lot of signature churn for something most events never have. If
        Event's subclasses ever collapse into a dataclass, reconsider.
        """
        if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
            raise TypeError("start_time and end_time must be datetimes")
        if end_time <= start_time:
            raise ValueError("end_time must be later than start_time")
        self.event_id = new_id()
        self.title = title
        self.start_time = start_time
        self.end_time = end_time
        self.is_complete = False
        self.is_hidden = False
        self.hidden_at = None
        self.completion_record = None
        self.recurrence = None
        self.occurrence_overrides: dict[str, OccurrenceOverride] = {}

    def set_recurrence(self, rule: Optional[Recurrence]) -> None:
        """
        Make this event repeat, or stop it repeating (pass None).

        The event's own start_time and end_time become the first date of the
        series; the rule says what follows. Its duration carries to every
        occurrence, so a 50-minute class is 50 minutes every week.

        Only meaningful on the series itself. Setting a rule on a generated
        occurrence does nothing that lasts, since the occurrence is discarded
        when the query that made it finishes.

        Clearing the rule leaves the event as its first date alone. Worth
        deciding what should then happen to hidden_occurrence_ids: they refer
        to dates that no longer exist, and keeping them means they come back
        to life if a rule is ever set again.
        """
        if parse_occurrence_id(self.event_id) is not None:
            raise ValueError("recurrence can only be set on a stored series")
        if rule is not None and not isinstance(rule, Recurrence):
            raise TypeError("rule must be a Recurrence or None")
        if rule is not None and rule.until is not None:
            if rule.until.date() < self.start_time.date():
                raise ValueError("recurrence until date cannot precede the first occurrence")
        self.recurrence = rule

    def is_recurring(self) -> bool:
        """
        Does this event repeat? True when recurrence is not None.

        Replaces the is_recurring bool that ClassSession and
        ExtracurricularEvent used to store. Same question, but now the answer
        is derived from a rule that can actually be acted on, instead of a
        flag nothing read.
        """
        return self.recurrence is not None

    def expand(self, window_start: datetime,
               window_end: datetime,
               include_hidden: bool = False) -> list["Event"]:
        """
        The individual dates of this event that fall in a window.

        For a one-off event: itself, if it lands in the window, else nothing -
        so callers never need to check is_recurring() first.

        For a series: one Event per date the rule produces, each a copy of
        this one with
          - start_time and end_time moved to that date, keeping the duration
          - event_id set to occurrence_id(self.event_id, that start)
          - everything else - title, course, employer, is_flexible() - carried
            over unchanged, because they belong to the series
        and each is a real instance of the same subclass, so nothing
        downstream has to know it's looking at a generated occurrence.

        Then the date's OccurrenceOverride is applied, if it has one: a moved
        start or end replaces the generated times, a changed title replaces
        the series title. The id is computed from the CANONICAL start before
        any of that, so a moved occurrence keeps the identity - and the study
        blocks - it had where the rule put it.

        Note this makes a moved occurrence able to fall outside the window it
        was generated for, or another occurrence to fall into it. Which
        window an occurrence belongs to is a question about where it actually
        is, so the window test has to happen after the override is applied,
        not before.

        Dates whose override is hidden are left out by default, and so is
        everything if the series itself is hidden. Pass include_hidden=True
        to include hidden occurrences; Calendar.get_events() exposes the same
        choice for normal callers.

        The window is required and there is no unbounded version: an
        open-ended series has no last date, so "give me all of them" has no
        answer.
        """
        if window_start >= window_end:
            raise ValueError("window_start must be earlier than window_end")
        if self.recurrence is None:
            return [self] if overlaps(
                self.start_time, self.end_time, window_start, window_end
            ) and (include_hidden or not self.is_hidden) else []
        if self.is_hidden and not include_hidden:
            return []

        duration = self.get_duration()
        # A meeting that began before the window can still occupy part of it.
        canonical_starts = self.recurrence.occurrence_starts(
            self.start_time, window_start - duration, window_end
        )
        # Overrides may move an occurrence into this window from any other
        # date, so inspect stored exceptions as well as canonical candidates.
        starts_by_id = {occurrence_id(self.event_id, start): start
                        for start in canonical_starts}
        for occ_id in self.occurrence_overrides:
            parsed = parse_occurrence_id(occ_id)
            if parsed is not None and parsed[0] == self.event_id:
                starts_by_id[occ_id] = parsed[1]

        occurrences: list[Event] = []
        for occ_id, canonical_start in starts_by_id.items():
            if not self._is_recurrence_start(canonical_start):
                continue
            occurrence = self._make_occurrence(canonical_start, include_hidden)
            if occurrence is not None and overlaps(
                occurrence.start_time, occurrence.end_time,
                window_start, window_end
            ):
                occurrences.append(occurrence)
        occurrences.sort(key=lambda item: item.start_time)
        return occurrences

    def _is_recurrence_start(self, start: datetime) -> bool:
        if self.recurrence is None:
            return False
        if start < self.start_time or (
            self.recurrence.until is not None
            and start.date() > self.recurrence.until.date()
        ):
            return False
        return start in self.recurrence.occurrence_starts(
            self.start_time, start, start + timedelta(microseconds=1)
        )

    def _make_occurrence(self, canonical_start: datetime,
                         include_hidden: bool = False) -> Optional["Event"]:
        occ_id = occurrence_id(self.event_id, canonical_start)
        override = self.occurrence_overrides.get(occ_id)
        if not include_hidden and (
            self.is_hidden or (override is not None and override.is_hidden)
        ):
            return None
        occurrence = copy(self)
        occurrence.event_id = occ_id
        occurrence.start_time = (
            override.start_time if override and override.start_time is not None
            else canonical_start
        )
        occurrence.end_time = (
            override.end_time if override and override.end_time is not None
            else canonical_start + self.get_duration()
        )
        if occurrence.end_time <= occurrence.start_time:
            raise ValueError(f"occurrence override for {occ_id} has invalid times")
        if override is not None and override.title is not None:
            occurrence.title = override.title
        if override is not None and override.is_hidden:
            occurrence.is_hidden = True
            occurrence.hidden_at = override.hidden_at
        occurrence.recurrence = None
        occurrence.occurrence_overrides = {}
        return occurrence

    def get_occurrence_override(self, occ_id: str) -> Optional[OccurrenceOverride]:
        """
        The stored exception for one date, or None if that date is exactly
        what the rule says it is.

        None is the normal answer. Most dates of most series are never
        touched, and only the exceptions are stored.
        """
        return self.occurrence_overrides.get(occ_id)

    def hide_occurrence(self, occ_id: str,
                        now: Optional[datetime] = None) -> bool:
        """
        Hide ONE date of a repeating event - the Tuesday the user skipped -
        by setting is_hidden and hidden_at on that date's override, creating
        the override if this is the first thing to differ about it.

        The narrow half of the choice RecurrenceScope describes; hide() is the
        wide half and takes the whole series with it. Which one the user meant
        is a question only they can answer, so Calendar.hide_event() takes a
        scope and the UI asks.

        Returns True if this call hid it, False if it was already hidden - and
        as with hide(), the False case must leave hidden_at alone so nothing
        can refresh its own expiry by being hidden twice.

        Recording hidden_at is what gives a skipped date the same retention
        clock a hidden event has, so it expires instead of accumulating for
        the life of the series.
        """
        parsed = parse_occurrence_id(occ_id)
        if (parsed is None or parsed[0] != self.event_id
                or not self._is_recurrence_start(parsed[1])):
            return False
        override = self.occurrence_overrides.get(occ_id)
        if override is None:
            override = OccurrenceOverride()
            self.occurrence_overrides[occ_id] = override
        if override.is_hidden:
            return False
        override.is_hidden = True
        override.hidden_at = now or datetime.now()
        return True

    def unhide_occurrence(self, occ_id: str) -> bool:
        """
        Put one hidden date back: clear is_hidden and hidden_at on its
        override, and drop the override entirely if is_empty() then says it
        has nothing else to record.

        Dropping the empty entry matters. A date the user hid and then
        restored should be indistinguishable from one they never touched -
        otherwise the overrides map fills with blanks, and anything counting
        exceptions reports dates that aren't exceptional.

        Returns True if this call changed anything.

        Unlike unhide() at the series level, there's no question about study
        blocks here - hiding a single occurrence never touched them, because
        the cascade in User.hide_event() works on the series.
        """
        override = self.occurrence_overrides.get(occ_id)
        if override is None or not override.is_hidden:
            return False
        override.is_hidden = False
        override.hidden_at = None
        if override.is_empty():
            del self.occurrence_overrides[occ_id]
        return True

    def is_occurrence_hidden(self, occ_id: str) -> bool:
        """Is this date's override present and marked hidden?"""
        override = self.occurrence_overrides.get(occ_id)
        return override is not None and override.is_hidden

    def edit_occurrence(self, occ_id: str,
                        start_time: Optional[datetime] = None,
                        end_time: Optional[datetime] = None,
                        title: Optional[str] = None) -> bool:
        """
        Change ONE date of a repeating event without touching the rest: the
        week the seminar ran an hour late, the lecture renamed "Midterm
        review".

        Records the differences on that date's override, creating it if
        needed. Arguments left as None are not changed - passing only
        start_time and end_time moves the occurrence and leaves its title
        alone.

        Named arguments rather than Planner.edit()'s **updates, deliberately:
        here the three things a date may differ in are known, so they can be
        parameters and a typo is a TypeError. Planner.edit() likewise rejects
        unknown fields and never allows an id to be rewritten.

        Does NOT change the occurrence's id, even when the times move. The id
        is the slot in the series, not where the date ended up - see
        occurrence_id(). Moving an occurrence therefore keeps every study
        block that points at it.

        Returns True if anything actually changed.

        Only start_time, end_time and title can differ per date; see
        OccurrenceOverride for why a ClassSession's location can't yet.
        """
        parsed = parse_occurrence_id(occ_id)
        if (parsed is None or parsed[0] != self.event_id
                or not self._is_recurrence_start(parsed[1])):
            return False
        if start_time is None and end_time is None and title is None:
            return False
        current = self.occurrence_overrides.get(occ_id)
        new_start = (
            start_time if start_time is not None else
            current.start_time if current and current.start_time is not None else
            parsed[1]
        )
        new_end = (
            end_time if end_time is not None else
            current.end_time if current and current.end_time is not None else
            parsed[1] + self.get_duration()
        )
        if new_end <= new_start:
            raise ValueError("end_time must be later than start_time")
        if current is None:
            current = OccurrenceOverride()
            self.occurrence_overrides[occ_id] = current
        changed = False
        for field_name, value in (
            ("start_time", start_time), ("end_time", end_time), ("title", title)
        ):
            if value is not None and getattr(current, field_name) != value:
                setattr(current, field_name, value)
                changed = True
        if current.is_empty():
            self.occurrence_overrides.pop(occ_id, None)
        return changed

    def reset_occurrence(self, occ_id: str) -> bool:
        """
        Discard a date's override completely, putting it back to whatever the
        rule says - undoing a move, a rename and a hide in one go.

        Returns True if there was an override to discard.

        The blunt counterpart to edit_occurrence(). Useful as an "undo my
        changes to this date" action, and as the thing purging expired
        overrides does.
        """
        return self.occurrence_overrides.pop(occ_id, None) is not None

    def purge_expired_overrides(self, keep_occurrence_ids: set[str],
                                now: Optional[datetime] = None) -> int:
        """
        Drop overrides for dates that were hidden more than
        HIDDEN_RETENTION_DAYS ago and that nothing still refers to.

        The per-date half of the retention rule. Without it, a user who skips
        the odd lecture accumulates exceptions on that series forever, and
        User.purge_hidden() can only ever tidy whole events.

        Removes an entry when BOTH hold, exactly mirroring the event purge:
          - override.is_expired(now)
          - its occurrence id is not in keep_occurrence_ids

        keep_occurrence_ids is required, no default, for the same reason
        Calendar.purge_hidden_events() requires its protection list: a study
        block in a past planner may point at this date, and dropping the
        override would quietly restore a date the user removed and redraw
        that week wrong.

        Only hidden overrides are ever dropped. One that records a moved or
        renamed date isn't a deletion waiting to be tidied, it's a fact about
        the schedule, and it keeps until the series itself goes.

        Returns how many overrides were removed.
        """
        removed = 0
        for occ_id, override in list(self.occurrence_overrides.items()):
            if occ_id not in keep_occurrence_ids and override.is_expired(now):
                del self.occurrence_overrides[occ_id]
                removed += 1
        return removed

    def hide(self, now: Optional[datetime] = None) -> bool:
        """
        Take this event out of use without destroying it: is_hidden = True,
        and hidden_at = now, which starts its retention clock.

        now defaults to the current time. It's a parameter so a test can hide
        something "15 days ago" and check that the purge picks it up, without
        waiting a fortnight or monkey-patching the clock.

        Returns True if this call changed it, False if it was already hidden -
        same guard shape as mark_complete(), so a caller can tell a real
        deletion from a repeat and skip the cascade on the second one. Note
        the False case must leave hidden_at alone: refreshing it on a repeat
        hide would let anything reset its own expiry.

        Hiding rather than deleting is the app's answer to "the user deleted
        it". A past planner can hold blocks that were made for this event and
        that the user actually worked; if the event vanished, that week's
        history would stop making sense - blocks pointing at nothing, with
        only a title to say what they were for. Keeping the object and
        flagging it means the record stays readable and the event still
        disappears everywhere the user expects it to.

        Does NOT touch the event's study blocks. Same reason as everywhere
        else - an Event can't see Planners - so User.hide_event() owns the
        cascade.
        """
        if self.is_hidden:
            return False
        self.is_hidden = True
        self.hidden_at = now or datetime.now()
        return True

    def unhide(self) -> bool:
        """
        Undo a hide: is_hidden = False, hidden_at = None.

        Clearing hidden_at is the point - it stops the retention clock, so a
        restored event is no longer a candidate for purging. Hide it again
        later and the fortnight starts over from that second hide.

        Returns True if this call changed it, False if it wasn't hidden.

        Restores the event ONLY. Its study blocks stay hidden unless a planner
        still needs them - that rule lives in User.unhide_event(), which is
        the method the UI calls.
        """
        if not self.is_hidden:
            return False
        self.is_hidden = False
        self.hidden_at = None
        return True

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """
        Has this event been hidden long enough to be purged?

        True when is_hidden and hidden_at is more than HIDDEN_RETENTION_DAYS
        before now. False for anything not hidden, so a live event can never
        be expired.

        Eligible is not the same as doomed. This answers the clock half of the
        question only; User.purge_hidden() also checks that nothing still
        refers to the event, and a reference beats an expired clock every
        time. Keeping the two apart means this method can't accidentally
        authorize destroying something history still points at.
        """
        if not self.is_hidden or self.hidden_at is None:
            return False
        if now is None:
            now = datetime.now(tz=self.hidden_at.tzinfo)
        return now - self.hidden_at > timedelta(days=HIDDEN_RETENTION_DAYS)

    def mark_complete(self) -> bool:
        """
        Record that this event is finished: set is_complete to True.

        Returns True if this call is what changed it, False if it was already
        complete. That return value is not cosmetic - it is the recursion guard
        for the study-block link described in User.complete_assignment(). A
        block completing its assignment completes the assignment's other
        blocks, and those blocks point back at the same assignment; the chain
        stops here, because the second visit returns False and the caller does
        not propagate again.

        Deliberately does NOT touch the study blocks. An Event has no way to
        reach them - it knows nothing about Planners - so the propagation lives
        on User, which holds both sides. See the design note on StudyBlock.

        Raises ValueError if called on a generated occurrence - an event whose
        id parse_occurrence_id() recognises. The occurrence is thrown away
        when the query that produced it ends, so the flag would be set on
        something nobody reads again, and setting it on the series instead
        would mark an entire term complete. Neither is what the caller asked
        for, and losing the write quietly is worse than refusing it. Nothing
        that recurs in this app is a thing anyone completes anyway - design
        note 1c on the Event hierarchy has the reasoning.
        """
        if parse_occurrence_id(self.event_id) is not None:
            raise ValueError("generated occurrences cannot be marked complete")
        if self.is_complete:
            return False
        self.is_complete = True
        return True

    @abstractmethod
    def is_flexible(self) -> bool:
        """
        Can the planner MOVE this event, or must it schedule around it?

        True  - the event is work to find time FOR. The AI may choose or change
                when it happens (Assignment, PersonalTask).
        False - the event is committed time to work AROUND. Its start and end
                are fixed by someone other than the planner - a timetable, an
                employer, other attendees, or a deliberate decision to rest
                (ClassSession, JobShift, Meeting, ExtracurricularEvent, OffDay).

        This is the one question every subclass MUST answer, which is what makes
        Event genuinely abstract. Keeping it here rather than as an isinstance
        chain in optimize_schedule() means adding an eighth Event subclass is a
        TypeError at construction, not a subclass the scheduler quietly ignores.

        Note this is about MOVABILITY, not importance - that's what Priority is
        for. A LOW-priority Assignment is still flexible; a JobShift is not,
        however optional the user feels it is.
        """
        ...

    def get_duration(self) -> timedelta:
        """
        How long the event lasts: end_time - start_time.

        Returns a timedelta (a length of time, not a point in time).
        The AI uses this to work out how much of the week is already spoken for.
        """
        return self.end_time - self.start_time

    def to_schedule_item(self) -> "ScheduleItem":
        """
        Describe this event as a row on a planner's week view.

        Produces a ScheduleItem with kind COMMITMENT, the event's title as the
        label, its real start and end, its event_id as source_id, and
        is_flexible() as is_movable.

        Each class converting itself is what keeps the view honest: Event
        knows its label is `title` and StudyBlock knows its label is
        `subject`, and neither the view nor the UI has to learn the
        difference. Adding an eighth Event subclass costs nothing here.

        The return type is quoted because ScheduleItem is defined further
        down, with the other dataclasses - same reason attach_completion_record
        quotes CompletionRecord.
        """
        return ScheduleItem(
            label=self.title,
            start_time=self.start_time,
            end_time=self.end_time,
            kind=ScheduleItemKind.COMMITMENT,
            source_id=self.event_id,
            is_movable=self.is_flexible(),
        )

    def attach_completion_record(self, record: "CompletionRecord") -> None:
        """
        Link a CompletionRecord to this event after the fact.

        Sets both sides of the relationship: stamps the record with this
        event's id, and stores the record on the event. For Assignments, it
        also snapshots the estimate, course, and category used for this work,
        so changing the assignment later cannot change its learning history.
        """
        if not isinstance(record, CompletionRecord):
            raise TypeError("record must be a CompletionRecord")
        self.completion_record = record
        record.event_id = self.event_id
        if isinstance(self, Assignment):
            record.planned_duration_minutes = max(
                1, round(self.estimated_duration.total_seconds() / 60)
            )
            record.course = self.course
            record.assignment_category = self.category

    def __repr__(self) -> str:
        """
        Short debug-friendly text form, e.g. <Assignment 'History essay' Wed 10:00>.

        __repr__ is what shows up when you print() the object or view it in a
        debugger. Uses self.__class__.__name__ so each subclass prints its own
        name without having to redefine this.
        """
        return (
            f"<{self.__class__.__name__} {self.title!r} "
            f"{self.start_time.strftime('%a %H:%M')}>"
        )


@dataclass(frozen=True)
class AssignmentDurationRecommendation:
    """A suggested focused-work duration and the history supporting it."""

    duration: timedelta
    sample_count: int
    match_scope: "AssignmentMatchScope"


class AssignmentDurationDecisionAction(Enum):
    """The user's response to an assignment-duration recommendation."""

    ACCEPT = "accept"
    EDIT = "edit"
    KEEP_CURRENT = "keep_current"


@dataclass(frozen=True)
class AssignmentDurationDecision:
    """An explicit accept, edit, or keep-current response from the user."""

    action: AssignmentDurationDecisionAction
    duration: Optional[timedelta] = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, AssignmentDurationDecisionAction):
            raise TypeError("action must be an AssignmentDurationDecisionAction")
        if self.action is AssignmentDurationDecisionAction.EDIT:
            if not isinstance(self.duration, timedelta):
                raise TypeError("an edit decision requires a timedelta duration")
            if self.duration <= timedelta(0):
                raise ValueError("edited duration must be positive")
        elif self.duration is not None:
            raise ValueError("only an edit decision may include a duration")


@dataclass(frozen=True)
class AssignmentDurationReview:
    """UI/API-ready recommendation and the user's response, if provided."""

    event_id: str
    subject: str
    current_duration: timedelta
    recommendation: AssignmentDurationRecommendation
    decision: Optional[AssignmentDurationDecision]
    applied_duration: timedelta


class AssignmentMatchScope(Enum):
    """The historical comparison tier used for an assignment recommendation."""

    COURSE_AND_CATEGORY = "course_and_category"
    COURSE = "course"
    CATEGORY = "category"


class AssignmentPlacementState(Enum):
    """How much of an assignment's adjusted work estimate was placed."""

    FULLY_SCHEDULED = "fully_scheduled"
    PARTIALLY_SCHEDULED = "partially_scheduled"
    NOT_SCHEDULED = "not_scheduled"


class AssignmentPlacementIssue(Enum):
    """Why some estimated assignment work remains unscheduled."""

    DEADLINE_BEFORE_START = "deadline_before_start"
    INSUFFICIENT_AVAILABLE_TIME = "insufficient_available_time"


class SchedulingAction(Enum):
    """Next steps the UI can offer for an assignment placement."""

    REVIEW_OPEN_TIME = "review_open_time"
    ADJUST_STUDY_PREFERENCES = "adjust_study_preferences"
    EDIT_ESTIMATE = "edit_estimate"
    REVIEW_DEADLINE = "review_deadline"
    SCHEDULE_MANUALLY = "schedule_manually"


@dataclass(frozen=True)
class AssignmentPlacement:
    """UI-ready summary of estimated and placed work for one assignment."""

    event_id: str
    subject: str
    estimated_duration: timedelta
    planned_duration: timedelta
    scheduled_duration: timedelta
    remaining_duration: timedelta
    state: AssignmentPlacementState
    issue: Optional[AssignmentPlacementIssue]
    recommended_actions: tuple[SchedulingAction, ...] = ()


@dataclass
class ScheduleResult:
    """Study blocks plus per-assignment placement summaries for a draft plan."""

    blocks: list["StudyBlock"]
    assignment_placements: list[AssignmentPlacement]

    def get_assignment_placement(self, event_id: str) -> Optional[AssignmentPlacement]:
        """Find the placement summary for an assignment, if present."""
        return next(
            (placement for placement in self.assignment_placements
             if placement.event_id == event_id),
            None,
        )


class Assignment(Event):
    """
    Schoolwork with a deadline - the main thing the planner schedules study time for.

    The inherited start_time/end_time define the earliest date and initial
    availability window for scheduling. estimated_duration is the total
    expected work, independent of any individual scheduled study block.
    due_date is when the assignment must be HANDED IN.

    Adds: due_date, course (e.g. "HIST 201"), a user-selected category,
    estimated_duration, priority.
    """

    def __init__(self, title, start_time, end_time, due_date: datetime,
                 course: str, category: str, estimated_duration: timedelta,
                 priority: Priority = Priority.MEDIUM):
        """
        Store the assignment's estimated total effort separately from its
        scheduling window. priority defaults to MEDIUM so the caller only has
        to specify it when it's unusual.
        """
        super().__init__(title, start_time, end_time)
        if not isinstance(due_date, datetime):
            raise TypeError("due_date must be a datetime")
        if not isinstance(course, str) or not course.strip():
            raise ValueError("course must be a non-empty string")
        if not isinstance(category, str) or not category.strip():
            raise ValueError("category must be a non-empty string")
        if not isinstance(priority, Priority):
            raise TypeError("priority must be a Priority")
        self.due_date = due_date
        self.course = course.strip()
        self.category = category.strip()
        self.set_estimated_duration(estimated_duration)
        self.priority = priority

    @property
    def estimated_duration(self) -> timedelta:
        """The user's current total focused-work estimate."""
        return self._estimated_duration

    @estimated_duration.setter
    def estimated_duration(self, duration: timedelta) -> None:
        self.set_estimated_duration(duration)

    def set_estimated_duration(self, duration: timedelta) -> None:
        """Set or edit the user's working estimate, requiring positive effort."""
        if not isinstance(duration, timedelta):
            raise TypeError("estimated_duration must be a timedelta")
        if duration <= timedelta(0):
            raise ValueError("estimated_duration must be positive")
        self._estimated_duration = duration

    def accept_duration_recommendation(
            self, recommendation: AssignmentDurationRecommendation) -> None:
        """Apply an AI recommendation only when the user explicitly accepts it."""
        if not isinstance(recommendation, AssignmentDurationRecommendation):
            raise TypeError(
                "recommendation must be an AssignmentDurationRecommendation"
            )
        self.set_estimated_duration(recommendation.duration)

    def is_flexible(self) -> bool:
        """Movable within its scheduling window and before its due date."""
        return True


class ExtracurricularEvent(Event):
    """
    Optional non-academic commitments - clubs, sports, practice, volunteering.

    Adds: location.

    The is_recurring bool is gone. A weekly club meeting now says so with
    set_recurrence(Recurrence(WEEKLY)), which the Calendar can actually
    expand - see design note 1 below.
    """

    def __init__(self, title, start_time, end_time, location: str):
        """Store shared fields via super(), then location."""
        super().__init__(title, start_time, end_time)
        self.location = location

    def is_flexible(self) -> bool:
        """Fixed. The club or team sets the time, not the user - so even though
        attending is optional, the planner cannot move it. Dropping it is the
        user's call to make by deleting the event, not the AI's by rescheduling
        it."""
        return False


class ClassSession(Event):
    """
    A scheduled class meeting - a fixed commitment the planner must work around.

    Adds: course_code, location.

    The is_recurring bool is gone, and with it the old default of True. A
    class that meets weekly says so with a Recurrence rule, which needs an
    `until` - the end of term - and that is exactly the information the flag
    never carried. Nothing can default it sensibly: a term's end date isn't
    guessable, and a timetable that keeps generating lectures forever is
    worse than one that repeats only when told to. See design note 1 below.
    """

    def __init__(self, title, start_time, end_time, course_code: str,
                 location: str):
        """Store shared fields via super(), then course_code and location."""
        super().__init__(title, start_time, end_time)
        self.course_code = course_code
        self.location = location

    def is_flexible(self) -> bool:
        """Fixed. A class meets when the timetable says it meets."""
        return False


class Meeting(Event):
    """
    A scheduled meeting with other people - advisor, group project, office hours.

    Adds: attendees, location.
    """

    def __init__(self, title, start_time, end_time,
                 attendees: Optional[list[str]] = None, location: str = ""):
        """
        Store shared fields via super(), then attendees and location.

        attendees defaults to None rather than [] on purpose: a mutable default
        like [] is created ONCE and shared by every Meeting, so one meeting's
        attendees would leak into the next. The body converts None to a fresh
        empty list instead.
        """
        super().__init__(title, start_time, end_time)
        self.attendees = list(attendees) if attendees is not None else []
        self.location = location

    def is_flexible(self) -> bool:
        """Fixed. Other people agreed to this time, so it isn't the planner's
        to move - rescheduling a meeting means renegotiating it with the
        attendees, which is outside what this app does."""
        return False


class JobShift(Event):
    """
    Paid work - a hard commitment the planner cannot schedule study time over.

    Adds: employer, hourly_rate (there so the app could total up expected
    earnings; not used by the scheduler).
    """

    def __init__(self, title, start_time, end_time, employer: str,
                 hourly_rate: float = 0.0):
        """Store shared fields via super(), then employer and hourly_rate."""
        super().__init__(title, start_time, end_time)
        self.employer = employer
        self.hourly_rate = hourly_rate

    def is_flexible(self) -> bool:
        """Fixed. The employer sets the shift; the planner works around it."""
        return False


class OffDay(Event):
    """
    Time deliberately blocked off - a holiday, break, or personal rest day.

    This is the inverse of the other events: it's on the calendar precisely so
    that nothing else gets scheduled there. The AI should treat it as unavailable
    time rather than as work.

    Adds: scope (CAMPUS or PERSONAL - see OffDayScope), reason.
    """

    def __init__(self, title, start_time, end_time,
                 scope: OffDayScope = OffDayScope.PERSONAL, reason: str = ""):
        """Store shared fields via super(), then scope and reason."""
        super().__init__(title, start_time, end_time)
        if not isinstance(scope, OffDayScope):
            raise TypeError("scope must be an OffDayScope")
        self.scope = scope
        self.reason = reason

    def is_flexible(self) -> bool:
        """Fixed, and the most important False of the seven. An OffDay exists
        precisely to keep the schedule empty, so treating it as movable would
        let the AI reschedule the user's rest away - the exact thing the class
        was created to prevent."""
        return False


class PersonalTask(Event):
    """
    Personal to-dos - errands, chores, appointments.

    Like Assignment it has a priority, but no due_date and no course, since
    it isn't schoolwork.

    Adds: category (e.g. "errand", "health"), priority.
    """

    def __init__(self, title, start_time, end_time, category: str = "general",
                 priority: Priority = Priority.MEDIUM):
        """Store shared fields via super(), then category and priority."""
        super().__init__(title, start_time, end_time)
        if not isinstance(priority, Priority):
            raise TypeError("priority must be a Priority")
        self.category = category
        self.priority = priority

    def is_flexible(self) -> bool:
        """Movable. An errand or chore can happen whenever there's a gap.

        Caveat worth reviewing: PersonalTask has no due_date, so unlike an
        Assignment there is nothing bounding how far the AI may push it. A
        task with an actual deadline ("renew passport") is currently
        indistinguishable from one without ("buy shampoo")."""
        return True



# ---------------------------------------------------------------------------
# Supporting classes
# ---------------------------------------------------------------------------

# These three are @dataclass instead of normal classes. @dataclass writes the
# boilerplate for you: given just the field list below, Python generates
# __init__, __repr__ and __eq__ automatically. That's why there is no visible
# __init__ here - compare to the Event classes above, where every one had to be
# written by hand.
#
# Use it when a class is mostly "a bundle of data" with little behavior.
# Event isn't a dataclass because it needs validation and an inheritance tree.
#
# Two rules that explain the syntax below:
#   1. Fields WITHOUT a default must come before fields WITH one, otherwise
#      Python can't work out a valid call order and raises a TypeError at import.
#   2. A default that is a NEW object each time (a list, or a fresh id) must use
#      field(default_factory=...), not "= []" or "= new_id()". A plain default is
#      evaluated ONCE when the class is created, so every instance would share
#      the same list, or every record would get the same id. default_factory is
#      a function that gets CALLED per instance - note it's "new_id" with no
#      parentheses, passing the function itself rather than calling it here.


# The self-rating scale, named rather than left as loose numbers in the code.
#
# UNRATED is deliberately OUTSIDE the 1-5 scale: it is not a bad score, it is
# the absence of a score. Giving it a name is what makes 0 a meaningful default
# instead of a magic number - "rating == UNRATED" says what it means, where
# "rating == 0" reads like the user rated their work zero.
#
# The rule that comes with it: anything that does arithmetic on ratings must
# drop unrated records first (see CompletionRecord.is_rated). Averaging UNRATED
# in treats "didn't say" as "worse than the worst possible rating", which would
# tell a user who simply skipped the rating box that they are falling behind.
UNRATED = 0
MIN_RATING = 1
MAX_RATING = 5


@dataclass
class CompletionRecord:
    """
    Captures how an event actually went, used by the AI to adapt future plans.

    This is the feedback loop of the whole system. Events are the PLAN; a
    CompletionRecord is what really happened. AIAssistant.adapt_from_history()
    reads a list of these to decide whether the user is over-scheduling.

    Created after an event finishes and linked via Event.attach_completion_record().

    Fields
    ------
    event_id                - which Event this describes; "" until it's attached
    completed_on_time       - did the user finish by the deadline
    actual_duration_minutes - total focused work time only, excluding
                               interruptions; None until the user reports it
    planned_duration_minutes - estimate used when this work was planned; kept
                               as a snapshot to compare against actual effort
    course / assignment_category
                            - assignment context copied when this record is
                              attached, so later edits don't rewrite history
    completed_at            - when this completion was recorded, for recency
                              weighting in duration recommendations
    performance_rating      - user's self-rating, MIN_RATING..MAX_RATING (1-5).
                              Defaults to UNRATED (0), which means "no rating
                              given" - not a score of zero. Rating is optional,
                              so this is the normal state for a record the user
                              never got around to rating, and callers are
                              expected to check is_rated() before using it.
    notes                   - free-text, e.g. "kept getting interrupted"
    record_id               - unique id for the record itself
    """

    event_id: str = ""
    completed_on_time: bool = False
    actual_duration_minutes: Optional[int] = None
    planned_duration_minutes: Optional[int] = None
    course: str = ""
    assignment_category: str = ""
    completed_at: datetime = field(default_factory=datetime.now)
    performance_rating: int = UNRATED  # UNRATED, or MIN_RATING..MAX_RATING
    notes: str = ""
    record_id: str = field(default_factory=new_id)

    def __post_init__(self) -> None:
        for field_name in ("actual_duration_minutes", "planned_duration_minutes"):
            duration = getattr(self, field_name)
            if duration is not None:
                if isinstance(duration, bool) or not isinstance(duration, int):
                    raise TypeError(f"{field_name} must be an integer number of minutes")
                if duration <= 0:
                    raise ValueError(f"{field_name} must be positive when provided")
        if not isinstance(self.course, str):
            raise TypeError("course must be a string")
        if not isinstance(self.assignment_category, str):
            raise TypeError("assignment_category must be a string")
        if not isinstance(self.completed_at, datetime):
            raise TypeError("completed_at must be a datetime")

    @property
    def estimate_error_minutes(self) -> Optional[int]:
        """Actual minus planned focused minutes; positive means under-estimated."""
        if (self.actual_duration_minutes is None
                or self.planned_duration_minutes is None):
            return None
        return self.actual_duration_minutes - self.planned_duration_minutes

    def is_rated(self) -> bool:
        """
        Has the user actually given this record a rating?

        True when performance_rating sits inside MIN_RATING..MAX_RATING, False
        when it is UNRATED. This is the gate every rating calculation goes
        through - adapt_from_history() filters with it before averaging, so an
        unrated record is excluded rather than counted as a 0.

        Checking the range instead of just "!= UNRATED" also catches a rating
        that arrived out of range from somewhere else; such a value is treated
        as unrated rather than trusted.
        """
        return MIN_RATING <= self.performance_rating <= MAX_RATING


class CompletionRecordStore:
    """Persist completion history in a local SQLite database."""

    def __init__(self, database_path: str | Path):
        if not isinstance(database_path, (str, Path)):
            raise TypeError("database_path must be a string or Path")
        if not str(database_path).strip():
            raise ValueError("database_path must not be empty")
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("database_path must name a persistent SQLite file")
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS completion_records (
                        user_id TEXT NOT NULL,
                        record_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        completed_on_time INTEGER NOT NULL,
                        actual_duration_minutes INTEGER,
                        planned_duration_minutes INTEGER,
                        course TEXT NOT NULL,
                        assignment_category TEXT NOT NULL,
                        completed_at TEXT NOT NULL,
                        performance_rating INTEGER NOT NULL,
                        notes TEXT NOT NULL,
                        PRIMARY KEY (user_id, record_id)
                    )
                    """
                )

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        if not user_id.strip():
            raise ValueError("user_id must be a non-empty string")

    def add_record(self, user_id: str, record: CompletionRecord) -> None:
        """Save one completion record for a user; duplicate ids raise an error."""
        self._validate_user_id(user_id)
        if not isinstance(record, CompletionRecord):
            raise TypeError("record must be a CompletionRecord")
        if not isinstance(record.completed_on_time, bool):
            raise TypeError("record.completed_on_time must be a boolean")
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO completion_records (
                        user_id, record_id, event_id, completed_on_time,
                        actual_duration_minutes, planned_duration_minutes, course,
                        assignment_category, completed_at, performance_rating, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        record.record_id,
                        record.event_id,
                        int(record.completed_on_time),
                        record.actual_duration_minutes,
                        record.planned_duration_minutes,
                        record.course,
                        record.assignment_category,
                        record.completed_at.isoformat(),
                        record.performance_rating,
                        record.notes,
                    ),
                )

    def get_records(self, user_id: str) -> list[CompletionRecord]:
        """Load one user's completion history in completion-time order."""
        self._validate_user_id(user_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT record_id, event_id, completed_on_time,
                       actual_duration_minutes, planned_duration_minutes,
                       course, assignment_category, completed_at,
                       performance_rating, notes
                FROM completion_records
                WHERE user_id = ?
                ORDER BY completed_at, record_id
                """,
                (user_id,),
            ).fetchall()
        return [
            CompletionRecord(
                record_id=row[0],
                event_id=row[1],
                completed_on_time=bool(row[2]),
                actual_duration_minutes=row[3],
                planned_duration_minutes=row[4],
                course=row[5],
                assignment_category=row[6],
                completed_at=datetime.fromisoformat(row[7]),
                performance_rating=row[8],
                notes=row[9],
            )
            for row in rows
        ]


class UserAccountStore:
    """Persist passwordless registrations and Argon2id-backed user accounts."""

    _PASSWORD_HASHER = PasswordHasher(type=Type.ID)

    def __init__(
        self,
        database_path: str | Path,
        verification_sender: Callable[[str, str], None],
    ):
        if not isinstance(database_path, (str, Path)):
            raise TypeError("database_path must be a string or Path")
        if not str(database_path).strip() or str(database_path) == ":memory:":
            raise ValueError("database_path must name a persistent SQLite file")
        if not callable(verification_sender):
            raise TypeError("verification_sender must be callable")
        self.database_path = str(database_path)
        self.verification_sender = verification_sender
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_accounts (
                        user_id TEXT PRIMARY KEY,
                        normalized_email TEXT NOT NULL UNIQUE,
                        email_verified INTEGER NOT NULL DEFAULT 0,
                        verification_token_hash TEXT,
                        verification_expires_at TEXT,
                        verification_sent_at TEXT,
                        password_hash TEXT,
                        password_setup_token_hash TEXT,
                        password_setup_expires_at TEXT,
                        created_at TEXT
                    )
                    """
                )
                existing_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(user_accounts)"
                    )
                }
                for column_name in (
                    "verification_expires_at",
                    "verification_sent_at",
                    "password_hash",
                    "password_setup_token_hash",
                    "password_setup_expires_at",
                    "created_at",
                ):
                    if column_name not in existing_columns:
                        connection.execute(
                            f"ALTER TABLE user_accounts ADD COLUMN {column_name} TEXT"
                        )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_login_attempts (
                        normalized_email TEXT PRIMARY KEY,
                        failed_attempts INTEGER NOT NULL,
                        locked_until TEXT
                    )
                    """
                )

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        if not isinstance(user_id, str):
            raise TypeError("user_id must be a string")
        if not user_id.strip():
            raise ValueError("user_id must be a non-empty string")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _utc_now(now: Optional[datetime]) -> datetime:
        if now is None:
            return datetime.now(timezone.utc)
        if not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must include a timezone")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _validate_password(password: str) -> None:
        if not isinstance(password, str):
            raise TypeError("password must be a string")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters"
            )

    def is_email_available(self, email: str) -> bool:
        """Friendly pre-check; registration still relies on SQLite's constraint."""
        normalized_email = normalize_email(email)
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM user_accounts WHERE normalized_email = ?",
                (normalized_email,),
            ).fetchone()
        return row is None

    def expire_pending_registrations(
        self, now: Optional[datetime] = None
    ) -> int:
        """Remove passwordless, unverified registrations older than seven days."""
        current_time = self._utc_now(now)
        cutoff = (current_time - PENDING_ACCOUNT_TTL).isoformat()
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    DELETE FROM user_accounts
                    WHERE email_verified = 0 AND created_at <= ?
                    """,
                    (cutoff,),
                )
        return cursor.rowcount

    def request_email_verification(
        self, email: str, now: Optional[datetime] = None
    ) -> str:
        """Create a passwordless pending account and send its verification token."""
        normalized_email = normalize_email(email)
        current_time = self._utc_now(now)
        self.expire_pending_registrations(current_time)
        if not self.is_email_available(normalized_email):
            raise ValueError("email is already registered")

        user_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        try:
            with closing(sqlite3.connect(self.database_path)) as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO user_accounts (
                            user_id, normalized_email, email_verified,
                            verification_token_hash, verification_expires_at,
                            verification_sent_at, password_hash, created_at
                        ) VALUES (?, ?, 0, ?, ?, ?, NULL, ?)
                        """,
                        (
                            user_id,
                            normalized_email,
                            self._token_hash(token),
                            (current_time + EMAIL_VERIFICATION_TTL).isoformat(),
                            current_time.isoformat(),
                            current_time.isoformat(),
                        ),
                    )
        except sqlite3.IntegrityError as error:
            if not self.is_email_available(normalized_email):
                raise ValueError("email is already registered") from error
            raise

        self.verification_sender(normalized_email, token)
        return user_id

    def send_verification_email(
        self, email: str, now: Optional[datetime] = None
    ) -> bool:
        """Resend verification or recover an expired password-setup link."""
        normalized_email = normalize_email(email)
        current_time = self._utc_now(now)
        token = secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT normalized_email, verification_sent_at,
                           email_verified, password_hash
                    FROM user_accounts
                    WHERE normalized_email = ?
                    """,
                    (normalized_email,),
                ).fetchone()
                if row is None:
                    return False
                if row[2] and row[3] is not None:
                    return False
                last_sent_at = (
                    datetime.fromisoformat(row[1]) if row[1] else None
                )
                if (
                    last_sent_at is not None
                    and current_time - last_sent_at < EMAIL_VERIFICATION_RESEND_DELAY
                ):
                    return False
                if row[2]:
                    connection.execute(
                        """
                        UPDATE user_accounts
                        SET password_setup_token_hash = ?,
                            password_setup_expires_at = ?,
                            verification_sent_at = ?
                        WHERE normalized_email = ?
                          AND email_verified = 1
                          AND password_hash IS NULL
                        """,
                        (
                            self._token_hash(token),
                            (current_time + PASSWORD_SETUP_TTL).isoformat(),
                            current_time.isoformat(),
                            normalized_email,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE user_accounts
                        SET verification_token_hash = ?,
                            verification_expires_at = ?,
                            verification_sent_at = ?
                        WHERE normalized_email = ? AND email_verified = 0
                        """,
                        (
                            self._token_hash(token),
                            (current_time + EMAIL_VERIFICATION_TTL).isoformat(),
                            current_time.isoformat(),
                            normalized_email,
                        ),
                    )

        self.verification_sender(row[0], token)
        return True

    def verify_email(
        self, token: str, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Verify email and return a short-lived, one-time password-setup token."""
        if not isinstance(token, str):
            raise TypeError("token must be a string")
        if not token:
            raise ValueError("token must not be empty")

        current_time = self._utc_now(now)
        password_setup_token = secrets.token_urlsafe(32)
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE user_accounts
                    SET email_verified = 1,
                        verification_token_hash = NULL,
                        verification_expires_at = NULL,
                        password_setup_token_hash = ?,
                        password_setup_expires_at = ?
                    WHERE verification_token_hash = ?
                      AND email_verified = 0
                      AND verification_expires_at > ?
                    """,
                    (
                        self._token_hash(password_setup_token),
                        (current_time + PASSWORD_SETUP_TTL).isoformat(),
                        self._token_hash(token),
                        current_time.isoformat(),
                    ),
                )
        return password_setup_token if cursor.rowcount == 1 else None

    def is_email_verified(self, user_id: str) -> bool:
        """Return whether the stored account has completed email verification."""
        self._validate_user_id(user_id)
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT email_verified FROM user_accounts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row is not None and bool(row[0])

    def set_password(
        self,
        setup_token: str,
        password: str,
        now: Optional[datetime] = None,
    ) -> None:
        """Set an Argon2id password using a valid email-verification capability."""
        if not isinstance(setup_token, str):
            raise TypeError("setup_token must be a string")
        if not setup_token:
            raise ValueError("setup_token must not be empty")
        self._validate_password(password)
        current_time = self._utc_now(now)
        setup_token_hash = self._token_hash(setup_token)
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT user_id FROM user_accounts
                WHERE password_setup_token_hash = ?
                  AND email_verified = 1
                  AND password_setup_expires_at > ?
                """,
                (setup_token_hash, current_time.isoformat()),
            ).fetchone()
        if row is None:
            raise ValueError(
                "a valid, unexpired email-verification token is required"
            )
        password_hash = self._PASSWORD_HASHER.hash(password)
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE user_accounts
                    SET password_hash = ?,
                        password_setup_token_hash = NULL,
                        password_setup_expires_at = NULL
                    WHERE user_id = ?
                      AND password_setup_token_hash = ?
                      AND password_setup_expires_at > ?
                    """,
                    (
                        password_hash,
                        row[0],
                        setup_token_hash,
                        current_time.isoformat(),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "password setup token is invalid or has expired"
                    )

    def authenticate(
        self,
        email: str,
        password: str,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Return a verified user id or None; lock out after repeated failures."""
        normalized_email = normalize_email(email)
        if not isinstance(password, str):
            raise TypeError("password must be a string")
        current_time = self._utc_now(now)
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT user_id, password_hash
                    FROM user_accounts
                    WHERE normalized_email = ? AND email_verified = 1
                    """,
                    (normalized_email,),
                ).fetchone()
                if row is None or row[1] is None:
                    return None

                attempts = connection.execute(
                    """
                    SELECT failed_attempts, locked_until
                    FROM user_login_attempts
                    WHERE normalized_email = ?
                    """,
                    (normalized_email,),
                ).fetchone()
                if attempts is not None and attempts[1] is not None:
                    if datetime.fromisoformat(attempts[1]) > current_time:
                        return None
                    connection.execute(
                        "DELETE FROM user_login_attempts WHERE normalized_email = ?",
                        (normalized_email,),
                    )
                    attempts = None

                try:
                    authenticated = self._PASSWORD_HASHER.verify(
                        row[1], password
                    )
                except (VerificationError, InvalidHashError):
                    authenticated = False

                if authenticated:
                    connection.execute(
                        "DELETE FROM user_login_attempts WHERE normalized_email = ?",
                        (normalized_email,),
                    )
                    return row[0]

                failed_attempts = (
                    attempts[0] if attempts is not None else 0
                ) + 1
                locked_until = (
                    (current_time + LOGIN_LOCKOUT_DURATION).isoformat()
                    if failed_attempts >= MAX_LOGIN_ATTEMPTS
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO user_login_attempts (
                        normalized_email, failed_attempts, locked_until
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(normalized_email) DO UPDATE SET
                        failed_attempts = excluded.failed_attempts,
                        locked_until = excluded.locked_until
                    """,
                    (normalized_email, failed_attempts, locked_until),
                )
        return None

    def create_user(
        self,
        name: str,
        email: str,
        preferences: "UserPreferences",
    ) -> "User":
        """Build a domain User only after verification and password setup."""
        normalized_email = normalize_email(email)
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT user_id
                FROM user_accounts
                WHERE normalized_email = ?
                  AND email_verified = 1
                  AND password_hash IS NOT NULL
                """,
                (normalized_email,),
            ).fetchone()
        if row is None:
            raise ValueError(
                "email verification and password setup are required"
            )
        return User(name, normalized_email, preferences, user_id=row[0])


MIN_DURATION_HISTORY = 3
MAX_DURATION_HISTORY = 5


@dataclass(frozen=True)
class HistoryAdjustment:
    """Typed summary of completion history used to adapt a schedule."""

    buffer_multiplier: float
    late_ratio: float
    avg_rating: Optional[float]
    notes: str

    def __post_init__(self) -> None:
        if (isinstance(self.buffer_multiplier, bool)
                or not isinstance(self.buffer_multiplier, (int, float))):
            raise TypeError("buffer_multiplier must be a number")
        if self.buffer_multiplier <= 0:
            raise ValueError("buffer_multiplier must be positive")
        if (isinstance(self.late_ratio, bool)
                or not isinstance(self.late_ratio, (int, float))):
            raise TypeError("late_ratio must be a number")
        if not 0 <= self.late_ratio <= 1:
            raise ValueError("late_ratio must be between 0 and 1")
        if self.avg_rating is not None:
            if (isinstance(self.avg_rating, bool)
                    or not isinstance(self.avg_rating, (int, float))):
                raise TypeError("avg_rating must be a number or None")
            if not MIN_RATING <= self.avg_rating <= MAX_RATING:
                raise ValueError(
                    f"avg_rating must be between {MIN_RATING} and {MAX_RATING}"
                )
        if not isinstance(self.notes, str):
            raise TypeError("notes must be a string")


@dataclass
class StudyBlock:
    """
    A chunk of study time the AI schedules, and the unit a Planner is built from.

    Deliberately NOT an Event subclass: an Event is something already committed
    to the calendar, while a StudyBlock is a suggestion the AI made that the
    user can still edit, move, or reject before approving the planner.

    A block does, however, know which event it was made for: source_event_id
    holds that event's id. subject stays as well, but only as a display label -
    the id is the real link, and anything acting on the relationship uses it.

    Fields
    ------
    subject         - what to study, copied from the source event's title.
                      Display only: titles are neither unique nor stable, so
                      two courses can have a "Read chapter 4" and renaming the
                      event would silently orphan the block.
    start_time      - when the block begins
    end_time        - when the block ends
    source_event_id - event_id of the Event this block was created for. Required,
                      with no default, because a block that isn't for anything
                      can't be reconciled with the calendar - and a default of
                      "" would let that happen quietly at construction.
                      For a repeating event this is the OCCURRENCE id, not the
                      series id, so study time for one Tuesday's class is
                      distinguishable from the next Tuesday's. Occurrence ids
                      are stable across expansions, so the link still resolves
                      through Calendar.get_event() weeks later.
    completes_source - is this the block that FINISHES the source event, or just
                      one work session along the way? An assignment split over
                      four blocks has this True on the last one only, so
                      finishing an early block doesn't declare the whole
                      assignment done. Defaults False: whatever creates the
                      blocks has to say so deliberately, since guessing wrong
                      in this direction marks work done that isn't.
    is_complete     - has this block been done, False until marked
    is_hidden       - the block's source event was hidden, so this study time
                      is no longer something to do. Mirrors Event.is_hidden and
                      exists for the same reason: a block the user already
                      completed is part of what happened that week and can't
                      just be deleted out of the plan.
    hidden_at       - when it was hidden, or None. Same retention clock as
                      Event.hidden_at, and the same reset-on-unhide rule.
    block_id        - unique id, so Planner.edit() can find this specific block
    """

    subject: str
    start_time: datetime
    end_time: datetime
    source_event_id: str
    completes_source: bool = False
    is_complete: bool = False
    is_hidden: bool = False
    hidden_at: Optional[datetime] = None
    block_id: str = field(default_factory=new_id)

    def get_duration(self) -> timedelta:
        """
        How long the block lasts: end_time - start_time.

        Same idea as Event.get_duration(). It's written out again rather than
        inherited because StudyBlock isn't an Event - see the class docstring.
        """
        return self.end_time - self.start_time

    def mark_complete(self) -> bool:
        """
        Mark just this block done: set is_complete to True.

        Returns True if this call changed it, False if it was already complete -
        the same recursion guard as Event.mark_complete(), and for the same
        reason.

        Note what this does NOT do: it never touches the source event, even
        when completes_source is True. A StudyBlock holds an id, not the Event
        itself, and has no way to look one up. Callers that should trigger the
        propagation go through User.complete_study_block() instead; this method
        is the one-object half it's built from.
        """
        if self.is_complete:
            return False
        self.is_complete = True
        return True

    def hide(self, now: Optional[datetime] = None) -> bool:
        """
        Take this block out of the plan without deleting it: is_hidden = True,
        hidden_at = now. Same signature and same clock as Event.hide().

        Returns True if this call changed it. Called by
        Planner.hide_blocks_for_event() when the source event is hidden - a
        block is never hidden on its own, since the only reason for it to go
        is that the work it was scheduled for went.
        """
        if self.is_hidden:
            return False
        self.is_hidden = True
        self.hidden_at = now or datetime.now()
        return True

    def unhide(self) -> bool:
        """
        Put this block back in the plan: is_hidden = False, hidden_at = None.

        Returns True if this call changed it.

        Unlike hide(), this is NOT the automatic counterpart of the event's -
        unhiding an event does not bring its blocks back by default. A block
        is restored only where a planner still needs it, which
        Planner.needs_hidden_block() decides and User.unhide_event() applies.
        """
        if not self.is_hidden:
            return False
        self.is_hidden = False
        self.hidden_at = None
        return True

    def to_schedule_item(self) -> "ScheduleItem":
        """
        Describe this block as a row on the week view.

        Produces a ScheduleItem with kind STUDY, `subject` as the label,
        block_id as source_id, and is_movable True - a study block is a
        suggestion, and moving it is the main thing the user does to a plan.

        The counterpart to Event.to_schedule_item(); see that one for why the
        conversion lives on each class rather than in the view.
        """
        return ScheduleItem(
            label=self.subject,
            start_time=self.start_time,
            end_time=self.end_time,
            kind=ScheduleItemKind.STUDY,
            source_id=self.block_id,
            is_movable=True,
        )

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        """
        Has this block been hidden long enough to be purged?

        Same rule as Event.is_expired(): hidden, and hidden_at more than
        HIDDEN_RETENTION_DAYS before now.

        A block is hidden only while unfinished. Its planner may later become
        PAST, but purging the expired hidden block is still safe because it
        does not represent completed work.
        """
        if not self.is_hidden or self.hidden_at is None:
            return False
        if now is None:
            now = datetime.now(tz=self.hidden_at.tzinfo)
        return now - self.hidden_at > timedelta(days=HIDDEN_RETENTION_DAYS)


@dataclass
class UserPreferences:
    """
    The user's fixed constraints and habits - the rules the AI schedules around.

    Passed into AIAssistant.optimize_schedule() so generated study blocks land
    at times the user will realistically use.

    Holds HABITS ONLY, not events. Anything that occupies a specific span of
    time belongs on the Calendar, which is the single source of truth for it;
    what lives here is the recurring daily pattern that isn't an event at all.
    The two fields below are clock times, not datetimes, which is the test:
    "I sleep from 23:00" is a habit, "PSY 210 meets Tuesday 10am" is an event.
    (design note 3)

    Fields
    ------
    sleep_start / sleep_end - these are `time` (a clock time like 23:00), not
                              `datetime` (a specific date AND time), because the
                              pattern repeats daily rather than happening once.
                              Note the range normally crosses midnight, so
                              sleep_start > sleep_end numerically - any check
                              like "is this hour asleep?" has to handle wrapping.
    preferred_study_hours   - windows the user focuses best in, as (start, end)
                              pairs, e.g. [(09:00, 11:00), (19:00, 21:00)]
    max_study_hours_per_day - optional total cap across all generated blocks
                              on a calendar day
    """

    sleep_start: time
    sleep_end: time
    preferred_study_hours: list[tuple[time, time]] = field(default_factory=list)
    max_study_hours_per_day: Optional[timedelta] = None


@dataclass
class ScheduleItem:
    """
    One row on a planner's week view: a commitment or a study block, described
    in the one shape a calendar grid can draw.

    This exists so the user can see their whole week in the planner - classes,
    shifts and assignments alongside the study time the AI suggested - rather
    than study blocks floating with no context. Judging "is 4pm Tuesday a good
    time to study?" is impossible without seeing what's on either side of it.

    It is a SNAPSHOT, not storage. Nothing holds a list of these; they are
    built on request by User.get_week_view() and thrown away. That matters -
    the alternative is copying events into the planner, which is the same
    duplication design note 3 removed from UserPreferences, and it would go
    wrong the same way the moment an event was edited. The Calendar stays the
    only place an Event lives, and the view is a read of it.

    Being a snapshot also means nothing here can be edited usefully. Changing
    a field changes this throwaway copy and nothing else. Edits go back to the
    real object by source_id, through Planner.edit() for a block or the
    Calendar for an event.

    Fields
    ------
    label      - what to print on the block: an Event's title, a StudyBlock's
                 subject. The two classes name it differently, which is why
                 each converts itself rather than the view reaching in.
    start_time - the item's real start, NOT clipped to the week. A shift
                 running Saturday 23:00 to Sunday 03:00 reports both true
                 times in both weeks' views; the grid clamps it at the edge
                 and shows it running off. Clipping here would tell the user
                 their shift ends at midnight. (Calendar design note 1a)
    end_time   - same, the real end.
    kind       - COMMITMENT or STUDY. The visual difference the user needs
                 most: one is fixed, one is the AI's proposal.
    source_id  - event_id or block_id, depending on kind. The handle for
                 acting on the real object behind the row.
    is_movable - can the planner move it? is_flexible() for an event, always
                 True for a study block. A display hint, not permission: it
                 says which rows the user can drag, and matches the meaning
                 is_flexible() already has on Event.

    No get_duration(), deliberately, though both source classes have one.
    These items are for drawing, and a view that straddles a week boundary
    reports hours that belong to the neighbouring week too - so summing them
    to measure "how full is this week" double-counts. Calendar design note 1a
    has the arithmetic.
    """

    label: str
    start_time: datetime
    end_time: datetime
    kind: ScheduleItemKind
    source_id: str
    is_movable: bool


@dataclass
class PurgeResult:
    """
    What one run of User.purge_hidden() actually destroyed.

    Three counts, because the purge works on three different things and
    lumping them together would hide which one did the work - "4 records
    removed" doesn't say whether a series went or four skipped Tuesdays did.

    A small class rather than a tuple of three ints on purpose. Positional
    results are read wrong eventually, and the same reasoning as design note
    9 on AIAssistant applies: name the fields and a mistake is an error
    instead of a number in the wrong column.

    Fields
    ------
    events_destroyed    - stored Events removed from the Calendar, series and
                          one-offs alike
    blocks_destroyed    - StudyBlocks removed from planners
    overrides_destroyed - per-date OccurrenceOverrides dropped, i.e. single
                          skipped dates that outlived the retention window
    """

    events_destroyed: int = 0
    blocks_destroyed: int = 0
    overrides_destroyed: int = 0


# Design notes for review:

# Purge/lifecycle trigger notes:
#
#       Successful User.login() calls purge_hidden() and advances planner
#       lifecycle state. A scheduled maintenance job can take over once user
#       calendars and planners are persisted. Both are safe: the purge and
#       lifecycle operations are idempotent.
#
#       Login-triggered cleanup is intentionally lazy: inactive users retain
#       hidden records longer than the 14-day minimum. A scheduled job requires
#       persistence for all user data, a system-wide user registry, a scheduler,
#       and operational logging. UserAccountStore alone persists credentials,
#       not calendars or planners.
#+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


class Calendar:
    """
    Holds everything a user has committed to - one mixed list of Events.

    This is the "what's already booked" side of the app. It is deliberately
    dumb storage: it adds, hides and looks up events, and does no scheduling
    or decision-making. All the thinking lives in AIAssistant, which READS a
    Calendar to find the gaps and WRITES its suggestions into a Planner.

    Because every item is an Event subclass, one list can hold classes, shifts,
    meetings and assignments together, and any of them can be asked for its
    duration without the Calendar knowing which type it actually is.

    Each User owns exactly one Calendar, created in User.__init__.

    Attributes
    ----------
    calendar_id - unique id, auto-generated
    events      - the list of Events, in the order they were added (not
                  sorted). Holds HIDDEN events too - "deleting" an event only
                  flags it (design note 2c on StudyBlock), so this list is
                  everything the user has ever had, not what they'd see. That
                  is why get_events() filters rather than just returning it.
    """

    def __init__(self):
        """Start an empty calendar: assign calendar_id via new_id(), events = []."""
        self.calendar_id = new_id()
        self.events: list[Event] = []

    def add_event(self, event: Event) -> None:
        """
        Append an event unless its id is already stored.

        Accepts any Event subclass. Raises ValueError for an id already in
        the calendar, including a hidden event; callers should use
        unhide_event() to restore that event rather than add another copy.
        Time overlaps are allowed here because they are not necessarily
        errors and are a separate conflict-checking concern.
        """
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        if parse_occurrence_id(event.event_id) is not None:
            raise ValueError("generated occurrences cannot be added to a calendar")
        if any(existing.event_id == event.event_id for existing in self.events):
            raise ValueError(f"event id {event.event_id!r} is already in this calendar")
        self.events.append(event)

    def check_conflicts(self, event: Event,
                        window_start: Optional[datetime] = None,
                        window_end: Optional[datetime] = None) -> list[Event]:
        """
        Return visible calendar events that overlap this event's time interval.

        This reports conflicts without blocking insertion; overlapping events
        may be intentional. The conflict-checking logic should query
        get_events(start, end), not the stored list, so recurring events are
        expanded and hidden events are excluded by default. Keep the half-open
        overlap test via overlaps().

        The event being checked is excluded, including generated occurrences
        from the same recurring series. For a recurring candidate, check each
        occurrence in the optional planning window separately. Without explicit
        bounds, only the candidate's first occurrence is checked.
        """
        if not isinstance(event, Event):
            raise TypeError("event must be an Event")
        if (window_start is None) != (window_end is None):
            raise ValueError("provide both planning window bounds or neither")
        if window_start is not None and window_end is not None:
            if window_start >= window_end:
                raise ValueError("window_start must be earlier than window_end")
        if event.is_recurring():
            candidates = event.expand(
                window_start if window_start is not None else event.start_time,
                window_end if window_end is not None else event.end_time,
            )
        else:
            candidates = [event]
        conflicts: list[Event] = []
        for candidate in candidates:
            existing_events = self.get_events(
                candidate.start_time, candidate.end_time
            )
            for existing in existing_events:
                existing_series_id = parse_occurrence_id(existing.event_id)
                existing_series_id = (
                    existing_series_id[0] if existing_series_id else existing.event_id
                )
                candidate_series_id = parse_occurrence_id(candidate.event_id)
                candidate_series_id = (
                    candidate_series_id[0] if candidate_series_id
                    else candidate.event_id
                )
                if (existing_series_id != candidate_series_id
                        and overlaps(
                            existing.start_time, existing.end_time,
                            candidate.start_time, candidate.end_time
                        )):
                    conflicts.append(existing)
        return conflicts

    def hide_event(self, event_id: str,
                   scope: RecurrenceScope = RecurrenceScope.THIS_OCCURRENCE,
                   now: Optional[datetime] = None) -> bool:
        """
        The user's "delete": find the event with this id and hide it.

        scope only matters when the id names an occurrence of a repeating
        event, and decides which of the two things the user meant:
          THIS_OCCURRENCE - hide that date alone, via
                            Event.hide_occurrence(). The rest of the term is
                            untouched. "I'm skipping Tuesday."
          ENTIRE_SERIES   - hide the series, via Event.hide(). Every date
                            goes, past and future. "I dropped the course."
        For a plain non-recurring event both do the same thing and scope is
        ignored.

        The default is THIS_OCCURRENCE because it is the recoverable mistake.
        Hiding one lecture when the user meant the course is a nuisance;
        hiding a whole term's classes when they meant one Tuesday cascades
        into their study blocks. The UI should still ask outright rather than
        lean on the default - that's what RecurrenceScope is for.

        Replaces the remove_event() this class used to have. Nothing is taken
        out of the events list here - past planners refer to events by id, so
        destroying one would leave a finished week pointing at nothing (design
        note 2c on StudyBlock). The event stays and is_hidden becomes True,
        which drops it out of get_events() and so out of everything the AI
        plans around. purge_hidden_events() is the only method that genuinely
        deletes, and only once the retention window has passed with nothing
        referring to the event.

        now is forwarded to Event.hide() to timestamp the hiding, and so
        decides when the event expires.

        Takes an id rather than the object itself, so the caller (a UI button,
        say) only has to keep track of a short string.

        Returns True if this call hid it, False if there was no such event OR
        it was already hidden - both mean "nothing changed here", which is what
        the caller needs to know before cascading.

        Hides the event ONLY. Its study blocks live in the user's planners,
        which a Calendar can't see, so callers use User.hide_event() rather
        than this.

        NOTE for the UML: the diagram has Calendar.remove_event(). This method
        replaces it, and the diagram needs updating to match.
        """
        if not isinstance(scope, RecurrenceScope):
            raise TypeError("scope must be a RecurrenceScope")
        parsed = parse_occurrence_id(event_id)
        if parsed is not None:
            event = next(
                (item for item in self.events if item.event_id == parsed[0]),
                None,
            )
            if event is None or not event.is_recurring():
                return False
            if scope is RecurrenceScope.THIS_OCCURRENCE:
                return event.hide_occurrence(event_id, now)
            return event.hide(now)
        event = next((item for item in self.events if item.event_id == event_id), None)
        return event.hide(now) if event is not None else False

    def unhide_event(self, event_id: str,
                     scope: RecurrenceScope = RecurrenceScope.THIS_OCCURRENCE
                     ) -> bool:
        """
        Bring a hidden event back: find it and call unhide() on it.

        The event has to be findable while hidden for this to be possible at
        all, which is why get_event() includes hidden events by default - the
        user searching their hidden items is the expected route in.

        scope mirrors hide_event(): THIS_OCCURRENCE restores one date through
        Event.unhide_occurrence(), ENTIRE_SERIES restores the series through
        Event.unhide().

        The two aren't quite inverses, and it's worth knowing which way it
        falls: restoring a series does NOT clear hidden_occurrence_ids, so
        individually skipped dates stay skipped. That's the better answer -
        "I dropped the course, then un-dropped it" shouldn't silently
        reinstate the three Tuesdays the user separately said they'd miss.

        Returns True if this call unhid it, False if there was no such event
        or it wasn't hidden.

        Restores the event ONLY, and unlike hide_event() there is no matching
        cascade: the blocks stay hidden. User.unhide_event() handles the one
        exception. See design note 2c on StudyBlock for why the two
        directions aren't symmetric.
        """
        if not isinstance(scope, RecurrenceScope):
            raise TypeError("scope must be a RecurrenceScope")
        parsed = parse_occurrence_id(event_id)
        if parsed is not None:
            event = next(
                (item for item in self.events if item.event_id == parsed[0]),
                None,
            )
            if event is None or not event.is_recurring():
                return False
            if scope is RecurrenceScope.THIS_OCCURRENCE:
                return event.unhide_occurrence(event_id)
            return event.unhide()
        event = next((item for item in self.events if item.event_id == event_id), None)
        return event.unhide() if event is not None else False

    def edit_occurrence(self, occurrence_id_: str,
                        start_time: Optional[datetime] = None,
                        end_time: Optional[datetime] = None,
                        title: Optional[str] = None) -> bool:
        """
        Change one date of a repeating event, given that date's id.

        The Calendar-level way in to Event.edit_occurrence(), and the one a
        UI can actually use: get_event() hands back a generated occurrence,
        which is a throwaway copy, so editing the object it returns changes
        nothing. The edit has to reach the series, and only the Calendar can
        find that from an occurrence id.

        Resolves the series with parse_occurrence_id(), then delegates.
        Returns False if the id isn't an occurrence id, names no series this
        calendar holds, or nothing changed.
        """
        parsed = parse_occurrence_id(occurrence_id_)
        if parsed is None:
            return False
        event = next(
            (item for item in self.events if item.event_id == parsed[0]),
            None,
        )
        if event is None:
            return False
        return event.edit_occurrence(
            occurrence_id_, start_time=start_time, end_time=end_time, title=title
        )

    def purge_expired_overrides(self, keep_occurrence_ids: set[str],
                                now: Optional[datetime] = None) -> int:
        """
        Sweep expired per-date overrides across every event on the calendar.

        Calls Event.purge_expired_overrides() on each and totals the result;
        see that method for what qualifies. Returns how many were removed.

        Separate from purge_hidden_events() because the two protect against
        different things - one is guarded by series ids, the other by
        occurrence ids - and because an override can expire on a series that
        is alive and well. Most of the time this is the only one of the two
        with anything to do.
        """
        return sum(
            event.purge_expired_overrides(keep_occurrence_ids, now)
            for event in self.events
        )

    def purge_hidden_events(self, keep_event_ids: set[str],
                            now: Optional[datetime] = None) -> int:
        """
        Permanently remove expired hidden events. The only method in this app
        that actually destroys anything.

        Removes an event from the events list only when BOTH hold:
          - event.is_expired(now) - hidden for more than HIDDEN_RETENTION_DAYS
          - its event_id is not in keep_event_ids

        Only ever destroys stored events - series and one-offs. Occurrences
        were never stored, so there is nothing of them to purge; a series
        going takes its dates with it, which is why keep_event_ids must
        already have had occurrence ids reduced to their series ids.

        keep_event_ids is the set of event ids that surviving study blocks
        still point at, collected by the caller from every planner. It is a
        required argument with no default on purpose: a default would let
        someone call this with no protection list and destroy the events a
        past planner depends on, which is the one outcome this design exists
        to prevent. Making it explicit forces the caller to have gathered it.

        Returns how many events were destroyed.

        Implementation shape is the old remove_event(): rebuild the list
        without the doomed events and compare lengths, rather than deleting
        while iterating.

        This is a Calendar-level method, so it cannot gather keep_event_ids
        itself - the blocks are in the planners. User.purge_hidden() is the
        caller that has both.
        """
        original_count = len(self.events)
        self.events = [
            event for event in self.events
            if not (event.event_id not in keep_event_ids and event.is_expired(now))
        ]
        return original_count - len(self.events)

    def get_event(self, event_id: str, include_hidden: bool = True) -> Optional[Event]:
        """
        Find one event by its id, or None if this calendar hasn't got it.

        Added because StudyBlock.source_event_id is only an id - something has
        to turn it back into the actual Event. User.complete_study_block() is
        one caller; anything displaying a past planner is another.

        Resolves OCCURRENCE ids as well as stored ones, and that is what keeps
        a study block's link working now that recurring events are expanded
        rather than stored. Given an id, parse_occurrence_id() says which kind
        it is:
          - a plain id: search the events list, as before.
          - an occurrence id: find the series it names, then rebuild that one
            date from it. Nothing was stored, so nothing needs to have been
            kept - the id carries the date, and the series carries everything
            else.
        Either way the caller gets an Event and doesn't have to care which.

        A rebuilt occurrence comes back even when that date is in the series'
        hidden_occurrence_ids, for the same reason hidden events do: a block
        in a past planner points at it, and the history has to stay readable.

        include_hidden defaults to TRUE here, the opposite of get_events().
        The difference is deliberate: asking for one event by id means you
        already have a specific reference to it - usually from a block in a
        past planner - and hiding it from that lookup is what would break the
        history this design is trying to protect. Browsing, on the other hand,
        should not turn up things the user got rid of. Pass False when you
        want the lookup to respect hiding.

        Returns None rather than raising, so "not found" is a case the caller
        handles rather than an error.
        """
        parsed = parse_occurrence_id(event_id)
        if parsed is None:
            event = next(
                (item for item in self.events if item.event_id == event_id),
                None,
            )
            if event is None or (event.is_hidden and not include_hidden):
                return None
            return event
        series_id, canonical_start = parsed
        series = next(
            (item for item in self.events if item.event_id == series_id),
            None,
        )
        if series is None or not series._is_recurrence_start(canonical_start):
            return None
        return series._make_occurrence(canonical_start, include_hidden)

    def get_events(self, start: Optional[datetime] = None,
                   end: Optional[datetime] = None,
                   include_hidden: bool = False) -> list[Event]:
        """
        Look up events, optionally limited to a time window.

        The window is HALF-OPEN, [start, end), and membership is OVERLAP, not
        containment: keep an event if overlaps(event.start_time,
        event.end_time, start, end) - it began before the window ended and
        finished after the window began. An event straddling a boundary is
        therefore returned by BOTH adjacent windows, which is correct, since
        both weeks really do have those hours occupied. Use the helper rather
        than rewriting the comparison; the strictness of each side is what
        makes the boundary behave.

        Callers measuring how full a week is must then clip with
        clipped_duration() instead of reading Event.get_duration(), or a
        boundary-straddling event is counted in full by both weeks. See
        design note 1 below.

        Both bounds are Optional and independent, giving four behaviors:
          - neither: every event
          - start only: everything from that point on
          - end only: everything up to that point
          - both: everything overlapping the window

        An open-ended recurring series cannot be completely enumerated by a
        start-only query, so that case raises ValueError and requires an end
        bound. End-only queries are finite because a series has a first date.

        EXPANDS recurring events. When a window is given, a series in that
        window is replaced by one Event per date it produces - so a weekly
        class returns the single occurrence that falls in the week asked for,
        and the caller sees a normal list of events with no idea any of them
        were generated. This is what stops the AI planning next week around
        an empty calendar (design note 1 on the Event hierarchy).

        With NO window, series come back unexpanded, as themselves. An
        open-ended series has no last date, so there is nothing else to
        return; a caller wanting occurrences has to say when.

        Hidden events are left out unless include_hidden is True. This default
        is doing real work and shouldn't be flipped casually: it's what keeps
        hiding meaningful. For a series it applies at both levels - a hidden
        series contributes nothing, and a series that is visible still skips
        its individually hidden dates. Every caller that browses the calendar - the UI, and
        AIAssistant.analyze_calendar() pulling the upcoming week - goes through
        here, so getting the default right means none of them has to remember
        the rule, and the AI can't schedule study time around an event the
        user got rid of. include_hidden=True is for history and debugging.

        Note this filters events, not blocks. A past planner's blocks are held
        by the Planner, so nothing here can hide that week's record.

        With no bounds it should return a COPY of the list, not the list
        itself - handing back the real one would let a caller modify the
        calendar's contents by accident.

        Expansion and the window filter interact, and the order matters: a
        series has to be expanded first and its occurrences tested
        individually. Testing the series itself would ask whether the FIRST
        date overlaps the window, which for a weekly class in week ten is
        always false.
        """
        if start is not None and end is not None and start >= end:
            raise ValueError("start must be earlier than end")
        if start is None and end is None:
            return [
                event for event in self.events
                if include_hidden or not event.is_hidden
            ]

        result: list[Event] = []
        for event in self.events:
            recurrence = event.recurrence
            if recurrence is None:
                if not include_hidden and event.is_hidden:
                    continue
                if start is not None and event.end_time <= start:
                    continue
                if end is not None and event.start_time >= end:
                    continue
                result.append(event)
                continue

            if start is None:
                # The series has a finite beginning, so an end-only query is
                # still a bounded expansion.
                if end is None:
                    raise ValueError("an end bound is required for this query")
                possible_starts = [event.start_time - event.get_duration()]
                possible_starts.extend(
                    override.start_time
                    for override in event.occurrence_overrides.values()
                    if override.start_time is not None
                )
                window_start = min(possible_starts)
                window_end = end
            elif end is None:
                if recurrence.until is None:
                    raise ValueError(
                        "an end bound is required to expand an open-ended series"
                    )
                window_start = start
                window_end = (
                    recurrence.until + timedelta(days=1)
                    + event.get_duration() + timedelta(microseconds=1)
                )
                for override in event.occurrence_overrides.values():
                    if override.end_time is not None:
                        window_end = max(window_end, override.end_time)
            else:
                window_start, window_end = start, end
            result.extend(event.expand(
                window_start, window_end, include_hidden=include_hidden
            ))
        return result

    def get_events_by_type(self, event_type: type,
                           include_hidden: bool = False) -> list[Event]:
        """
        Every event of one kind, e.g. get_events_by_type(ClassSession) for the
        user's classes.

        Added when UserPreferences.class_schedule was removed and the Calendar
        became the source of truth for classes (design note 3). That list used
        to answer "what are my classes?"; this is how the question gets
        answered now, from the one place the classes actually live.

        event_type is an Event subclass, matched with isinstance - so it
        includes subclasses of whatever is passed, which is the behavior you
        want if the hierarchy ever grows.

        Hidden events are excluded unless include_hidden is True, same default
        and same reasoning as get_events(). Hiding a class the user dropped
        takes it out of the AI's planning with nothing else to update.

        One caution: this is for "show me the classes" questions, not for
        scheduling ones. Code deciding what the planner may move should ask
        is_flexible() rather than naming types - see the note on Event about
        why that distinction lives on the events themselves.
        """
        if not isinstance(event_type, type) or not issubclass(event_type, Event):
            raise TypeError("event_type must be an Event subclass")
        return [
            event for event in self.events
            if isinstance(event, event_type)
            and (include_hidden or not event.is_hidden)
        ]

# Design notes to keep for later:
#
# 1. Calendar.events is specified to keep insertion order, so callers needing
#    chronological order sort their results; User.get_week_view() does this.
#    Hidden events remain stored until purged, but purge_hidden_events() can
#    remove expired, unreferenced events, so the list can shrink as well as
#    grow. Events referenced by surviving visible study blocks are protected
#    from purging. Linear scans may be fine at this scale; if performance
#    becomes an issue, consider an id index for get_event(). A time-window
#    index is more involved because recurring events expand per query and
#    hidden state, overrides, edits, and purges affect the results.


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class Planner:
    """
    One week's study plan: a set of StudyBlocks plus a status.

    Where Calendar holds what the user has ALREADY committed to, a Planner
    holds what the AI SUGGESTS they do with the time left over. Keeping the two
    apart is what makes a draft safe to throw away and regenerate - rejecting a
    plan never touches the real calendar.

    STORES suggestions, DISPLAYS the whole week. The user sees their classes,
    shifts and assignments on the planner alongside the study blocks - a
    suggested 4pm Tuesday session means nothing without the class that ends at
    3:45 next to it. That view is assembled on demand from the Calendar by
    User.get_week_view() and held by nobody; study_blocks below is still the
    only thing a Planner owns. The separation above is about storage, and it
    survives showing the two together.

    A planner moves through the PlannerStatus lifecycle:
        DRAFT     - freshly generated; may be approved during its week, then
                    discarded if the week ends without approval
        APPROVED  - the user accepted it during its week; this is the live plan
        PAST      - an approved week's plan after the week ends; read-only history,
                    fed back to the AI through its CompletionRecords. System
                    retention cleanup may still purge expired hidden blocks.

    Created by AIAssistant.generate_planner() and stored on the User via
    User.add_planner(), so a user builds up one planner per week over time.

    Attributes
    ----------
    planner_id      - unique id, auto-generated; how User.edit_planner finds it
    week_start   - the instant the week begins, INCLUSIVE. For a Sunday
                   through Saturday week, Sunday 00:00.
    week_end     - the instant the week ends, EXCLUSIVE: the FOLLOWING
                   Sunday 00:00, not Saturday's. The week is the half-open
                   range [week_start, week_end), matching every other window
                   in the app - see overlaps() and Calendar design note 1b.
                   Saturday 00:00 here would silently drop everything after
                   midnight on the last day, an 11pm shift included.
                   Named week_end rather than week_end_date because it is an
                   instant, not a day, and the old name read like the last
                   day covered - which is exactly the value that breaks it.
    study_blocks - the AI's suggestions for this week
    status       - where it is in the lifecycle; starts at DRAFT
    """

    def __init__(self, week_start: datetime, week_end: datetime):
        """
        Set up an empty draft planner for the given week.

        Raises ValueError unless week_start is earlier than week_end.

        Assigns planner_id via new_id(), stores the two dates, starts
        study_blocks as an empty list, and sets status to DRAFT - blocks are
        added afterwards by generate_planner() rather than passed in here.
        """
        if not isinstance(week_start, datetime) or not isinstance(week_end, datetime):
            raise TypeError("week_start and week_end must be datetimes")
        if week_start >= week_end:
            raise ValueError("week_end must be later than week_start")

        self.planner_id = new_id()
        self.week_start = week_start
        self.week_end = week_end
        self.study_blocks: list[StudyBlock] = []
        self.assignment_placements: list[AssignmentPlacement] = []
        self.assignment_duration_reviews: list[AssignmentDurationReview] = []
        self.status = PlannerStatus.DRAFT

    def get_assignment_placement(
            self, event_id: str
    ) -> Optional[AssignmentPlacement]:
        """Find this week's assignment scheduling summary by event id."""
        return next(
            (placement for placement in self.assignment_placements
             if placement.event_id == event_id),
            None,
        )

    def get_assignment_duration_review(
            self, event_id: str
    ) -> Optional[AssignmentDurationReview]:
        """Find a generated duration recommendation by assignment id."""
        return next(
            (review for review in self.assignment_duration_reviews
             if review.event_id == event_id),
            None,
        )

    def _validate_block_times(self, start_time: datetime, end_time: datetime,
                              exclude_block_id: Optional[str] = None) -> None:
        """
        Validate a proposed block interval against this planner and its blocks.

        The interval must have start_time before end_time and fit entirely
        within [week_start, week_end). It must not overlap another visible
        study block, using half-open interval overlap; exclude_block_id
        identifies the existing block being edited so it is not compared
        with itself.

        Raises TypeError for non-datetime bounds and ValueError for an invalid
        interval, one outside this planner's week, or an overlap.
        """
        if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
            raise TypeError("start_time and end_time must be datetimes")
        if end_time <= start_time:
            raise ValueError("end_time must be later than start_time")
        if start_time < self.week_start or end_time > self.week_end:
            raise ValueError("study block must fit within this planner's week")

        for block in self.study_blocks:
            if block.block_id == exclude_block_id or block.is_hidden:
                continue
            if overlaps(start_time, end_time, block.start_time, block.end_time):
                raise ValueError(
                    f"study block overlaps existing block {block.block_id}"
                )

    def add_study_block(self, block: StudyBlock) -> None:
        """
        Append one StudyBlock after validating its time interval.

        Raises ValueError if this planner is PAST.

        Rejects blocks whose start_time is not before end_time, that fall
        outside [week_start, week_end), or that overlap another visible block
        already in this planner. Hidden blocks do not occupy schedule time. A
        block cannot be moved to another week through this method; cross-week
        changes must be coordinated explicitly by User after the corresponding
        Calendar change.
        """
        if self.status is PlannerStatus.PAST:
            raise ValueError("cannot add a study block to a past planner")
        if not isinstance(block, StudyBlock):
            raise TypeError("block must be a StudyBlock")
        if any(existing.block_id == block.block_id for existing in self.study_blocks):
            raise ValueError(f"study block id already exists: {block.block_id}")

        self._validate_block_times(block.start_time, block.end_time)
        self.study_blocks.append(block)

    def edit(self, block_id: str, **updates) -> int:
        """
        Change fields on one study block, found by its id.

        Raises ValueError if this planner is PAST.

        **updates means the caller passes any number of named arguments and
        they arrive as a dict, so a single method covers every field:
            planner.edit(some_id, subject="Chemistry")
            planner.edit(some_id, start_time=..., end_time=...)

            Only subject, start_time, and end_time are editable.
            Unknown or non-editable field names raise ValueError; block_id is never
        editable. Validate the complete proposed interval before applying any
        updates: start_time must precede end_time, the interval must stay
        within [week_start, week_end), and it must not overlap another block.
        These checks also apply when only one time bound is updated. A rejected
        edit leaves every field unchanged. A block cannot be moved to another
        week through this method; cross-week changes must be coordinated
        explicitly by User after the corresponding Calendar change.

        Returns the number of fields whose values changed, or 0 if no block
        with that id exists or every supplied value was already set.
        """
        if self.status is PlannerStatus.PAST:
            raise ValueError("cannot edit a past planner")

        editable_fields = {"subject", "start_time", "end_time"}
        unknown_fields = updates.keys() - editable_fields
        if unknown_fields:
            names = ", ".join(sorted(unknown_fields))
            raise ValueError(f"unknown or non-editable study block field(s): {names}")

        block = self.get_block(block_id)
        if block is None:
            return 0
        if "subject" in updates and not isinstance(updates["subject"], str):
            raise TypeError("subject must be a string")

        start_time = updates.get("start_time", block.start_time)
        end_time = updates.get("end_time", block.end_time)
        self._validate_block_times(start_time, end_time, exclude_block_id=block_id)

        changed = 0
        for name, value in updates.items():
            if getattr(block, name) != value:
                setattr(block, name, value)
                changed += 1
        return changed

    def get_block(self, block_id: str) -> Optional[StudyBlock]:
        """
        Find one study block by its id, or None if this planner hasn't got it.

        Same shape as Calendar.get_event(), and used the same way: the caller
        has an id and needs the object. edit() already does this lookup inline;
        having it as a method means complete_study_block() isn't a third copy.
        """
        for block in self.study_blocks:
            if block.block_id == block_id:
                return block
        return None

    @staticmethod
    def _block_matches_event(block: StudyBlock, event_id: str) -> bool:
        """Match an event id or, for a series id, one of its occurrence ids."""
        if block.source_event_id == event_id:
            return True
        parsed_occurrence = parse_occurrence_id(block.source_event_id)
        return parsed_occurrence is not None and parsed_occurrence[0] == event_id

    def get_blocks_for_event(self, event_id: str,
                             include_hidden: bool = False) -> list[StudyBlock]:
        """
        Every block in this week's plan made for one particular event.

        Hidden blocks are left out unless include_hidden is True, matching
        Calendar.get_events(). Completing an assignment shouldn't tick off
        study time the user already got rid of; a history view wants the lot.

        Matches on StudyBlock.source_event_id, never on subject - two courses
        can have an identically titled task, and matching text would complete
        the wrong one. Passing a series id matches all its occurrence ids;
        passing one occurrence id matches only that date.

        This is the "assignment -> its blocks" direction of the link. One
        planner is only one week, though, and work on an assignment can span
        several, so User.complete_assignment() calls this across every planner
        rather than trusting a single week to hold them all.

        Returns an empty list when none match, which is normal: most of a
        user's events never had study blocks scheduled for them.
        """
        return [
            block for block in self.study_blocks
            if self._block_matches_event(block, event_id)
            and (include_hidden or not block.is_hidden)
        ]

    def hide_blocks_for_event(self, event_id: str,
                              now: Optional[datetime] = None) -> int:
        """
        Hide this week's blocks for one event - the ones that are still future
        work. Called when the event itself is hidden.

        now is forwarded to StudyBlock.hide() to timestamp each one.

        Matched on source_event_id, never on subject, same as
        get_blocks_for_event(). Passing a series id hides blocks for all its
        occurrences.

        Two kinds of block are deliberately left alone, which is the whole
        point of this method rather than a plain sweep (design note 2c on
        StudyBlock):

          - every block, if this planner's status is PAST. That week already
            happened; the blocks were really on the user's plan, and editing
            them now would make the history a lie. A PAST planner returns 0
            without touching anything.
          - any block already marked complete, in a live planner. The user did
            that work. The assignment going away doesn't un-spend the time.

        What's left - the unfinished blocks in a draft or approved week - is
        study time for something that no longer exists, so it gets hidden.

        Returns how many blocks this call actually hid, so User.hide_event()
        can report what really changed. 0 is a normal answer: most events
        never had blocks, and a past planner always answers 0.
        """
        if self.status is PlannerStatus.PAST:
            return 0

        hidden_count = 0
        for block in self.study_blocks:
            if (
                self._block_matches_event(block, event_id)
                and not block.is_complete
                and block.hide(now)
            ):
                hidden_count += 1
        return hidden_count

    def get_schedule_items(self) -> list[ScheduleItem]:
        """
        This planner's own study blocks as week-view rows.

        Converts each visible block with StudyBlock.to_schedule_item(), and
        skips hidden ones - a block whose event the user deleted shouldn't
        reappear on the grid.

        Half of the week view. User.get_week_view() gets the other half from
        the Calendar and merges the two; a Planner can't do that itself,
        because it has no way to reach the Calendar.

        Not sorted here - merging with the events resorts everything anyway.
        """
        return [
            block.to_schedule_item()
            for block in self.study_blocks
            if not block.is_hidden
        ]

    def needs_hidden_block(self, block: StudyBlock,
                           now: Optional[datetime] = None) -> bool:
        """
        Would this planner still use this hidden block if its event came back?

        The test that makes unhiding asymmetric with hiding. Hiding an event
        hides its future study time; unhiding the event does NOT hand that
        time back wholesale, because by then it is usually study time at hours
        that have already gone by. Only a block the plan could still act on
        comes back.

        True when all of:
          - the block is hidden (a visible block was never taken away)
          - this planner's status is DRAFT or APPROVED. A PAST planner never
            needs anything restored; nothing in it was hidden to begin with.
          - block.end_time is still in the future relative to now. A slot that
            has already passed is not a plan, it is a gap in the record, and
            reinstating it would put stale sessions into a live week alongside
            whatever the AI scheduled in the meantime.

        Everything failing that stays hidden and eventually expires. The event
        itself comes back as work to be planned, and the AI schedules fresh
        blocks for it on the next run - which is the behavior that actually
        serves the user, rather than resurrecting an old plan.

        See design note 2c on StudyBlock.
        """
        if self.status is PlannerStatus.PAST or not block.is_hidden:
            return False

        if now is None:
            now = datetime.now(tz=block.end_time.tzinfo)
        return block.end_time > now

    def restore_blocks_for_event(self, event_id: str,
                                 now: Optional[datetime] = None) -> int:
        """
        Unhide the blocks for one event that this planner still needs -
        the partial counterpart to hide_blocks_for_event().

        Walks this planner's hidden blocks for the event and unhides exactly
        those where needs_hidden_block() is True and their interval remains
        valid and non-overlapping. Blocks that no longer fit remain hidden so
        restoring them cannot create an invalid schedule. Deliberately partial:
        in the common case it restores nothing at all, and that is the intended
        outcome, not a failure.

        Returns how many blocks were restored.
        """
        if self.status is PlannerStatus.PAST:
            return 0

        restored_count = 0
        for block in self.study_blocks:
            if not self._block_matches_event(block, event_id):
                continue
            if not self.needs_hidden_block(block, now):
                continue
            try:
                self._validate_block_times(
                    block.start_time,
                    block.end_time,
                    exclude_block_id=block.block_id,
                )
            except ValueError:
                continue
            if block.unhide():
                restored_count += 1
        return restored_count

    def get_referenced_event_ids(self) -> set[str]:
        """
        The event ids this planner still depends on: the source_event_id of
        every block in it that is NOT hidden.

        This is the protection list for the purge. An event named here has
        surviving study time pointing at it - most importantly a completed
        block in a past week - so destroying the event would leave that record
        referring to nothing. User.purge_hidden() unions this across all
        planners and hands the result to Calendar.purge_hidden_events() as
        keep_event_ids.

        A block made for a recurring event holds an OCCURRENCE id, and the
        thing that would actually be destroyed is the series behind it. So an
        occurrence id has to be reduced to its series id with
        parse_occurrence_id() before going in the set - otherwise the set
        names ids the purge never compares against, the series looks
        unreferenced, and destroying it takes every occurrence a past planner
        pointed at with it.

        Hidden blocks are excluded on purpose. They are on their way out
        themselves, and counting them would keep an event alive on the
        strength of a reference that is also about to be destroyed - the two
        would prop each other up and neither would ever be purged.
        """
        referenced_ids: set[str] = set()
        for block in self.study_blocks:
            if block.is_hidden:
                continue
            parsed_occurrence = parse_occurrence_id(block.source_event_id)
            referenced_ids.add(
                parsed_occurrence[0]
                if parsed_occurrence is not None
                else block.source_event_id
            )
        return referenced_ids

    def get_referenced_occurrence_ids(self) -> set[str]:
        """
        The source_event_id of every visible block in this planner that names
        a specific date of a repeating event - occurrence ids, kept whole.

        The protection list for Calendar.purge_expired_overrides(), where
        get_referenced_event_ids() protects the events themselves. Two
        methods rather than one because the two purges compare against
        different things: destroying a series needs the series id, dropping
        one date's override needs that date's id, and the reduction that
        makes the first set usable is exactly what would make the second
        useless.

        Plain, non-occurrence ids are left out - they name no override.
        """
        return {
            block.source_event_id
            for block in self.study_blocks
            if not block.is_hidden
            and parse_occurrence_id(block.source_event_id) is not None
        }

    def purge_hidden_blocks(self, now: Optional[datetime] = None) -> int:
        """
        Permanently remove this planner's expired hidden blocks.

        Drops every block where is_expired(now) is True. No protection list is
        needed here, unlike the event purge: nothing in the system points AT a
        study block, and a hidden block is by construction an unfinished block
        when hidden. An approved planner may later become PAST while such a
        block is still within its retention window.

        This retention purge is the only automatic mutation allowed for a
        PAST planner; it does not restore or edit any block. Returns how many
        blocks were destroyed.

        Runs before the event purge in User.purge_hidden(), since removing
        these blocks may drop the last reference to an event and let it go in
        the same pass.
        """
        retained_blocks = [
            block for block in self.study_blocks
            if not block.is_expired(now)
        ]
        removed_count = len(self.study_blocks) - len(retained_blocks)
        self.study_blocks[:] = retained_blocks
        return removed_count

    def approve(self, now: Optional[datetime] = None) -> None:
        """
        Mark the plan accepted by the user: status becomes APPROVED.

        Approval is allowed only while status is DRAFT and now is within
        [week_start, week_end); now defaults to the current time. Raises
        ValueError otherwise. Every available duration recommendation must
        have an explicit decision before approval. An unapproved DRAFT is
        discarded by its User once its week has ended; it is never archived
        as PAST.
        """
        if self.status is not PlannerStatus.DRAFT:
            raise ValueError("only a draft planner can be approved")
        if now is None:
            now = datetime.now(tz=self.week_start.tzinfo)
        elif not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        if not self.week_start <= now < self.week_end:
            raise ValueError("a planner can only be approved during its week")
        if any(review.decision is None
               for review in self.assignment_duration_reviews):
            raise ValueError(
                "all assignment duration recommendations need a user decision "
                "before approval"
            )
        self.status = PlannerStatus.APPROVED

    def archive(self, now: Optional[datetime] = None) -> None:
        """
        Retire an approved plan once its week has passed: status becomes PAST.

        Raises ValueError unless status is APPROVED and week_end is at or
        before now. PAST planners are read-only and are returned by
        User.view_planner_history(); their CompletionRecords are what the AI
        learns from.
        """
        if self.status is not PlannerStatus.APPROVED:
            raise ValueError("only an approved planner can be archived")
        if now is None:
            now = datetime.now(tz=self.week_end.tzinfo)
        elif not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        if now < self.week_end:
            raise ValueError("a planner can only be archived after its week ends")
        self.status = PlannerStatus.PAST

    def __repr__(self) -> str:
        """
        Debug text form, e.g. <Planner 2026-09-14 status=draft blocks=6>.

        Shows the week, the status and the block COUNT rather than every block,
        so printing a list of planners stays readable.
        """
        return (
            f"<Planner {self.week_start.date().isoformat()} "
            f"status={self.status.value} blocks={len(self.study_blocks)}>"
        )


# Design notes for review:
#
# 1. Potential future change: add a user_id if planners ever need to be stored
#    or passed around independently of their User. Currently, User owns the
#    planner list and coordinates access to the user's Calendar, so a reverse
#    link is not needed.


# ---------------------------------------------------------------------------
# AIAssistant
# ---------------------------------------------------------------------------


class AIAssistant:
    """
    Generates and adapts weekly planners from a user's calendar and history.

    This is the brain of the app, and the only class that makes decisions.
    Everything else stores data; this turns it into a plan.

    The four methods form a pipeline, which generate_planner() runs end to end:

        Calendar ──analyze_calendar()──> events the week must work around
                                              │
        past CompletionRecords ──adapt_from_history()──> adjustment
                                              │
                    events + preferences + adjustment
                                              │
                                     optimize_schedule()
                                              │
                                      ScheduleResult ──> Planner (DRAFT)
                                      (blocks + assignment placement status)

    Splitting it into four steps rather than one big function means each piece
    can be tested on its own - you can hand optimize_schedule() a fixed set of
    events and check what comes back, with no calendar or history involved.

    The class holds no state of its own (there's no __init__), so one instance
    can serve any user; nothing is remembered between calls. Everything it
    needs arrives as arguments.
    """

    def analyze_calendar(self, calendar: Calendar, week_start: datetime,
                         week_end: datetime) -> list[Event]:
        """
        Pull visible events overlapping the planner's half-open week window.

        Calendar.get_events() expands recurring events for this window and
        includes commitments that overlap either week boundary.

        Whatever this ends up totalling, it measures with clipped_duration(),
        not Event.get_duration(). A shift crossing into the next week belongs
        to this one only for the hours inside it, and counting the whole span
        makes the week look fuller than it is - which this class then reads
        as the user over-scheduling. Calendar design note 1a.

        This is also the natural place to separate events by what they mean to
        the scheduler: a ClassSession or JobShift is immovable time to work
        AROUND, while an Assignment is work to schedule study time FOR. That
        distinction now lives on the events themselves - split the list on
        Event.is_flexible() rather than testing concrete types here, so an
        eighth Event subclass sorts itself without this method changing.
        """
        if not isinstance(calendar, Calendar):
            raise TypeError("calendar must be a Calendar")
        if not isinstance(week_start, datetime) or not isinstance(week_end, datetime):
            raise TypeError("week_start and week_end must be datetimes")
        if week_start >= week_end:
            raise ValueError("week_end must be later than week_start")
        return calendar.get_events(start=week_start, end=week_end)

    def adapt_from_history(
            self, records: list[CompletionRecord]
    ) -> HistoryAdjustment:
        """
        Turn past CompletionRecords into adjustments for the coming week.

        This is what makes the planner adaptive instead of static: if the user
        keeps running late, next week's blocks get more breathing room.

        The adjustment computes:
          late_ratio - the fraction not completed on time (0.0 to 1.0)
          avg_rating - the mean self-rating over the RATED records only, i.e.
                       those where is_rated() is true. Unrated records are
                       UNRATED (0), which is not a score, so counting them
                       would pull the average below the worst real rating.
                       If nothing is rated there is no average at all - fall
                       back to the neutral, no-complaint path rather than
                       inventing a number.

        and returns a HistoryAdjustment with:
          buffer_multiplier - 1.0 + late_ratio * 0.5, so a user who finishes
                              everything on time gets 1.0 (no padding) and one
                              who is always late gets 1.5 (50% more time)
          avg_rating        - None if no records have a valid rating
          late_ratio        - 0.0 if there are no records
          notes             - a short message based on late_ratio and avg_rating

        With no records it returns a neutral 1.0 multiplier, so a brand-new
        user still gets a sensible plan.

        """
        if any(not isinstance(record, CompletionRecord) for record in records):
            raise TypeError("records must contain CompletionRecord instances")
        if any(not isinstance(record.completed_on_time, bool) for record in records):
            raise TypeError("completed_on_time must be a boolean")

        if records:
            late_ratio = sum(not record.completed_on_time for record in records) / len(records)
        else:
            late_ratio = 0.0

        ratings = [
            record.performance_rating
            for record in records
            if record.is_rated()
        ]
        avg_rating = sum(ratings) / len(ratings) if ratings else None
        buffer_multiplier = 1.0 + late_ratio * 0.5

        notes: list[str] = []
        if late_ratio >= 0.5:
            notes.append("Recent work has often missed its deadline; study time includes extra buffer.")
        elif records and late_ratio == 0:
            notes.append("Recent work has been completed on time.")
        elif records:
            notes.append("Some recent work missed its deadline; study time includes a small buffer.")

        if avg_rating is not None:
            if avg_rating >= 4:
                notes.append("Your ratings suggest the recent workload has felt manageable.")
            elif avg_rating <= 2:
                notes.append("Your ratings suggest the recent workload has felt challenging.")
            else:
                notes.append("Your recent workload ratings are around the middle of the scale.")
        elif records:
            notes.append("No rated completion history is available yet.")
        else:
            notes.append("No completion history yet; using a neutral adjustment.")

        return HistoryAdjustment(
            buffer_multiplier=buffer_multiplier,
            late_ratio=late_ratio,
            avg_rating=avg_rating,
            notes=" ".join(notes),
        )

    def recommend_assignment_duration(
            self, assignment: Assignment,
            records: list[CompletionRecord]
    ) -> Optional[AssignmentDurationRecommendation]:
        """
        Suggest focused-work time from comparable completed assignments.

        Comparisons are tried in order: same course and category, same course,
        then same category across courses. A tier needs at least
        MIN_DURATION_HISTORY valid actual-duration records. Use at most the
        MAX_DURATION_HISTORY most recent matches, weighting newer records
        linearly more heavily. Return None when no tier has enough evidence.

        The recommendation is only a suggestion; this method never changes
        assignment.estimated_duration.
        """
        if not isinstance(assignment, Assignment):
            raise TypeError("assignment must be an Assignment")

        def normalize(value: str) -> str:
            return " ".join(value.casefold().split())

        course_key = normalize(assignment.course)
        category_key = normalize(assignment.category)
        if any(not isinstance(record, CompletionRecord) for record in records):
            raise TypeError("records must contain CompletionRecord instances")
        eligible: list[tuple[CompletionRecord, int]] = []
        for record in records:
            actual_minutes = record.actual_duration_minutes
            if (actual_minutes is None or actual_minutes <= 0
                    or not record.course.strip()
                    or not record.assignment_category.strip()):
                continue
            eligible.append((record, actual_minutes))

        match_tiers = (
            (
                lambda record: normalize(record.course) == course_key
                and normalize(record.assignment_category) == category_key,
                AssignmentMatchScope.COURSE_AND_CATEGORY,
            ),
            (
                lambda record: normalize(record.course) == course_key,
                AssignmentMatchScope.COURSE,
            ),
            (
                lambda record: normalize(record.assignment_category) == category_key,
                AssignmentMatchScope.CATEGORY,
            ),
        )

        for matches, match_scope in match_tiers:
            matching = [
                item for item in eligible if matches(item[0])
            ]
            if len(matching) < MIN_DURATION_HISTORY:
                continue

            recent = sorted(
                matching, key=lambda item: item[0].completed_at.timestamp()
            )[-MAX_DURATION_HISTORY:]
            weights = range(1, len(recent) + 1)
            weighted_minutes = sum(
                actual_minutes * weight
                for (_, actual_minutes), weight in zip(recent, weights)
            )
            total_weight = sum(weights)
            recommended_minutes = max(
                1, round(weighted_minutes / total_weight)
            )
            return AssignmentDurationRecommendation(
                duration=timedelta(minutes=recommended_minutes),
                sample_count=len(recent),
                match_scope=match_scope,
            )

        return None

    def record_assignment_completion(
            self, assignment: Assignment, actual_duration: timedelta,
            completion_store: CompletionRecordStore, user_id: str,
            completed_at: Optional[datetime] = None,
            performance_rating: int = UNRATED, notes: str = ""
    ) -> CompletionRecord:
        """Create and persist a completion record with the estimate snapshot."""
        if not isinstance(assignment, Assignment):
            raise TypeError("assignment must be an Assignment")
        if parse_occurrence_id(assignment.event_id) is not None:
            raise ValueError("generated occurrences cannot be completed")
        if assignment.completion_record is not None:
            raise ValueError("assignment already has a completion record")
        if not isinstance(actual_duration, timedelta):
            raise TypeError("actual_duration must be a timedelta")
        if actual_duration <= timedelta(0):
            raise ValueError("actual_duration must be positive")
        if not isinstance(completion_store, CompletionRecordStore):
            raise TypeError("completion_store must be a CompletionRecordStore")
        CompletionRecordStore._validate_user_id(user_id)
        if completed_at is None:
            completed_at = datetime.now(tz=assignment.due_date.tzinfo)
        elif not isinstance(completed_at, datetime):
            raise TypeError("completed_at must be a datetime")
        try:
            completed_on_time = completed_at <= assignment.due_date
        except TypeError as error:
            raise ValueError(
                "completed_at and assignment due_date must use compatible timezones"
            ) from error

        record = CompletionRecord(
            event_id=assignment.event_id,
            completed_on_time=completed_on_time,
            actual_duration_minutes=max(
                1, round(actual_duration.total_seconds() / 60)
            ),
            planned_duration_minutes=max(
                1, round(assignment.estimated_duration.total_seconds() / 60)
            ),
            course=assignment.course,
            assignment_category=assignment.category,
            completed_at=completed_at,
            performance_rating=performance_rating,
            notes=notes,
        )
        completion_store.add_record(user_id, record)
        assignment.attach_completion_record(record)
        assignment.mark_complete()
        return record

    def optimize_schedule(self, events: list[Event],
                          preferences: UserPreferences,
                          adjustment: HistoryAdjustment,
                          week_start: Optional[datetime] = None,
                          week_end: Optional[datetime] = None) -> ScheduleResult:
        """
        Decide when the study blocks actually go. The core scheduling step.

        For each incomplete, visible Assignment or PersonalTask, schedule its
        work estimate (minimum 30 minutes) scaled by buffer_multiplier.
        Assignments use estimated_duration, scheduled backward from due_date
        in sessions of up to two hours on distinct days, starting no earlier
        than the assignment's start date. A PersonalTask uses its event
        duration and gets one session before its start time.

        Search preferred study windows first, then other awake time. Candidate
        blocks may not overlap fixed calendar events or blocks already
        generated by this call. Flexible events are work to schedule, not
        occupied time. Intervals use half-open overlap, so adjacent blocks are
        allowed. Work that cannot fit is left unscheduled.

        Every generated block links to its source event. For an assignment
        split across sessions, only the chronologically last block is marked
        completes_source=True.

        Return ScheduleResult so the UI can show each assignment's original
        estimate, buffered planning target, scheduled and remaining time, and
        ordered next-step actions when work does not fit.

        When both optional week bounds are provided, generated blocks and
        candidate dates are limited to that half-open window.
        """
        if (week_start is None) != (week_end is None):
            raise ValueError("week_start and week_end must be supplied together")
        if week_start is not None:
            if not isinstance(week_start, datetime) or not isinstance(week_end, datetime):
                raise TypeError("week_start and week_end must be datetimes")
            if week_start >= week_end:
                raise ValueError("week_end must be later than week_start")

        if not isinstance(adjustment, HistoryAdjustment):
            raise TypeError("adjustment must be a HistoryAdjustment")
        buffer_multiplier = adjustment.buffer_multiplier

        if preferences.sleep_start == preferences.sleep_end:
            raise ValueError("sleep_start and sleep_end must differ")
        daily_study_cap = preferences.max_study_hours_per_day
        if daily_study_cap is not None:
            if not isinstance(daily_study_cap, timedelta):
                raise TypeError("max_study_hours_per_day must be a timedelta or None")
            if daily_study_cap <= timedelta(0):
                raise ValueError("max_study_hours_per_day must be positive")
        for window_start, window_end in preferences.preferred_study_hours:
            if window_start == window_end:
                raise ValueError("preferred study windows must have different endpoints")

        def clock_window(day: date, window_start: time, window_end: time,
                         reference: datetime) -> tuple[datetime, datetime]:
            start = datetime.combine(day, window_start, tzinfo=reference.tzinfo)
            end_day = day + (timedelta(days=1) if window_end <= window_start
                             else timedelta())
            end = datetime.combine(end_day, window_end, tzinfo=reference.tzinfo)
            return start, end

        def overlaps_sleep(start: datetime, end: datetime) -> bool:
            day = start.date() - timedelta(days=1)
            while day <= end.date():
                sleep_start, sleep_end = clock_window(
                    day, preferences.sleep_start, preferences.sleep_end, start
                )
                if start < sleep_end and sleep_start < end:
                    return True
                day += timedelta(days=1)
            return False

        def fits_preferred_hours(start: datetime, end: datetime) -> bool:
            for window_start, window_end in preferences.preferred_study_hours:
                day = start.date() - timedelta(days=1)
                while day <= end.date():
                    preferred_start, preferred_end = clock_window(
                        day, window_start, window_end, start
                    )
                    if preferred_start <= start and end <= preferred_end:
                        return True
                    day += timedelta(days=1)
            return False

        fixed_events = [
            event for event in events
            if not event.is_flexible() and not event.is_hidden and not event.is_complete
        ]
        blocks: list[StudyBlock] = []
        placements: list[AssignmentPlacement] = []

        def actions_for(
                issue: AssignmentPlacementIssue
        ) -> tuple[SchedulingAction, ...]:
            if issue is AssignmentPlacementIssue.DEADLINE_BEFORE_START:
                return (
                    SchedulingAction.REVIEW_DEADLINE,
                    SchedulingAction.EDIT_ESTIMATE,
                    SchedulingAction.REVIEW_OPEN_TIME,
                    SchedulingAction.ADJUST_STUDY_PREFERENCES,
                    SchedulingAction.SCHEDULE_MANUALLY,
                )
            return (
                SchedulingAction.REVIEW_OPEN_TIME,
                SchedulingAction.EDIT_ESTIMATE,
                SchedulingAction.ADJUST_STUDY_PREFERENCES,
                SchedulingAction.REVIEW_DEADLINE,
                SchedulingAction.SCHEDULE_MANUALLY,
            )

        def find_start(day: date, deadline: datetime, earliest: datetime,
                       duration: timedelta,
                       require_preferred: bool) -> Optional[datetime]:
            day_start = datetime.combine(
                    day, time.min, tzinfo=deadline.tzinfo
            )
            day_start = max(day_start, earliest)
            day_end = datetime.combine(
                    day + timedelta(days=1), time.min, tzinfo=deadline.tzinfo
            )
            latest_start = min(deadline, day_end) - duration
            candidate = latest_start
            while candidate >= day_start:
                    candidate_end = candidate + duration
                    if (not overlaps_sleep(candidate, candidate_end)
                            and (not require_preferred
                                 or fits_preferred_hours(candidate, candidate_end))
                            and (
                                daily_study_cap is None
                                or sum(
                                    (
                                        block.get_duration()
                                        for block in blocks
                                        if block.start_time.date() == day
                                    ),
                                    timedelta(0),
                                ) + duration <= daily_study_cap
                            )
                            and not any(
                                overlaps(candidate, candidate_end,
                                         event.start_time, event.end_time)
                                for event in fixed_events
                            )
                            and not any(
                                overlaps(candidate, candidate_end,
                                         block.start_time, block.end_time)
                                for block in blocks
                            )):
                        return candidate
                    candidate -= timedelta(minutes=1)
            return None

        schedulable_events = [
            event for event in events
            if isinstance(event, (Assignment, PersonalTask))
            and not event.is_complete
            and not event.is_hidden
        ]
        schedulable_events.sort(
            key=lambda event: (
                -event.priority.value,
                event.due_date if isinstance(event, Assignment) else event.start_time,
                -(event.estimated_duration if isinstance(event, Assignment)
                  else event.get_duration()).total_seconds(),
            )
        )

        for event in schedulable_events:

            if isinstance(event, Assignment):
                    work_estimate = event.estimated_duration
                    earliest = event.start_time
                    deadline = event.due_date
                    if week_start is not None and week_end is not None:
                        earliest = max(earliest, week_start)
                        deadline = min(deadline, week_end)
                        last_day = (
                            deadline - timedelta(microseconds=1)
                        ).date()
                    else:
                        last_day = event.due_date.date()
                    first_day = max(event.start_time.date(), earliest.date())
                    daily_limit = timedelta(hours=2)
                    if deadline <= earliest:
                        planned_duration = max(
                            timedelta(minutes=30),
                            event.estimated_duration * buffer_multiplier,
                        )
                        placements.append(AssignmentPlacement(
                            event_id=event.event_id,
                            subject=event.title,
                            estimated_duration=event.estimated_duration,
                            planned_duration=planned_duration,
                            scheduled_duration=timedelta(0),
                            remaining_duration=planned_duration,
                            state=AssignmentPlacementState.NOT_SCHEDULED,
                            issue=AssignmentPlacementIssue.DEADLINE_BEFORE_START,
                            recommended_actions=actions_for(
                                AssignmentPlacementIssue.DEADLINE_BEFORE_START
                            ),
                        ))
                        continue
            else:
                    work_estimate = event.get_duration()
                    earliest = event.start_time
                    deadline = event.start_time
                    if week_start is not None and week_end is not None:
                        earliest = max(earliest, week_start)
                        deadline = min(deadline, week_end)
                    first_day = max(event.start_time.date(), earliest.date())
                    last_day = first_day
                    daily_limit = work_estimate

            remaining = max(
                    timedelta(minutes=30),
                    work_estimate * buffer_multiplier,
            )

            if last_day < first_day:
                    continue

            planned_duration = remaining
            event_blocks: list[StudyBlock] = []
            scheduled_days: set[date] = set()
            for require_preferred in (True, False):
                    if require_preferred and not preferences.preferred_study_hours:
                        continue
                    day = last_day
                    while day >= first_day and remaining > timedelta(0):
                        if day in scheduled_days:
                            day -= timedelta(days=1)
                            continue

                        session_length = min(remaining, daily_limit)
                        minimum_session = min(timedelta(minutes=30), session_length)
                        start = None
                        while session_length >= minimum_session:
                            start = find_start(
                                day, deadline, earliest, session_length,
                                require_preferred
                            )
                            if start is not None:
                                break
                            if session_length == minimum_session:
                                break
                            session_length = max(
                                minimum_session, session_length - timedelta(minutes=30)
                            )

                        if start is not None:
                            block = StudyBlock(
                                event.title,
                                start,
                                start + session_length,
                                event.event_id,
                            )
                            blocks.append(block)
                            event_blocks.append(block)
                            scheduled_days.add(day)
                            remaining -= session_length
                        day -= timedelta(days=1)

            if event_blocks:
                    max(event_blocks, key=lambda block: block.start_time).completes_source = True

            if isinstance(event, Assignment):
                    scheduled_duration = sum(
                        (block.get_duration() for block in event_blocks),
                        timedelta(0),
                    )
                    remaining_duration = max(
                        timedelta(0), planned_duration - scheduled_duration
                    )
                    if remaining_duration == timedelta(0):
                        state = AssignmentPlacementState.FULLY_SCHEDULED
                        issue = None
                        recommended_actions = ()
                    else:
                        state = (
                            AssignmentPlacementState.PARTIALLY_SCHEDULED
                            if scheduled_duration > timedelta(0)
                            else AssignmentPlacementState.NOT_SCHEDULED
                        )
                        issue = AssignmentPlacementIssue.INSUFFICIENT_AVAILABLE_TIME
                        recommended_actions = actions_for(issue)
                    placements.append(AssignmentPlacement(
                        event_id=event.event_id,
                        subject=event.title,
                        estimated_duration=event.estimated_duration,
                        planned_duration=planned_duration,
                        scheduled_duration=scheduled_duration,
                        remaining_duration=remaining_duration,
                        state=state,
                        issue=issue,
                        recommended_actions=recommended_actions,
                    ))

        return ScheduleResult(
            blocks=sorted(blocks, key=lambda block: block.start_time),
            assignment_placements=placements,
        )

    def generate_planner(self, calendar: Calendar, preferences: UserPreferences,
                         week_start: datetime, week_end: datetime,
                         history: Optional[list[CompletionRecord]] = None,
                         duration_decisions: Optional[
                             dict[str, AssignmentDurationDecision]
                         ] = None,
                         completion_store: Optional[CompletionRecordStore] = None,
                         user_id: Optional[str] = None) -> Planner:
        """
        Run the whole pipeline and hand back a finished DRAFT planner.

        Load history from the supplied store when requested, request matching
        duration recommendations for assignments, apply only explicit user
        decisions, then generate a bounded ScheduleResult. The Planner carries
        recommendation review data for display, including recommendations
        awaiting a decision.

        Pass duration_decisions keyed by assignment event id to collect
        accept, edit, or keep-current responses. A missing decision leaves the
        current estimate unchanged and the review pending. history can be
        supplied directly; alternatively, completion_store and user_id load
        durable records for this user. With neither, history is empty.

        Returns a planner still in DRAFT - this method never approves it.
        Nothing is scheduled for real until the user calls approve(), which is
        the safeguard that keeps the AI from silently committing their time.

        Note it does NOT attach the planner to the user; the caller does that
        with User.add_planner().
        """
        if not isinstance(calendar, Calendar):
            raise TypeError("calendar must be a Calendar")
        if not isinstance(preferences, UserPreferences):
            raise TypeError("preferences must be UserPreferences")
        if not isinstance(week_start, datetime) or not isinstance(week_end, datetime):
            raise TypeError("week_start and week_end must be datetimes")
        if week_start >= week_end:
            raise ValueError("week_end must be later than week_start")
        if (completion_store is None) != (user_id is None):
            raise ValueError(
                "completion_store and user_id must be supplied together"
            )
        if history is not None and completion_store is not None:
            raise ValueError("provide history or completion_store, not both")
        if completion_store is not None:
            if not isinstance(completion_store, CompletionRecordStore):
                raise TypeError("completion_store must be a CompletionRecordStore")
            assert user_id is not None
            history = completion_store.get_records(user_id)
        elif history is None:
            history = []
        if any(not isinstance(record, CompletionRecord) for record in history):
            raise TypeError("history must contain CompletionRecord instances")
        if duration_decisions is None:
            duration_decisions = {}
        if not isinstance(duration_decisions, dict):
            raise TypeError("duration_decisions must be a dictionary")
        if any(
            not isinstance(event_id, str) or not event_id
            for event_id in duration_decisions
        ):
            raise TypeError("duration_decisions keys must be non-empty event ids")
        if any(
            not isinstance(decision, AssignmentDurationDecision)
            for decision in duration_decisions.values()
        ):
            raise TypeError(
                "duration_decisions must contain AssignmentDurationDecision values"
            )

        events = self.analyze_calendar(calendar, week_start, week_end)
        recommendations: dict[str, tuple[
            Assignment, AssignmentDurationRecommendation
        ]] = {}
        for event in events:
            if isinstance(event, Assignment) and not event.is_complete:
                recommendation = self.recommend_assignment_duration(event, history)
                if recommendation is not None:
                    recommendations[event.event_id] = (event, recommendation)
        unknown_decisions = set(duration_decisions) - set(recommendations)
        if unknown_decisions:
            raise ValueError(
                "duration decisions require assignments with available "
                f"recommendations: {', '.join(sorted(unknown_decisions))}"
            )

        reviews: list[AssignmentDurationReview] = []
        for event_id, (assignment, recommendation) in recommendations.items():
            current_duration = assignment.estimated_duration
            decision = duration_decisions.get(event_id)
            if decision is None:
                applied_duration = current_duration
            elif decision.action is AssignmentDurationDecisionAction.ACCEPT:
                assignment.accept_duration_recommendation(recommendation)
                applied_duration = assignment.estimated_duration
            elif decision.action is AssignmentDurationDecisionAction.EDIT:
                assert decision.duration is not None
                assignment.set_estimated_duration(decision.duration)
                applied_duration = assignment.estimated_duration
            else:
                applied_duration = current_duration
            reviews.append(AssignmentDurationReview(
                event_id=event_id,
                subject=assignment.title,
                current_duration=current_duration,
                recommendation=recommendation,
                decision=decision,
                applied_duration=applied_duration,
            ))

        adjustment = self.adapt_from_history(history)
        result = self.optimize_schedule(
            events,
            preferences,
            adjustment,
            week_start=week_start,
            week_end=week_end,
        )

        planner = Planner(week_start, week_end)
        for block in result.blocks:
            planner.add_study_block(block)
        planner.assignment_placements = list(result.assignment_placements)
        planner.assignment_duration_reviews = reviews
        return planner


# Design notes for review - Problems fixed. Need to add UI to address remaining notes.
#
# 1. The UI still needs to render Planner.assignment_placements alongside its
#    blocks, including the recommended next-step actions.
#
# 2. Planner generation now returns assignment_duration_reviews and accepts
#    explicit accept/edit/keep-current decisions. CompletionRecordStore
#    persists history in SQLite and generate_planner() can load it by user id.
#    A future UI still needs to render the reviews and collect those decisions.
#


# ---------------------------------------------------------------------------
# User and Admin
# ---------------------------------------------------------------------------


class Person:
    """Common identity details shared by User and Admin accounts."""

    def __init__(self, name: str, email: str):
        self.name = name
        self.email = normalize_email(email)


class User(Person):
    """
    A student using the planner - the owner of everything else in the system.

    A User is the top of the object graph: it holds the one Calendar of what
    they've committed to, the UserPreferences the AI schedules around, and the
    list of Planners built up week by week.

        User ─┬─ Calendar ── Events
              ├─ UserPreferences
              └─ Planners ── StudyBlocks

    The methods here are mostly a front door: add_event and edit_planner just
    pass the work down to the Calendar or Planner that owns it. That's
    deliberate - the UI talks to a User and doesn't need to know the internals.

    Attributes
    ----------
    user_id     - unique id, auto-generated
    name        - display name
    email       - validated, normalized contact address and login identifier
    calendar    - a fresh empty Calendar, created here (each user owns exactly one)
    preferences - passed in at construction; the AI's scheduling constraints
    planners    - retained planners in chronological order; expired unapproved
                  DRAFT planners are discarded
    """

    def __init__(
        self,
        name: str,
        email: str,
        preferences: UserPreferences,
        *,
        user_id: Optional[str] = None,
    ):
        """
        Create a user, assigning user_id via new_id().

        Note the calendar is created here rather than passed in - a user always
        starts with their own empty one - while preferences is a required
        argument, since the AI can't schedule without knowing sleep hours.
        planners starts empty.
        """
        super().__init__(name, email)
        if user_id is not None and (
            not isinstance(user_id, str) or not user_id.strip()
        ):
            raise ValueError("user_id must be a non-empty string")
        self.user_id = user_id or new_id()
        self.calendar = Calendar()
        self.preferences = preferences
        self.planners: list[Planner] = []

    def login(self, password: str, account_store: UserAccountStore) -> bool:
        """
        Authenticate this account without storing a plaintext password.

        Returns False for an invalid password, unverified email, or an account
        that does not match this User object's id. Successful authentication
        runs hidden-record cleanup and advances planner lifecycle state.
        """
        if not isinstance(account_store, UserAccountStore):
            raise TypeError("account_store must be a UserAccountStore")
        authenticated_user_id = account_store.authenticate(self.email, password)
        if authenticated_user_id != self.user_id:
            return False
        self.purge_hidden()
        self.advance_planner_lifecycle()
        return True

    def add_event(self, event: Event) -> None:
        """
        Put an event on this user's calendar.

        A pass-through to Calendar.add_event() so callers never have to reach
        into user.calendar themselves. Keeping the wrapper means the Calendar
        could later be swapped for a database without changing any caller.
        """
        self.calendar.add_event(event)

    def hide_event(self, event_id: str,
                   scope: RecurrenceScope = RecurrenceScope.THIS_OCCURRENCE,
                   now: Optional[datetime] = None) -> bool:
        """
        What "delete this event" means in this app: hide the event, and hide
        the study time still scheduled for it.

        now is passed down to stamp hidden_at on everything this call hides,
        so the event and its blocks share one retention deadline.

        scope is forwarded to Calendar.hide_event() and decides how wide the
        block cascade goes, which is the part that surprises people:
          THIS_OCCURRENCE - hides one date, so only the blocks made for THAT
                            occurrence id go. Next week's session for the
                            same class stays, which is right - the user is
                            skipping one lecture, not the course.
          ENTIRE_SERIES   - hides the series, so blocks for EVERY occurrence
                            of it go. Step 2 has to match on the series id
                            rather than a single occurrence id, or it sweeps
                            one week and leaves the rest of the term behind.

        The cascading version of Calendar.hide_event(), and the one the UI
        calls. Getting rid of an assignment has to take its future study
        blocks with it, or the plan keeps telling the user to work on
        something that no longer exists.

        Nothing is destroyed, and nothing that already happened is touched.
        The full reasoning is in design note 2c on StudyBlock; the rule is
        that hiding only affects work that hasn't happened yet.

        The sequence:
          1. calendar.hide_event(event_id). If it returns False the event
             doesn't exist or was already hidden - either way nothing changed,
             so return False and leave the planners alone. That check is also
             what stops a double-click from doing the sweep twice.
          2. For every planner, hide_blocks_for_event(event_id). All of them,
             because an event's blocks can be spread across weeks - but each
             planner decides what it's willing to hide, and a PAST one hides
             nothing. Completed blocks survive in the live planners too.

        Note the order. The event goes first, so if step 2 fails partway the
        leftover blocks are visible and obviously wrong, rather than the event
        surviving with its study time quietly gone.

        No recursion guard needed: hiding travels event -> blocks only, and a
        block has nothing to hide back.

        Returns True if this call hid the event.
        """
        if not self.calendar.hide_event(event_id, scope, now):
            return False

        for planner in self.planners:
            planner.hide_blocks_for_event(event_id, now)
        return True

    def unhide_event(self, event_id: str,
                     scope: RecurrenceScope = RecurrenceScope.THIS_OCCURRENCE,
                     now: Optional[datetime] = None) -> int:
        """
        Bring a hidden event back, and restore only the study blocks a planner
        still needs.

        The route in is the user searching their hidden events and picking one
        - Calendar.get_event() finds hidden events precisely so this is
        possible.

        Deliberately NOT the mirror image of hide_event(). Hiding an event
        hides its future study time; unhiding it does not hand that time back,
        because the hours those blocks occupied have usually passed. The event
        returns as work to be planned and the AI schedules fresh blocks for it
        on the next run. Only a block the plan could still act on comes back -
        live planner, time not yet passed - which Planner.needs_hidden_block()
        decides. Full reasoning in design note 2c on StudyBlock.

        scope is forwarded to Calendar.unhide_event() and means what it does
        on hide_event(): one date back, or the series back.

        The sequence:
          1. calendar.unhide_event(event_id, scope). If it returns False the
             event doesn't exist or wasn't hidden; return 0 and touch nothing.
          2. For every planner, restore_blocks_for_event(event_id, now), and
             total what came back.

        Returns the number of blocks restored, which is normally 0 - the event
        itself is back either way. A count rather than a bool because "the
        event returned and nothing else did" and "the event returned along
        with three sessions still on this week's plan" are different enough
        that the UI should be able to tell the user which happened.
        """
        if not self.calendar.unhide_event(event_id, scope):
            return 0

        return sum(
            planner.restore_blocks_for_event(event_id, now)
            for planner in self.planners
        )

    def get_week_view(self, planner_id: str) -> list[ScheduleItem]:
        """
        Everything the user has that week, in one chronological list: their
        commitments from the Calendar and the AI's study blocks together.

        This is what the planner screen draws. A study block on its own tells
        the user almost nothing - whether 4pm Tuesday is sensible depends on
        the class that ends at 3:45 and the shift that starts at 6 - so the
        planner shows the surroundings, not just the suggestions.

        On User for the usual reason: it needs the calendar AND the planner,
        and is the only object holding both.

        The sequence:
          1. Find the planner by id. Return an empty list if there isn't one.
          2. calendar.get_events(planner.week_start,
             planner.week_end) -> to_schedule_item() on each. Hidden
             events drop out here by default, which is what should happen.
          3. planner.get_schedule_items() for the study blocks.
          4. Concatenate and sort by start_time. The sort belongs here because
             Calendar.events is unsorted (Calendar design note 3) and the two
             halves interleave.

        Nothing is copied into the planner. The list is built fresh each call
        and discarded, so the Calendar stays the single source of truth for
        events - see ScheduleItem, and design note 3 on the duplication this
        avoids repeating.

        Two consequences worth expecting rather than discovering:

          - Which events step 2 returns depends on the window filter that
            Calendar design note 1 is still deciding. Under "fully contained",
            a shift crossing Saturday midnight vanishes from the view of both
            weeks it touches, which on a visual grid is an obvious hole.
            Overlap is the right choice for this too.

          - The view will make optimize_schedule's missing conflict check
            visible. Today nothing stops a study block landing on top of a
            class (note 2 on optimize_schedule); until now that was invisible,
            and the first thing this screen will show is a block sitting over
            a lecture. That's the view working, not a bug in it.
        """
        planner = next(
            (item for item in self.planners if item.planner_id == planner_id),
            None,
        )
        if planner is None:
            return []

        items = [
            event.to_schedule_item()
            for event in self.calendar.get_events(
                planner.week_start, planner.week_end
            )
        ]
        items.extend(planner.get_schedule_items())
        return sorted(items, key=lambda item: item.start_time)

    def purge_hidden(self, now: Optional[datetime] = None) -> PurgeResult:
        """
        Destroy hidden records that have outlived HIDDEN_RETENTION_DAYS and
        that nothing still refers to. The only operation in the app that
        permanently deletes anything.

        Hiding keeps deleted things readable; this is what stops them piling
        up forever. The two tests are independent and both must pass - see
        design note 2c on StudyBlock.

        The sequence, and the order is the substance of the method:
          1. For every planner, purge_hidden_blocks(now). Blocks go first
             because destroying one can drop the last reference to an event
             and let that event go in the same pass; doing events first would
             miss it until the next run.
          2. Union get_referenced_event_ids() across every planner. These are
             the events that surviving blocks still point at - above all a
             completed block in a past week.
          3. calendar.purge_hidden_events(keep_event_ids, now), which destroys
             expired hidden events except those in the set.
          4. Union get_referenced_occurrence_ids() across every planner, and
             calendar.purge_expired_overrides(keep_occurrence_ids, now), which
             drops expired single skipped dates. Its protection list is a
             separate collection, not the one from step 2 - that set was
             reduced to series ids, which name no override.

        Step 4 comes last for the same reason step 1 comes first: by then the
        expired blocks are gone, so a date whose only reference was one of
        them is free to go too.

        An event referenced by a past planner is therefore never purged, no
        matter how long it has been hidden. That is not a leak, it is the
        guarantee: a finished week keeps making sense forever, and the 14 days
        only ever applies to records nothing needs.

        now defaults to the current time; pass one to test the boundary.

        Returns a PurgeResult - three named counts, so a caller can't read
        "one series went" as "one skipped Tuesday went".

        Intended to run after a successful login, so the retention rule is
        applied whenever the user turns up, and later from a scheduled
        maintenance job once the data persists. The current login placeholder
        does not invoke cleanup because it verifies no credentials. Safe to
        call from both routes - it's idempotent. Design note 2c has the
        reasoning and what the maintenance route still needs.
        """
        blocks_destroyed = sum(
            planner.purge_hidden_blocks(now) for planner in self.planners
        )

        keep_event_ids: set[str] = set()
        for planner in self.planners:
            keep_event_ids.update(planner.get_referenced_event_ids())
        events_destroyed = self.calendar.purge_hidden_events(
            keep_event_ids, now
        )

        keep_occurrence_ids: set[str] = set()
        for planner in self.planners:
            keep_occurrence_ids.update(
                planner.get_referenced_occurrence_ids()
            )
        overrides_destroyed = self.calendar.purge_expired_overrides(
            keep_occurrence_ids, now
        )

        return PurgeResult(
            events_destroyed=events_destroyed,
            blocks_destroyed=blocks_destroyed,
            overrides_destroyed=overrides_destroyed,
        )

    def edit_planner(self, planner_id: str, block_id: str,
                     **updates: object) -> Optional[int]:
        """
        Change a study block, given which planner it lives in.

        Finds the planner by id, then hands off to Planner.edit() with the
        block id and the updates. **updates is forwarded straight through, so
        this accepts the same named arguments Planner.edit() does.

        This wrapper performs only an in-week edit; it does not move blocks
        across planners or change Calendar events. Any calendar-driven
        cross-week change must be an explicit User-level operation.

        Returns the number of fields changed, or None if no planner had that
        id. A result of 0 means the planner existed but the block was missing
        or all supplied values were already set.
        """
        planner = next(
            (item for item in self.planners if item.planner_id == planner_id),
            None,
        )
        if planner is None:
            return None
        return planner.edit(block_id, **updates)

    def complete_study_block(self, planner_id: str, block_id: str) -> bool:
        """
        Mark a study block done, and finish its source event if that's the
        block that was meant to finish it.

        This is one half of the StudyBlock <-> Event link (design note 2 on
        StudyBlock); complete_assignment() is the other. It lives on User
        because it needs both sides - the planners hold the blocks, the
        calendar holds the events - and User is the only object with both.

        The sequence:
          1. Find the planner, then the block in it (Planner.get_block). Return
             False if either is missing or the planner is PAST.
          2. block.mark_complete(). If it returns False the block was already
             done, so stop here - nothing changed and there is nothing to
             propagate.
          3. If block.completes_source is False, stop. This was one work
             session out of several; the assignment isn't finished just
             because an early block is.
          4. Otherwise look up the source event via
             calendar.get_event(block.source_event_id) and, if it exists, hand
             off to complete_assignment(), which completes the event AND its
             sibling blocks - the other sessions for work that is now done.

        A missing source event at step 4 shouldn't happen now that events are
        hidden rather than destroyed - get_event() finds hidden ones too - but
        it isn't treated as an error. The block stays complete and the
        propagation just stops.

        Returns True if the block was marked complete by this call.
        """
        planner = next(
            (item for item in self.planners if item.planner_id == planner_id),
            None,
        )
        if planner is None or planner.status is PlannerStatus.PAST:
            return False

        block = planner.get_block(block_id)
        if block is None or not block.mark_complete():
            return False

        if block.completes_source:
            event = self.calendar.get_event(block.source_event_id)
            if event is not None:
                self.complete_assignment(block.source_event_id)
        return True

    def complete_assignment(self, event_id: str) -> bool:
        """
        Mark an event done, and complete every study block scheduled for it.

        The other half of the link. Named for the case that matters - an
        Assignment is what the AI schedules study blocks for - but it takes any
        event, since nothing about the rule is specific to that subclass and
        PersonalTask gets blocks too.

        The sequence:
          1. calendar.get_event(event_id); return False if there's no such event.
          2. event.mark_complete(). If it returns False the event was already
             complete, so stop - this is what keeps step 3 from bouncing back.
          3. For each non-PAST planner this user has,
             get_blocks_for_event(event_id) and mark_complete() each one.
             PAST planners are read-only, so late completion never changes
             archived history.

        Note step 3 calls StudyBlock.mark_complete() directly rather than
        complete_study_block(). That's the loop-breaker: the block-level method
        propagates back to the event, and here the event is the thing that
        started it. Since the event is already complete, mark_complete() would
        return False and stop anyway - so this is belt and braces, but it also
        says plainly which direction is running.

        Returns True if the event was marked complete by this call.
        """
        event = self.calendar.get_event(event_id)
        if event is None or not event.mark_complete():
            return False

        for planner in self.planners:
            if planner.status is PlannerStatus.PAST:
                continue
            for block in planner.get_blocks_for_event(event_id):
                block.mark_complete()
        return True

    def view_planner_history(self) -> list[Planner]:
        """
        Return the user's past planners - the ones with status PAST.

        Deliberately excludes DRAFT and APPROVED, so "history" means finished
        weeks only, not the plan currently in use. These are the planners whose
        CompletionRecords feed back into AIAssistant.adapt_from_history().
        """
        return [
            planner for planner in self.planners
            if planner.status is PlannerStatus.PAST
        ]

    def add_planner(self, planner: Planner) -> None:
        """
        Store a newly generated planner on this user.

        Called after AIAssistant.generate_planner(), which builds a DRAFT
        planner but doesn't attach it to anyone. Multiple drafts for the same
        week are allowed. At most one planner for an exact week may be
        APPROVED; use approve_planner() to enforce that rule. Adding an
        already-approved planner that conflicts with the current one raises
        ValueError.
        """
        if not isinstance(planner, Planner):
            raise TypeError("planner must be a Planner")
        if planner.status is PlannerStatus.APPROVED and any(
            existing.status is PlannerStatus.APPROVED
            and existing.week_start == planner.week_start
            and existing.week_end == planner.week_end
            for existing in self.planners
        ):
            raise ValueError("a planner for this week is already approved")
        self.planners.append(planner)
        self.planners.sort(key=lambda item: item.week_start)

    def approve_planner(
        self,
        planner_id: str,
        replace_approved: bool = False,
        now: Optional[datetime] = None,
    ) -> bool:
        """
        Approve one of this user's drafts, optionally replacing that week's plan.

        Returns False if no planner has planner_id, if lifecycle maintenance
        has already discarded it, or if replacement was declined because
        another planner for the exact same [week_start, week_end) is approved.
        In those cases existing planner statuses are left unchanged.

        Planner.approve() performs all draft, time-window and duration-review
        validation before any existing approval is demoted. If that validation
        raises, the currently approved plan remains approved. This coordinates
        the transition in memory; callers persisting planners must serialize
        concurrent approval requests for the same user and week.
        """
        if not isinstance(replace_approved, bool):
            raise TypeError("replace_approved must be a boolean")

        self.advance_planner_lifecycle(now)
        planner = next(
            (item for item in self.planners if item.planner_id == planner_id),
            None,
        )
        if planner is None:
            return False

        approved_for_week = [
            item for item in self.planners
            if item is not planner
            and item.status is PlannerStatus.APPROVED
            and item.week_start == planner.week_start
            and item.week_end == planner.week_end
        ]
        if approved_for_week and not replace_approved:
            return False

        planner.approve(now)
        for existing in approved_for_week:
            existing.status = PlannerStatus.DRAFT
        return True

    def advance_planner_lifecycle(
        self, now: Optional[datetime] = None
    ) -> tuple[int, int]:
        """
        Archive ended approved plans and discard ended drafts.

        At the exclusive week_end boundary, APPROVED planners become PAST and
        all DRAFT planners for ended weeks are removed. Multiple drafts are
        allowed; approval uniqueness is maintained by approve_planner().

        Returns (archived_approved_count, discarded_draft_count). Invoke this
        from the application's scheduled maintenance or user-entry workflow;
        this in-memory model has no background scheduler of its own.
        """
        if now is not None and not isinstance(now, datetime):
            raise TypeError("now must be a datetime")

        archived_count = 0
        for planner in self.planners:
            current_time = now
            if current_time is None:
                current_time = datetime.now(tz=planner.week_end.tzinfo)
            if (
                planner.status is PlannerStatus.APPROVED
                and planner.week_end <= current_time
            ):
                planner.archive(current_time)
                archived_count += 1

        discarded_count = self.discard_expired_draft_planners(now)
        return archived_count, discarded_count

    def discard_expired_draft_planners(
            self, now: Optional[datetime] = None) -> int:
        """
        Remove this user's unapproved draft planners whose weeks have ended.

        A DRAFT is eligible when week_end is at or before now. Approved plans
        are never discarded by this method; they remain available to be
        archived as PAST. This can run at login or from scheduled maintenance
        and is safe to call repeatedly.

        Returns the number of draft planners removed.
        """
        retained_planners: list[Planner] = []
        for planner in self.planners:
            current_time = now
            if current_time is None:
                current_time = datetime.now(tz=planner.week_end.tzinfo)
            if not (
                planner.status is PlannerStatus.DRAFT
                and planner.week_end <= current_time
            ):
                retained_planners.append(planner)

        removed_count = len(self.planners) - len(retained_planners)
        self.planners = retained_planners
        return removed_count


class Admin(Person):
    """
    A staff account that manages users and inspects the system.

    Note Admin does NOT inherit from User: it's a separate role, not a kind of
    student. That means an admin has no calendar, preferences or planners of
    their own - they administer the system rather than use it. If admins are
    ever meant to plan their own week too, this needs rethinking (see below).

    Attributes
    ----------
    admin_id        - unique id, auto-generated
    name            - display name
    email           - validated, normalized contact address
    _managed_users  - the users this admin looks after. The leading underscore
                      is a Python convention meaning "internal - don't touch
                      from outside the class". It isn't enforced; it's a signal
                      that the list should be changed through manage_users()
                      rather than reached into directly.
    """

    def __init__(self, name: str, email: str):
        """
        Create an admin, assigning admin_id via new_id() and starting
        _managed_users empty.
        """
        super().__init__(name, email)
        self.admin_id = new_id()
        self._managed_users: list[User] = []

    def manage_users(self, action: str, user: User) -> None:
        """
        Add or remove a user from this admin's managed list.

        action is the string "add" or "remove". Both cases check membership
        first, so adding a user twice or removing one who isn't there does
        nothing rather than erroring. A non-User argument raises TypeError;
        any other action raises ValueError.

        Returns nothing, so the caller can't tell whether anything happened -
        see the notes below on that limitation.
        """
        if not isinstance(user, User):
            raise TypeError("user must be a User")
        if action == "add":
            if user not in self._managed_users:
                self._managed_users.append(user)
        elif action == "remove":
            if user in self._managed_users:
                self._managed_users.remove(user)
        else:
            raise ValueError("action must be 'add' or 'remove'")

    def view_system_logs(self) -> list[str]:
        """
        Placeholder for real logging - currently returns an empty list.

        Nothing in the system records anything yet, so there is no source to
        read from. Standing in for a decision not yet made.
        """
        return []


# Design notes for review:
#
# 1. Implemented: UserAccountStore starts passwordless registration, verifies
#    a 24-hour email token, then issues a 30-minute one-time password-setup
#    capability. Passwords are stored only as Argon2id hashes. Login requires
#    a verified account and applies a 15-minute lock after five failed attempts.
#    Pending registrations expire after seven days; email and setup tokens are
#    stored as hashes, and verification email delivery is an injected callback.
#    Production still needs a real mail sender, HTTPS, and API-level IP
#    throttling/session management; these are outside this in-memory model.
#
# 2. Admin restrictions and authorization policy (planned; not enforced by
#    these domain classes):
#      - Authenticate the caller first, then load their identity, role, and
#        permissions from trusted server-side account data. Never trust a role
#        or admin id supplied by the UI or request.
#      - An Admin cannot edit a User's planner, events, or preferences.
#      - Admin database reads are limited to non-schedule account/status and
#        system/audit data. Admins must never view planners, events, or
#        preferences through database queries or reports.
#      - Admins are read-only by default. Database edits require a separate,
#        explicitly granted permission, remain limited to non-schedule data,
#        and must be audited. Admin status alone does not grant edit rights.
#      - Enforce these checks in the application/service layer on every
#        operation, not just by hiding UI controls. Use narrowly scoped
#        repository methods rather than unrestricted database access.
#    The Admin class is a role/domain object, not an authorization boundary.
#    Authentication exists for User accounts, but an Admin registry, explicit
#    permission model, service layer, and audit log remain to be implemented.
#
# 3. Resolved: manage_users() accepts only "add" and "remove", and raises
#    ValueError for any other action so a typo cannot silently look successful.
#
# 4. Resolved: Person centralizes validated display names and normalized email
#    addresses. User and Admin inherit those common identity fields while
#    keeping their role-specific ids and account behavior separate.
#
# 5. Resolved: User and Admin require a conventional email address and store
#    its trimmed, case-folded comparison form. UserAccountStore applies the
#    same normalization, checks availability for friendly feedback, and uses
#    a SQLite UNIQUE constraint as the authoritative User-email uniqueness
#    rule. Admin emails are validated but are not in the User-only registry.
#    Signup starts as a passwordless pending account: email verification is
#    required before a short-lived setup capability can create an Argon2id
#    password hash. Verification/setup tokens are stored only as hashes.
#    Pending accounts expire after seven days; resend and login attempts are
#    throttled. The application supplies email delivery and must require
#    verified credentials before granting access.
#
# 6. Resolved: multiple DRAFT planners may cover a week, but User.approve_planner()
#    permits only one APPROVED plan for the same exact week interval. Replacing
#    one requires explicit confirmation; the new plan is validated and approved
#    before the existing plan is demoted to DRAFT. At week_end, User's
#    advance_planner_lifecycle() archives approved plans as PAST and discards
#    remaining drafts. The application must invoke that method from scheduled
#    maintenance or its user-entry workflow because this in-memory model has no
#    background scheduler.
