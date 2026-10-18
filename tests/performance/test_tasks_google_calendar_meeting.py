"""Performance checks for the source module: tasks/google_calender_meeting.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'tasks/google_calender_meeting.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('tasks.google_calender_meeting')
    assert module is not None


def test_calendar_task_registration_is_fast():
    from _helpers import import_module_for_performance
    module = import_module_for_performance("tasks.google_calender_meeting")
    assert callable(module.execute_calendar_schedule_task)
