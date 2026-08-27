# Phase 3 — 5 Query Results (REAL output, run against the live corpus)

Verified with `verify_phase3.sh` first — all six acceptance checks passed
(28 total docs, 25/3 doc_type split, 3/1/1/2 error-code counts, exact
BUG-1013 lookup, TC-0209 dangling-ref check). Queries below run against
that confirmed-good index via `GET /search`.

## 1. Exact error-code query
`curl "http://localhost:8000/search?q=ERR_401_UNAUTH"`

```json
{"query":"ERR_401_UNAUTH","doc_type":null,"module":null,"status":null,"results":[
  {"document_id":1,"external_id":"BUG-1001","title":"Login fails after 3 attempts even with correct password","doc_type":"bug_report","module":"Login","status":"Open","score":7.018542},
  {"document_id":15,"external_id":"BUG-1015","title":"Login session expires mid-checkout without warning","doc_type":"bug_report","module":"Checkout","status":"In Progress","score":7.018542},
  {"document_id":24,"external_id":"BUG-1024","title":"Login - see BUG-1001, still unresolved","doc_type":"bug_report","module":"Login","status":"Reopened","score":6.772981}
]}
```
Matches the exact 3-document set `eval_seed.json`'s `error_code_aggregation`
question expects. Scores aren't identical despite all three matching the
same boosted `error_codes` term — the `multi_match` BM25 component on
`title`/`cleaned_text` adds a small independent contribution per document
based on field-length normalization, so the boosted term match dominates
but doesn't fully flatten the ranking.

## 2. Bug-ID query
`curl "http://localhost:8000/search?q=BUG-1013"`

```json
{"query":"BUG-1013","doc_type":null,"module":null,"status":null,"results":[
  {"document_id":13,"external_id":"BUG-1013","title":"Payment form accepts expired card without client-side warning","doc_type":"bug_report","module":"Payments","status":"Open","score":14.809153},
  {"document_id":11,"external_id":"BUG-1011","title":"Duplicate notification bug","doc_type":"bug_report","module":"Notifications","status":"Open","score":5.6275983},
  {"document_id":24,"external_id":"BUG-1024","title":"Login - see BUG-1001, still unresolved","doc_type":"bug_report","module":"Login","status":"Reopened","score":4.6368055},
  {"document_id":17,"external_id":"BUG-1017","title":"Admin bulk export - resolved, follow-up to BUG-1007","doc_type":"bug_report","module":"Admin","status":"Resolved","score":3.9426622},
  {"document_id":2,"external_id":"BUG-1002","title":"Account lockout not clearing after correct password entry","doc_type":"bug_report","module":"Login","status":"Open","score":2.2081275},
  {"document_id":19,"external_id":"BUG-1019","title":"ERR_403_FORBIDDEN on password change for SSO-linked accounts","doc_type":"bug_report","module":"Login","status":"Open","score":1.4435464},
  {"document_id":22,"external_id":"BUG-1022","title":"Admin role edit form silently drops permissions not in current view","doc_type":"bug_report","module":"Admin","status":"Open","score":1.3777779}
]}
```
Top result correct (BUG-1013, ~2.6x separation from #2). Weakness: the
tail (BUG-1011, BUG-1024, BUG-1017, BUG-1022) is topically unrelated to
payments/expired cards — pulled in because the standard analyzer splits
"BUG-1013" into `["bug","1013"]` and `multi_match` then matches the bare
word "bug" wherever it appears, including inside other documents' own
cross-reference mentions ("see BUG-1001", "follow-up to BUG-1007"). Real,
demonstrated limitation of relying on the analyzed text field for ID
lookups — the exact-term boost rescues the top result but doesn't
suppress the noisy tail.

## 3. Test-case-ID query
`curl "http://localhost:8000/search?q=TC-0209"` and `q=TC-0142`

```json
{"query":"TC-0209","doc_type":null,"module":null,"status":null,"results":[
  {"document_id":13,"external_id":"BUG-1013","title":"Payment form accepts expired card without client-side warning","doc_type":"bug_report","module":"Payments","status":"Open","score":12.004721},
  {"document_id":2,"external_id":"BUG-1002","title":"Account lockout not clearing after correct password entry","doc_type":"bug_report","module":"Login","status":"Open","score":1.6616777},
  {"document_id":1,"external_id":"BUG-1001","title":"Login fails after 3 attempts even with correct password","doc_type":"bug_report","module":"Login","status":"Open","score":1.4788429},
  {"document_id":28,"external_id":"TC-0302","title":"Exact SKU match ranks first in search results","doc_type":"test_case","module":"Search","status":null,"score":1.3777779},
  {"document_id":27,"external_id":"TC-0301","title":"Search tokenization consistency for hyphenated terms","doc_type":"test_case","module":"Search","status":null,"score":1.3177412},
  {"document_id":26,"external_id":"TC-0142","title":"Account lockout and cooldown after failed login attempts","doc_type":"test_case","module":"Login","status":null,"score":1.1221502}
]}
```
```json
{"query":"TC-0142","doc_type":null,"module":null,"status":null,"results":[
  {"document_id":26,"external_id":"TC-0142","title":"Account lockout and cooldown after failed login attempts","doc_type":"test_case","module":"Login","status":null,"score":20.983688},
  {"document_id":2,"external_id":"BUG-1002","title":"Account lockout not clearing after correct password entry","doc_type":"bug_report","module":"Login","status":"Open","score":7.47692},
  {"document_id":1,"external_id":"BUG-1001","title":"Login fails after 3 attempts even with correct password","doc_type":"bug_report","module":"Login","status":"Open","score":7.0355687},
  {"document_id":13,"external_id":"BUG-1013","title":"Payment form accepts expired card without client-side warning","doc_type":"bug_report","module":"Payments","status":"Open","score":1.4435464},
  {"document_id":28,"external_id":"TC-0302","title":"Exact SKU match ranks first in search results","doc_type":"test_case","module":"Search","status":null,"score":1.3777779},
  {"document_id":27,"external_id":"TC-0301","title":"Search tokenization consistency for hyphenated terms","doc_type":"test_case","module":"Search","status":null,"score":1.3177412}
]}
```
The intended contrast, confirmed: no document with `external_id: TC-0209`
exists in either result set — the system does not hallucinate a document
that was never ingested. `TC-0209`'s top result is BUG-1013 (score 12.0),
surfaced correctly via its `mentioned_tc_ids` field — this is the citing
document, not the cited one, and that distinction held. `TC-0142`, which
genuinely exists, returns itself as the clear top result (score 21.0,
~2.8x separation) with related login/lockout bugs as secondary matches.
This is the strongest result in the whole test set: the dangling-reference
design from Phase 2 (Postgres `target_document_id IS NULL`, citation still
recorded) survived translation into the search layer intact, with no
special-casing required in the query logic — it fell out naturally from
indexing `mentioned_tc_ids` as an extracted field.

## 4. Natural-language QA issue query
`curl "http://localhost:8000/search?q=login+keeps+failing+after+password+reset"`

Note: the original run of this query lost the space between "password"
and "reset" (`%20` didn't survive a stacked-command paste into Git Bash),
so the query that actually executed was `"login keeps failing after
passwordreset"` — one token, not two.

```json
{"query":"login keeps failing after passwordreset","doc_type":null,"module":null,"status":null,"results":[
  {"document_id":1,"external_id":"BUG-1001","title":"Login fails after 3 attempts even with correct password","doc_type":"bug_report","module":"Login","status":"Open","score":10.048056},
  {"document_id":8,"external_id":"BUG-1008","title":"Password reset email delayed by up to 20 minutes","doc_type":"bug_report","module":"Login","status":"Open","score":8.996567},
  {"document_id":2,"external_id":"BUG-1002","title":"Account lockout not clearing after correct password entry","doc_type":"bug_report","module":"Login","status":"Open","score":6.9192605},
  {"document_id":26,"external_id":"TC-0142","title":"Account lockout and cooldown after failed login attempts","doc_type":"test_case","module":"Login","status":null,"score":6.9192605},
  {"document_id":24,"external_id":"BUG-1024","title":"Login - see BUG-1001, still unresolved","doc_type":"bug_report","module":"Login","status":"Reopened","score":4.0857162},
  {"document_id":15,"external_id":"BUG-1015","title":"Login session expires mid-checkout without warning","doc_type":"bug_report","module":"Checkout","status":"In Progress","score":3.8592315},
  {"document_id":19,"external_id":"BUG-1019","title":"ERR_403_FORBIDDEN on password change for SSO-linked accounts","doc_type":"bug_report","module":"Login","status":"Open","score":3.656537},
  {"document_id":23,"external_id":"BUG-1023","title":"Payment retry after decline charges card twice","doc_type":"bug_report","module":"Payments","status":"Open","score":3.4435878},
  {"document_id":21,"external_id":"BUG-1021","title":"Notifications - push token not refreshed after app update","doc_type":"bug_report","module":"Notifications","status":"Open","score":3.2627234},
  {"document_id":16,"external_id":"BUG-1016","title":"Notification preferences toggle doesn't save","doc_type":"bug_report","module":"Notifications","status":"Open","score":2.8164816}
]}
```
Even with the mangled token, results are reasonable: BUG-1001, BUG-1008,
BUG-1002, TC-0142 all genuinely login/password-related, driven by
"login," "password," and "failing" matching independently. This is
partly luck, not proof the query behaved as intended — "passwordreset"
as a single token never matched anything, since no document contains
that literal string. NOT rerun yet with corrected encoding
(`login+keeps+failing+after+password+reset`) — this is an open item, not
a completed check. The deeper, expected limitation (BM25 missing true
paraphrases with zero shared vocabulary) wasn't actually tested by this
run, since every result shares literal words with the query. Re-run with
a genuine paraphrase (e.g. "users getting locked out even with correct
credentials") would be a better test of Phase 4's actual motivation.

## 5. Filtered query (doc_type + module)
`curl "http://localhost:8000/search?doc_type=bug_report&module=Checkout"`

```json
{"query":null,"doc_type":"bug_report","module":"Checkout","status":null,"results":[
  {"document_id":3,"external_id":"BUG-1003","title":"Checkout page times out on slow connections","doc_type":"bug_report","module":"Checkout","status":"In Progress","score":1.0},
  {"document_id":9,"external_id":"BUG-1009","title":"Checkout allows negative quantity via URL manipulation","doc_type":"bug_report","module":"Checkout","status":"Open","score":1.0},
  {"document_id":15,"external_id":"BUG-1015","title":"Login session expires mid-checkout without warning","doc_type":"bug_report","module":"Checkout","status":"In Progress","score":1.0},
  {"document_id":18,"external_id":"BUG-1018","title":"Checkout total miscalculates with multiple discount codes","doc_type":"bug_report","module":"Checkout","status":"Open","score":1.0},
  {"document_id":25,"external_id":"BUG-1025","title":"Checkout page - minor CSS misalignment on Safari","doc_type":"bug_report","module":"Checkout","status":"Won't Fix","score":1.0}
]}
```
```json
{"query":null,"doc_type":"bug_report","module":"Checkout","status":"Open","results":[
  {"document_id":9,"external_id":"BUG-1009","title":"Checkout allows negative quantity via URL manipulation","doc_type":"bug_report","module":"Checkout","status":"Open","score":1.0},
  {"document_id":18,"external_id":"BUG-1018","title":"Checkout total miscalculates with multiple discount codes","doc_type":"bug_report","module":"Checkout","status":"Open","score":1.0}
]}
```
Module-only filter returns all 5 Checkout bug reports regardless of
status, all scored `1.0` — confirms filters are genuinely unscored, not
silently ranked. Adding `status=Open` narrows to exactly BUG-1009 and
BUG-1018 — an exact match to `eval_seed.json`'s own `structured_filter`
note ("BUG-1003(In Progress, excl)... BUG-1009(Open)... answer is 2").

## Overall assessment

BM25 keyword search over the real 28-document corpus works as designed
for its strongest use case: exact identifiers and codes. The error-code
aggregation (query 1), exact test-case lookup (query 3's TC-0142 half),
and structured filtering (query 5) all matched predictions made before
the corpus was queried, which is real evidence, not just plausible-looking
output. The dangling-reference case (query 3's TC-0209 half) is the
single most important result in this set: it confirms an architectural
decision made all the way back in Phase 2's schema design (recording a
citation to a document that was never ingested, rather than silently
dropping it) survived, unmodified, through ingestion, indexing, and
querying — three separate pieces of code that all had to agree on what
"TC-0209 doesn't exist but is referenced" means.

The real weakness is query 2: free-text ID search pollutes its result
tail with documents that share no topic, only the accidental bare word
"bug." That's not a hypothetical concern I wrote into the design
docstring pre-emptively turning out to be right — it's now demonstrated
against real data. It doesn't block Phase 4, but it's a legitimate
finding for the "known limitations" section of any writeup, and a
concrete example of BM25's failure mode to contrast against once
Phase 4's embeddings exist.

Query 4 is the one open item: the natural-language test was compromised
by a URL-encoding mistake during manual testing, not a system fault, and
should be re-run with correct encoding and a genuine paraphrase (not just
overlapping vocabulary) before treating it as a real baseline for
Phase 4's improvement claim.
