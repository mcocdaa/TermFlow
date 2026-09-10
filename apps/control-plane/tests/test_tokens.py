from termflow_control_plane.auth.tokens import hash_token, issue_token, token_matches


def test_issued_token_is_high_entropy_and_hash_matches() -> None:
    token = issue_token()
    assert len(token) >= 43
    assert token_matches(token, hash_token(token))
    assert not token_matches(token + "x", hash_token(token))


def test_token_hash_does_not_contain_raw_token() -> None:
    token = issue_token()
    digest = hash_token(token)
    assert token not in digest
    assert len(digest) == 64

