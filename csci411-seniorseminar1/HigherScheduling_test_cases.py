from datetime import datetime, timedelta, time
import sqlite3
from typing import cast

import pytest

import HigherScheduling_logic as logic


def test_new_id_generates_unique_short_ids():
    first = logic.new_id()
    second = logic.new_id()

    assert isinstance(first, str)
    assert len(first) == 8
    assert first != second


def test_occurrence_id_round_trip_and_validation():
    start = datetime(2026, 9, 15, 10, 0)
    occ_id = logic.occurrence_id("abc123", start)

    assert occ_id == "abc123@2026-09-15T10:00:00"
    assert logic.parse_occurrence_id(occ_id) == ("abc123", start)
    assert logic.parse_occurrence_id("abc123") is None

    with pytest.raises(ValueError):
        logic.occurrence_id("", start)
    with pytest.raises(ValueError):
        logic.occurrence_id("bad@id", start)
    with pytest.raises(TypeError):
        logic.occurrence_id("abc123", cast(datetime, "2026-09-15T10:00:00"))


def test_overlap_and_clipped_duration_are_half_open():
    first_start = datetime(2026, 1, 1, 10, 0)
    first_end = datetime(2026, 1, 1, 12, 0)
    second_start = datetime(2026, 1, 1, 11, 30)
    second_end = datetime(2026, 1, 1, 13, 0)
    non_overlapping = (
        datetime(2026, 1, 1, 12, 0),
        datetime(2026, 1, 1, 12, 30),
        datetime(2026, 1, 1, 13, 0),
        datetime(2026, 1, 1, 14, 0),
    )

    assert logic.overlaps(first_start, first_end, second_start, second_end)
    assert not logic.overlaps(*non_overlapping)
    assert logic.clipped_duration(first_start, first_end, second_start, second_end) == timedelta(hours=0, minutes=30)


def test_recurrence_occurrence_starts_are_in_order():
    first_start = datetime(2026, 9, 14, 10, 0)
    rule = logic.Recurrence(
        logic.RecurrenceFrequency.WEEKLY,
        interval=2,
        until=datetime(2026, 9, 28, 0, 0),
    )

    starts = rule.occurrence_starts(
        first_start,
        datetime(2026, 9, 14, 0, 0),
        datetime(2026, 9, 30, 0, 0),
    )

    assert starts == [
        datetime(2026, 9, 14, 10, 0),
        datetime(2026, 9, 28, 10, 0),
    ]


def test_event_hide_unhide_and_completion_behaviors():
    event = logic.Assignment(
        "History review",
        datetime(2026, 9, 20, 9, 0),
        datetime(2026, 9, 20, 10, 0),
        datetime(2026, 9, 22, 18, 0),
        "HIST 201",
        category="essay",
        estimated_duration=timedelta(hours=1),
        priority=logic.Priority.HIGH,
    )

    assert event.is_flexible() is True
    assert event.get_duration() == timedelta(hours=1)
    assert event.estimated_duration == timedelta(hours=1)
    assert event.hide(now=datetime(2026, 9, 20, 8, 0)) is True
    assert event.is_hidden is True
    assert event.unhide() is True
    assert event.is_hidden is False
    assert event.mark_complete() is True
    assert event.is_complete is True
    assert event.mark_complete() is False

    with pytest.raises(ValueError):
        logic.Assignment(
            "Invalid",
            datetime(2026, 9, 20, 10, 0),
            datetime(2026, 9, 20, 10, 0),
            datetime(2026, 9, 22, 18, 0),
            "HIST 201",
            category="essay",
            estimated_duration=timedelta(hours=1),
        )


def test_assignment_requires_positive_timedelta_estimate():
    assignment_args = (
        "Essay",
        datetime(2026, 9, 18, 9, 0),
        datetime(2026, 9, 18, 10, 0),
        datetime(2026, 9, 19, 12, 0),
        "WRIT 101",
        "essay",
    )

    with pytest.raises(TypeError, match="estimated_duration must be a timedelta"):
        logic.Assignment(
            *assignment_args, estimated_duration=cast(timedelta, "one hour")
        )
    with pytest.raises(ValueError, match="estimated_duration must be positive"):
        logic.Assignment(*assignment_args, estimated_duration=timedelta(0))


def test_occurrence_expansion_and_override_update():
    series = logic.ClassSession(
        "Intro to CS",
        datetime(2026, 9, 14, 10, 0),
        datetime(2026, 9, 14, 11, 0),
        "CSCI 101",
        "RCH 310",
    )
    series.set_recurrence(
        logic.Recurrence(
            logic.RecurrenceFrequency.WEEKLY,
            until=datetime(2026, 9, 28, 0, 0),
        )
    )
    calendar = logic.Calendar()
    calendar.add_event(series)

    occurrences = calendar.get_events(
        start=datetime(2026, 9, 14),
        end=datetime(2026, 9, 30),
    )
    assert len(occurrences) == 3
    assert [item.start_time for item in occurrences] == [
        datetime(2026, 9, 14, 10, 0),
        datetime(2026, 9, 21, 10, 0),
        datetime(2026, 9, 28, 10, 0),
    ]

    first_occurrence_id = occurrences[0].event_id
    resolved_occurrence = calendar.get_event(first_occurrence_id)
    assert resolved_occurrence is not None
    assert resolved_occurrence.event_id == first_occurrence_id

    assert series.edit_occurrence(first_occurrence_id, title="Intro to CS Lab") is True
    resolved_occurrence = calendar.get_event(first_occurrence_id)
    assert resolved_occurrence is not None
    assert resolved_occurrence.title == "Intro to CS Lab"

    assert calendar.hide_event(series.event_id, scope=logic.RecurrenceScope.ENTIRE_SERIES, now=datetime(2026, 9, 15, 8, 0)) is True
    assert series.is_hidden is True
    assert calendar.unhide_event(series.event_id, scope=logic.RecurrenceScope.ENTIRE_SERIES) is True
    assert series.is_hidden is False


def test_study_block_and_completion_record_logic():
    block = logic.StudyBlock(
        "Read chapter 2",
        datetime(2026, 9, 15, 9, 0),
        datetime(2026, 9, 15, 10, 0),
        "series-123",
        completes_source=True,
    )

    assert block.mark_complete() is True
    assert block.is_complete is True
    assert block.hide(now=datetime(2026, 9, 15, 9, 0)) is True
    assert block.is_hidden is True
    assert block.unhide() is True
    assert block.is_hidden is False

    record = logic.CompletionRecord(
        actual_duration_minutes=95,
        performance_rating=logic.UNRATED,
    )
    assert record.is_rated() is False
    record.performance_rating = 4
    assert record.is_rated() is True

    event = logic.Assignment(
        "Essay",
        datetime(2026, 9, 18, 9, 0),
        datetime(2026, 9, 18, 10, 0),
        datetime(2026, 9, 19, 12, 0),
        "WRIT 101",
        category="essay",
        estimated_duration=timedelta(hours=1),
    )
    event.attach_completion_record(record)
    assert record.event_id == event.event_id
    assert record.course == "WRIT 101"
    assert record.assignment_category == "essay"
    assert record.planned_duration_minutes == 60
    assert record.estimate_error_minutes == 35

    event.estimated_duration = timedelta(hours=2)
    event.course = "WRIT 102"
    event.category = "research"
    assert record.planned_duration_minutes == 60
    assert record.course == "WRIT 101"
    assert record.assignment_category == "essay"


def test_duration_recommendation_uses_latest_five_with_recency_weights():
    assignment = logic.Assignment(
        "Final paper",
        datetime(2026, 9, 17, 8, 0),
        datetime(2026, 9, 17, 9, 0),
        datetime(2026, 9, 24, 18, 0),
        "CS 101",
        category="Essay",
        estimated_duration=timedelta(hours=1),
    )
    history = [
        logic.CompletionRecord(
            actual_duration_minutes=actual_minutes,
            course=" cs 101 ",
            assignment_category=" essay ",
            completed_at=datetime(2026, 9, 1 + index),
        )
        for index, actual_minutes in enumerate((1000, 10, 20, 30, 40, 50, 60))
    ]
    assistant = logic.AIAssistant()

    recommendation = assistant.recommend_assignment_duration(assignment, history[:2])
    assert recommendation is None

    recommendation = assistant.recommend_assignment_duration(assignment, history)

    assert recommendation is not None
    assert recommendation.match_scope is logic.AssignmentMatchScope.COURSE_AND_CATEGORY
    assert recommendation.sample_count == 5
    assert recommendation.duration == timedelta(minutes=47)
    assert assignment.estimated_duration == timedelta(hours=1)

    assignment.accept_duration_recommendation(recommendation)
    assert assignment.estimated_duration == timedelta(minutes=47)
    assignment.estimated_duration = timedelta(minutes=55)
    assert assignment.estimated_duration == timedelta(minutes=55)
    with pytest.raises(ValueError, match="estimated_duration must be positive"):
        assignment.estimated_duration = timedelta(0)
    assert assignment.estimated_duration == timedelta(minutes=55)


def test_duration_recommendation_falls_back_course_before_category():
    assignment = logic.Assignment(
        "New essay",
        datetime(2026, 9, 17, 8, 0),
        datetime(2026, 9, 17, 9, 0),
        datetime(2026, 9, 24, 18, 0),
        "CS 101",
        category="essay",
        estimated_duration=timedelta(hours=1),
    )
    same_course = [
        logic.CompletionRecord(
            actual_duration_minutes=minutes,
            course="CS 101",
            assignment_category=category,
            completed_at=datetime(2026, 9, day),
        )
        for minutes, category, day in (
            (60, "essay", 1),
            (120, "essay", 2),
            (180, "reading", 3),
        )
    ]
    other_courses_same_category = [
        logic.CompletionRecord(
            actual_duration_minutes=minutes,
            course=course,
            assignment_category="essay",
            completed_at=datetime(2026, 9, day),
        )
        for minutes, course, day in (
            (300, "BIO 201", 4),
            (360, "BIO 201", 5),
            (420, "HIST 301", 6),
        )
    ]
    assistant = logic.AIAssistant()

    course_recommendation = assistant.recommend_assignment_duration(
        assignment, same_course + other_courses_same_category
    )
    category_recommendation = assistant.recommend_assignment_duration(
        assignment, same_course[:2] + other_courses_same_category
    )

    assert course_recommendation is not None
    assert course_recommendation.match_scope is logic.AssignmentMatchScope.COURSE
    assert course_recommendation.sample_count == 3
    assert category_recommendation is not None
    assert category_recommendation.match_scope is logic.AssignmentMatchScope.CATEGORY
    assert category_recommendation.sample_count == 5


@pytest.mark.parametrize(
    ("decision", "expected_duration"),
    [
        (None, timedelta(hours=1)),
        (
            logic.AssignmentDurationDecision(
                logic.AssignmentDurationDecisionAction.ACCEPT
            ),
            timedelta(minutes=140),
        ),
        (
            logic.AssignmentDurationDecision(
                logic.AssignmentDurationDecisionAction.EDIT,
                timedelta(minutes=75),
            ),
            timedelta(minutes=75),
        ),
        (
            logic.AssignmentDurationDecision(
                logic.AssignmentDurationDecisionAction.KEEP_CURRENT
            ),
            timedelta(hours=1),
        ),
    ],
)
def test_generate_planner_exposes_recommendations_and_applies_only_decisions(
        decision, expected_duration):
    calendar = logic.Calendar()
    assignment = logic.Assignment(
        "History essay",
        datetime(2026, 9, 14, 8, 0),
        datetime(2026, 9, 14, 9, 0),
        datetime(2026, 9, 18, 18, 0),
        "HIST 201",
        category="essay",
        estimated_duration=timedelta(hours=1),
    )
    calendar.add_event(assignment)
    history = [
        logic.CompletionRecord(
            actual_duration_minutes=minutes,
            course="HIST 201",
            assignment_category="essay",
            completed_at=datetime(2026, 9, day),
        )
        for minutes, day in ((60, 14), (120, 15), (180, 16))
    ]
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )
    decisions = (
        {assignment.event_id: decision} if decision is not None else None
    )

    planner = logic.AIAssistant().generate_planner(
        calendar,
        preferences,
        datetime(2026, 9, 13),
        datetime(2026, 9, 20),
        history=history,
        duration_decisions=decisions,
    )

    assert len(planner.assignment_duration_reviews) == 1
    review = planner.assignment_duration_reviews[0]
    assert review.event_id == assignment.event_id
    assert review.current_duration == timedelta(hours=1)
    assert review.recommendation.duration == timedelta(minutes=140)
    assert review.decision == decision
    assert review.applied_duration == expected_duration
    assert assignment.estimated_duration == expected_duration
    placement = planner.get_assignment_placement(assignment.event_id)
    assert placement is not None
    assert placement.estimated_duration == expected_duration
    if decision is None:
        with pytest.raises(ValueError, match="need a user decision"):
            planner.approve(now=datetime(2026, 9, 14))
    else:
        planner.approve(now=datetime(2026, 9, 14))
        assert planner.status is logic.PlannerStatus.APPROVED


def test_completion_store_persists_and_supplies_history_to_planner(tmp_path):
    database_path = tmp_path / "completion-history.sqlite3"
    store = logic.CompletionRecordStore(database_path)
    assignment = logic.Assignment(
        "Research paper",
        datetime(2026, 9, 14, 8, 0),
        datetime(2026, 9, 14, 9, 0),
        datetime(2026, 9, 17, 10, 0),
        "WRIT 101",
        category="project",
        estimated_duration=timedelta(hours=2),
    )
    assistant = logic.AIAssistant()
    completed_at = datetime(2026, 9, 15, 12, 0)
    record = assistant.record_assignment_completion(
        assignment,
        timedelta(minutes=95),
        store,
        "user-123",
        completed_at=completed_at,
        notes="Focused time only",
    )
    store.add_record(
        "user-123",
        logic.CompletionRecord(
            actual_duration_minutes=125,
            course="WRIT 101",
            assignment_category="project",
            completed_at=datetime(2026, 9, 16, 12, 0),
        ),
    )
    store.add_record(
        "user-123",
        logic.CompletionRecord(
            actual_duration_minutes=155,
            course="WRIT 101",
            assignment_category="project",
            completed_at=datetime(2026, 9, 17, 12, 0),
        ),
    )

    loaded_history = logic.CompletionRecordStore(database_path).get_records(
        "user-123"
    )
    assert loaded_history[0].record_id == record.record_id
    assert loaded_history[0].event_id == assignment.event_id
    assert loaded_history[0].completed_on_time is True
    assert loaded_history[0].actual_duration_minutes == 95
    assert loaded_history[0].planned_duration_minutes == 120
    assert loaded_history[0].course == "WRIT 101"
    assert loaded_history[0].assignment_category == "project"
    assert loaded_history[0].completed_at == completed_at
    assert loaded_history[0].notes == "Focused time only"
    assert assignment.completion_record is record
    assert assignment.is_complete is True
    assignment.estimated_duration = timedelta(hours=3)
    assert record.planned_duration_minutes == 120

    calendar = logic.Calendar()
    next_assignment = logic.Assignment(
        "Next research paper",
        datetime(2026, 9, 16, 8, 0),
        datetime(2026, 9, 16, 9, 0),
        datetime(2026, 9, 19, 18, 0),
        "WRIT 101",
        category="project",
        estimated_duration=timedelta(hours=1),
    )
    calendar.add_event(next_assignment)
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )
    planner = assistant.generate_planner(
        calendar,
        preferences,
        datetime(2026, 9, 13),
        datetime(2026, 9, 20),
        completion_store=logic.CompletionRecordStore(database_path),
        user_id="user-123",
    )

    assert len(planner.assignment_duration_reviews) == 1
    assert planner.assignment_duration_reviews[0].event_id == next_assignment.event_id
    assert planner.assignment_duration_reviews[0].recommendation.sample_count == 3
    assert planner.assignment_duration_reviews[0].recommendation.duration == timedelta(
        minutes=135
    )


def test_planner_validates_blocks_and_edits_atomically():
    week_start = datetime(2026, 9, 13)
    week_end = datetime(2026, 9, 20)
    planner = logic.Planner(week_start, week_end)
    first = logic.StudyBlock(
        "Read chapter 2",
        datetime(2026, 9, 15, 9, 0),
        datetime(2026, 9, 15, 10, 0),
        "assignment-1",
    )
    planner.add_study_block(first)

    adjacent = logic.StudyBlock(
        "Review notes",
        datetime(2026, 9, 15, 10, 0),
        datetime(2026, 9, 15, 11, 0),
        "assignment-2",
    )
    planner.add_study_block(adjacent)

    with pytest.raises(ValueError):
        planner.add_study_block(
            logic.StudyBlock(
                "Overlapping session",
                datetime(2026, 9, 15, 9, 30),
                datetime(2026, 9, 15, 10, 30),
                "assignment-3",
            )
        )
    with pytest.raises(ValueError):
        planner.add_study_block(
            logic.StudyBlock(
                "Outside the week",
                datetime(2026, 9, 20),
                datetime(2026, 9, 20, 1, 0),
                "assignment-4",
            )
        )

    assert planner.edit(first.block_id, subject="Read chapter 3") == 1
    assert planner.edit(
        first.block_id,
        start_time=datetime(2026, 9, 15, 8, 0),
        end_time=datetime(2026, 9, 15, 9, 0),
    ) == 2
    original_state = (first.subject, first.start_time, first.end_time)

    with pytest.raises(ValueError):
        planner.edit(
            first.block_id,
            start_time=datetime(2026, 9, 15, 10, 0),
            end_time=datetime(2026, 9, 15, 11, 0),
        )
    assert (first.subject, first.start_time, first.end_time) == original_state

    with pytest.raises(ValueError):
        planner.edit(first.block_id, subjekt="typo")
    assert planner.edit("missing") == 0
    assert planner.edit(first.block_id, subject=first.subject) == 0
    assert planner.get_block(first.block_id) is first
    assert planner.get_block("missing") is None


def test_planner_event_links_visibility_and_reference_ids():
    week_start = datetime(2026, 9, 13)
    planner = logic.Planner(week_start, datetime(2026, 9, 20))
    series_id = "series-123"
    occurrence_start = datetime(2026, 9, 15, 9, 0)
    occurrence = logic.occurrence_id(series_id, occurrence_start)
    block = logic.StudyBlock(
        "Attend lecture",
        occurrence_start,
        datetime(2026, 9, 15, 10, 0),
        occurrence,
    )
    completed = logic.StudyBlock(
        "Finish assignment",
        datetime(2026, 9, 16, 9, 0),
        datetime(2026, 9, 16, 10, 0),
        "assignment-456",
    )
    completed.mark_complete()
    planner.add_study_block(block)
    planner.add_study_block(completed)

    assert planner.get_blocks_for_event(series_id) == [block]
    assert planner.get_blocks_for_event(occurrence) == [block]
    assert planner.get_referenced_event_ids() == {series_id, "assignment-456"}
    assert planner.get_referenced_occurrence_ids() == {occurrence}
    assert [item.source_id for item in planner.get_schedule_items()] == [
        block.block_id,
        completed.block_id,
    ]

    assert planner.hide_blocks_for_event(series_id) == 1
    assert planner.get_blocks_for_event(series_id) == []
    assert planner.get_blocks_for_event(series_id, include_hidden=True) == [block]
    assert planner.get_referenced_event_ids() == {"assignment-456"}
    assert [item.source_id for item in planner.get_schedule_items()] == [
        completed.block_id,
    ]
    assert planner.hide_blocks_for_event("assignment-456") == 0
    assert completed.is_hidden is False


def test_planner_restores_only_live_nonoverlapping_blocks_and_purges_hidden():
    planner = logic.Planner(
        datetime(2026, 9, 13),
        datetime(2026, 9, 20),
    )
    hidden = logic.StudyBlock(
        "Study session",
        datetime(2026, 9, 15, 9, 0),
        datetime(2026, 9, 15, 10, 0),
        "assignment-1",
    )
    planner.add_study_block(hidden)
    hidden.hide(now=datetime(2026, 9, 15, 8, 0))

    assert planner.needs_hidden_block(hidden, now=datetime(2026, 9, 15, 9, 30))
    assert planner.restore_blocks_for_event(
        "assignment-1",
        now=datetime(2026, 9, 15, 8, 30),
    ) == 1
    assert hidden.is_hidden is False

    hidden.hide(now=datetime(2026, 9, 15, 8, 0))
    replacement = logic.StudyBlock(
        "Replacement session",
        hidden.start_time,
        hidden.end_time,
        "assignment-2",
    )
    planner.add_study_block(replacement)
    assert planner.restore_blocks_for_event(
        "assignment-1",
        now=datetime(2026, 9, 15, 8, 30),
    ) == 0
    assert hidden.is_hidden is True

    hidden.hidden_at = datetime(2026, 9, 1)
    assert planner.purge_hidden_blocks(now=datetime(2026, 9, 20)) == 1
    assert planner.get_block(hidden.block_id) is None
    assert planner.get_block(replacement.block_id) is replacement


def test_planner_lifecycle_guards_and_past_read_only():
    week_start = datetime(2026, 9, 13)
    week_end = datetime(2026, 9, 20)
    planner = logic.Planner(week_start, week_end)
    hidden_block = logic.StudyBlock(
        "Hidden session",
        datetime(2026, 9, 15, 9, 0),
        datetime(2026, 9, 15, 10, 0),
        "assignment-hidden",
    )
    planner.add_study_block(hidden_block)
    hidden_block.hide(now=week_start + timedelta(hours=1))

    with pytest.raises(ValueError):
        planner.approve(now=week_start - timedelta(seconds=1))
    planner.approve(now=week_start)
    assert planner.status is logic.PlannerStatus.APPROVED
    with pytest.raises(ValueError):
        planner.approve(now=week_start)
    with pytest.raises(ValueError):
        planner.archive(now=week_end - timedelta(seconds=1))

    planner.archive(now=week_end)
    assert planner.status is logic.PlannerStatus.PAST
    assert repr(planner) == "<Planner 2026-09-13 status=past blocks=1>"
    with pytest.raises(ValueError):
        planner.add_study_block(
            logic.StudyBlock(
                "Study",
                datetime(2026, 9, 15, 9, 0),
                datetime(2026, 9, 15, 10, 0),
                "assignment-1",
            )
        )
    with pytest.raises(ValueError):
        planner.edit("missing", subject="Changed")
    assert planner.hide_blocks_for_event("assignment-hidden") == 0
    assert planner.restore_blocks_for_event("assignment-hidden") == 0
    assert planner.purge_hidden_blocks(
        now=week_end + timedelta(days=logic.HIDDEN_RETENTION_DAYS + 1)
    ) == 1
    assert planner.study_blocks == []


def test_user_preferences_and_calendar_store_basic_behavior():
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    assert preferences.sleep_start == time(22, 0)
    assert preferences.sleep_end == time(6, 0)
    assert preferences.preferred_study_hours == [(time(9, 0), time(11, 0))]

    event = logic.Meeting(
        "Advisor meeting",
        datetime(2026, 9, 17, 13, 0),
        datetime(2026, 9, 17, 14, 0),
        attendees=["Professor Lee"],
        location="Faculty Hall",
    )
    calendar = logic.Calendar()
    calendar.add_event(event)
    assert calendar.get_event(event.event_id) == event


def test_user_week_view_edit_and_completion_propagate_to_calendar():
    week_start = datetime(2026, 11, 1)
    week_end = datetime(2026, 11, 8)
    user = logic.User(
        "Student",
        "student@example.edu",
        logic.UserPreferences(time(23, 0), time(7, 0)),
    )
    assignment = logic.Assignment(
        "Research paper",
        datetime(2026, 11, 2, 9),
        datetime(2026, 11, 2, 10),
        datetime(2026, 11, 6),
        "BIO 101",
        "Research",
        timedelta(hours=4),
    )
    user.add_event(assignment)

    planner = logic.Planner(week_start, week_end)
    first_block = logic.StudyBlock(
        "Research paper",
        datetime(2026, 11, 2, 11),
        datetime(2026, 11, 2, 12),
        assignment.event_id,
        completes_source=True,
    )
    sibling_block = logic.StudyBlock(
        "Research paper",
        datetime(2026, 11, 3, 11),
        datetime(2026, 11, 3, 12),
        assignment.event_id,
    )
    planner.add_study_block(first_block)
    planner.add_study_block(sibling_block)
    user.add_planner(planner)

    week_items = user.get_week_view(planner.planner_id)
    assert [item.start_time for item in week_items] == sorted(
        item.start_time for item in week_items
    )
    assert len(week_items) == 3
    assert user.get_week_view("missing-planner") == []

    assert user.edit_planner(
        planner.planner_id, sibling_block.block_id, subject="Revised paper"
    ) == 1
    assert user.complete_study_block(planner.planner_id, first_block.block_id)
    assert assignment.is_complete
    assert sibling_block.is_complete
    assert not user.complete_study_block(planner.planner_id, first_block.block_id)


def test_user_hides_and_restores_event_blocks_and_purges_expired_records():
    week_start = datetime(2026, 11, 1)
    week_end = datetime(2026, 11, 8)
    user = logic.User(
        "Student",
        "student@example.edu",
        logic.UserPreferences(time(23, 0), time(7, 0)),
    )
    assignment = logic.Assignment(
        "Lab report",
        datetime(2026, 11, 2, 9),
        datetime(2026, 11, 2, 10),
        datetime(2026, 11, 6),
        "BIO 101",
        "Lab",
        timedelta(hours=3),
    )
    user.add_event(assignment)
    planner = logic.Planner(week_start, week_end)
    block = logic.StudyBlock(
        "Lab report",
        datetime(2026, 11, 2, 11),
        datetime(2026, 11, 2, 12),
        assignment.event_id,
    )
    planner.add_study_block(block)
    user.add_planner(planner)

    hidden_at = datetime(2026, 11, 1)
    assert user.hide_event(assignment.event_id, now=hidden_at)
    assert block.is_hidden
    assert not user.hide_event(assignment.event_id, now=hidden_at)
    assert user.unhide_event(
        assignment.event_id, now=datetime(2026, 11, 2)
    ) == 1
    assert not block.is_hidden

    assert user.hide_event(assignment.event_id, now=hidden_at)
    purge_result = user.purge_hidden(
        now=hidden_at + timedelta(days=logic.HIDDEN_RETENTION_DAYS + 1)
    )
    assert purge_result == logic.PurgeResult(
        events_destroyed=1,
        blocks_destroyed=1,
        overrides_destroyed=0,
    )
    assert user.calendar.get_event(assignment.event_id) is None
    assert planner.study_blocks == []


def test_user_discards_only_expired_drafts_and_keeps_planners_chronological():
    user = logic.User(
        "Student",
        "student@example.edu",
        logic.UserPreferences(time(23, 0), time(7, 0)),
    )
    old_start = datetime(2026, 10, 25)
    expired_draft = logic.Planner(old_start, old_start + timedelta(days=7))
    approved = logic.Planner(old_start, old_start + timedelta(days=7))
    approved.status = logic.PlannerStatus.APPROVED
    future_start = datetime(2026, 11, 1)
    future_draft = logic.Planner(future_start, future_start + timedelta(days=7))

    user.add_planner(future_draft)
    user.add_planner(approved)
    user.add_planner(expired_draft)

    assert [planner.week_start for planner in user.planners] == sorted(
        planner.week_start for planner in user.planners
    )
    assert user.planners == [approved, expired_draft, future_draft]
    assert user.discard_expired_draft_planners(now=future_start) == 1
    assert user.planners == [approved, future_draft]
    assert user.view_planner_history() == []
    approved.status = logic.PlannerStatus.PAST
    assert user.view_planner_history() == [approved]


def test_user_planner_approval_requires_confirmation_and_replaces_atomically():
    week_start = datetime(2026, 11, 1)
    week_end = datetime(2026, 11, 8)
    now = week_start + timedelta(hours=1)
    user = logic.User(
        "Student",
        "student@example.edu",
        logic.UserPreferences(time(23, 0), time(7, 0)),
    )
    current_plan = logic.Planner(week_start, week_end)
    replacement = logic.Planner(week_start, week_end)
    alternative_draft = logic.Planner(week_start, week_end)
    for planner in (current_plan, replacement, alternative_draft):
        user.add_planner(planner)

    assert user.approve_planner(current_plan.planner_id, now=now)
    preapproved_duplicate = logic.Planner(week_start, week_end)
    preapproved_duplicate.status = logic.PlannerStatus.APPROVED
    with pytest.raises(ValueError, match="already approved"):
        user.add_planner(preapproved_duplicate)

    assert not user.approve_planner(replacement.planner_id, now=now)
    assert current_plan.status is logic.PlannerStatus.APPROVED
    assert replacement.status is logic.PlannerStatus.DRAFT
    assert alternative_draft.status is logic.PlannerStatus.DRAFT

    with pytest.raises(ValueError):
        user.approve_planner(
            replacement.planner_id,
            replace_approved=True,
            now=week_start - timedelta(seconds=1),
        )
    assert current_plan.status is logic.PlannerStatus.APPROVED
    assert replacement.status is logic.PlannerStatus.DRAFT

    assert user.approve_planner(
        replacement.planner_id,
        replace_approved=True,
        now=now,
    )
    assert current_plan.status is logic.PlannerStatus.DRAFT
    assert replacement.status is logic.PlannerStatus.APPROVED
    assert alternative_draft.status is logic.PlannerStatus.DRAFT
    assert sum(
        planner.status is logic.PlannerStatus.APPROVED
        for planner in user.planners
    ) == 1


def test_user_planner_lifecycle_archives_and_discards_at_week_end():
    week_start = datetime(2026, 11, 1)
    week_end = datetime(2026, 11, 8)
    user = logic.User(
        "Student",
        "student@example.edu",
        logic.UserPreferences(time(23, 0), time(7, 0)),
    )
    approved = logic.Planner(week_start, week_end)
    draft_one = logic.Planner(week_start, week_end)
    draft_two = logic.Planner(week_start, week_end)
    next_week = logic.Planner(week_end, week_end + timedelta(days=7))
    for planner in (approved, draft_one, draft_two, next_week):
        user.add_planner(planner)
    assert user.approve_planner(
        approved.planner_id, now=week_start
    )

    assert user.advance_planner_lifecycle(week_end - timedelta(microseconds=1)) == (
        0,
        0,
    )
    assert approved.status is logic.PlannerStatus.APPROVED
    assert user.advance_planner_lifecycle(week_end) == (1, 2)
    assert approved.status is logic.PlannerStatus.PAST
    assert user.planners == [approved, next_week]
    assert user.view_planner_history() == [approved]

    assert user.advance_planner_lifecycle(week_end) == (0, 0)


def test_admin_manages_users_idempotently_and_rejects_unknown_actions():
    admin = logic.Admin("Administrator", "admin@example.edu")
    user = logic.User(
        "Student",
        "student@example.edu",
        logic.UserPreferences(time(23, 0), time(7, 0)),
    )

    admin.manage_users("add", user)
    admin.manage_users("add", user)
    assert admin._managed_users == [user]
    admin.manage_users("remove", user)
    admin.manage_users("remove", user)
    assert admin._managed_users == []
    with pytest.raises(ValueError, match="action must be"):
        admin.manage_users("Add", user)
    assert admin.view_system_logs() == []


def test_login_requires_verified_password_and_cleans_up_only_after_success(
    tmp_path,
):
    deliveries: list[tuple[str, str]] = []
    account_store = logic.UserAccountStore(
        tmp_path / "login-accounts.sqlite3",
        lambda email, token: deliveries.append((email, token)),
    )
    now = datetime.now(logic.timezone.utc)
    user_id = account_store.request_email_verification(
        "student@example.edu", now=now - timedelta(days=2)
    )
    with pytest.raises(ValueError, match="valid, unexpired"):
        account_store.set_password(
            "not-a-setup-token", "correct horse battery", now=now
        )

    preferences = logic.UserPreferences(time(23, 0), time(7, 0))
    with pytest.raises(ValueError, match="verification and password"):
        account_store.create_user("Student", "student@example.edu", preferences)

    verified_at = now - timedelta(days=2)
    setup_token = account_store.verify_email(deliveries[0][1], now=verified_at)
    assert setup_token is not None
    with pytest.raises(ValueError, match="at least"):
        account_store.set_password(setup_token, "short", now=verified_at)
    account_store.set_password(
        setup_token, "correct horse battery", now=verified_at
    )
    user = account_store.create_user(
        "Student", "student@example.edu", preferences
    )
    assert user.user_id == user_id

    old_event_time = now - timedelta(days=20)
    event = logic.Meeting(
        "Old hidden event",
        old_event_time.replace(tzinfo=None),
        (old_event_time + timedelta(hours=1)).replace(tzinfo=None),
        attendees=[],
        location="Campus",
    )
    user.add_event(event)
    assert user.calendar.hide_event(
        event.event_id,
        now=(now - timedelta(days=logic.HIDDEN_RETENTION_DAYS + 1)).replace(
            tzinfo=None
        ),
    )
    old_week_end = (now - timedelta(days=1)).replace(tzinfo=None)
    expired_draft = logic.Planner(
        old_week_end - timedelta(days=7), old_week_end
    )
    user.add_planner(expired_draft)

    assert not user.login("wrong password string", account_store)
    assert user.calendar.get_event(event.event_id) is event
    assert user.planners == [expired_draft]
    assert user.login("correct horse battery", account_store)
    assert user.calendar.get_event(event.event_id) is None
    assert user.planners == []


def test_email_validation_and_normalization_for_user_and_admin():
    preferences = logic.UserPreferences(time(23, 0), time(7, 0))
    user = logic.User("Student", "  STUDENT@Example.edu ", preferences)
    admin = logic.Admin("Administrator", " ADMIN@Example.edu ")

    assert isinstance(user, logic.Person)
    assert isinstance(admin, logic.Person)
    assert user.name == "Student"
    assert admin.name == "Administrator"
    assert user.email == "student@example.edu"
    assert admin.email == "admin@example.edu"
    for invalid_email in (
        "",
        "   ",
        "missing-at.example.com",
        "student@localhost",
        "student..name@example.com",
        "student@-example.com",
    ):
        with pytest.raises(ValueError):
            logic.User("Student", invalid_email, preferences)
        with pytest.raises(ValueError):
            logic.Admin("Administrator", invalid_email)
    with pytest.raises(TypeError):
        logic.normalize_email(cast(str, None))


def test_user_account_store_enforces_normalized_uniqueness_and_verification(
    tmp_path,
):
    deliveries: list[tuple[str, str]] = []
    store = logic.UserAccountStore(
        tmp_path / "accounts.sqlite3",
        lambda email, token: deliveries.append((email, token)),
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=logic.timezone.utc)

    assert store.is_email_available("STUDENT@example.edu")
    user_id = store.request_email_verification(
        " Student@Example.edu ", now=now
    )
    assert deliveries[0][0] == "student@example.edu"
    assert not store.is_email_available("student@example.edu")
    assert not store.is_email_verified(user_id)
    with pytest.raises(ValueError, match="valid, unexpired"):
        store.set_password(
            "not-a-setup-token",
            "correct horse battery",
            now=now,
        )
    assert not store.verify_email("not-the-token")
    assert not store.verify_email(
        deliveries[0][1],
        now=now + logic.EMAIL_VERIFICATION_TTL,
    )
    assert not store.send_verification_email(
        "student@example.edu",
        now=now + logic.EMAIL_VERIFICATION_RESEND_DELAY
        - timedelta(seconds=1),
    )
    assert store.send_verification_email(
        "student@example.edu",
        now=now + logic.EMAIL_VERIFICATION_RESEND_DELAY,
    )
    assert not store.verify_email(deliveries[0][1])
    setup_token = store.verify_email(
        deliveries[1][1],
        now=now + logic.EMAIL_VERIFICATION_RESEND_DELAY,
    )
    assert setup_token is not None
    assert store.is_email_verified(user_id)
    assert not store.verify_email(deliveries[1][1])

    setup_expired_at = (
        now
        + logic.EMAIL_VERIFICATION_RESEND_DELAY
        + logic.PASSWORD_SETUP_TTL
    )
    with pytest.raises(ValueError, match="valid, unexpired"):
        store.set_password(
            setup_token,
            "correct horse battery",
            now=setup_expired_at,
        )
    assert store.send_verification_email(
        "student@example.edu",
        now=setup_expired_at + logic.EMAIL_VERIFICATION_RESEND_DELAY,
    )
    setup_token = deliveries[2][1]

    preferences = logic.UserPreferences(time(23, 0), time(7, 0))
    with pytest.raises(ValueError, match="at least"):
        store.set_password(setup_token, "short", now=setup_expired_at)
    setup_at = setup_expired_at + logic.EMAIL_VERIFICATION_RESEND_DELAY
    store.set_password(setup_token, "correct horse battery", now=setup_at)
    with pytest.raises(ValueError, match="valid, unexpired"):
        store.set_password(
            setup_token, "another correct horse", now=setup_at
        )
    user = store.create_user("Student", "student@example.edu", preferences)
    assert user.user_id == user_id
    assert store.authenticate("STUDENT@example.edu", "incorrect password") is None
    assert store.authenticate(
        "STUDENT@example.edu", "correct horse battery"
    ) == user_id

    duplicate = logic.User("Another student", "student@EXAMPLE.edu", preferences)
    with pytest.raises(ValueError, match="already registered"):
        store.request_email_verification(duplicate.email, now=setup_at)
    assert len(deliveries) == 3

    with sqlite3.connect(tmp_path / "accounts.sqlite3") as connection:
        stored_hash = connection.execute(
            "SELECT password_hash FROM user_accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    assert stored_hash.startswith("$argon2id$")
    assert stored_hash != "correct horse battery"

    reloaded_store = logic.UserAccountStore(
        tmp_path / "accounts.sqlite3",
        lambda email, token: deliveries.append((email, token)),
    )
    assert reloaded_store.is_email_verified(user.user_id)
    assert not reloaded_store.send_verification_email(user.email)


def test_user_account_store_can_resend_verification_after_delivery_failure(
    tmp_path,
):
    deliveries: list[tuple[str, str]] = []

    def failing_sender(email: str, token: str) -> None:
        assert email and token
        raise RuntimeError("email service unavailable")

    store = logic.UserAccountStore(
        tmp_path / "accounts.sqlite3", failing_sender
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=logic.timezone.utc)
    with pytest.raises(RuntimeError, match="email service unavailable"):
        store.request_email_verification("student@example.edu", now=now)

    retry_store = logic.UserAccountStore(
        tmp_path / "accounts.sqlite3",
        lambda email, token: deliveries.append((email, token)),
    )
    assert retry_store.send_verification_email(
        "student@example.edu",
        now=now + logic.EMAIL_VERIFICATION_RESEND_DELAY,
    )
    assert deliveries[0][0] == "student@example.edu"
    setup_token = retry_store.verify_email(
        deliveries[0][1],
        now=now + logic.EMAIL_VERIFICATION_RESEND_DELAY,
    )
    assert setup_token is not None
    retry_store.set_password(
        setup_token,
        "correct horse battery",
        now=now + logic.EMAIL_VERIFICATION_RESEND_DELAY,
    )


def test_pending_email_verification_accounts_expire_and_release_email(tmp_path):
    deliveries: list[tuple[str, str]] = []
    store = logic.UserAccountStore(
        tmp_path / "pending-expiry.sqlite3",
        lambda email, token: deliveries.append((email, token)),
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=logic.timezone.utc)
    store.request_email_verification("student@example.edu", now=now)
    assert not store.is_email_available("student@example.edu")
    assert store.expire_pending_registrations(
        now=now + logic.PENDING_ACCOUNT_TTL - timedelta(microseconds=1)
    ) == 0
    assert store.expire_pending_registrations(
        now=now + logic.PENDING_ACCOUNT_TTL
    ) == 1
    assert store.is_email_available("student@example.edu")


def test_user_account_store_migrates_existing_email_verification_schema(
    tmp_path,
):
    database_path = tmp_path / "legacy-accounts.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE user_accounts (
                user_id TEXT PRIMARY KEY,
                normalized_email TEXT NOT NULL UNIQUE,
                email_verified INTEGER NOT NULL DEFAULT 0,
                verification_token_hash TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO user_accounts (
                user_id, normalized_email, email_verified
            ) VALUES ('legacy-user', 'legacy@example.edu', 1)
            """
        )

    def unused_sender(email: str, token: str) -> None:
        assert email and token

    store = logic.UserAccountStore(database_path, unused_sender)
    assert store.is_email_verified("legacy-user")
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(user_accounts)"
            )
        }
    assert {
        "password_hash",
        "password_setup_token_hash",
        "created_at",
    }.issubset(columns)


def test_login_rate_limit_expires_and_correct_password_resets_failures(tmp_path):
    deliveries: list[tuple[str, str]] = []
    store = logic.UserAccountStore(
        tmp_path / "login-lockout.sqlite3",
        lambda email, token: deliveries.append((email, token)),
    )
    now = datetime(2026, 10, 20, 12, 0, tzinfo=logic.timezone.utc)
    user_id = store.request_email_verification(
        "student@example.edu", now=now
    )
    setup_token = store.verify_email(deliveries[0][1], now=now)
    assert setup_token is not None
    store.set_password(setup_token, "correct horse battery", now=now)

    for _ in range(logic.MAX_LOGIN_ATTEMPTS):
        assert store.authenticate(
            "student@example.edu", "incorrect password", now=now
        ) is None
    assert store.authenticate(
        "student@example.edu",
        "correct horse battery",
        now=now + logic.LOGIN_LOCKOUT_DURATION - timedelta(seconds=1),
    ) is None
    assert store.authenticate(
        "student@example.edu",
        "correct horse battery",
        now=now + logic.LOGIN_LOCKOUT_DURATION,
    ) == user_id
    assert store.authenticate(
        "student@example.edu", "incorrect password", now=now
    ) is None


def test_analyze_calendar_returns_visible_events_overlapping_week():
    calendar = logic.Calendar()
    before = logic.Meeting(
        "Prior meeting",
        datetime(2026, 9, 12, 23, 0),
        datetime(2026, 9, 13, 1, 0),
    )
    inside = logic.Meeting(
        "Week meeting",
        datetime(2026, 9, 15, 13, 0),
        datetime(2026, 9, 15, 14, 0),
    )
    hidden = logic.Meeting(
        "Hidden meeting",
        datetime(2026, 9, 16, 13, 0),
        datetime(2026, 9, 16, 14, 0),
    )
    hidden.hide(now=datetime(2026, 9, 14))
    for event in (before, inside, hidden):
        calendar.add_event(event)

    events = logic.AIAssistant().analyze_calendar(
        calendar,
        datetime(2026, 9, 13),
        datetime(2026, 9, 20),
    )

    assert events == [before, inside]
    with pytest.raises(ValueError, match="week_end must be later"):
        logic.AIAssistant().analyze_calendar(
            calendar,
            datetime(2026, 9, 20),
            datetime(2026, 9, 13),
        )


def test_adapt_from_history_calculates_lateness_ratings_and_neutral_case():
    assistant = logic.AIAssistant()
    records = [
        logic.CompletionRecord(
            completed_on_time=False,
            performance_rating=1,
        ),
        logic.CompletionRecord(
            completed_on_time=False,
            performance_rating=5,
        ),
        logic.CompletionRecord(
            completed_on_time=True,
            performance_rating=logic.UNRATED,
        ),
    ]

    adjustment = assistant.adapt_from_history(records)
    neutral = assistant.adapt_from_history([])

    assert isinstance(adjustment, logic.HistoryAdjustment)
    assert adjustment.late_ratio == pytest.approx(2 / 3)
    assert adjustment.avg_rating == 3
    assert adjustment.buffer_multiplier == pytest.approx(4 / 3)
    assert "missed its deadline" in adjustment.notes
    assert "middle of the scale" in adjustment.notes
    assert neutral == logic.HistoryAdjustment(
        buffer_multiplier=1.0,
        late_ratio=0.0,
        avg_rating=None,
        notes="No completion history yet; using a neutral adjustment.",
    )
    with pytest.raises(TypeError, match="CompletionRecord"):
        assistant.adapt_from_history([cast(logic.CompletionRecord, object())])


def test_history_adjustment_is_typed_validated_and_immutable():
    adjustment = logic.HistoryAdjustment(1.0, 0.0, None, "No history")

    with pytest.raises((AttributeError, TypeError)):
        setattr(adjustment, "buffer_multiplier", 2.0)
    with pytest.raises(ValueError, match="buffer_multiplier must be positive"):
        logic.HistoryAdjustment(0.0, 0.0, None, "")
    with pytest.raises(ValueError, match="late_ratio must be between 0 and 1"):
        logic.HistoryAdjustment(1.0, 1.1, None, "")
    with pytest.raises(TypeError, match="adjustment must be a HistoryAdjustment"):
        logic.AIAssistant().optimize_schedule(
            [],
            logic.UserPreferences(sleep_start=time(22, 0), sleep_end=time(6, 0)),
            cast(logic.HistoryAdjustment, {"buffer_multiplier": 1.0}),
        )


def test_optimize_schedule_prefers_preferred_study_hours():
    event = logic.Assignment(
        "History review",
        datetime(2026, 9, 17, 14, 0),
        datetime(2026, 9, 17, 15, 0),
        datetime(2026, 9, 19, 18, 0),
        "HIST 201",
        category="reading",
        estimated_duration=timedelta(hours=1),
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [event], preferences, logic.HistoryAdjustment(1.0, 0.0, None, "")
    )
    blocks = result.blocks

    assert len(blocks) == 1
    assert blocks[0].start_time == datetime(2026, 9, 19, 10, 0)
    assert blocks[0].end_time == datetime(2026, 9, 19, 11, 0)
    assert blocks[0].source_event_id == event.event_id
    assert blocks[0].completes_source is True
    placement = result.get_assignment_placement(event.event_id)
    assert placement is not None
    assert placement.state is logic.AssignmentPlacementState.FULLY_SCHEDULED
    assert placement.estimated_duration == timedelta(hours=1)
    assert placement.scheduled_duration == timedelta(hours=1)
    assert placement.remaining_duration == timedelta(0)
    assert placement.recommended_actions == ()


def test_optimize_schedule_respects_maximum_daily_study_hours():
    assignments: list[logic.Event] = [
        logic.Assignment(
            f"Assignment {index}",
            datetime(2026, 9, 14),
            datetime(2026, 9, 14, 1),
            datetime(2026, 9, 15),
            "HIST 201",
            category="homework",
            estimated_duration=timedelta(hours=2),
        )
        for index in range(2)
    ]
    max_daily_study = timedelta(hours=3)
    preferences = logic.UserPreferences(
        sleep_start=time(22),
        sleep_end=time(6),
        max_study_hours_per_day=max_daily_study,
    )

    result = logic.AIAssistant().optimize_schedule(
        assignments,
        preferences,
        logic.HistoryAdjustment(1.0, 0.0, None, ""),
        week_start=datetime(2026, 9, 14),
        week_end=datetime(2026, 9, 15),
    )

    total_study = sum(
        (block.get_duration() for block in result.blocks),
        timedelta(0),
    )
    assert total_study == timedelta(hours=3)
    assert all(
        sum(
            (
                block.get_duration()
                for block in result.blocks
                if block.start_time.date() == day
            ),
            timedelta(0),
        ) <= max_daily_study
        for day in {block.start_time.date() for block in result.blocks}
    )


def test_optimize_schedule_falls_back_to_other_awake_hours():
    event = logic.Assignment(
        "History review",
        datetime(2026, 9, 17, 8, 0),
        datetime(2026, 9, 17, 10, 0),
        datetime(2026, 9, 17, 10, 0),
        "HIST 201",
        category="essay",
        estimated_duration=timedelta(hours=2),
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(10, 0), time(11, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [event], preferences, logic.HistoryAdjustment(1.0, 0.0, None, "")
    )
    blocks = result.blocks

    assert len(blocks) == 1
    assert blocks[0].start_time == datetime(2026, 9, 17, 8, 0)
    assert blocks[0].end_time == datetime(2026, 9, 17, 10, 0)


def test_optimize_schedule_does_not_place_blocks_during_overnight_sleep():
    event = logic.Assignment(
        "History review",
        datetime(2026, 9, 17, 3, 0),
        datetime(2026, 9, 17, 4, 0),
        datetime(2026, 9, 17, 4, 0),
        "HIST 201",
        category="reading",
        estimated_duration=timedelta(hours=1),
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(1, 0), time(2, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [event], preferences, logic.HistoryAdjustment(1.0, 0.0, None, "")
    )

    assert result.blocks == []
    placement = result.get_assignment_placement(event.event_id)
    assert placement is not None
    assert placement.state is logic.AssignmentPlacementState.NOT_SCHEDULED
    assert placement.remaining_duration == timedelta(hours=1)
    assert placement.issue is logic.AssignmentPlacementIssue.INSUFFICIENT_AVAILABLE_TIME
    assert placement.recommended_actions[0] is logic.SchedulingAction.REVIEW_OPEN_TIME
    assert set(placement.recommended_actions) == set(logic.SchedulingAction)


def test_optimize_schedule_splits_assignment_work_before_due_date():
    event = logic.Assignment(
        "Research paper",
        datetime(2026, 9, 17, 8, 0),
        datetime(2026, 9, 17, 9, 0),
        datetime(2026, 9, 19, 18, 0),
        "WRIT 101",
        category="project",
        estimated_duration=timedelta(hours=5),
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [event], preferences, logic.HistoryAdjustment(1.0, 0.0, None, "")
    )
    blocks_by_start = sorted(result.blocks, key=lambda block: block.start_time)

    assert [(block.start_time, block.end_time) for block in blocks_by_start] == [
        (datetime(2026, 9, 17, 10, 0), datetime(2026, 9, 17, 11, 0)),
        (datetime(2026, 9, 18, 9, 0), datetime(2026, 9, 18, 11, 0)),
        (datetime(2026, 9, 19, 9, 0), datetime(2026, 9, 19, 11, 0)),
    ]
    assert [block.completes_source for block in blocks_by_start] == [False, False, True]
    placement = result.get_assignment_placement(event.event_id)
    assert placement is not None
    assert placement.state is logic.AssignmentPlacementState.FULLY_SCHEDULED
    assert placement.estimated_duration == timedelta(hours=5)
    assert placement.scheduled_duration == timedelta(hours=5)
    assert placement.remaining_duration == timedelta(0)


def test_optimize_schedule_prioritizes_priority_then_longest_estimate():
    start = datetime(2026, 9, 17, 9, 0)
    deadline = datetime(2026, 9, 17, 11, 0)
    highest_priority_long = logic.Assignment(
        "High-priority long assignment",
        start,
        start + timedelta(hours=1),
        deadline,
        "HIST 201",
        category="essay",
        estimated_duration=timedelta(hours=2),
        priority=logic.Priority.HIGH,
    )
    highest_priority_short = logic.Assignment(
        "High-priority short assignment",
        start,
        start + timedelta(hours=1),
        deadline,
        "HIST 201",
        category="reading",
        estimated_duration=timedelta(hours=1),
        priority=logic.Priority.HIGH,
    )
    lower_priority = logic.Assignment(
        "Low-priority assignment",
        start,
        start + timedelta(hours=1),
        deadline,
        "HIST 201",
        category="reading",
        estimated_duration=timedelta(hours=1),
        priority=logic.Priority.LOW,
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [lower_priority, highest_priority_short, highest_priority_long],
        preferences,
        logic.HistoryAdjustment(1.0, 0.0, None, ""),
    )

    long_placement = result.get_assignment_placement(highest_priority_long.event_id)
    short_placement = result.get_assignment_placement(highest_priority_short.event_id)
    low_placement = result.get_assignment_placement(lower_priority.event_id)
    assert long_placement is not None
    assert short_placement is not None
    assert low_placement is not None
    assert long_placement.state is logic.AssignmentPlacementState.FULLY_SCHEDULED
    assert long_placement.scheduled_duration == timedelta(hours=2)
    assert short_placement.state is logic.AssignmentPlacementState.NOT_SCHEDULED
    assert low_placement.state is logic.AssignmentPlacementState.NOT_SCHEDULED


def test_optimize_schedule_prioritizes_earlier_deadline_when_priority_ties():
    start = datetime(2026, 9, 17, 9, 0)
    earlier_deadline = logic.Assignment(
        "Earlier deadline",
        start,
        start + timedelta(hours=1),
        datetime(2026, 9, 17, 10, 0),
        "HIST 201",
        category="essay",
        estimated_duration=timedelta(hours=1),
        priority=logic.Priority.HIGH,
    )
    later_deadline = logic.Assignment(
        "Later deadline",
        start,
        start + timedelta(hours=1),
        datetime(2026, 9, 17, 11, 0),
        "HIST 201",
        category="reading",
        estimated_duration=timedelta(hours=1),
        priority=logic.Priority.HIGH,
    )
    fixed_event = logic.ExtracurricularEvent(
        "Fixed commitment",
        datetime(2026, 9, 17, 10, 0),
        datetime(2026, 9, 17, 11, 0),
        "Campus",
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [later_deadline, fixed_event, earlier_deadline],
        preferences,
        logic.HistoryAdjustment(1.0, 0.0, None, ""),
    )

    earlier_placement = result.get_assignment_placement(earlier_deadline.event_id)
    later_placement = result.get_assignment_placement(later_deadline.event_id)
    assert earlier_placement is not None
    assert later_placement is not None
    assert earlier_placement.state is logic.AssignmentPlacementState.FULLY_SCHEDULED
    assert earlier_placement.scheduled_duration == timedelta(hours=1)
    assert later_placement.state is logic.AssignmentPlacementState.NOT_SCHEDULED


def test_optimize_schedule_reports_partial_assignment_and_actions():
    event = logic.Assignment(
        "Long project",
        datetime(2026, 9, 17, 8, 0),
        datetime(2026, 9, 17, 9, 0),
        datetime(2026, 9, 19, 18, 0),
        "WRIT 101",
        category="project",
        estimated_duration=timedelta(hours=7),
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [event], preferences, logic.HistoryAdjustment(1.0, 0.0, None, "")
    )
    placement = result.get_assignment_placement(event.event_id)

    assert placement is not None
    assert placement.state is logic.AssignmentPlacementState.PARTIALLY_SCHEDULED
    assert placement.estimated_duration == timedelta(hours=7)
    assert placement.planned_duration == timedelta(hours=7)
    assert placement.scheduled_duration == timedelta(hours=6)
    assert placement.remaining_duration == timedelta(hours=1)
    assert placement.issue is logic.AssignmentPlacementIssue.INSUFFICIENT_AVAILABLE_TIME
    assert placement.recommended_actions == (
        logic.SchedulingAction.REVIEW_OPEN_TIME,
        logic.SchedulingAction.EDIT_ESTIMATE,
        logic.SchedulingAction.ADJUST_STUDY_PREFERENCES,
        logic.SchedulingAction.REVIEW_DEADLINE,
        logic.SchedulingAction.SCHEDULE_MANUALLY,
    )


def test_optimize_schedule_reports_deadline_before_start_and_action_order():
    event = logic.Assignment(
        "Late assignment",
        datetime(2026, 9, 19, 10, 0),
        datetime(2026, 9, 19, 11, 0),
        datetime(2026, 9, 18, 17, 0),
        "WRIT 101",
        category="essay",
        estimated_duration=timedelta(hours=2),
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
    )

    result = logic.AIAssistant().optimize_schedule(
        [event], preferences, logic.HistoryAdjustment(1.0, 0.0, None, "")
    )
    placement = result.get_assignment_placement(event.event_id)

    assert result.blocks == []
    assert placement is not None
    assert placement.state is logic.AssignmentPlacementState.NOT_SCHEDULED
    assert placement.issue is logic.AssignmentPlacementIssue.DEADLINE_BEFORE_START
    assert placement.recommended_actions == (
        logic.SchedulingAction.REVIEW_DEADLINE,
        logic.SchedulingAction.EDIT_ESTIMATE,
        logic.SchedulingAction.REVIEW_OPEN_TIME,
        logic.SchedulingAction.ADJUST_STUDY_PREFERENCES,
        logic.SchedulingAction.SCHEDULE_MANUALLY,
    )


def test_generate_planner_runs_pipeline_and_keeps_placement_statuses():
    calendar = logic.Calendar()
    assignment = logic.Assignment(
        "Research project",
        datetime(2026, 9, 14, 8, 0),
        datetime(2026, 9, 14, 9, 0),
        datetime(2026, 9, 24, 18, 0),
        "WRIT 101",
        category="project",
        estimated_duration=timedelta(hours=15),
    )
    fixed = logic.Meeting(
        "Advisor meeting",
        datetime(2026, 9, 15, 9, 30),
        datetime(2026, 9, 15, 10, 30),
    )
    calendar.add_event(assignment)
    calendar.add_event(fixed)
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    planner = logic.AIAssistant().generate_planner(
        calendar,
        preferences,
        datetime(2026, 9, 13),
        datetime(2026, 9, 20),
    )

    assert planner.status is logic.PlannerStatus.DRAFT
    assert planner.assignment_placements
    placement = planner.get_assignment_placement(assignment.event_id)
    assert placement is not None
    assert placement.state is logic.AssignmentPlacementState.PARTIALLY_SCHEDULED
    assert placement.scheduled_duration > timedelta(0)
    assert placement.scheduled_duration + placement.remaining_duration == timedelta(hours=15)
    assert all(
        planner.week_start <= block.start_time < block.end_time <= planner.week_end
        for block in planner.study_blocks
    )
    assert all(
        not logic.overlaps(
            block.start_time,
            block.end_time,
            fixed.start_time,
            fixed.end_time,
        )
        for block in planner.study_blocks
    )
    assert any(block.source_event_id == assignment.event_id for block in planner.study_blocks)


def test_optimize_schedule_avoids_fixed_events_and_other_study_blocks():
    first = logic.Assignment(
        "First assignment",
        datetime(2026, 9, 17, 8, 0),
        datetime(2026, 9, 17, 9, 0),
        datetime(2026, 9, 17, 11, 0),
        "HIST 201",
        category="essay",
        estimated_duration=timedelta(hours=1),
    )
    second = logic.Assignment(
        "Second assignment",
        datetime(2026, 9, 17, 8, 0),
        datetime(2026, 9, 17, 9, 0),
        datetime(2026, 9, 17, 11, 0),
        "WRIT 101",
        category="essay",
        estimated_duration=timedelta(hours=1),
    )
    fixed_event = logic.Meeting(
        "Advisor meeting",
        datetime(2026, 9, 17, 9, 30),
        datetime(2026, 9, 17, 10, 30),
    )
    preferences = logic.UserPreferences(
        sleep_start=time(22, 0),
        sleep_end=time(6, 0),
        preferred_study_hours=[(time(9, 0), time(11, 0))],
    )

    result = logic.AIAssistant().optimize_schedule(
        [first, second, fixed_event], preferences,
        logic.HistoryAdjustment(1.0, 0.0, None, ""),
    )
    blocks = result.blocks

    assert len(blocks) == 2
    assert any(block.start_time == fixed_event.end_time for block in blocks)
    assert all(
        not logic.overlaps(
            block.start_time,
            block.end_time,
            fixed_event.start_time,
            fixed_event.end_time,
        )
        for block in blocks
    )
    assert not logic.overlaps(
        blocks[0].start_time,
        blocks[0].end_time,
        blocks[1].start_time,
        blocks[1].end_time,
    )


# Note: Some parts of this scheduling logic are not cleanly testable in isolation,
# such as the AI assistant's planner-generation heuristics, user-interface flows,
# and any behavior that depends on real-time app state, external persistence, or
# human feedback. The tests above focus on the deterministic logic that can be
# exercised directly in Python.
