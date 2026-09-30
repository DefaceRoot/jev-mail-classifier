import pytest

from tests import factories


@pytest.fixture(autouse=True)
def _isolated_state_path(tmp_path, monkeypatch):
    monkeypatch.setattr(factories, "DEFAULT_STATE_PATH", str(tmp_path / "state" / "jev-mail.sqlite"))
    (tmp_path / "state").mkdir()
