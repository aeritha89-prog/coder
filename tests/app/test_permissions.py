from coder.permissions import DefaultPermissionPolicy, PermissionRequest


def test_plan_denies_even_when_session_allowed() -> None:
    policy = DefaultPermissionPolicy(ask=lambda request: "allow_session")
    request = PermissionRequest("edit_file", {"path": "a"}, "interactive")
    assert policy.decide(request) == "allow_session"
    assert policy.decide(PermissionRequest("edit_file", {}, "plan")) == "deny"


def test_interactive_requires_permission_callback_or_yes() -> None:
    request = PermissionRequest("bash", {"command": "python -m pytest"}, "interactive")
    assert DefaultPermissionPolicy().decide(request) == "deny"
    assert DefaultPermissionPolicy(yes=True).decide(request) == "allow_once"
    assert DefaultPermissionPolicy(ask=lambda _: "allow_once").decide(request) == "allow_once"
