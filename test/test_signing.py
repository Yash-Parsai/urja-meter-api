from app.signing import sign

# Made with OpenSSL, independent of our code:
#   printf 'GET\n/portal/export\npage=1\n1793500000' | openssl dgst -sha256 -hmac test-secret
KNOWN = "90c41b438aa6de353c52d4e3d16a696371436d315520ea3e2087fccb0e33ec2a"


def test_matches_an_independently_computed_signature():
    assert sign("test-secret", "GET", "/portal/export", "page=1", 1793500000) == KNOWN


def test_method_case_does_not_matter():
    assert sign("test-secret", "get", "/portal/export", "page=1", 1793500000) == KNOWN


def test_changing_any_part_changes_the_signature():
    variants = [
        sign("other-secret", "GET", "/portal/export", "page=1", 1793500000),
        sign("test-secret", "POST", "/portal/export", "page=1", 1793500000),
        sign("test-secret", "GET", "/portal/other", "page=1", 1793500000),
        sign("test-secret", "GET", "/portal/export", "page=2", 1793500000),
        sign("test-secret", "GET", "/portal/export", "page=1", 1793500001),
    ]
    assert KNOWN not in variants
    assert len(set(variants)) == len(variants)
