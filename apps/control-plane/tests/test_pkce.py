from termflow_control_plane.auth.pkce import create_s256_challenge, verify_s256


def test_rfc7636_s256_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"

    assert create_s256_challenge(verifier) == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
    assert verify_s256(
        verifier,
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    )


def test_pkce_verification_rejects_noncanonical_or_wrong_values() -> None:
    verifier = "a" * 43
    challenge = create_s256_challenge(verifier)

    assert not verify_s256("b" * 43, challenge)
    assert not verify_s256("short", challenge)
    assert not verify_s256(verifier, f"{challenge}=")
