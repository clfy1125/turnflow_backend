"""design_seed 테스트 — job 시드 안정성 + pick 결정성."""

from __future__ import annotations

from .services.design_seed import pick, seed_from_job_id


class TestSeedFromJobId:
    def test_stable_for_same_id(self):
        assert seed_from_job_id("abc-123") == seed_from_job_id("abc-123")

    def test_uuid_and_str_consistent(self):
        import uuid

        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert seed_from_job_id(u) == seed_from_job_id(str(u))

    def test_different_ids_differ(self):
        a = seed_from_job_id("job-a")
        b = seed_from_job_id("job-b")
        assert a != b
        assert isinstance(a, int) and a >= 0


class TestPick:
    def test_deterministic_index(self):
        opts = ["x", "y", "z"]
        assert pick(0, opts) == "x"
        assert pick(1, opts) == "y"
        assert pick(3, opts) == "x"  # wraps

    def test_salt_offsets(self):
        opts = ["x", "y", "z"]
        assert pick(0, opts, salt=1) == "y"
        assert pick(0, opts, salt=2) == "z"

    def test_empty_returns_none(self):
        assert pick(5, []) is None
