#!/usr/bin/env python3
"""
Sample / advisory SQL autograder for CPSC 285 (self-run GitHub org model).

Runs a setup.sql to build a throwaway database, then executes each student query
file (q1.sql, q2.sql, ...) from the student's submission folder and compares its
output to expected/qN.csv.

This copy lives in the PUBLIC cpsc285-autograde repo and is called by student
repos on push (see sample-feedback.yml). It grades ONLY the public sample tests
that already live in the student's own repo, reshaped into the grader layout:

    hwN/schema.sql            -> tests/hwN/setup.sql
    hwN/sample/expected_q1.csv -> tests/hwN/expected/q1.csv

There is therefore no answer key to protect here - the student already has every
expected CSV in their working tree, and check.py prints the same comparison
locally. Failure detail is always shown in full. The PRIVATE grader repo runs a
different dataset and gates student-facing detail behind --feedback-detail; that
distinction does not apply to this file.

Keep the helper functions below in sync with the grader repo's grade.py. They are
deliberately byte-identical so the two files diff cleanly; the intended
differences are the docstring, --no-points, and the sample-feedback.md output.

Design notes
------------
* Output is captured with `psql --csv -t` (CSV rows, no header/footer), so
  column aliases chosen by the student do not affect grading.
* Comparison is ROW-ORDER-INSENSITIVE by default. If a check requires a
  specific order (e.g., the question says "ordered by ..."), list its name in
  the "ordered" array of points.json.
* The job never fails on a low score - grading is informational. The result is
  written to the workflow Step Summary, to grade-result.json, and to
  sample-feedback.md (used as the advisory Release body).

Usage
-----
    python3 grade.py --assignment hw1 --submission submission --tests tests --no-points

Environment (read automatically by psql):
    PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

# How much of a failing check to print before truncating.
MAX_ROWS = 10           # rows shown per "missing" / "unexpected" list
MAX_ROW_CHARS = 200     # a single very wide row is clipped to this
MAX_ERR_LINES = 15      # lines of psql stderr kept


def run_psql(extra_args, sql_file):
    cmd = ["psql", "-v", "ON_ERROR_STOP=1"] + extra_args + ["-f", str(sql_file)]
    return subprocess.run(cmd, capture_output=True, text=True)


def normalize(csv_text, ordered=False):
    lines = [ln.strip() for ln in csv_text.strip().splitlines() if ln.strip() != ""]
    return lines if ordered else sorted(lines)


def clip(row):
    return row if len(row) <= MAX_ROW_CHARS else row[:MAX_ROW_CHARS] + " ...(clipped)"


def tidy_psql_error(stderr, sql_path):
    """Strip the leading path psql prefixes onto every line, keep it short.

    psql writes `psql:submission/hw1/q1.sql:3: ERROR:  syntax error ...`.
    The directory is noise in a summary, so reduce it to `q1.sql:3: ERROR: ...`.
    """
    text = stderr.replace("\r", "").strip()
    if not text:
        return "psql exited non-zero but wrote nothing to stderr."
    # psql prefixes each line with the path exactly as it was given on the
    # command line, which may be relative or absolute. Reduce any such prefix
    # to the bare file name so the message reads `q1.sql:3: ERROR: ...`.
    text = re.sub(r"^psql:\S*?([^/\\]+\.sql:\d+:)", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^psql:", "", text, flags=re.MULTILINE)
    lines = text.splitlines()
    if len(lines) > MAX_ERR_LINES:
        lines = lines[:MAX_ERR_LINES] + [f"...({len(text.splitlines()) - MAX_ERR_LINES} more lines)"]
    return "\n".join(lines)


def diff_rows(got, want, ordered):
    """Describe how `got` differs from `want`.

    Both are already normalized lists of CSV row strings. Counter (multiset)
    rather than set, so a row returned twice when it was expected once is
    reported as an extra instead of silently matching.
    """
    detail = {
        "kind": "mismatch",
        "ordered": ordered,
        "expected_rows": len(want),
        "got_rows": len(got),
    }

    # Order-sensitive check whose rows are right but sequenced wrong is a
    # distinct and very common failure; saying so beats printing two lists
    # that look identical to the eye.
    if ordered and sorted(got) == sorted(want):
        detail["kind"] = "order"
        first_bad = next((i for i, (g, w) in enumerate(zip(got, want)) if g != w), 0)
        detail["first_diff_index"] = first_bad
        detail["first_diff_expected"] = clip(want[first_bad]) if first_bad < len(want) else ""
        detail["first_diff_got"] = clip(got[first_bad]) if first_bad < len(got) else ""
        return detail

    missing = list((Counter(want) - Counter(got)).elements())
    extra = list((Counter(got) - Counter(want)).elements())
    missing.sort()
    extra.sort()

    detail["missing"] = [clip(r) for r in missing[:MAX_ROWS]]
    detail["missing_hidden"] = max(0, len(missing) - MAX_ROWS)
    detail["missing_total"] = len(missing)
    detail["extra"] = [clip(r) for r in extra[:MAX_ROWS]]
    detail["extra_hidden"] = max(0, len(extra) - MAX_ROWS)
    detail["extra_total"] = len(extra)
    return detail


def _code_block(rows):
    if not rows:
        return "_(none)_\n"
    # Fenced, not a Markdown table: CSV rows contain | and would break a table.
    return "```\n" + "\n".join(rows) + "\n```\n"


def failure_markdown(check, level="full"):
    """Render one failing check as Markdown. Returns "" when there is nothing
    to say at this detail level."""
    if level == "none":
        return ""
    d = check.get("detail") or {}
    kind = d.get("kind")
    name = check["name"]
    out = [f"**`{name}` - {check['status']}**\n"]

    if kind == "missing":
        out.append(f"No file at `{d.get('path','')}`.\n")
        return "\n".join(out)

    if kind == "error":
        if level == "counts":
            out.append("The query did not run (SQL error).\n")
        else:
            out.append("The query did not run:\n")
            out.append("```\n" + d.get("message", "") + "\n```\n")
        return "\n".join(out)

    if kind == "order":
        out.append(
            f"Right rows, wrong order. Returned {d.get('got_rows', 0)} rows, all of them "
            f"correct, but the sequence differs starting at row {d.get('first_diff_index', 0) + 1}. "
            "This check is order-sensitive, so the ORDER BY is what to look at.\n"
        )
        if level == "full":
            out.append(f"Row {d.get('first_diff_index', 0) + 1} expected:\n")
            out.append(_code_block([d.get("first_diff_expected", "")]))
            out.append("Row returned instead:\n")
            out.append(_code_block([d.get("first_diff_got", "")]))
        return "\n".join(out)

    # kind == "mismatch"
    exp_n, got_n = d.get("expected_rows", 0), d.get("got_rows", 0)
    miss_n, extra_n = d.get("missing_total", 0), d.get("extra_total", 0)
    out.append(f"Returned **{got_n}** rows, expected **{exp_n}**. "
               f"{miss_n} expected row(s) not returned, {extra_n} unexpected row(s) returned.\n")

    if got_n == 0:
        out.append("The query ran but produced no rows at all - check the WHERE clause "
                   "and any JOIN conditions first.\n")

    if level == "counts":
        return "\n".join(out)

    if miss_n:
        hidden = d.get("missing_hidden", 0)
        label = f"Expected but not returned ({miss_n})"
        if hidden:
            label += f", showing first {MAX_ROWS}"
        out.append(f"{label}:\n")
        out.append(_code_block(d.get("missing", [])))
        if hidden:
            out.append(f"_...and {hidden} more._\n")

    if extra_n:
        hidden = d.get("extra_hidden", 0)
        label = f"Returned but not expected ({extra_n})"
        if hidden:
            label += f", showing first {MAX_ROWS}"
        out.append(f"{label}:\n")
        out.append(_code_block(d.get("extra", [])))
        if hidden:
            out.append(f"_...and {hidden} more._\n")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--assignment", required=True, help="assignment folder, e.g. hw1")
    ap.add_argument("--submission", required=True, help="path to the student's checked-out repo")
    ap.add_argument("--tests", required=True, help="path to the grader tests directory")
    ap.add_argument("--student", default="", help="student username (shown in the summary)")
    ap.add_argument("--no-points", action="store_true",
                    help="show pass/fail counts instead of points (for advisory sample feedback)")
    a = ap.parse_args()

    tests_dir = Path(a.tests) / a.assignment
    sub_dir = Path(a.submission) / a.assignment
    expected_dir = tests_dir / "expected"
    setup = tests_dir / "setup.sql"

    if not tests_dir.exists():
        print(f"::error::No tests defined for assignment '{a.assignment}'")
        sys.exit(1)

    # 1) Build the database (schema + seed).
    if setup.exists():
        r = run_psql([], setup)
        if r.returncode != 0:
            print("::error::setup.sql failed:\n" + r.stderr)
            sys.exit(1)

    # 2) Load optional config: per-check points and which checks are order-sensitive.
    cfg = {}
    cfg_file = tests_dir / "points.json"
    if cfg_file.exists():
        cfg = json.loads(cfg_file.read_text())
    points = cfg.get("points", {})
    ordered_checks = set(cfg.get("ordered", []))
    default_points = float(cfg.get("default", 1))

    checks = sorted(expected_dir.glob("*.csv"))
    if not checks:
        print("::error::No expected/*.csv checks found for this assignment")
        sys.exit(1)

    total, max_total, rows = 0.0, 0.0, []

    for exp in checks:
        name = exp.stem                      # e.g. "q1"
        weight = float(points.get(name, default_points))
        max_total += weight
        student_sql = sub_dir / f"{name}.sql"

        if not student_sql.exists():
            rows.append((name, "MISSING", 0.0, weight,
                         {"kind": "missing", "path": f"{a.assignment}/{name}.sql"}))
            continue

        # -q suppresses psql command tags (DROP TABLE / CREATE TABLE), which appear
        # when an answer file contains DDL. check.py uses -q too, so the local
        # self-check and this advisory run see byte-identical output.
        res = run_psql(["-q", "--csv", "-t"], student_sql)
        if res.returncode != 0:
            rows.append((name, "ERROR", 0.0, weight,
                         {"kind": "error", "message": tidy_psql_error(res.stderr, student_sql)}))
            continue

        is_ordered = name in ordered_checks
        got = normalize(res.stdout, ordered=is_ordered)
        want = normalize(exp.read_text(), ordered=is_ordered)
        if got == want:
            total += weight
            rows.append((name, "PASS", weight, weight, None))
        else:
            rows.append((name, "FAIL", 0.0, weight, diff_rows(got, want, is_ordered)))

    score = round(total, 1)
    max_score = round(max_total, 1)
    passed = sum(1 for _n, s, _p, _w, _d in rows if s == "PASS")
    ntotal = len(rows)

    # Console output.
    if a.no_points:
        print(f"\nPassed: {passed} / {ntotal} checks")
        for name, status, pts, wt, detail in rows:
            print(f"  {name:8} {status}")
            if detail:
                body = failure_markdown(
                    {"name": name, "status": status, "detail": detail}, "full")
                for ln in body.splitlines():
                    if ln.strip():
                        print("      " + ln.replace("**", "").replace("`", ""))
    else:
        who = f"{a.student} · " if a.student else ""
        print(f"\n{who}Score: {score} / {max_score}")
        for name, status, pts, wt, detail in rows:
            print(f"  {name:8} {status:8} {round(pts,1)}/{round(wt,1)}")
            if detail:
                body = failure_markdown(
                    {"name": name, "status": status, "detail": detail}, "full")
                for ln in body.splitlines():
                    if ln.strip():
                        print("      " + ln.replace("**", "").replace("`", ""))

    checks_json = [
        {"name": n, "status": s, "points": round(p, 1), "weight": round(w, 1),
         **({"detail": d} if d else {})}
        for n, s, p, w, d in rows
    ]
    failures = [c for c in checks_json if c["status"] != "PASS"]

    # 3) Write a Markdown summary for the Actions run page.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            if a.no_points:
                f.write(f"## Sample / Advisory Feedback: {a.assignment}\n\n")
                f.write(f"**{passed} / {ntotal} questions passed**  (advisory result - not your official grade)\n\n")
                f.write("| Question | Result |\n|---|---|\n")
                for name, status, pts, wt, _d in rows:
                    f.write(f"| `{name}` | {status} |\n")
            else:
                title = f"{a.student} · {a.assignment}" if a.student else a.assignment
                f.write(f"## Autograding Result: {title}\n\n")
                if a.student:
                    f.write(f"**Student:** `{a.student}`  \n")
                f.write(f"**Score: {score} / {max_score}**\n\n")
                f.write("| Check | Result | Points |\n|---|---|---|\n")
                for name, status, pts, wt, _d in rows:
                    f.write(f"| `{name}` | {status} | {round(pts,1)} / {round(wt,1)} |\n")
            if failures:
                f.write(f"\n### Why {len(failures)} question(s) did not pass\n\n")
                for c in failures:
                    f.write(failure_markdown(c, "full"))
                    f.write("\n")
                f.write("\n_The sample dataset is not the dataset your official grade uses. "
                        "Passing every question here is necessary, not sufficient._\n")

    # 4) Machine-readable result.
    Path("grade-result.json").write_text(json.dumps({
        "student": a.student,
        "assignment": a.assignment,
        "score": score,
        "max": max_score,
        "checks": checks_json,
    }, indent=2))

    # 5) Body for the advisory Release, so the workflow does not build Markdown
    #    in bash. GitHub caps release notes at 125,000 characters.
    notes = [
        f"Advisory sample-test result only - **NOT your official grade**. "
        f"{passed}/{ntotal} questions passed.\n",
    ]
    notes.append("".join(f"- `{n}`: {s}\n" for n, s, _p, _w, _d in rows))
    if failures:
        notes.append(f"\n### Why {len(failures)} question(s) did not pass\n")
        for c in failures:
            notes.append(failure_markdown(c, "full"))
        notes.append(
            "\nThese are the **sample** tests that ship in your repo. Your official grade runs "
            "the same questions against a different dataset, so fix the query, not the rows.\n"
        )
    notes.append("\nYour graded score is posted in Canvas.\n")
    body = "\n".join(notes)
    if len(body) > 120000:
        body = body[:119800] + "\n\n_...truncated._\n"
    Path("sample-feedback.md").write_text(body)


if __name__ == "__main__":
    main()
