
from hermeshq.services.egress_allowlist import domain_allowed, extract_host, parse_env_allowlist


class TestDomainMatching:
    def test_exact_match(self) -> None:
        assert domain_allowed("uport-b.sixmanager.io", ["uport-b.sixmanager.io"])

    def test_leading_dot_matches_subdomains_and_apex(self) -> None:
        entries = [".puget.sixmanager.io"]
        assert domain_allowed("puget.sixmanager.io", entries)
        assert domain_allowed("api.puget.sixmanager.io", entries)
        assert not domain_allowed("uport-b.sixmanager.io", entries)

    def test_bare_entry_matches_subdomains(self) -> None:
        assert domain_allowed("api.openai.com", ["api.openai.com"])
        assert not domain_allowed("other.api.openai.com", ["api.openai.com"])

    def test_case_and_dot_insensitive(self) -> None:
        assert domain_allowed("Uport-B.SixManager.io.", ["uport-b.sixmanager.io"])

    def test_empty_entries(self) -> None:
        assert not domain_allowed("example.com", [])
        assert not domain_allowed("example.com", ["", "  "])


class TestExtractHost:
    def test_url_with_path(self) -> None:
        assert extract_host("https://uport-b.sixmanager.io/v1") == "uport-b.sixmanager.io"

    def test_invalid(self) -> None:
        assert extract_host("") is None
        assert extract_host("not a url") is None


class TestParseEnvAllowlist:
    def test_splits_and_dedups(self) -> None:
        class FakeSettings:
            runtime_egress_allowlist = ".a.io, .b.io,.a.io,"

        assert parse_env_allowlist(FakeSettings()) == [".a.io", ".b.io"]

    def test_empty(self) -> None:
        class FakeSettings:
            runtime_egress_allowlist = ""

        assert parse_env_allowlist(FakeSettings()) == []
