from datetime import timedelta

from backup.core.incremental import IncrementalTracker

from .conftest import BASE_TIME, make_message


class TestIncrementalTracker:
    def test_first_backup_is_full(self, db):
        tracker = IncrementalTracker(db)
        assert tracker.get_incremental(100) is None
        assert tracker.get_incremental_point(100)[0].year == 1

    def test_incremental_point_persists(self, db):
        tracker = IncrementalTracker(db)
        tracker.update_incremental_point(100, BASE_TIME, "m9")
        assert tracker.get_incremental(100) == (BASE_TIME, "m9")
        # 新建 tracker 依然能读到（走库）
        assert IncrementalTracker(db).get_incremental(100)[1] == "m9"

    def test_is_newer_boundary(self, db):
        tracker = IncrementalTracker(db)
        tracker.update_incremental_point(100, BASE_TIME, "m5")
        assert tracker.is_newer(100, (BASE_TIME, "m6")) is True
        assert tracker.is_newer(100, (BASE_TIME, "m5")) is False
        assert tracker.is_newer(100, (BASE_TIME - timedelta(seconds=1), "m99")) is False

    def test_advance_keeps_latest(self, db):
        tracker = IncrementalTracker(db)
        tracker.advance(100, [make_message(i) for i in [3, 1, 4]])
        ts, mid = tracker.get_incremental(100)
        assert mid == "m4"
        assert ts == make_message(4).timestamp

    def test_reset(self, db):
        tracker = IncrementalTracker(db)
        tracker.update_incremental_point(100, BASE_TIME, "m1")
        tracker.reset(100)
        assert tracker.get_incremental(100) is None
