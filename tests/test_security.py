from app.security import is_valid_secret


def test_correct_secret_accepted():
    assert is_valid_secret("test-secret-123") is True


def test_wrong_secret_rejected():
    assert is_valid_secret("wrong-secret") is False


def test_missing_secret_rejected():
    assert is_valid_secret(None) is False
    assert is_valid_secret("") is False
