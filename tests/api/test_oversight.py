"""Health oversight: the summary and the two CSV exports."""

from __future__ import annotations

import csv
import io
import re


def rows(text: str) -> list[dict]:
    lines = [ln for ln in text.splitlines() if not ln.startswith("#")]
    return list(csv.DictReader(io.StringIO("\n".join(lines))))


def test_the_summary(client, people):
    res = client.get("/api/oversight/summary", headers=people["oversight"].read)
    assert res.status_code == 200
    body = res.json()
    assert body["is_demo"] is True and body["coverage"]["city"]["status"]
    assert "completion_basis" in body


def test_the_weekly_csv_is_demo_stamped_and_holds_no_phone_number(client, people, ctx):
    res = client.get("/api/exports/weekly.csv", headers=people["oversight"].read)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "DEMO" in res.headers["content-disposition"] and "attachment" in res.headers["content-disposition"]
    first = res.text.splitlines()[0]
    assert first.startswith("# DEMO DATA")
    assert rows(res.text)
    assert not re.search(r"\d{10}", res.text), "a ten-digit number is in the export"
    assert "99999" not in res.text and "+91" not in res.text and "sim:" not in res.text


def test_the_register_csv_is_demo_stamped_and_holds_no_phone_number(client, people):
    res = client.get("/api/exports/register.csv", headers=people["reviewer"].read)
    assert res.status_code == 200
    assert res.text.splitlines()[0].startswith("# DEMO DATA")
    assert "DEMO" in res.headers["content-disposition"]
    assert rows(res.text)
    assert not re.search(r"\+?\d{10}", res.text)


def test_only_the_reviewer_reads_the_row_level_register(client, people):
    """One row per patient course is the Health register's own data: oversight (aggregates), the
    administrator and every other role are refused, and so is a visitor viewing as any of them."""
    for name, who in people.items():
        if name in ("reviewer", "visitor-reviewer", "out"):
            continue
        assert client.get("/api/exports/register.csv", headers=who.read).status_code == 403, name
    assert client.get("/api/exports/register.csv", headers=people["out"].read).status_code == 401
    assert client.get("/api/exports/register.csv", headers=people["visitor-reviewer"].read).status_code == 200


def test_the_reviewer_reads_the_aggregate_digest(client, people):
    summary = client.get("/api/oversight/summary", headers=people["reviewer"].read)
    assert summary.status_code == 200 and summary.json()["coverage"]["city"]["status"]
    assert client.get("/api/exports/weekly.csv", headers=people["reviewer"].read).status_code == 200


def test_the_register_csv_columns_hold_no_name_or_contact(client, people):
    text = client.get("/api/exports/register.csv", headers=people["reviewer"].read).text
    header = rows(text)[0].keys()
    for banned in ("patient_name", "phone", "address", "contact", "mobile", "age", "dob"):
        assert not [h for h in header if banned == h.lower() or banned in h.lower().split("_")], (
            banned,
            list(header),
        )


def test_every_download_is_written_to_the_audit_log(client, ctx, people):
    client.get("/api/exports/weekly.csv", headers=people["oversight"].read)
    client.get("/api/exports/register.csv", headers=people["reviewer"].read)
    seen = {
        (r["actor"], r["object_id"])
        for r in ctx.db.query("SELECT actor, object_id FROM audit WHERE action = 'export.download'")
    }
    assert seen == {("staff:oversight", "weekly"), ("staff:reviewer", "register")}


def test_a_visitor_may_download_what_their_view_may(client, people):
    assert client.get("/api/exports/weekly.csv", headers=people["visitor-oversight"].read).status_code == 200
    assert client.get("/api/exports/weekly.csv", headers=people["visitor-clinic"].read).status_code == 403
