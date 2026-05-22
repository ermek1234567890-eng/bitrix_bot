import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    import db
    db.DB_PATH = str(tmp_path / "test.db")
    db.init_db()
    db.init_mop_tables()
    yield
    db.DB_PATH = "bot.db"


def test_upsert_and_get_mop():
    import db
    db.upsert_mop(101, 999888, "Самал")
    assert db.get_mop_telegram(101) == 999888


def test_get_all_mops():
    import db
    db.upsert_mop(101, 111, "Самал")
    db.upsert_mop(102, 222, "Заир")
    mops = db.get_all_mops()
    assert len(mops) == 2
    names = {m["name"] for m in mops}
    assert names == {"Самал", "Заир"}


def test_upsert_mop_updates_existing():
    import db
    db.upsert_mop(101, 111, "Самал")
    db.upsert_mop(101, 222, "Самал")
    assert db.get_mop_telegram(101) == 222


def test_get_mop_telegram_returns_none_for_unknown():
    import db
    assert db.get_mop_telegram(999) is None


def test_reminder_not_sent_initially():
    import db
    assert not db.is_reminder_sent(42)


def test_mark_and_check_reminder_sent():
    import db
    db.mark_reminder_sent(42)
    assert db.is_reminder_sent(42)


def test_mark_reminder_sent_idempotent():
    import db
    db.mark_reminder_sent(42)
    db.mark_reminder_sent(42)  # не должно упасть
    assert db.is_reminder_sent(42)


def test_cleanup_old_reminders():
    import db
    db.mark_reminder_sent(10)
    db.cleanup_old_reminders(days=0)
    assert not db.is_reminder_sent(10)


def test_cleanup_keeps_recent_reminders():
    import db
    db.mark_reminder_sent(20)
    db.cleanup_old_reminders(days=7)
    assert db.is_reminder_sent(20)
