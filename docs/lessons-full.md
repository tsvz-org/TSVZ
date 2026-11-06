# Lessons: full entries

Why this project's APIs, formats and agreed behaviours had to change. `lessons.md` has one line per
entry: read that first, and look entries up here by date and name rather than reading this whole. New
entries go last, in the same commit as the change:

```markdown
## YYYY-MM-DD <short name>
- Changed: ...
- Why we had to change: ...
- Original design was wrong because: ...
- Lesson: ...
```

## 2026-10-29 TSVZ: strict writes
- Changed: writing text the file's encoding cannot hold raises `TSVZEncodeError` (a `ValueError`) instead of storing `?`; `errors='replace'` brings `?` back.
- Why we had to change: 4.9 stored `?` for such text (for example the lone surrogate `os.fsdecode` gives for a non-UTF-8 file name), so the file no longer held what the program set, and nothing said so.
- Original design was wrong because: the write path replaced what it could not encode, the way a read may show what it cannot decode; a lossy write is never what the caller meant.
- Lesson: a default may be lossy on read (show what is there) but never on write; refuse, and let the caller ask for the loss.

## 2026-10-29 TSVZ: pointer x-delimiter escaping
- Changed: a handler's pointer file writes `x-delimiter` with backslash escapes (`\t`, `\x00`), and clients decode it after their control-character check.
- Why we had to change: every plain `.tsv` handler wrote a raw TAB, which clients and `tsvz stop` rejected as a control character, so no `.tsv` handler was ever found.
- Original design was wrong because: the pointer format allowed a value its own validator refuses; no test routed a `.tsv` store through a handler (the routing test routed 0 of its 23 steps).
- Lesson: a field that can hold any byte needs an escaping rule in the format, and a test of each store kind through the real path.

## 2026-10-29 TSVZ: rewrite order
- Changed: whole rewrites of `.tsv`-family files write over the file from offset 0 and cut it last (spec §19.8's order), instead of emptying it first.
- Why we had to change: another process's background read takes no lock; meeting an emptied file, it undid that process's deletes (two processes on a `.tsv.gz`: 7 of 12 runs clean, now 24 of 24).
- Original design was wrong because: emptying first creates a state, an empty store, that is never a real state of the data, and every reader that does not wait on the lock can see it.
- Lesson: an in-place rewrite should pass only through states a reader can live with; write over, then cut, and make readers notice a cut short (every codec, not only the one that raises).

## 2026-10-29 TSVZ: last chance at close
- Changed: close() and the exit hook try once more to make a damaged file again from memory, whatever `rewrite_on_exit` says and however lately a rewrite was refused or failed. A refusal or failure that lasts to that point is reported again there, and says nothing is tried after it.
- Why we had to change: at the default `rewrite_interval=0` the background writer retried a refused rewrite every second, so close() nearly always fell inside the refusal window and tried nothing. Each refusal and failure was also reported only once. A damaged store's memory, the only good copy, then went with the process without a word. The earlier report had said it would be tried again.
- Original design was wrong because: the retry throttles and the report dedupe were written for a store that keeps running. close() is the one moment with no later try, and it went through the same throttle and dedupe.
- Lesson: when a message promises what happens next, compute that promise from the same state that schedules it. Check every throttle and dedupe at the point of no return (close, exit, a Ctrl-C in either), because that is where "later" stops existing.

## 2026-11-01 TSVZ 5.1: a legacy scrub that writes nothing fails
- Changed: a `tsvz scrub` of a `.tsv`-family file that writes nothing because the file kept changing (three reads, each overtaken by another writer before the write) exits 1 with `scrub refused; nothing was written`, and a `tsvz serve` handler answers refused, as README C6 already said for a `.tsvz`. 5.0 exited 0. `scrubTabularFile` still returns the rows it read, with a warning.
- Why we had to change: the scrub now writes only if the file is still what its read saw (spec §19.8), so it can end having written nothing; reporting that as success tells a cron job or an operator the file was compacted when it was not. 5.0 never skipped: it wrote over the rows another process had appended after its read, and its exit 0 was "true" only because it lost them.
- Original design was wrong because: the legacy scrub was a read followed by a blind write, so it had no "did not write" outcome at all, and the CLI and handler hard-coded success for it while the `.tsvz` path, built later from the spec, returned the real outcome.
- Lesson: when two dialects share a command, give both one result shape (rows, done) from the library, and let the CLI and handler map that one value; a hard-coded `return True` in a caller is a result nobody computed.

## 2026-11-01 TSVZ 5.1: scrubTabularFile(lastLineOnly=True) of a .tsv is a read
- Changed: `scrubTabularFile(..., lastLineOnly=True)` of a `.tsv`-family file returns the last row and writes nothing, as it already did for a `.tsvz`. 3.39 and 5.0 wrote the file back holding only that row, cell by cell as deletion lines (`id\tv\na\t\n3\t\n`), so the store was gone.
- Why we had to change: no caller can want a "scrub" that keeps one row of a key-value store; the flag was passed through from `readTabularFile`, where it means "read only the last line", and the write-back then treated the returned row as a whole store.
- Original design was wrong because: scrub forwarded every read option to its read and wrote back whatever came back, without asking whether the option changes the shape of what is returned (a row, not a dict of rows).
- Lesson: a function built as "read, then write back" must take only read options that keep the result a whole store; an option that changes the result's shape either makes it a pure read or is refused, never written back.

## 2026-11-01 TSVZ 5.1: a legacy scrub keeps the file's #_defaults_# lines
- Changed: a scrub of a `.tsv`-family file writes the defaults in effect at the end of its read (the last non-empty `#_defaults_#` line, its empty cells filled from the line before it as a reader fills them) after the rows, and an empty defaults line it read (defaults off) before the rows, padded to their width. The caller's read-time defaults are neither written nor changed. A scrub, or a header-less `TSVZed` rewrite, of a headed file keeps the header on line 1 (the `TSVZed`'s defaults line right after it) instead of putting an empty line in front of it. 3.39 and 5.0 dropped the defaults lines.
- Why we had to change: rows appended after a scrub got no defaults, and a store that set defaults through the file lost them at its next open; the empty line put in front of a header made a reader given that header read it as a row.
- Original design was wrong because: scrub was written as "clear, then append the rows", and clear knows only the header, so everything in the file that is not a row or the header (defaults lines, the empty line standing for a header) was lost or misplaced. Defaults lines also apply from where they stand, so where they are written is part of what the rows mean.
- Lesson: a compaction must write back everything that changes how the rows read, not only the rows, and put it where it reads the same; test a compaction by reading before and after with every reader, not only by comparing to an expected byte layout.

## 2026-11-03 TSVZ 5.2: --x-header makes line 1 the header for every CLI operation
- Changed: with `tsvz --x-header H`, line 1 of a `.tsv`-family file that is H (an exact match: the same cells, each without the whitespace around it, a trailing empty cell counting) is the header for every operation (`read`, `keys`, `len`, `get`, `has`, `pop`, `popitem`, `delete`, `set`, `append`, `clear`, `scrub`) and for a `tsvz serve STORE --x-header H` handler's requests: never a key, kept by `scrub` and `clear`. A line 1 that differs makes the file header-less (line 1 is a row) with 3.39's "Header mismatch", for reads and the scrub alike, and the scrub of such a file never writes a row equal to H as line 1 (it puts an empty line first). `--x-strict` makes a mismatch an error (exit 1, nothing written), and `tsvz serve --x-strict` is now strict too (5.1 ignored it). 5.1 used `--x-header` only for `set`/`append`/`delete`/`clear`, so `tsvz keys` printed `id a b` and `popitem first` popped the header; the handler ignored it. Without `--x-header` nothing changed. A `.tsvz`'s `pop`, `popitem` and `setdefault` now check `--x-header` as `set` does. The library's own header check (`header=` to `appendTabularFile`, `clearTabularFile`, `TSVZed`) stays 3.39's containment check (`idaho\tvalue` passes for `id\tv`); only the CLI rule is exact.
- Why we had to change: the CLI built its store with the header for some commands (`checkHeader=operation in (...)`) and without it for others, so one file meant two things. Passing the header to the scrub alone (an earlier 5.2 build) made it worse: a loose scrub of `a\t1\nb\t2\na\t\n` kept line 1 as the header while reads took it as a row, and the deleted key `a` came back. A first fix that reused 3.39's containment check hid rows (`--x-header 'id\t'` matched any line starting `id`), and its scrub could move a row equal to H onto line 1, where the next read took it for the header.
- Original design was wrong because: "is line 1 the header?" was answered per command, each by its own library call with its own rule (a read never asked, a write asked by substring, a clear kept the file's line), instead of once for the file. Commands that rewrite a file must read it the way the reads do, and must not write a file whose line 1 the same rule reads differently.
- Lesson: a fact about a file that changes how every row reads (its header, its width, its defaults) is decided in one place that every operation goes through, from the file, never from which command is running, by an exact rule (a loose match turns data into structure). Test it by running every operation on the same file and checking that the rewrite keeps exactly what the reads showed, and that the rewritten file reads the same way under the rule again.

## 2026-11-04 TSVZ 5.3: readers lock, and the file lock is split into byte ranges
- Changed: the inter-process lock layout of `.tsv`-family files (and of `.tsvz` part writes). Readers take a shared
  lock on byte 0 while they read; appends lock byte 1; rewrites lock byte 1, then the whole file. On Linux these
  are open file description locks; on macOS and other POSIX systems every lock is a whole-file `flock` (shared for
  reads). `lockf` remains only where neither works, and then readers do not lock. 3.39 to 5.2 took one exclusive
  whole-file lock per write, and readers took none.
- Why we had to change: tsvzed lets two processes share one `.tsv` and rewrite it periodically. A reader that took
  no lock read another process's in-place rewrite part way, and a delete made in that moment was lost. On macOS
  `lockf` was dropped by any close of the file in the process and did not exclude threads.
- Original design was wrong because: it relied on the rewrite order (2026-10-29: "pass only through states a
  reader can live with"), which an in-place rewrite that moves a key cannot keep; and it used process-owned locks
  where the lockers were threads.
- Lesson: when processes share a file that is rewritten in place, readers take part in the lock from the first
  version. Treat the lock layout (who locks which range, and who owns the lock) as an API, with older versions'
  locks in its compatibility table. Old whole-file locks still exclude every new range, so the change needed no
  flag day.
- Amended (review, same day): Linux grants new shared locks while an exclusive one waits, so readers that overlap
  held a rewrite off for as long as they read, and the appends queued behind it. Reads now pass a gate (byte 2,
  shared, let go once byte 0 is held) that a rewrite takes after byte 1. Lesson: a reader/writer lock built from
  kernel locks needs a turnstile against writer starvation; check fairness before arguing that reads "drain".

## 2026-11-04 TSVZ 5.5: a legacy row is its own cells, and a compaction only drops lines
- Changed: readers of a .tsv-family file no longer cut a row wider than the file's column count (they pad a shorter
  row and drop only empty cells past the count), and writes (`TSVZed[k] =`, `appendTabularFile`, `tsvz set`) pad
  only. A scrub, and a TSVZed rewrite of a healthy file, compact by dropping lines (comments, superseded lines, dead
  keys' lines) through one function, `_legacyCompact`, and keep every other line byte for byte where it was: line 1,
  the `#_defaults_#` lines and the lines a strict reader takes included. The exceptions are named: a key's last line
  may move up into its first line's place, a dead key's line that sets the column count is kept emptied, and a
  header passed with verifyHeader=False is added as line 1. A TSVZed then appends what memory holds that the
  compacted file does not say (rows, deletions, and its defaults at the end when the file does not end with them). A TSVZed reads a one-column file's lone keys as rows, and a whole reload starts
  from the program's own defaults. 3.39 to 5.4 cut rows on every read and wrote back a re-laid-out file (rows padded
  or cut, defaults moved, resets made, line 1 rewritten, the caller's defaults baked into rows).
- Why we had to change: three review rounds of 5.2, and a first width design that still needed a synthetic width
  line, defaults placement rules and four strict-scrub refusals, kept finding cases where a scrub or rewrite changed
  what some reader saw, or erased cells: a differential fuzzer found 32,228 changed views and 341,655 lost cells in
  1.44 M cases of 5.2.
- Original design was wrong because: the read threw data away, and the compaction rebuilt the file from that read,
  so it had to re-create every layout fact readers depend on (the width, where each defaults line stands, line 1),
  and each one it missed changed a reader's view. The width was a fact of the layout that also decided what a row
  means.
- Lesson: a reader may pad or validate but never drop data; a compaction removes what no reader needs and changes
  nothing it keeps, so what every reader sees is kept by construction instead of by rules that rebuild it. Test a
  compaction differentially: old and new readers, both strictnesses and other arguments, on the same bytes before and
  after.
- Amended (review and the user's decisions, same day): a strict reader now takes a wider row whole and drops only a
  narrower one, since hiding a row a writer wrote whole is the data loss the read rule was meant to end; and
  `rewrite_on_load` is off by default (the three automatic rewrites are to go, for explicit `mapToFile()` and scrub
  calls). A rewrite whose cost is a second parse of the file, at every open, by default, made every program pay for
  compaction it never asked for, and an implicit rewrite is a write the caller cannot see or schedule. Lesson: a
  maintenance write (compaction) is something the caller asks for; defaults should not write.
- Amended (2026-11-05, two more review rounds and the user's decisions): `move_to_end` now appends the key's deletion
  line and its row; a one-column delete is a queue item that replay applies and the commit's compaction carries out;
  the memory dump is left only for a damaged, removed or emptied file, through the same rewrite path, and keeps the
  column count; a store following another process no longer rewrites at its next tick; `setDefaults()` may change a
  header-less store's column count. Both review rounds found every data-loss bug in the memory dump and in the state
  (`_unlined`, `_dumpNext`, `_dumpGen`, `_dumpTaken`, an implicit rewrite_on_exit) that existed because those two
  changes were not written when made. Lesson: a change that is not written when made needs state to remember it, and
  that state is where the bugs live. Say it in the form the file already has, or put it in the one queue every other
  write goes through, so reads and retries keep it with no extra part. And when a fuzzer's invariant keeps flagging
  what the design allows ("reopen == memory" against defaults the program changed), restate it as what the operator
  relies on (the rewrite changes nothing the same reader reads) before patching code to it.

## 2026-11-05 TSVZ 5.5: correctColumnNum= on a TSVZed no longer pads
- Changed: `TSVZed(correctColumnNum=N)` no longer makes `N` the store's column count: the count is the one the read
  takes (the header's, the defaults' of two cells or more, or the first keyed line's), rows are not padded to `N`
  or cut to it, and no rewrite writes them that way. `readTabularFile(correctColumnNum=N, strict=False)` still
  pads to `N`.
- Why we had to change: in 5.4 an `N` below the file's width cut rows in memory at the open and in the file at a rewrite in a loose store
  (`N=1` on `a\t1` held `a`, then wrote `a`; `N=2` on `a\t1\t2` wrote `a\t1`) and emptied the file in a strict one, which is data loss on a number
  the caller gave. An `N` above the width padded in a loose store (`N=5` wrote `a\t1` as `a\t1\t\t\t`) and, in a
  strict one, refused the rows (memory empty, the rewrite emptied the file). 5.5 takes the width from the file on
  every path, so that went with it.
- Original design was wrong because: a number the caller gives about the file's layout was trusted over the file,
  and the rewrite then made the file say it. The width is a fact of the layout, found by one rule from the file
  (see 5.2 and 5.5 above), so a second source for it is a way to disagree with the file.
- Lesson: an argument that restates a fact of the file the library can read for itself is a hint at best: read the
  file, and never let the hint rewrite it. A caller who wants padded rows pads them (or gives defaults with that many
  cells, counting the `#_defaults_#` key, or a header line 1 already holds), and the library's padding is the file's own
  rule.

## 2026-11-06 TSVZ 5.6: a reader works on its own copy of defaults
- Changed: `readTabularFile`, `readTSV`, `scrubTabularFile` and `scrubTSV` split text `defaults=` as `TSVZed` does, and read
  a copy, so the file's `#_defaults_#` lines no longer change the caller's list, of a `.tsv` or a `.tsvz`. A sequence keeps
  3.39's meaning (its first cell stands in the key's column).
- Why we had to change: the same call read differently the second time, with the file's defaults taken for the
  preamble; and `defaults='#_defaults_#\tP'` read one character per cell.
- Original design was wrong because: the read used its argument as its working state. `TSVZedLite` then came to rely on
  that to learn the file's defaults, and a `.tsvz` test pinned it as "updated in place", so an argument became an output
  nobody had designed.
- Lesson: an argument is an input: work on a copy, and give what the read found its own way out (a return value, an
  attribute). Before making two entry points agree by routing both through one normaliser, check what each form means
  to each: a shared helper here would have moved every list that does not begin with `#_defaults_#` one cell over.
