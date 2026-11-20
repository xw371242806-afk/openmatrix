import asyncio, threading
from datetime import datetime
from unittest import mock
import pytest
import main
from agent import scheduler

def test_startup_restores_persistent_schedules_and_runs_ticker(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENMATRIX_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('OPENMATRIX_ENABLE_AUTH','off')
    first=scheduler.ScheduleManager(db_path=tmp_path/'schedules.sqlite3')
    first.register('paused-job','* * * * *','fixture','fixture-run',{'ok':True});first.pause('paused-job')
    monkeypatch.setattr(scheduler,'_default_manager',None)
    try:
        asyncio.run(main._initialize_auth())
        restored=scheduler.default_manager()
        assert restored.get('paused-job').paused
        assert restored._thread.is_alive()
        assert restored.persistent
    finally:asyncio.run(main._stop_schedule_ticker())

def test_pause_failure_rolls_back_memory(tmp_path):
    manager=scheduler.ScheduleManager(db_path=tmp_path/'schedules.sqlite3')
    manager.register('s1','* * * * *','fixture','r1')
    with mock.patch.object(manager,'_save_entry',side_effect=OSError('fixture write failure')),pytest.raises(OSError):manager.pause('s1')
    assert not manager.get('s1').paused

def test_deleted_job_is_not_resurrected_by_a_failing_callback(tmp_path):
    started=threading.Event();release=threading.Event();done=threading.Event()
    def callback(*args):
        started.set();release.wait(5);raise RuntimeError('fixture failure')
    manager=scheduler.ScheduleManager(callback,db_path=tmp_path/'schedules.sqlite3')
    manager.register('s1','* * * * *','fixture','r1')
    original=manager.mark_done
    def finished(name):original(name);done.set()
    manager.mark_done=finished
    manager.tick(datetime(2026,1,1,0,0));assert started.wait(3)
    manager.unregister('s1');release.set();assert done.wait(3)
    assert scheduler.ScheduleManager(db_path=tmp_path/'schedules.sqlite3').get('s1') is None


def test_schedule_default_path_matches_application_data_root(monkeypatch):
    from pathlib import Path
    monkeypatch.delenv('OPENMATRIX_DATA_DIR', raising=False)
    monkeypatch.delenv('OPENMATRIX_SCHEDULES_PERSIST', raising=False)
    expected = Path(main.__file__).resolve().parent / '.openmatrix' / 'schedules.sqlite3'
    assert Path(scheduler._schedule_persistence_path()) == expected
