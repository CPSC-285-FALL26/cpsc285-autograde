#!/usr/bin/env python3
"""
Central SQL autograder for CPSC 285 (self-run GitHub org model).

Runs the INSTRUCTOR-controlled setup.sql to build a throwaway database, then
executes each student query file (q1.sql, q2.sql, ...) from the student's
submission folder and compares its output to the instructor's expected/qN.csv.

Design notes
------------
* This script lives in the PRIVATE grader repo, never in the student's repo,
  so students cannot see or edit the tests or the point values.
* Output is captured with `psql --csv -t` (CSV rows, no header/footer), so
  column aliases chosen by the student do not affect grading.
* Comparison is ROW-ORDER-INSENSITIVE by default. If a check requires a
  specific order (e.g., the question says "ordered by ..."), list its name in
  the "ordered" array of points.json.
* The job never fails on a low score — grading is informational. The score is
  written to the workflow Step Summary and to grade-result.json (an artifact).

Usage
-----
    python3 grade.py --assignment hw1 --submission submission --tests tests

Environment (read automatically by psql):
    PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run_psql(extra_args, sql_file):
    cmd = ["psql", "-v", "ON_ERROR_STOP=1"] + extra_args + ["-f", str(sql_file)]
    return subprocess.run(cmd, capture_output=True, text=True)


def normalize(csv_text, ordered=False):
    lines = [ln.strip() for ln in csv_text.strip().splitlines() if ln.strip() != ""]
    return lines if ordered else sorted(lines)


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

    # 1) Build the instructor-controlled database (schema + seed).
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
    # Points for a check not listed in "points": use points.json "default", else 1.
    # The assignment total (max) is simply the SUM of every check's points — it is
    # NOT scaled to 100. So the max = sum of the per-question points you configure.
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
            rows.append((name, "MISSING", 0.0, weight))
            continue

        # -q suppresses psql command tags (DROP TABLE / CREATE TABLE), which appear
        # when an answer file contains DDL. check.py uses -q too, so the local
        # self-check and the official grade see byte-identical output.
        res = run_psql(["-q", "--csv", "-t"], student_sql)
        if res.returncode != 0:
            rows.append((name, "ERROR", 0.0, weight))
            continue

        got = normalize(res.stdout, ordered=name in ordered_checks)
        want = normalize(exp.read_text(), ordered=name in ordered_checks)
        if got == want:
            total += weight
            rows.append((name, "PASS", weight, weight))
        else:
            rows.append((name, "FAIL", 0.0, weight))

    score = round(total, 1)
    max_score = round(max_total, 1)
    passed = sum(1 for _n, s, _p, _w in rows if s == "PASS")
    ntotal = len(rows)

    # Console output.
    if a.no_points:
        print(f"\nPassed: {passed} / {ntotal} checks")
        for name, status, pts, wt in rows:
            print(f"  {name:8} {status}")
    else:
        who = f"{a.student} · " if a.student else ""
        print(f"\n{who}Score: {score} / {max_score}")
        for name, status, pts, wt in rows:
            print(f"  {name:8} {status:8} {round(pts,1)}/{round(wt,1)}")

    # 3) Write a Markdown summary for the Actions run page.
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            if a.no_points:
                f.write(f"## Sample / Advisory Feedback: {a.assignment}\n\n")
                f.write(f"**{passed} / {ntotal} questions passed**  (advisory result - not your official grade)\n\n")
                f.write("| Question | Result |\n|---|---|\n")
                for name, status, pts, wt in rows:
                    f.write(f"| `{name}` | {status} |\n")
            else:
                title = f"{a.student} · {a.assignment}" if a.student else a.assignment
                f.write(f"## Autograding Result: {title}\n\n")
                if a.student:
                    f.write(f"**Student:** `{a.student}`  \n")
                f.write(f"**Score: {score} / {max_score}**\n\n")
                f.write("| Check | Result | Points |\n|---|---|---|\n")
                for name, status, pts, wt in rows:
                    f.write(f"| `{name}` | {status} | {round(pts,1)} / {round(wt,1)} |\n")

    # 4) Machine-readable result (uploaded as an artifact; feed into a gradebook).
    Path("grade-result.json").write_text(json.dumps({
        "student": a.student,
        "assignment": a.assignment,
        "score": score,
        "max": max_score,
        "checks": [
            {"name": n, "status": s, "points": round(p, 1), "weight": round(w, 1)}
            for n, s, p, w in rows
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
