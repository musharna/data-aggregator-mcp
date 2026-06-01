# tests/test_operate.py
from data_aggregator_mcp import operate


def test_operate_available_is_bool():
    assert isinstance(operate.OPERATE_AVAILABLE, bool)


def test_missing_extra_message_names_the_extra():
    assert "data-aggregator-mcp[operate]" in operate.MISSING_EXTRA_MSG
