from unittest.mock import Mock

import pytest

from openwebui_import import authenticate_user, request


def test_forbidden_identifies_endpoint_and_auth_checks_without_leaking_secrets():
    session = Mock()
    session.request.return_value.status_code = 403
    session.request.return_value.text = "private response with a secret-token"
    with pytest.raises(ValueError) as exc:
        request(session, "GET", "http://localhost:3080/api/v1/auths/?token=secret-token")
    message = str(exc.value)
    assert "GET /api/v1/auths/" in message
    assert "HTTP 403" in message
    assert "API Keys" in message
    assert "endpoint restrictions" in message
    assert "No uploads have started" in message
    assert "secret-token" not in message
    assert "private response" not in message


def test_forbidden_import_has_permission_hint_but_does_not_claim_no_writes():
    session = Mock()
    session.request.return_value.status_code = 403
    with pytest.raises(ValueError) as exc:
        request(session, "POST", "http://localhost:3080/api/v1/memories/add")
    message = str(exc.value)
    assert "POST /api/v1/memories/add" in message
    assert "permission" in message
    assert "No uploads have started" not in message


def test_unauthorized_has_credential_hint():
    session = Mock()
    session.request.return_value.status_code = 401
    with pytest.raises(ValueError, match="OPENWEBUI_TOKEN"):
        request(session, "GET", "http://localhost:3080/api/v1/auths/")


def response(status, data):
    result = Mock(status_code=status)
    result.json.return_value = data
    return result


def test_api_key_human_session_rejection_falls_back_to_account_status():
    session = Mock()
    session.request.side_effect = [
        response(403, {"detail": "A human session is required."}),
        response(200, {"id": "stable-user-id", "role": "admin"}),
    ]
    assert authenticate_user(session, "http://localhost:3080")["id"] == "stable-user-id"
    assert [call.args for call in session.request.call_args_list] == [
        ("GET", "http://localhost:3080/api/v1/auths/"),
        ("GET", "http://localhost:3080/api/v1/users/user/status"),
    ]


def test_other_forbidden_does_not_trigger_fallback():
    session = Mock()
    session.request.return_value = response(403, {"detail": "Access denied"})
    with pytest.raises(ValueError, match="HTTP 403"):
        authenticate_user(session, "http://localhost:3080")
    assert session.request.call_count == 1


def test_disabled_status_endpoint_requires_session_token():
    session = Mock()
    session.request.side_effect = [
        response(403, {"detail": "A human session is required."}),
        response(403, {"detail": "Action prohibited"}),
    ]
    with pytest.raises(ValueError, match="session token"):
        authenticate_user(session, "http://localhost:3080")


def test_session_identity_does_not_need_fallback():
    session = Mock()
    session.request.return_value = response(200, {"id": "user"})
    assert authenticate_user(session, "http://localhost:3080")["id"] == "user"
    assert session.request.call_count == 1


@pytest.mark.parametrize("data", [None, {}, {"id": ""}, {"id": 12}])
def test_identity_must_be_a_nonempty_user_id(data):
    session = Mock()
    session.request.return_value = response(200, data)
    with pytest.raises(ValueError, match="identity"):
        authenticate_user(session, "http://localhost:3080")
