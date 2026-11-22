"""Успешная прошлая сборка не делает новую неудачную попытку зелёной."""
import pytest

from portal.config import StandConfig
from portal.stands import _health


@pytest.mark.parametrize("state,expected", [
    ("ready", "ok"), ("failed", "broken"), ("waiting", "unknown"),
    ("building", "unknown"), ("checking", "unknown"), ("stopped", "unknown"),
])
def test_old_successful_build_checks_do_not_hide_current_stand_state(state, expected):
    info = {"build_id": "previous-successful-build", "health": [{"name": "smoke", "ok": True}]}
    stand = StandConfig(name="stable-id", title="Feature", source="database", state=state, build_info=info)
    health = _health(stand, info, [])
    assert health.state == expected
    if state == "failed":
        assert health.reasons == ["последняя сборка завершилась ошибкой или была отменена"]
