"""
AI Planner - Python class implementation

Mirrors the UML class diagram: User, Admin, AIAssistant, Calendar, Planner,
Event (and its subtypes), CompletionRecord, StudyBlock, and UserPreferences.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Optional
import uuid


def new_id() -> str:
    """Generate a short unique id for any entity."""
    return str(uuid.uuid4())[:8]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PlannerStatus(Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PAST = "past"


class Priority(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class OffDayScope(Enum):
    CAMPUS = "campus"
    PERSONAL = "personal"


# ---------------------------------------------------------------------------
# Event hierarchy
# ---------------------------------------------------------------------------

class Event(ABC):
    """Abstract base class for anything that occupies time on the calendar."""

    def __init__(self, title: str, start_time: datetime, end_time: datetime):
        if end_time <= start_time:
            raise ValueError("end_time must be after start_time")
        self.event_id: str = new_id()
        self.title: str = title
        self.start_time: datetime = start_time
        self.end_time: datetime = end_time
        self.completion_record: Optional["CompletionRecord"] = None

    def get_duration(self) -> timedelta:
        return self.end_time - self.start_time

    def attach_completion_record(self, record: "CompletionRecord") -> None:
        record.event_id = self.event_id
        self.completion_record = record

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} '{self.title}' {self.start_time:%a %H:%M}>"


class Assignment(Event):
    def __init__(self, title, start_time, end_time, due_date: datetime,
                 course: str, priority: Priority = Priority.MEDIUM):
        super().__init__(title, start_time, end_time)
        self.due_date = due_date
        self.course = course
        self.priority = priority


class ExtracurricularEvent(Event):
    def __init__(self, title, start_time, end_time, location: str,
                 is_recurring: bool = False):
        super().__init__(title, start_time, end_time)
        self.location = location
        self.is_recurring = is_recurring


class ClassSession(Event):
    def __init__(self, title, start_time, end_time, course_code: str,
                 location: str, is_recurring: bool = True):
        super().__init__(title, start_time, end_time)
        self.course_code = course_code
        self.location = location
        self.is_recurring = is_recurring


class Meeting(Event):
    def __init__(self, title, start_time, end_time,
                 attendees: Optional[list[str]] = None, location: str = ""):
        super().__init__(title, start_time, end_time)
        self.attendees = attendees or []
        self.location = location


class JobShift(Event):
    def __init__(self, title, start_time, end_time, employer: str,
                 hourly_rate: float = 0.0):
        super().__init__(title, start_time, end_time)
        self.employer = employer
        self.hourly_rate = hourly_rate


class OffDay(Event):
    def __init__(self, title, start_time, end_time,
                 scope: OffDayScope = OffDayScope.PERSONAL, reason: str = ""):
        super().__init__(title, start_time, end_time)
        self.scope = scope
        self.reason = reason


class PersonalTask(Event):
    def __init__(self, title, start_time, end_time, category: str = "general",
                 priority: Priority = Priority.MEDIUM):
        super().__init__(title, start_time, end_time)
        self.category = category
        self.priority = priority


# ---------------------------------------------------------------------------
# Supporting classes
# ---------------------------------------------------------------------------

@dataclass
class CompletionRecord:
    """Captures how an event actually went, used by the AI to adapt future plans."""
    event_id: str = ""
    completed_on_time: bool = False
    actual_duration_minutes: int = 0
    performance_rating: int = 0  # e.g. 1-5
    notes: str = ""
    record_id: str = field(default_factory=new_id)


@dataclass
class StudyBlock:
    subject: str
    start_time: datetime
    end_time: datetime
    block_id: str = field(default_factory=new_id)

    def get_duration(self) -> timedelta:
        return self.end_time - self.start_time


@dataclass
class UserPreferences:
    sleep_start: time
    sleep_end: time
    preferred_study_hours: list[tuple[time, time]] = field(default_factory=list)
    class_schedule: list[ClassSession] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

class Calendar:
    def __init__(self):
        self.calendar_id: str = new_id()
        self.events: list[Event] = []

    def add_event(self, event: Event) -> None:
        self.events.append(event)

    def remove_event(self, event_id: str) -> bool:
        before = len(self.events)
        self.events = [e for e in self.events if e.event_id != event_id]
        return len(self.events) < before

    def get_events(self, start: Optional[datetime] = None,
                    end: Optional[datetime] = None) -> list[Event]:
        if start is None and end is None:
            return list(self.events)
        return [e for e in self.events
                if (start is None or e.start_time >= start)
                and (end is None or e.end_time <= end)]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class Planner:
    def __init__(self, week_start_date: datetime, week_end_date: datetime):
        self.planner_id: str = new_id()
        self.week_start_date = week_start_date
        self.week_end_date = week_end_date
        self.study_blocks: list[StudyBlock] = []
        self.status: PlannerStatus = PlannerStatus.DRAFT

    def add_study_block(self, block: StudyBlock) -> None:
        self.study_blocks.append(block)

    def edit(self, block_id: str, **updates) -> bool:
        for block in self.study_blocks:
            if block.block_id == block_id:
                for key, value in updates.items():
                    if hasattr(block, key):
                        setattr(block, key, value)
                return True
        return False

    def approve(self) -> None:
        self.status = PlannerStatus.APPROVED

    def archive(self) -> None:
        self.status = PlannerStatus.PAST

    def __repr__(self) -> str:
        return (f"<Planner {self.week_start_date:%Y-%m-%d} "
                f"status={self.status.value} blocks={len(self.study_blocks)}>")


# ---------------------------------------------------------------------------
# AIAssistant
# ---------------------------------------------------------------------------

class AIAssistant:
    """Generates and adapts weekly planners from a user's calendar and history."""

    def analyze_calendar(self, calendar: Calendar) -> list[Event]:
        """Return events relevant to the upcoming week (simplified: all events)."""
        return calendar.get_events()

    def adapt_from_history(self, records: list[CompletionRecord]) -> dict:
        """
        Turn past CompletionRecords into adjustments, e.g. more buffer time
        for tasks that consistently run late or score low.
        """
        if not records:
            return {"buffer_multiplier": 1.0, "notes": "No history yet."}

        late_ratio = sum(1 for r in records if not r.completed_on_time) / len(records)
        avg_rating = sum(r.performance_rating for r in records) / len(records)

        buffer_multiplier = 1.0 + late_ratio * 0.5
        notes = "On track" if avg_rating >= 3.5 else "Consider lighter daily load"

        return {"buffer_multiplier": round(buffer_multiplier, 2), "notes": notes}

    def optimize_schedule(self, events: list[Event],
                           preferences: UserPreferences,
                           adjustment: dict) -> list[StudyBlock]:
        """
        Placeholder scheduling logic: slot study blocks into preferred
        study hours, scaled by the adjustment from adapt_from_history.
        """
        blocks: list[StudyBlock] = []
        multiplier = adjustment.get("buffer_multiplier", 1.0)

        for event in events:
            if isinstance(event, (Assignment, PersonalTask)):
                base_minutes = max(30, int(event.get_duration().total_seconds() // 60))
                minutes = int(base_minutes * multiplier)
                start = event.start_time - timedelta(minutes=minutes)
                end = event.start_time
                blocks.append(StudyBlock(subject=event.title, start_time=start, end_time=end))

        return blocks

    def generate_planner(self, calendar: Calendar, preferences: UserPreferences,
                          week_start: datetime, week_end: datetime,
                          history: Optional[list[CompletionRecord]] = None) -> Planner:
        events = self.analyze_calendar(calendar)
        adjustment = self.adapt_from_history(history or [])
        study_blocks = self.optimize_schedule(events, preferences, adjustment)

        planner = Planner(week_start, week_end)
        for block in study_blocks:
            planner.add_study_block(block)
        return planner


# ---------------------------------------------------------------------------
# User and Admin
# ---------------------------------------------------------------------------

class User:
    def __init__(self, name: str, email: str, preferences: UserPreferences):
        self.user_id: str = new_id()
        self.name = name
        self.email = email
        self.calendar = Calendar()
        self.preferences = preferences
        self.planners: list[Planner] = []

    def login(self) -> bool:
        # Placeholder for real authentication logic.
        return True

    def add_event(self, event: Event) -> None:
        self.calendar.add_event(event)

    def edit_planner(self, planner_id: str, block_id: str, **updates) -> bool:
        for planner in self.planners:
            if planner.planner_id == planner_id:
                return planner.edit(block_id, **updates)
        return False

    def view_planner_history(self) -> list[Planner]:
        return [p for p in self.planners if p.status == PlannerStatus.PAST]

    def add_planner(self, planner: Planner) -> None:
        self.planners.append(planner)


class Admin:
    def __init__(self, name: str, email: str):
        self.admin_id: str = new_id()
        self.name = name
        self.email = email
        self._managed_users: list[User] = []

    def manage_users(self, action: str, user: User) -> None:
        if action == "add" and user not in self._managed_users:
            self._managed_users.append(user)
        elif action == "remove" and user in self._managed_users:
            self._managed_users.remove(user)

    def view_system_logs(self) -> list[str]:
        # Placeholder for real logging integration.
        return []


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    prefs = UserPreferences(sleep_start=time(23, 0), sleep_end=time(7, 0))
    student = User(name="Alex Rivera", email="alex@example.edu", preferences=prefs)

    now = datetime.now()
    student.add_event(Assignment(
        title="History essay",
        start_time=now + timedelta(days=2, hours=10),
        end_time=now + timedelta(days=2, hours=12),
        due_date=now + timedelta(days=3),
        course="HIST 201",
        priority=Priority.HIGH,
    ))

    ai = AIAssistant()
    past_records = [CompletionRecord(completed_on_time=False, performance_rating=3)]
    planner = ai.generate_planner(
        calendar=student.calendar,
        preferences=student.preferences,
        week_start=now,
        week_end=now + timedelta(days=7),
        history=past_records,
    )
    student.add_planner(planner)

    print(planner)
    print(student.view_planner_history())