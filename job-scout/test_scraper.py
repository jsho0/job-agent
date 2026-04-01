"""
Tests for scraper filter functions.
Run with: python -m pytest job-scout/test_scraper.py -v
(from job-agent root, with venv active)
"""
import sqlite3
import tempfile
import os
import pytest

# Patch DB_PATH and env before importing scraper
import importlib
import sys

# Provide a temp DB path so init_db() doesn't touch the real jobs.db
_tmp_db = tempfile.mktemp(suffix=".db")
os.environ.setdefault("MEMORY_REPO_PATH", "")


def _import_scraper():
    """Import scraper with a test DB path."""
    import scraper
    scraper.DB_PATH = _tmp_db
    return scraper


# ── Experience regex filter ────────────────────────────────────────────────────

class TestExperienceFilterRe:
    """EXPERIENCE_FILTER_RE must catch 5+ years and NOT false-positive on entry-level."""

    def setup_method(self):
        import scraper
        self.pattern = scraper.EXPERIENCE_FILTER_RE

    def _match(self, text):
        return bool(self.pattern.search(text))

    # Should filter
    def test_five_plus_years_experience(self):
        assert self._match("Requires 5+ years of experience in B2B sales")

    def test_six_years_experience(self):
        assert self._match("Minimum 6 years experience required")

    def test_ten_plus_years(self):
        assert self._match("10+ years in enterprise SaaS required")

    def test_minimum_seven_years(self):
        assert self._match("minimum of 7 years of relevant experience")

    def test_five_to_eight_years(self):
        assert self._match("5 to 8 years of experience preferred")

    def test_nine_plus_yrs(self):
        assert self._match("9+ yrs experience selling to Fortune 500")

    def test_case_insensitive(self):
        assert self._match("REQUIRES 5+ YEARS OF EXPERIENCE")

    def test_years_at_end_of_description(self):
        # Experience requirement buried deep in description
        filler = "Great company. Great culture. " * 50
        assert self._match(filler + "Must have 5+ years of sales experience.")

    # Should NOT filter
    def test_one_to_two_years(self):
        assert not self._match("1-2 years of experience preferred")

    def test_zero_to_one_years(self):
        assert not self._match("0-1 years experience, new grads welcome")

    def test_two_plus_years(self):
        assert not self._match("2+ years of experience preferred")

    def test_three_years_max(self):
        assert not self._match("up to 3 years experience required")

    def test_four_years(self):
        assert not self._match("3-4 years experience a plus")

    def test_no_experience_required(self):
        assert not self._match("No prior experience required. We will train you.")

    def test_company_name_with_number(self):
        # "5 Years of Code" should not trigger the filter
        assert not self._match("Working at 5 Years of Code Inc will teach you")

    def test_number_in_unrelated_context(self):
        assert not self._match("We have 5 offices in 7 countries")

    # Regression: hyphenated ranges like "3-5 years" were false positives because
    # \b matched the digit after the hyphen. These must NOT filter entry-level jobs.
    def test_regression_three_to_five_years(self):
        assert not self._match("3-5 years of experience preferred")

    def test_regression_two_to_five_years(self):
        assert not self._match("2-5 years of experience preferred")

    def test_regression_one_to_five_years(self):
        assert not self._match("1-5 years experience a plus")

    def test_regression_four_to_six_years(self):
        assert not self._match("4-6 years of experience")

    def test_standalone_five_still_matches(self):
        # "5 years" not preceded by a digit or hyphen must still be caught
        assert self._match("5 years of experience required")


# ── is_repost_by_title ─────────────────────────────────────────────────────────

class TestIsRepostByTitle:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE seen_jobs (
                job_url TEXT PRIMARY KEY,
                title TEXT,
                company TEXT,
                date_found TEXT
            )
        """)
        self.conn.commit()
        import scraper
        self.fn = scraper.is_repost_by_title

    def teardown_method(self):
        self.conn.close()

    def _insert(self, title, company, days_ago=1):
        from datetime import datetime, timezone, timedelta
        date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        self.conn.execute(
            "INSERT INTO seen_jobs VALUES (?, ?, ?, ?)",
            (f"https://example.com/{title}", title, company, date)
        )
        self.conn.commit()

    def test_same_title_company_within_7_days(self):
        self._insert("SDR", "Acme Corp", days_ago=3)
        assert self.fn(self.conn, "SDR", "Acme Corp") is True

    def test_same_title_company_case_insensitive(self):
        self._insert("sdr", "acme corp", days_ago=2)
        assert self.fn(self.conn, "SDR", "Acme Corp") is True

    def test_same_title_company_outside_7_days(self):
        self._insert("SDR", "Acme Corp", days_ago=8)
        assert self.fn(self.conn, "SDR", "Acme Corp") is False

    def test_different_company_not_repost(self):
        self._insert("SDR", "Acme Corp", days_ago=1)
        assert self.fn(self.conn, "SDR", "Other Corp") is False

    def test_different_title_not_repost(self):
        self._insert("SDR", "Acme Corp", days_ago=1)
        assert self.fn(self.conn, "BDR", "Acme Corp") is False

    def test_empty_db_not_repost(self):
        assert self.fn(self.conn, "SDR", "Acme Corp") is False


# ── passes_or_filter repost fallback ──────────────────────────────────────────

class TestPassesOrFilter:
    def setup_method(self):
        import scraper
        self.fn = scraper.passes_or_filter

    def _job(self, applicants=None, date_posted=None):
        return {"applicants": applicants, "date_posted": date_posted}

    def test_repost_with_unknown_applicants_rejected(self):
        # Previously this returned True (let Claude decide). Now must return False for reposts.
        passes, _, _ = self.fn(self._job(applicants=None, date_posted=None), is_repost=True)
        assert passes is False

    def test_repost_with_few_applicants_passes(self):
        # Reposts with confirmed low applicant count still pass
        passes, _, _ = self.fn(self._job(applicants=5), is_repost=True)
        assert passes is True

    def test_repost_with_many_applicants_rejected(self):
        passes, _, _ = self.fn(self._job(applicants=200), is_repost=True)
        assert passes is False

    def test_non_repost_unknown_age_passes(self):
        # Non-reposts with unknown age still let Claude decide
        passes, _, _ = self.fn(self._job(), is_repost=False)
        assert passes is True


# ── get_vault_rejection_context ────────────────────────────────────────────────

class TestGetVaultRejectionContext:
    def test_returns_empty_when_no_memory_repo(self, monkeypatch):
        import scraper
        monkeypatch.setattr(scraper, "MEMORY_REPO_PATH", None)
        assert scraper.get_vault_rejection_context() == ""

    def test_returns_empty_when_file_missing(self, monkeypatch, tmp_path):
        import scraper
        monkeypatch.setattr(scraper, "MEMORY_REPO_PATH", str(tmp_path))
        assert scraper.get_vault_rejection_context() == ""

    def test_parses_markdown_table(self, monkeypatch, tmp_path):
        import scraper
        monkeypatch.setattr(scraper, "MEMORY_REPO_PATH", str(tmp_path))
        md = tmp_path / "rejection-patterns.md"
        md.write_text(
            "# Rejection Patterns\n\n"
            "| Date | Title | Company | Reason |\n"
            "|------|-------|---------|--------|\n"
            "| 2026-03-01 | SDR | Acme | too many cold calls |\n"
            "| 2026-03-15 | AE | Beta Corp | requires 5 years |\n"
        )
        result = scraper.get_vault_rejection_context()
        assert "Acme" in result
        assert "too many cold calls" in result
        assert "Beta Corp" in result

    def test_handles_malformed_table_gracefully(self, monkeypatch, tmp_path):
        import scraper
        monkeypatch.setattr(scraper, "MEMORY_REPO_PATH", str(tmp_path))
        md = tmp_path / "rejection-patterns.md"
        md.write_text("not a table at all\n\njust some text\n")
        # Should not raise
        result = scraper.get_vault_rejection_context()
        assert result == ""
