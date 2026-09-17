import subprocess
from pathlib import Path
from unittest.mock import patch
import pytest
import local_session


def test_acl_once_per_process(tmp_path):
    with patch.object(local_session,'DATA',tmp_path), patch.object(local_session,'_protected',False), patch('local_session.subprocess.check_output',return_value='"user","S-1-5-21-123"') as who, patch('local_session.subprocess.run') as acl:
        local_session.protect_dir()
        local_session.protect_dir()
        assert who.call_count==acl.call_count==1
        assert who.call_args.kwargs['timeout']==5
        assert acl.call_args.kwargs['timeout']==5


def test_acl_timeout_fails_closed(tmp_path):
    with patch.object(local_session,'DATA',tmp_path), patch.object(local_session,'_protected',False), patch('local_session.subprocess.check_output',side_effect=subprocess.TimeoutExpired('whoami',5)), patch('local_session.subprocess.run') as acl:
        with pytest.raises(RuntimeError,match='权限检查超时'):
            local_session.protect_dir()
        assert local_session._protected is False
        acl.assert_not_called()


def test_acl_invalid_identity_fails_closed(tmp_path):
    with patch.object(local_session,'DATA',tmp_path), patch.object(local_session,'_protected',False), patch('local_session.subprocess.check_output',return_value='invalid'), patch('local_session.subprocess.run') as acl:
        with pytest.raises(RuntimeError,match='无法确认'):
            local_session.protect_dir()
        acl.assert_not_called()


def test_start_batch_never_installs():
    root=Path(__file__).resolve().parents[1]
    text=(root/'start_booking.bat').read_text()
    assert 'uv sync' not in text
    assert 'bootstrap.py' not in text
    assert 'launcher.py' in text
    assert 'uv sync --locked' in (root/'setup_environment.bat').read_text()
