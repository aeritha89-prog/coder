from calculator import divide


def test_divide() -> None:
    assert divide(6, 2) == 3


def test_zero() -> None:
    try:
        divide(1, 0)
    except ZeroDivisionError:
        return
    raise AssertionError("expected ZeroDivisionError")

