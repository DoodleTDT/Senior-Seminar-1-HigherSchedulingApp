# Constraints to follow:
# 1. User sleeps from 10 PM to 6 AM.
# 2. User prefers to study from 12 PM to 3 PM.
# 3. User has a maximum of 4 hours available for study each day.
# 4. User has 5 assignments, 1 due on Monday (Biology), 2 due on Thursday (Chemistry and Physics), 
#    1 due on Friday (Biology), and 1 due on Saturday (Physics).
# 5. User states assignments take 2 hours to complete, except for Biology assignments which take 
#    4 hours.
# 6. User has volunteer work on Wednesday from 2 PM to 5 PM.
# 7. Biology assignments have highest priority, followed by Physics, then Chemistry.
# 8. User takes every Sunday off (recurring weekly).
# 9. Week starts on Sunday and ends on Saturday.
# _____________________________________________________________________________

from collections import defaultdict
from datetime import date, datetime, time, timedelta

import HigherScheduling_logic as logic


def generate_weekly_planner() -> logic.Planner:
    """Create and display a draft planner for the sample week."""
    # The planner uses a Sunday-to-Saturday half-open week.
    week_start = datetime(2026, 9, 13)
    week_end = week_start + timedelta(days=7)
    monday_due = datetime(2026, 9, 14)
    thursday_due = datetime(2026, 9, 17)
    friday_due = datetime(2026, 9, 18)
    saturday_due = datetime(2026, 9, 20)

    assignments = [
        logic.Assignment(
            "Biology assignment due Monday",
            week_start,
            monday_due,
            datetime(2026, 9, 15),
            "Biology",
            "homework",
            timedelta(hours=4),
            priority=logic.Priority.HIGH,
        ),
        logic.Assignment(
            "Chemistry assignment due Thursday",
            week_start,
            thursday_due,
            datetime(2026, 9, 18),
            "Chemistry",
            "homework",
            timedelta(hours=2),
            priority=logic.Priority.LOW,
        ),
        logic.Assignment(
            "Physics assignment due Thursday",
            week_start,
            thursday_due,
            datetime(2026, 9, 18),
            "Physics",
            "homework",
            timedelta(hours=2),
            priority=logic.Priority.MEDIUM,
        ),
        logic.Assignment(
            "Biology assignment due Friday",
            week_start,
            friday_due,
            datetime(2026, 9, 19),
            "Biology",
            "homework",
            timedelta(hours=4),
            priority=logic.Priority.HIGH,
        ),
        logic.Assignment(
            "Physics assignment due Saturday",
            week_start,
            saturday_due,
            saturday_due,
            "Physics",
            "homework",
            timedelta(hours=2),
            priority=logic.Priority.MEDIUM,
        ),
    ]

    calendar = logic.Calendar()
    for assignment in assignments:
        calendar.add_event(assignment)

    # The volunteer event is fixed calendar time, so generated study blocks
    # must fit around it.
    volunteer_start = datetime(2026, 9, 16, 14)
    calendar.add_event(
        logic.ExtracurricularEvent(
            "Volunteer work",
            volunteer_start,
            volunteer_start + timedelta(hours=3),
            "Community center",
        )
    )
    # Anchor the recurring off-day on the first Sunday in the planner window.
    sunday_start = week_start
    sunday_off = logic.OffDay(
        "Sunday off",
        sunday_start,
        sunday_start + timedelta(days=1),
        reason="Weekly rest day",
    )
    sunday_off.set_recurrence(
        logic.Recurrence(logic.RecurrenceFrequency.WEEKLY)
    )
    calendar.add_event(sunday_off)

    # Preferred study time is noon to 3 PM; sleep is blocked from 10 PM to 6 AM.
    preferences = logic.UserPreferences(
        sleep_start=time(22),
        sleep_end=time(6),
        preferred_study_hours=[(time(12), time(15))],
        max_study_hours_per_day=timedelta(hours=4),
    )

    # Generate a draft, leaving approval to the user.
    planner = logic.AIAssistant().generate_planner(
        calendar=calendar,
        preferences=preferences,
        week_start=week_start,
        week_end=week_end,
    )

    # Show fixed calendar commitments and generated study blocks by day.
    fixed_events = [
        event
        for event in calendar.get_events(start=week_start, end=week_end)
        if not event.is_flexible() and not event.is_hidden and not event.is_complete
    ]
    study_hours_by_day: dict[date, timedelta] = defaultdict(timedelta)
    for block in planner.study_blocks:
        study_hours_by_day[block.start_time.date()] += block.get_duration()

    print(f"Weekly planner: {week_start:%B %d} - {(week_end - timedelta(days=1)):%B %d, %Y}")
    for day_offset in range(7):
        day_start = week_start + timedelta(days=day_offset)
        day_end = day_start + timedelta(days=1)
        print(f"\n{day_start:%A, %B %d}")

        day_items = [
            (event.start_time, event.end_time, event.title, isinstance(event, logic.OffDay))
            for event in fixed_events
            if event.start_time < day_end and event.end_time > day_start
        ]
        day_items.extend(
            (block.start_time, block.end_time, block.subject, False)
            for block in planner.study_blocks
            if block.start_time < day_end and block.end_time > day_start
        )
        for start, end, label, is_off_day in sorted(day_items):
            if is_off_day:
                print(f"  All day: {label}")
            else:
                print(f"  {start:%I:%M %p} - {end:%I:%M %p}: {label}")
        if not day_items:
            print("  No scheduled study or fixed commitments.")

        daily_hours = study_hours_by_day[day_start.date()]
        study_hours = daily_hours.total_seconds() / 3600
        unit = "hour" if study_hours == 1 else "hours"
        print(f"  Study total: {study_hours:g} {unit}")
        assert daily_hours <= timedelta(hours=4), (
            f"Generated more than 4 study hours on {day_start:%A}"
        )
        if day_start.date() == sunday_start.date():
            assert daily_hours == timedelta(0), "No study should be scheduled on Sunday."

    print("\nAssignment placement:")
    for placement in planner.assignment_placements:
        scheduled = placement.scheduled_duration.total_seconds() / 3600
        planned = placement.planned_duration.total_seconds() / 3600
        print(
            f"  {placement.subject}: {scheduled:g}/{planned:g} hours "
            f"({placement.state.value})"
        )
        if placement.state is not logic.AssignmentPlacementState.FULLY_SCHEDULED:
            print(
                "    Some work remains unscheduled; assignments are limited "
                "to two study hours per day, and Sunday is off."
            )
    print(f"\nPlanner status: {planner.status.value} (awaiting user approval)")
    return planner


# Example output for the week of September 13 - September 19, 2026:
"""
Weekly planner: September 13 - September 19, 2026

Sunday, September 13
  All day: Sunday off
  Study total: 0 hours

Monday, September 14
  01:00 PM - 03:00 PM: Biology assignment due Monday
  Study total: 2 hours

Tuesday, September 15
  02:00 PM - 03:00 PM: Chemistry assignment due Thursday
  Study total: 1 hour

Wednesday, September 16
  12:00 PM - 01:00 PM: Chemistry assignment due Thursday
  01:00 PM - 02:00 PM: Physics assignment due Thursday
  02:00 PM - 05:00 PM: Volunteer work
  Study total: 2 hours

Thursday, September 17
  12:00 PM - 01:00 PM: Physics assignment due Thursday
  01:00 PM - 03:00 PM: Biology assignment due Friday
  Study total: 3 hours

Friday, September 18
  01:00 PM - 03:00 PM: Biology assignment due Friday
  Study total: 2 hours

Saturday, September 19
  01:00 PM - 03:00 PM: Physics assignment due Saturday
  Study total: 2 hours

Assignment placement:
  Biology assignment due Monday: 2/4 hours (partially_scheduled)
    Some work remains unscheduled; assignments are limited to two study hours per day, and Sunday is off.
  Biology assignment due Friday: 4/4 hours (fully_scheduled)
  Physics assignment due Thursday: 2/2 hours (fully_scheduled)
  Physics assignment due Saturday: 2/2 hours (fully_scheduled)
  Chemistry assignment due Thursday: 2/2 hours (fully_scheduled)

Planner status: draft (awaiting user approval)
"""

# Constraints to follow for the next week:
# 1. User has 3 assignments due: 2 on Tuesday (Chemistry), and 1 on Wednesday (Biology).
# 2. User has volunteer work Monday from 1 PM to 4 PM, and Thursday from 10 AM to 1 PM.
# 3. Constraints not mentioned are the same as the previous week, including sleep, study preferences, and off-day on Sunday.
# 4. User rated last week's planner a 5.
# 5. User approved the draft planner for the week of September 13 - September 19, 2026
# 6. User has meetings on Tuesday from 3 PM to 4 PM, and Friday from 11 AM to 12 PM.
# ______________________________________________________________________________


def generate_following_week_planner(
    previous_planner: logic.Planner,
) -> logic.Planner:
    """Approve last week's draft, then generate the following week's draft."""
    week_start = datetime(2026, 9, 20)
    week_end = week_start + timedelta(days=7)
    tuesday_due = datetime(2026, 9, 22)
    wednesday_due = datetime(2026, 9, 23)

    preferences = logic.UserPreferences(
        sleep_start=time(22),
        sleep_end=time(6),
        preferred_study_hours=[(time(12), time(15))],
        max_study_hours_per_day=timedelta(hours=4),
    )
    user = logic.User("Sample Student", "student@example.com", preferences)
    user.add_planner(previous_planner)
    if not user.approve_planner(
        previous_planner.planner_id,
        now=datetime(2026, 9, 14, 12),
    ):
        raise RuntimeError("Could not approve the previous week's planner.")

    # Simulate the prior week ending so its approved plan becomes PAST.
    user.advance_planner_lifecycle(now=week_start)

    # Planner feedback is represented by a rated completion record in this
    # logic, rather than a separate overall planner-rating field.
    previous_assignment = logic.Assignment(
        "Completed Biology assignment",
        datetime(2026, 9, 13),
        datetime(2026, 9, 14),
        datetime(2026, 9, 14),
        "Biology",
        "homework",
        timedelta(hours=4),
        priority=logic.Priority.HIGH,
    )
    feedback = logic.CompletionRecord(
        event_id=previous_assignment.event_id,
        completed_on_time=True,
        actual_duration_minutes=240,
        planned_duration_minutes=240,
        course="Biology",
        assignment_category="homework",
        completed_at=datetime(2026, 9, 14, 12),
        performance_rating=5,
        notes="Last week's planner was rated 5.",
    )
    previous_assignment.attach_completion_record(feedback)
    previous_assignment.mark_complete()

    assignments = [
        logic.Assignment(
            "Chemistry assignment 1 due Tuesday",
            week_start,
            tuesday_due,
            datetime(2026, 9, 23),
            "Chemistry",
            "homework",
            timedelta(hours=2),
            priority=logic.Priority.LOW,
        ),
        logic.Assignment(
            "Chemistry assignment 2 due Tuesday",
            week_start,
            tuesday_due,
            datetime(2026, 9, 23),
            "Chemistry",
            "homework",
            timedelta(hours=2),
            priority=logic.Priority.LOW,
        ),
        logic.Assignment(
            "Biology assignment due Wednesday",
            week_start,
            wednesday_due,
            datetime(2026, 9, 24),
            "Biology",
            "homework",
            timedelta(hours=4),
            priority=logic.Priority.HIGH,
        ),
    ]

    calendar = logic.Calendar()
    for assignment in assignments:
        calendar.add_event(assignment)

    monday_volunteer_start = datetime(2026, 9, 21, 13)
    thursday_volunteer_start = datetime(2026, 9, 24, 10)
    calendar.add_event(
        logic.ExtracurricularEvent(
            "Volunteer work",
            monday_volunteer_start,
            monday_volunteer_start + timedelta(hours=3),
            "Community center",
        )
    )
    calendar.add_event(
        logic.ExtracurricularEvent(
            "Volunteer work",
            thursday_volunteer_start,
            thursday_volunteer_start + timedelta(hours=3),
            "Community center",
        )
    )

    tuesday_meeting_start = datetime(2026, 9, 22, 15)
    friday_meeting_start = datetime(2026, 9, 25, 11)
    calendar.add_event(
        logic.Meeting(
            "Tuesday meeting",
            tuesday_meeting_start,
            tuesday_meeting_start + timedelta(hours=1),
        )
    )
    calendar.add_event(
        logic.Meeting(
            "Friday meeting",
            friday_meeting_start,
            friday_meeting_start + timedelta(hours=1),
        )
    )

    sunday_off = logic.OffDay(
        "Sunday off",
        week_start,
        week_start + timedelta(days=1),
        reason="Weekly rest day",
    )
    sunday_off.set_recurrence(
        logic.Recurrence(logic.RecurrenceFrequency.WEEKLY)
    )
    calendar.add_event(sunday_off)

    planner = logic.AIAssistant().generate_planner(
        calendar=calendar,
        preferences=preferences,
        week_start=week_start,
        week_end=week_end,
        history=[feedback],
    )

    fixed_events = [
        event
        for event in calendar.get_events(start=week_start, end=week_end)
        if not event.is_flexible() and not event.is_hidden and not event.is_complete
    ]
    study_hours_by_day: dict[date, timedelta] = defaultdict(timedelta)
    for block in planner.study_blocks:
        study_hours_by_day[block.start_time.date()] += block.get_duration()

    print(
        f"\nFollowing week's planner: {week_start:%B %d} - "
        f"{(week_end - timedelta(days=1)):%B %d, %Y}"
    )
    for day_offset in range(7):
        day_start = week_start + timedelta(days=day_offset)
        day_end = day_start + timedelta(days=1)
        print(f"\n{day_start:%A, %B %d}")

        day_items = [
            (
                event.start_time,
                event.end_time,
                event.title,
                isinstance(event, logic.OffDay),
            )
            for event in fixed_events
            if event.start_time < day_end and event.end_time > day_start
        ]
        day_items.extend(
            (block.start_time, block.end_time, block.subject, False)
            for block in planner.study_blocks
            if block.start_time < day_end and block.end_time > day_start
        )
        for start, end, label, is_off_day in sorted(day_items):
            if is_off_day:
                print(f"  All day: {label}")
            else:
                print(f"  {start:%I:%M %p} - {end:%I:%M %p}: {label}")
        if not day_items:
            print("  No scheduled study or fixed commitments.")

        daily_hours = study_hours_by_day[day_start.date()]
        study_hours = daily_hours.total_seconds() / 3600
        unit = "hour" if study_hours == 1 else "hours"
        print(f"  Study total: {study_hours:g} {unit}")
        assert daily_hours <= timedelta(hours=4), (
            f"Generated more than 4 study hours on {day_start:%A}"
        )
        if day_start.date() == week_start.date():
            assert daily_hours == timedelta(0), "No study should be scheduled on Sunday."

    print("\nAssignment placement:")
    for placement in planner.assignment_placements:
        scheduled = placement.scheduled_duration.total_seconds() / 3600
        planned = placement.planned_duration.total_seconds() / 3600
        print(
            f"  {placement.subject}: {scheduled:g}/{planned:g} hours "
            f"({placement.state.value})"
        )

    adjustment = logic.AIAssistant().adapt_from_history([feedback])
    print(
        f"\nHistory adjustment: average rating "
        f"{adjustment.avg_rating:g}; buffer multiplier "
        f"{adjustment.buffer_multiplier:g}"
    )
    print(f"Planner status: {planner.status.value} (awaiting user approval)")
    return planner


if __name__ == "__main__":
    previous_planner = generate_weekly_planner()
    generate_following_week_planner(previous_planner)

# Example output for the week of September 20 - September 26, 2026:
"""
Following week's planner: September 20 - September 26, 2026

Sunday, September 20
  All day: Sunday off
  Study total: 0 hours

Monday, September 21
  12:00 PM - 01:00 PM: Chemistry assignment 1 due Tuesday
  01:00 PM - 04:00 PM: Volunteer work
  09:00 PM - 10:00 PM: Chemistry assignment 2 due Tuesday
  Study total: 2 hours

Tuesday, September 22
  12:00 PM - 01:00 PM: Chemistry assignment 1 due Tuesday
  01:00 PM - 03:00 PM: Biology assignment due Wednesday
  03:00 PM - 04:00 PM: Tuesday meeting
  09:00 PM - 10:00 PM: Chemistry assignment 2 due Tuesday
  Study total: 4 hours

Wednesday, September 23
  01:00 PM - 03:00 PM: Biology assignment due Wednesday
  Study total: 2 hours

Thursday, September 24
  10:00 AM - 01:00 PM: Volunteer work
  Study total: 0 hours

Friday, September 25
  11:00 AM - 12:00 PM: Friday meeting
  Study total: 0 hours

Saturday, September 26
  No scheduled study or fixed commitments.
  Study total: 0 hours

Assignment placement:
  Biology assignment due Wednesday: 4/4 hours (fully_scheduled)
  Chemistry assignment 1 due Tuesday: 2/2 hours (fully_scheduled)
  Chemistry assignment 2 due Tuesday: 2/2 hours (fully_scheduled)

History adjustment: average rating 5; buffer multiplier 1
Planner status: draft (awaiting user approval)
"""
