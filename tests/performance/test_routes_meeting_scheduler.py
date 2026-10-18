"""Performance checks for the source module: routes/meeting_scheduler.py."""
from _helpers import assert_source_scan_is_fast

SOURCE_FILE = 'routes/meeting_scheduler.py'


def test_source_analysis_is_fast():
    assert_source_scan_is_fast(SOURCE_FILE)


def test_module_import_is_within_budget():
    from _helpers import import_module_for_performance
    module = import_module_for_performance('routes.meeting_scheduler')
    assert module is not None


def test_scheduler_route_registration_is_small_and_fast():
    from _helpers import import_module_for_performance
    module = import_module_for_performance("routes.meeting_scheduler")
    paths = {getattr(route, "path", None) for route in module.router.routes}
    assert "/api/v1/async-schedule" in paths
    assert len(module.router.routes) < 20
