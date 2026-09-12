# btsearch-toolkit

A self-hosted BitTorrent DHT indexer with working Chinese full-text search on plain SQLite.

Zero dependencies — Python standard library only. Runs on Windows and Linux.

**Requires SQLite 3.43+**, which is what your Python bundles, not something you install
separately. The index uses a contentless FTS5 table (`content=''`, `contentless_delete=1`),
introduced in 3.43; below that the database cannot be created at all. Do not infer this from the
Python version — two 3.11 patch releases can ship different SQLite builds. Check it directly:

```
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Anything from python.org 3.12 or newer is comfortably above the line. `check.bat` (or
`python btcheck.py`) builds the real table and fails loudly if it can't.

[中文文档](README.md) has the full user manual.

---

## The problem this solves

**The BitTorrent DHT has no keyword search.** The protocol answers exactly one question: *"who has the torrent with infohash X?"* There is no way to ask *"which torrents have 'ubuntu' in the name."*

So keyword search over BitTorrent can only work one of two ways:

1. **Query someone else's index** — fast, but you depend on a third party staying up
2. **Build your own index first** — slow to accumulate, but nothing can take it away

This toolkit does both, into the same SQLite database, and lets you search across them.

```
   External sources                      DHT network
   Jackett / Internet Archive              │ sniff announce_peer and get_peers
   Academic Torrents / local .torrent      ▼
        │                          dhtsniff.py + dhtmeta.py
        ▼                                  │
   btimport.py ────────────► bt.db ◄───────┘
                               │
                               ├──► btweb.py     browser UI (search + browse + delete)
                               ├──► btsearch.py  CLI, also queries external sources live
                               ├──► btpeers.py   real swarm size via DHT lookups
                               └──► btprune.py   dead-entry cleanup, index repair
```

---

## Quick start

```bash
python btcheck.py                                   # environment self-test
python btimport.py folder /path/to/torrents         # seed the index from local .torrent files
python btweb.py --db bt.db                          # browse at http://127.0.0.1:8080
python dhtmeta.py --sniff --db bt.db --with-lookup  # start crawling the DHT
```

Or skip the command line entirely: start `btweb.py` and open
**http://127.0.0.1:8080/tasks** — the crawler, all four import sources, and maintenance actions
each have a form and a live log there. On Windows that is double-click `web.bat`, then click.

Letting a web page spawn processes is a meaningful privilege escalation, so the command line is
**assembled from a fixed template**, never concatenated: the endpoint accepts a task kind plus
named parameters, integers are clamped to sane ranges, paths and URLs are passed as separate
argv elements, and nothing goes through a shell. Same CSRF token and same-origin checks as the
delete endpoint. Pass `--no-tasks --no-delete` if you ever bind to anything other than
`127.0.0.1`.

`btcheck.py` verifies more than the environment: it statically scans every script for undefined
names, checks that the inline browser JavaScript has no unterminated string literals, verifies
that every flag used in the `.bat` launchers actually exists in the script it calls, and confirms
that CLI options are accepted both before and after the subcommand. Those four checks exist
because each one caught a real bug during development.

---

## Chinese full-text search on SQLite

This is the part most likely to be useful outside this project.

SQLite's FTS5 ships with `unicode61`, which splits on non-alphanumerics. Chinese has no spaces,
so an entire title collapses into a single token and substring search silently returns nothing:

```
tokenizer    query        result
unicode61    "阿凡达"      0 hits   ← on a document containing 阿凡达2水之道
trigram      "阿凡达"      1 hit
```

`trigram` works but has a hard 3-character minimum, so common 2-character queries (合集, 国语,
字幕) can never match.

The approach here is **CJK bigram expansion** against `unicode61`: at index time, every run of
CJK characters is expanded into overlapping 2-character pairs, and the same expansion is applied
to the query.

```
复仇者联盟  →  "复仇" "仇者" "者联" "联盟"
```

Two-character queries work, the index is smaller than trigram, and there is no minimum length
problem. One detail that is easy to miss: Latin/digit runs must be extracted *separately* as
well, or a title like `联盟4` never matches a query for `4`, because the mixed run is treated
as one CJK token.

See `expand_text()` and `build_match()` in `btindex.py`. Both must use identical rules — if index
time and query time disagree by even one rule, nothing matches and there is no error to see.

**Single-character CJK queries need an escape hatch.** The stored body holds bigrams plus the
original run, and `unicode61` treats an unbroken CJK run as one token — so a one-character query
matches neither, and FTS returns zero rows with no indication that the query was simply
unreachable. Those queries fall back to a `LIKE` scan. Two details matter: the fallback has to be
unconditional rather than "retry when FTS finds nothing" (tokenizer boundaries such as the slash
in `字幕/猫.srt` make FTS match a stray row, which suppresses the retry and hides the rest), and it
must not extend to single Latin letters, where a substring scan would return every row containing
that letter.

**Latin terms are matched as prefixes** (`"ubun"*`), with a floor of `PREFIX_MIN` = 3 characters.
The obvious win is typing half a word, but the real motivation is the tokenizer: `LATIN` is
`[0-9A-Za-z]+`, so `1080p` is a single token and an exact-match query for `1080` returns nothing
even when the index holds dozens of `1080p` releases. The same applies to `x264`, `S01E05`,
`amd64`. The floor is not optional — a one- or two-character prefix matches thousands of distinct
terms, and unioning their postings lists is a scan, not a lookup.

Every Latin term gets the prefix, not just the last one. Prefixing only the trailing term makes
`matrix 1080` match while `1080 matrix` does not, and order-dependent results in a search box are
worse than a little extra noise. Terms are ANDed, so extra words only narrow the result set.

CJK bigrams are left alone: every indexed CJK token is exactly two characters, so a prefix on a
two-character term can only match itself. The star goes outside the quotes — `"ubun"*` is a prefix
query, `"ubun*"` searches for a term that literally contains an asterisk. The quoting itself is
what keeps user input from being parsed as FTS5 syntax, so it cannot be dropped.

The cost is real: prefixes widen the hit set, and bm25 has to score every hit (see below).
`--exact` on the CLI turns it off.

---

## Measured performance

Not estimates. Benchmarked on a single database built to 5 million rows.

| Rows | DB size | Rare term | Show title (357k matches) | Very common term (1.17M matches) |
|---|---|---|---|---|
| 100k | 46 MB | 0.5 ms | 13 ms | 31 ms |
| 500k | 231 MB | 0.8 ms | 67 ms | 150 ms |
| 1M | 464 MB | 0.9 ms | 119 ms | 303 ms |
| 2M | 930 MB | 1.2 ms | 241 ms | 582 ms |
| 5M | 2.3 GB | 0.3 ms | 267 ms | 593 ms |

Write throughput held at **~31,000 rows/sec** throughout.

**Latency tracks the number of matches, not the size of the database.** A rare term returns in
under a millisecond whether the index holds 100 thousand rows or 5 million. Breaking down the
slowest query at 2M rows:

```
count matches (FTS only)          44 ms
fetch 25 rowids, no ORDER BY     0.1 ms   ← the search itself
ORDER BY hits DESC, LIMIT 25     231 ms   ← all of the cost
```

The index lookup is essentially free. The cost is sorting 469,307 matching rows to find the top
25. That is inherent to `ORDER BY` over a large match set, not something SQLite is doing badly.
Ranking by `bm25()` costs about 2.6× more than ordering by a stored column, because a score has
to be computed for every match.

**Prefix matching pushes against exactly this.** A prefix query is not a scan — the term
dictionary is sorted, so it is one b-tree seek plus a sequential read, nothing like the `LIKE`
fallback. What it does is enlarge the match set, and the match set is what costs. `PREFIX_MIN`
keeps the worst one- and two-character prefixes out, the 8-second wall clock in the web UI is the
backstop, and `--exact` is the opt-out. If that proves insufficient at 100M rows, the next step is
`prefix='2 3'` on the FTS table — genuinely fast short prefixes, paid for in index size and
another full rebuild. Not worth doing before it is measured.

Tuning that was tried: `mmap_size=1GB` gives a consistent ~17% improvement and is enabled on read
connections. Raising `cache_size` made things *worse* — the first query pays to fill the page
cache and never earns it back.

Row cost, measured after `VACUUM`:

| Stored per row | Bytes | 1M rows | 10M rows |
|---|---|---|---|
| Name only | 235 | 0.22 GB | 2.2 GB |
| Name + 5 file paths | 401 | 0.37 GB | 3.7 GB |
| Name + 40 file paths (default) | 1667 | 1.55 GB | 15.5 GB |

The full-text index dominates, not the source data — CJK bigram expansion is larger than the
original text.

The FTS table is **contentless** (`content=''`, `contentless_delete=1`), so the indexed body is
not stored a second time. Nothing ever reads it back: every FTS access in the codebase is a
`MATCH`, a `rowid`, or `bm25()`, display columns come from the main table, and the body itself is
recomputable from `name` and `filelist`. On a 6M-row database built the old way,
`torrents_fts_content` alone was 2.30 GB against 0.51 GB for the actual inverted index — 47% of
the file was dead weight. Dropping it cuts the table above by 30–50%. Requires SQLite 3.43+;
`btmigrate.py` converts an existing database.

### Per-page cost, which is what actually hurts at scale

Search latency tracks match count. These do not — they are paid on *every* page load, and they
scale linearly with the table. Measured at 6M rows:

| Action | Before | After |
|---|---|---|
| Header stats (`COUNT` + `SUM` + `MAX`) | 3.86 s | 0.00 s |
| Source dropdown (`GROUP BY source`) | 3.14 s | 0.00 s |
| Single-CJK-character search (LIKE fallback) | 15.69 s | 0.28 s |

Extrapolated to 100M rows the first two alone would have meant **two minutes to open any page**,
including the task panel.

Three fixes. Stats are cached with stale-while-revalidate, but only above
`STATS_CACHE_FROM` (100k) — below that a full scan costs tens of milliseconds and caching only
produces artifacts, like a page listing 80 rows under a header reading "0 rows". Emptiness is
checked with `SELECT 1 FROM torrents LIMIT 1`, constant time, never from cache. `get_stats` was
split into three queries: written as one, SQLite can only `SCAN torrents`, because no index covers
both `size` and `last_seen`; split, each uses its own covering index and never touches the main
table — 2.5 s becomes 0.9 s. **Three queries beat one.** And the LIKE fallback for single-character
queries is bounded to the most recent `LIKE_WINDOW` (500k) rows, which the results page states
plainly; `--deep` removes the bound on the CLI.

---

## Components

| File | Purpose |
|---|---|
| `dhtsniff.py` | DHT node that collects infohashes from `get_peers` / `announce_peer` traffic |
| `dhtmeta.py` | BEP 9 metadata fetch — turns an infohash into a name and file list; `--sniff` runs the full pipeline |
| `btindex.py` | Storage and retrieval (SQLite + FTS5, CJK bigrams) |
| `btimport.py` | Bulk import from Jackett/Prowlarr (Torznab), Internet Archive, Academic Torrents, local `.torrent` files |
| `btenrich.py` | Refetches metadata from the DHT for entries that arrived without a file list |
| `bttasks.py` | Process management behind the task panel — command lines are assembled from fixed templates, never concatenated |
| `btweb.py` | Browser UI: search, browse-all, source filter, select-and-delete, server-side folder picker |
| `btsearch.py` | CLI search that also queries external sources live |
| `btpeers.py` | Iterative DHT `get_peers` lookup to measure real swarm size |
| `btprune.py` | Index maintenance: health report, dead-entry pruning, FTS repair, VACUUM |
| `btmaint.py` | Scheduled-task entry point that chains the above |
| `btcompat.py` | Cross-platform layer (Windows console encoding, SQLite URIs, socket options) |
| `btmigrate.py` | One-shot migration of an existing database to a contentless FTS index (~47% smaller) |
| `make_test_torrents.py` | Generates structurally valid `.torrent` fixtures, including tokenizer edge cases |
| `btcheck.py` | Environment and self-consistency checks |

---

## Implementation notes

**`ORDER BY` can silently discard your range predicate.** The single-character LIKE fallback is
bounded with `rowid > MAX - 500000`, but the first version did nothing: `ORDER BY t.hits DESC`
pushed SQLite onto `idx_hits`, scanning the whole index from the top and checking the rowid bound
per row (`SCAN t USING INDEX idx_hits`). The bound degenerated into a filter. A unary plus —
`ORDER BY +hits DESC` — makes the expression stop matching an index column, and the plan becomes
`SEARCH t USING INTEGER PRIMARY KEY (rowid>?)`: 1.21 s to 0.26 s. **Always `EXPLAIN QUERY PLAN`
after adding a range predicate**, or you are only assuming it took effect.

**Splitting one aggregate query into three made it faster.** `SELECT COUNT(*), SUM(size),
MAX(last_seen)` forces a full table scan, because no single index covers both `size` and
`last_seen`. As three statements each one rides its own covering index and never touches the main
table: 2.5 s to 0.9 s at 6M rows.

**A centred layout shifts when the scrollbar appears.** `.wrap` is `margin:0 auto`; one page
having a vertical scrollbar and the next not changes the available width by the scrollbar, moving
the centre by half of it (~8px on Windows). Switching pages made the whole UI jump sideways.
`html{scrollbar-gutter:stable}` reserves the gutter permanently — it must go on `html`, not
`body`.

**A local web page cannot learn a local absolute path.** `<input type="file" webkitdirectory>`
yields names and relative paths only, while `btimport` needs `D:\torrents`. The folder picker is
therefore served by the backend (`/api/browse`), which is reasonable here because the server runs
on the user's own machine. It is gated behind the task panel (`--no-tasks` disables it too),
returns directory names and a `.torrent` count only — never file names or contents — and the
frontend renders entries with `textContent`, because directory names can contain angle brackets.

Things that cost real debugging time. Most are not in any documentation.

**FTS5 has no triggers here, so deletes must touch both tables.** The FTS table is standalone and
synced manually. A plain `DELETE FROM torrents` leaves orphan rows in `torrents_fts`. SQLite then
recycles the freed rowids, and the next insert collides with a leftover FTS entry:

```
after deleting only the main table -> torrents: 0 rows, FTS: 20 orphans
next insert -> IntegrityError: constraint failed
```

The crawler dies hours later, far from the cause.

**Deleting by a criterion that reads the FTS table destroys that criterion.** Deleting the FTS
rows first and then reusing the same `WHERE` clause for the main table silently deletes nothing —
the subquery it depends on was just emptied. The result is FTS rows gone, main rows still there:
permanently unsearchable zombie rows, and a reported delete count of zero. Materialize the
matching rowids into a temp table first, then delete from both tables by rowid.

**FTS5 deletes make the file grow.** Deletions are recorded as tombstone segments, not removed in
place. After deleting 2000 rows, `torrents_fts_data` went from 45 rows to 73. `VACUUM` alone only
gets partway; `optimize` first, then `VACUUM`, returns the file to the exact size of a fresh empty
database:

```
after deleting everything      1464 KB
VACUUM only                     288 KB
optimize + VACUUM                44 KB   ← same as a brand-new database
```

**`SO_REUSEADDR` means the opposite thing on Windows.** On POSIX it permits rebinding a port in
`TIME_WAIT`. On Windows it permits *another process to take a port you are already using*. Two
crawler instances silently bind the same port and split the traffic between them. The symptom is
that yield mysteriously halves, with nothing in any log. Use `SO_EXCLUSIVEADDRUSE` on Windows.

**Python's stdout must be forced to UTF-8 on Windows, twice.** Console output works because
Python uses the Unicode console API — but the moment output is redirected to a file (which is what
Task Scheduler does), it switches to the locale codepage and throws `UnicodeEncodeError` on the
first `✓`. Separately, `subprocess.run(text=True)` decodes *child* output with the locale
codepage, so a parent reading a UTF-8-emitting child raises inside the reader thread, `stdout`
comes back as `None`, and the caller dies on a `TypeError`. Same root cause, two different places.

**Inline JavaScript needs a raw string.** Serving browser JS from a normal Python `"""..."""`
means Python interprets `\n` inside JS string literals as an actual newline. JS string literals
cannot span lines, so the whole file fails to parse and *every* handler on the page silently stops
working — with nothing on the server side to indicate anything is wrong. Use `r"""..."""`.

**A Kademlia lookup must drain its socket every round.** The original implementation sent `alpha`
queries per round but read exactly one datagram. Candidates never caught up with queries, so the
"no closer nodes left" termination fired while most replies were still sitting in the socket
buffer — converging far away from the target, where no node holds the peer list. Fixing this took
the hit rate from **1.4% to 31.8%** on live traffic.

**Bootstrap routers only speak `find_node`.** Sending `get_peers` straight at
`router.bittorrent.com` and friends gets no reply. A lookup then exits in about a second with zero
peers, which looks exactly like a dead torrent. Warm up with `find_node` to collect real nodes
first, then run the `get_peers` iteration against those.

**Torznab hides the infohash in five different places.** Depending on the indexer it may be in a
`torznab:attr` named `infohash` or `magneturl`, in `<link>`, in `<guid>`, or in
`<enclosure url="...">` — and it may be base32 rather than hex. Checking only one or two places
misclassifies large numbers of results as "no magnet link available."

**Jackett's `/dl/` URL 302-redirects to `magnet:` for magnet-only indexers.** `urllib` follows the
redirect and raises `unknown url type: magnet`. Disable redirect following and read `Location`
yourself; if it starts with `magnet:`, the infohash is right there.

**argparse only accepts global options before the subcommand.** People naturally write them after.
Add the same options to each subparser with `default=argparse.SUPPRESS` — without `SUPPRESS`, the
subparser's default silently overwrites the value the user passed at the top level.

**Never write `except Exception: pass`.** Every hardest-to-diagnose problem in this project was a
swallowed exception. Metadata fetches failing, Jackett indexer enumeration failing, torrent
parsing failing — all presented identically as "the result is 0" with no other signal. Counting
and printing the actual reasons turned multi-hour investigations into minutes.

---

## Preview images

A torrent carries no image. The `info` dictionary holds a name, piece length, piece hashes and a
file list — `cover.jpg` in that list is a filename, not an image; the bytes only exist once you
join the swarm.

Some indexers do publish a cover URL separately (`coverurl` in the Torznab schema), and Internet
Archive thumbnails are derivable from the item identifier. Both are captured at import time. Only
the URL is stored — roughly 75 bytes per row, against 50–300 KB for the image itself.

The detail page renders a **"show preview" button, and loads nothing until it is clicked**. That
is a privacy decision as much as a layout one: the image is hosted by the indexer, so fetching it
tells them your IP. Leave it unclicked and no outbound request is made. Nothing is ever written to
disk by this project; whatever the browser caches is governed by the remote server's headers.

## Scope

This tool indexes metadata that is **publicly broadcast on the DHT**, plus entries from indexers
that you configure yourself. It does not host, transfer, or distribute any content. The database
holds names, sizes, file lists, and infohashes.

What flows through the DHT is not something a user of this tool controls. Running a private local
index and operating a public search service are legally very different activities, and operators
of the latter have faced action in multiple jurisdictions. The web UI binds to `127.0.0.1` by
default; evaluate the consequences before changing `--host`. No indexer list ships with this
project — which sites `btimport.py` can reach is entirely determined by what you configure in
Jackett.

Provided as-is. You are responsible for how you use it.

---

## License

[MIT](LICENSE)
