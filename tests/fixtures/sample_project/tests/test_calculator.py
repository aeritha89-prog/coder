from calculator import divide


def test_divide():
    assert divide(6, 2) == 3


def test_zero():
    try:
        divide(1, 0)
    except ZeroDivisionError:
        pass
    else:
        raise AssertionError("expected ZeroDivisionError")

