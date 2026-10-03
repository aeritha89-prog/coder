from parse_config import parse_config


def test_none():
    assert parse_config(None) == ""

