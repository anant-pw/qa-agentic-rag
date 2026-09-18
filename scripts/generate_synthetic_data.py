"""
Generates a larger, realistic-looking synthetic QA corpus, in exactly the
format ingest.py already parses -- same CSV columns
(id,title,description,steps_to_reproduce,module,status,date_created),
same markdown shape (# TC-XXXX: <title> / **Module:** / ## Preconditions
/ ## Steps / ## Expected Result) confirmed by reading ingest.py's real
parsing regex directly, not guessed.

SAFE BY DESIGN, not by luck: new IDs start at BUG-2000 / TC-2000,
deliberately outside the existing BUG-1001-1024 / TC-0142/0301/0302
range eval_seed.json's gold answers depend on. This script APPENDS to
data/bug_reports.csv and ADDS new files to data/test_cases/ -- it never
touches or overwrites the original 25+3 documents, so the existing,
already-proven eval baseline stays valid after ingestion.

Deterministic (fixed random seed) -- rerunning this script produces the
exact same corpus, not a new random one each time, so results stay
reproducible the same way every other real number in this project has
been.

Usage:
    python scripts/generate_synthetic_data.py --bugs 120 --test-cases 25

Then re-ingest the COMBINED corpus (original + new, all in the same
files now) with a clean schema, since ingest.py's idempotency on
re-running against already-loaded rows was never verified in this
project -- safer to rebuild from a known-clean state than assume:

    docker exec -i rag-postgres psql -U rag_user -d rag_db < schema.sql
    python ingest.py
    python scripts/build_index.py
    python -m eval.run_eval --endpoint /search/hybrid   # sanity check: should still show the same Recall/MRR numbers on the original 15 scored questions
"""

import argparse
import os
import random

DATA_DIR = "./data"
BUG_CSV = os.path.join(DATA_DIR, "bug_reports.csv")
TC_DIR = os.path.join(DATA_DIR, "test_cases")

MODULES = ["Login", "Payments", "Checkout", "Search", "Notifications",
           "Admin", "Profile", "Cart", "Reports", "API"]
STATUSES = ["Open", "In Progress", "Closed", "Resolved"]
BROWSERS = ["Chrome 128", "Firefox 130", "Safari 17", "Edge 127"]
ERROR_CODES = ["ERR_401_UNAUTH", "ERR_403_FORBIDDEN", "ERR_500_INTERNAL",
               "ERR_504_TIMEOUT", "ERR_422_VALIDATION", "ERR_409_CONFLICT"]

# One realistic bug template per module -- {browser}/{status-ish detail}
# get filled in per-instance so the same template produces varied,
# non-identical rows, not literal copies.
BUG_TEMPLATES = {
    "Login": "User cannot sign in on {browser} despite entering the correct password; session times out after {n} seconds.",
    "Payments": "Payment confirmation email is delayed by up to {n} minutes after a successful {browser} checkout.",
    "Checkout": "Cart total fails to update when a promo code is applied on {browser}, off by ${n}.00.",
    "Search": "Search results ignore the selected category filter on {browser} when query contains a hyphen.",
    "Notifications": "Push notification is delivered twice for the same event on {browser}, {n} seconds apart.",
    "Admin": "Bulk export times out for reports over {n} rows when run from {browser}.",
    "Profile": "Profile photo upload silently fails on {browser} for files over {n}MB, no error shown.",
    "Cart": "Item quantity resets to 1 after page refresh on {browser} when quantity was set above {n}.",
    "Reports": "Scheduled report fails to send on {browser}-generated schedules created after a DST change.",
    "API": "API returns a {code} for a valid request on {browser}-originated calls when a trailing slash is present.",
}

TC_TEMPLATES = {
    "Login": ("An account exists with a known valid password.",
              "1. Enter valid credentials.\n2. Submit the login form.",
              "User is redirected to the dashboard within {n} seconds."),
    "Payments": ("A test payment processor is configured in sandbox mode.",
                 "1. Add an item to cart.\n2. Complete checkout with a test card.",
                 "Confirmation email arrives within {n} minutes of a successful charge."),
    "Checkout": ("A valid promo code exists in the promotions table.",
                 "1. Add an item to cart.\n2. Apply the promo code at checkout.",
                 "Cart total reflects the discount immediately, no page reload required."),
    "Search": ("The catalog contains items with hyphenated terms in the title.",
               "1. Search using a hyphenated query.\n2. Apply a category filter.",
               "Results respect both the query and the selected filter."),
    "Admin": ("An admin account exists with export permissions.",
              "1. Navigate to Reports.\n2. Trigger a bulk export over {n} rows.",
              "Export completes and downloads without timing out."),
}


def generate_bug_rows(n: int, start_id: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        module = rng.choice(MODULES)
        template = BUG_TEMPLATES.get(module, BUG_TEMPLATES["Login"])
        desc = template.format(
            browser=rng.choice(BROWSERS),
            n=rng.randint(2, 45),
            code=rng.choice(ERROR_CODES),
        )
        bug_id = f"BUG-{start_id + i}"
        rows.append({
            "id": bug_id,
            "title": f"{module}: {desc.split('.')[0]}",
            "description": desc,
            "steps_to_reproduce": f"1. Open the {module} page on {rng.choice(BROWSERS)}.\n2. Reproduce the condition described above.\n3. Observe the incorrect behavior.",
            "module": module,
            "status": rng.choice(STATUSES),
            "date_created": f"2026-{rng.randint(1,9):02d}-{rng.randint(1,28):02d}",
        })
    return rows


def generate_test_cases(n: int, start_id: int, seed: int) -> list[dict]:
    rng = random.Random(seed + 1)  # different stream than bugs, still deterministic
    cases = []
    modules_with_tc = list(TC_TEMPLATES.keys())
    for i in range(n):
        module = rng.choice(modules_with_tc)
        pre, steps, expected = TC_TEMPLATES[module]
        tc_id = f"TC-{start_id + i}"
        cases.append({
            "id": tc_id,
            "title": f"{module} verification case {i+1}",
            "module": module,
            "preconditions": pre,
            "steps": steps.format(n=rng.randint(2, 20)),
            "expected_result": expected.format(n=rng.randint(2, 20)),
        })
    return cases


def append_bugs_to_csv(rows: list[dict]) -> None:
    import csv
    file_exists = os.path.isfile(BUG_CSV)
    if not file_exists:
        raise SystemExit(f"{BUG_CSV} not found -- run this from the project root, after Phase 2's original data is already in place.")

    with open(BUG_CSV, "r", newline="", encoding="utf-8") as f:
        existing_ids = {r["id"] for r in csv.DictReader(f)}
    collisions = [r["id"] for r in rows if r["id"] in existing_ids]
    if collisions:
        raise SystemExit(f"ID collision, refusing to write: {collisions[:5]}... -- already present in {BUG_CSV}. Raise --start-bug-id.")

    with open(BUG_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "title", "description", "steps_to_reproduce", "module", "status", "date_created"])
        writer.writerows(rows)
    print(f"Appended {len(rows)} bug reports to {BUG_CSV} (IDs {rows[0]['id']}..{rows[-1]['id']})")


def write_test_case_files(cases: list[dict]) -> None:
    os.makedirs(TC_DIR, exist_ok=True)
    for c in cases:
        path = os.path.join(TC_DIR, f"{c['id']}.md")
        if os.path.isfile(path):
            raise SystemExit(f"{path} already exists -- refusing to overwrite. Raise --start-tc-id.")
        content = (
            f"# {c['id']}: {c['title']}\n"
            f"**Module:** {c['module']}\n\n"
            f"## Preconditions\n{c['preconditions']}\n\n"
            f"## Steps\n{c['steps']}\n\n"
            f"## Expected Result\n{c['expected_result']}\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"Wrote {len(cases)} test case files to {TC_DIR}/ (IDs {cases[0]['id']}..{cases[-1]['id']})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bugs", type=int, default=120)
    parser.add_argument("--test-cases", type=int, default=25)
    parser.add_argument("--start-bug-id", type=int, default=2000)
    parser.add_argument("--start-tc-id", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    bug_rows = generate_bug_rows(args.bugs, args.start_bug_id, args.seed)
    tc_rows = generate_test_cases(args.test_cases, args.start_tc_id, args.seed)

    append_bugs_to_csv(bug_rows)
    write_test_case_files(tc_rows)

    print(f"\nTotal corpus after this run: 28 original + {args.bugs + args.test_cases} new = {28 + args.bugs + args.test_cases} documents")
    print("Next: rebuild the schema fresh and re-ingest the combined corpus (see this file's module docstring for exact commands).")
