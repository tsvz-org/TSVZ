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

## 2026-11-06 TSVZ 5.6: rewrite(force=True) always writes
- Changed: `rewrite(force=True)` writes the file whatever `dirty` says, as `mapToFile()` does, and so do `rewrite_on_load`
  and `rewrite_on_exit` (not over damage in a compressed file that no row went after). `rewrite()` without `force` is
  unchanged: it follows another process's change.
- Why we had to change: a row changed in place does not set `dirty`, so `rewrite(force=True)` returned True, wrote nothing,
  and the change went with the store; `rewrite_on_load=True` left comment lines alone. Rewrites are moving to explicit
  calls only, and an explicit call must do what it says.
- Original design was wrong because: `force` in 3.39 meant "do not skip the sync", and the write inside stayed behind
  `dirty`, a flag that says what the store knows it changed, not what the caller asked for. It returned True all the
  same.
- Lesson: a flag named for doing something does it. `dirty` may schedule automatic work; it never gates a call that
  asked for the work. Check what an automatic path would now destroy: a forced write must not ride along into a path
  that never asked (a close over damage).

## 2026-11-06 TSVZ 5.6: a delete is the same act through every door
- Changed: `tsvz delete`, `pop` and `popitem`, a handler's, and `TSVZedLite`'s take a key's lines out of a one-column file
  (`_legacyDropKeys`, on `_legacyDropLines`, which `TSVZed`'s rewrite uses) instead of appending the lone key. Where the file
  has no keyed line nothing is written. `TSVZedLite` takes its column count from its load, and reads a lone row at its
  offset.
- Why we had to change: the lone key is a row there, so a delete exited 0, grew the file, and left the key (and on an
  empty file it created the key). 5.5 fixed `TSVZed` alone, so two APIs disagreed on one file.
- Original design was wrong because: "a delete is an appended line" was each writer's own rule. It is true only at two
  columns or more, and only a reader knows the width: a writer that appends blind cannot tell.
- Lesson: when one rule depends on a fact of the file, every writer asks the file, through one function. Fixing the one
  writer that a test found leaves the others saying the opposite. Ask the first lines, so the common case stays one append.

## 2026-11-06 TSVZ 5.6: a rewrite widens line 1 to the header the program gives
- Changed: a `TSVZed` rewrite of a healthy file whose line 1 is the beginning of the store's header writes the header as
  line 1 and pads the narrower rows and `#_defaults_#` lines to it; memory then reads the file again. 5.5 kept line 1
  always, so its rule that a rewrite of a healthy file only drops lines has this one named exception. Not for a one-column
  file holding rows with no value, which stays as it is with a warning.
- Why we had to change: a program that added a column got "Header mismatch" at every open and an IndexError on the new
  column, and had to edit line 1 by hand; 5.4 had widened it at a rewrite.
- Original design was wrong because: 5.4 wrote its header over whatever line 1 was, which lost a row, and 5.5 answered that
  by never touching line 1. Neither asked what line 1 was. The header the program gives is a fact about the program,
  and line 1 can be told apart from a row only by an exact comparison.
- Lesson: when a rule was added to stop one bad case, check what good case it stopped too, and separate them by an exact
  test rather than by forbidding the operation. A fact that changes how every row reads moves all at once: line 1 alone
  would have made strict readers drop the rows, and from one column it would have turned every lone key into a deletion.


## 2026-11-06 TSVZ 5.6: a delete reads the file under the lock it writes under
- Changed: the one-column delete (`_legacyDropKeys`) takes the rewrite lock first and reads the file through it, then
  writes in place; it has no retry loop, stamp or second look at the width. A row that must move up past a defaults line
  moves with the cells it read; a delete that would change what another row reads (a valued row moved above a
  `#_defaults_#` line) moves nothing. `TSVZedLite` changes its memory only once the file is written, and loads with the
  defaults it was constructed with.
- Why we had to change: the first 5.6 delete read the file, then asked for the lock, and checked a stamp to see whether
  another writer had come between; beside an appender at 200 Hz it failed at every try. It could also change another
  row's value by moving it across a defaults line, and left Lite's memory changed when the write failed.
- Original design was wrong because: the read and the write were two steps with a gap, and the code defended the gap.
  The gap is the cause; the defence added a stamp, a retry and an error path, each its own place to fail.
- Lesson: when a decision is made on a read and acted on with a write, read through the lock that the write holds. Check
  the fix by removing parts: if the retry, the stamp and the error path can go, the design was right.

## 2026-11-06 TSVZ 5.6: a strict store follows a widening header, and a clean store takes in what others wrote
- Changed: a strict `TSVZed` takes a line 1 that extends its header (the same leading cells, then named ones) for another
  program's widening, at open and whenever it follows the file, and its column count becomes the wider header's; it used
  to raise "Header mismatch" and stop following. Any other line 1 is refused as before. A rewrite of a store that has
  changed nothing takes in another process's change first, also with `monitor_external_changes=False`, so it no longer
  deletes a row another process appended; with `createIfNotExist=False` it makes no file where there is none.
  `errors=` is honoured when a widened file is read back.
- Why we had to change: a program that widened its header made every strict reader refuse the file for good, and a
  `monitor_external_changes=False` store deleted another process's row on its next clean rewrite.
- Original design was wrong because: "line 1 is not my header" was read as "the file is not mine". The flag that turns off
  following was also used as the guard against writing over unseen rows, which it was never meant to be.
- Lesson: when a check stops a good case that looks like the bad one, compare exactly (prefix cells, then named ones)
  instead of forbidding both. A flag that turns a check off must not turn off the guard against deleting what the program
  never held.

## 2026-11-06 TSVZ 5.6: a file is compacted at its own width, never the store's
- Changed: a `TSVZed` rewrite of a file of one column (its first keyed line has one cell) compacts it at column count 1, also
  when the store's defaults have two cells or more. `a\nb\nc\n` under `defaults=['#_defaults_#', 'P']` becomes
  `a\nb\nc\n#_defaults_#\tP\n`, not `a\n#_defaults_#\tP\n`. `TSVZedLite`'s delete at one column asks the file with its own
  width too (no defaults), so `setDefaults()` after the load does not make the delete append a lone key that is a row.
- Why we had to change: the compaction took the store's width from the defaults and carried out the lone keys as deletions
  there, with no output; a plain open read three keys before and one after, on every rewrite route. 5.5's README and
  limitations entry described the loss as accepted behaviour; the user chose to fix it in 5.6.
- Original design was wrong because: one number, the store's width, answered two questions: how the store reads the file and
  what the file's lines mean. Only the first can differ between readers; the second is the file's, and the compaction acts on
  it for every reader.
- Lesson: before a writer drops a line, ask which reader's meaning makes it droppable. If that reader is the store and not
  the file, the line stays. Not done: widening such a file to the defaults or to a wider header, because a value-less row at
  two columns is a deletion (limitations.md, "A file of lone keys does not widen").

## 2026-11-06 TSVZ 5.6: a rule that tells two lines apart is one function, exact
- Changed: a strict `TSVZed` that follows a line 1 widened by another program (the same cells as its header, then named
  ones) tells it from other lines with the rule the rewrite uses to write one (`_sameCells`: equal once the whitespace around
  each cell is taken off), not the containment of the header check; it reads the rows of such a file loose, padded, the file
  untouched, and pads a narrower `set` instead of refusing it. A strict store that never met a widening still refuses it.
- Why we had to change: the containment rule let `id\tvalue\tw` pass for a widening of `id\tv`; and the strict read of a
  widened file dropped every row narrower than the new count, so memory held `{}` and a rewrite appended a deletion line for
  each row of the file.
- Original design was wrong because: the same question (is this line 1 my header, widened?) was answered by two rules, the
  looser where the file was read and the exact where it was written. And a strict check built to drop a narrower line was
  applied to a file whose rows are narrower on purpose.
- Lesson: one question, one function. When a check lets a file through to the part that acts on it, the part that acts must
  not be able to disagree with it.

## 2026-11-06 TSVZ: a command that differs from another only in case is a script file, not an entry point
- Changed: `TSVZ` is no longer a console entry point of TSVZ: `setup.py` installs it as the script file `scripts/TSVZ` (`scripts=`), and the other spellings stay entry points. Every spelling is still a command and prints the same version.
- Why we had to change: pip 9.0.3, the pip of EL7 and EL8, reads `entry_points.txt` with a case-folding config parser. With `TSVZ` and `tsvz` in it, `pip3 install -U TSVZ` and `pip3 uninstall TSVZ` stopped with `configparser.DuplicateOptionError`, and the old release stayed installed. The upgrade from a release that has the collision still fails on pip 9 (it reads the old release's metadata): `pip3 install -U pip` once, then it works, and later upgrades need nothing.
- Original design was wrong because: it took a command name for a file name only, where a case-sensitive file system keeps `TSVZ` and `tsvz` apart, and not for the key of a config file that a different tool reads on uninstall.
- Lesson: no two commands in one `entry_points.txt` may differ only in case. Keep one spelling as the entry point and ship the other as a script file, and install and uninstall with the stock pip of the oldest platform the package supports before a release.

## 2026-11-06 TSVZ 5.6: a file's width is the file's, asked of one function
- Changed: `_legacyFileWidth` says the column count a file's own lines give (the header's when line 1 holds it, else 1 when line 1 is one cell, else the first keyed line's), with no reader's defaults in it. A rewrite's widening and compaction, a scrub and every delete ask it. `tsvz delete` and a handler given `--x-defaults` now take a key's lines out of a one-column file where they appended a line that read as a row, and write nothing in a file with no keyed line; a scrub keeps a one-column file's lone keys under wider defaults or a wider header. The read that takes a line 1 for a widening is the only place that says a store followed one, and every whole read says it again; what is appended and a rewrite's own look at its lines read as that read did.
- Why we had to change: three fix rounds each closed findings and opened others in the same logic. Under a wider header with wider defaults a rewrite dropped every lone key (the guard asked the store's defaults, which said two columns, so nothing looked lost), a one-cell line 1 with a valued first row lost the lone keys below it, a delete in an empty file wrote a row because no lines fell through to the defaults, and a flag set from the final width turned on for every header-less store.
- Original design was wrong because: a question about the file was put to the reader. `_legacyLinesWidth` took the reader's defaults, and each route then corrected for them in its own way: a guard, a second call with no defaults, an `if raw`, a width check on a flag. The answer changed with who asked, which is why each fix moved the failure to the next route.
- Lesson: keep "what does this reader make of the file" apart from "what does the file say", and let any write that can lose a line ask only the second. When a guard has to be told to ignore an argument, the argument should not have reached it.

## 2026-11-06 TSVZ 5.7: a daemon thread stays out of the streams at the exit, and a library says nothing unasked
- Changed: every print of the module passes one door (`_say`). `_stopLiveStores`, at exit, closes it to daemon threads once the stores are stopped and waits, up to 5 s, for a print under way; past it, a daemon thread's message goes to file descriptor 2 by `os.write`. "External changes detected" and "Time anomalies detected" print only with `verbose=True`.
- Why we had to change: the interpreter's end freezes a daemon thread where it stands. A writer thread frozen inside a print keeps the stream's lock, the last flush times out on it, and the program dies with `_enter_buffered_busy`, exit 134: 21 of 600 runs on 3.14 and 12 of 600 on 3.6 for a plain program beside an appender, with `verbose=False`.
- Original design was wrong because: it took a print for a thing that can happen anywhere. A daemon thread's print is a lock held across a system call, in a place the exit does not wait for. And a change seen was printed unconditionally, so a library talked on stdout of a program that asked for quiet. Gating on `verbose` alone would have left verbose stores exposed, and a check of `sys.is_finalizing()` before the print leaves the window between the check and the end of the print.
- Lesson: anything a daemon thread does at the moment of exit must be safe to be frozen in; a lock taken there is held for ever. Close the door at the exit, wait for those inside, with a bound, and let what comes after use a path with no lock. Put the door where every print passes, not at the one message that showed the bug.

## 2026-11-07 TSVZ 5.8: a delete keeps every line that sets a reader's count in its place
- Changed: in a loose one-column file, a delete that takes a key's lines out (`_legacyDropLines`: `tsvz delete`, `pop`, `popitem`, a handler, `TSVZedLite`, a `TSVZed` commit or rewrite) takes out only those lines. Where one of them set a reader's column count (the first keyed line, and under `--x-header H` with line 1 not H also the first keyed line after line 1, which a reader given H counts from) and the line that would come in its place has another cell count, the key of a later row that stays goes there with as many empty cells as the line had: a key whose line holds no value first, else one that no row with a value before the place names, never after a line 1 of one cell; a bare `#` on line 1 when the count stays. The CLI and a handler ask the width with the header given (`_ServeStore._dropKeys`, `_legacyDropKeys(xHeader=True)`). `_legacyMoved`, the move-up and the moved key's memory move are gone, and `_legacyDropLines` returns the lines alone.
- Why we had to change: 5.6 kept the count by moving the narrowest row up into the place. A moved row passes the key's earlier lines (dropped as superseded, which a strict reader that drops the later, narrower line still needed), the defaults lines and resets between (padded, dropped, or copied in 5.07's round), and lands on line 1, which a reader given a header skips. Each review round of 5.07 fixed the layout for one family of readers and found new shapes for the other; 5.07 shipped 5.6's delete. In the depth-4 sweep the stand-in leaves the command and 5.07's readers with no changed row (5.6: 320 cells), the 5.6, 5.5 and 3.39 readers with only the 5,235 gained rows that no layout avoids (5.6: 10,315), and readers given a header that line 1 isn't with 17,999 changed cells and nothing else (5.6: about 350,000 verdicts).
- Original design was wrong because: it kept the fact (the count) by moving a neighbour into its place, and a neighbour carries everything that depends on where it stands. Two families of readers count from two different lines, so a moved row is always in the wrong place for one of them.
- Lesson: when one fact that every reader takes from one place has to survive a removal, keep the place and change what it holds; moving a neighbour into it changes what every reader of the neighbour, and of the lines it passes, sees.

## 2026-11-08 TSVZ 5.09: a delete's commit is a delete, not a compaction
- Changed: a `TSVZed`'s commit of a one-column delete (`_snapshotForRewrite(commit=True)`) takes out only the deleted keys'
  lines, with their stand-ins, and compacts nothing: the bytes `tsvz delete`, a handler and `TSVZedLite` write. Comments,
  empty lines and superseded lines stay until a rewrite asked for or a scrub.
- Why we had to change: the commit rewrote through the general rewrite and so also compacted, at the store's own column
  count. In `id\tv\na\nb\t1\nb\t\t1\n`, a store given `id\tv\tw` reads one column from `a`; the compaction dropped `b\t1` as
  superseded there, and 3.39's strict reader given no header, which reads two columns from line 1 and takes only `b\t1`, lost
  `b` (2 + 2 cases at depth 3 of the delete sweep, 88 at depth 4). The other doors, which only drop lines, were right.
- Original design was wrong because: a delete in a one-column file has to be a rewrite, and the rewrite it borrowed brought
  its other job along. A compaction is a maintenance write the caller asks for (the 5.5 lesson); at a delete's commit
  nobody asked for one, and a compaction is right only for the reader whose count it is made at.
- Lesson: when one operation is carried out by another's machinery, it takes only its own effect from it; each of the
  machinery's other effects is a request of its own. A door that does more than the others is the one to check first.
- Reversed by 2026-11-08 TSVZ 5.10 (below): the commit compacts again, with a compaction right for every reader. The
  lesson stands only in part: a compaction was made right for one reader, and taking it out of the commit made files grow.

## 2026-11-08 TSVZ 5.10: a compaction keeps what every count the file is read at takes
- Changed: under a header that line 1 does not hold (line 1 a row to readers given no header, or another program's
  widening of the header), a compaction of a loose file (a `TSVZed` rewrite, a scrub, and again a one-column delete's
  commit) keeps every line that any of the readers takes: the caller's, which counts from the first keyed line after
  line 1, and the readers given no header and given line 1 for a header, which count from line 1 (a store that follows a
  widening is one of them); and, from the review rounds, a file read as one column also for the readers given defaults
  at their count (F1), and each of these readers again given no defaults, at the count it then reads at, where that
  count or its strictness differs (F3b, F9). Each reader's plan is made as before (`_legacyCompactPlan`); a line kept for one reader is
  kept as it is; a key's last line moves into its first line's place, or a dead key's line is emptied, only where every
  plan agrees; and a line kept for another reader brings along the deletion or the last line its own plan needs around
  it. A file with no header, or whose line 1 holds the header, compacts as in 5.9 but for the readers given none. A key's lines now also become one
  line when they are as wide as each other and each strict reader takes both or none of them (5.5 to 5.9: only when
  both strict readers took both). A `TSVZed` delete's commit compacts again, as in 5.8, now for every count.
- Why we had to change: three findings had one cause. W1: a store given `id\tv` on `id\tv\tw\nb\t1\nb\t\t1\nb\t1\n` (line 1
  widened by another program) reads three columns, as every reader given no header does, but its rewrite compacted at the
  count of the line after line 1 (two), where `b\t\t1` is superseded, and every strict reader given no header lost `b`
  (5.6 to 5.9). R2: a delete under a header whose stand-in sets the store's count to one, then a rewrite, the same (72
  tier-1 reader-cases in the store's doors, 396 after `tsvz delete`, at depth 4; 5.07 to 5.9). N2: 5.9 had cured the
  commit's loss of the same kind (Call 3 of the 5.08 design) by taking the compaction out of the commit, and a one-column
  file that a store updates and deletes in then kept every superseded line: 4,056 lines after 4,000 rounds, 80 s
  (quadratic), against 61 lines and 15 s in 5.8.
- Original design was wrong because: a compaction was made for one reader, the caller's, and `_legacyFileWidth` chose
  which count that was, though a file under a header line 1 does not hold is read at two counts by the programs that
  matter. 5.9's fix removed the step from the door where the loss showed instead of making the step right; the step was
  also what kept the file small.
  The first build of 5.10 made the same mistake once more inside one plan: it let a key's lines collapse when a strict
  reader at the plan's count took neither, and the fuzz found a reader at the defaults' count that took one of them
  (`\na\nc\tX\tX\tX\t2\nc\t1\n`, three-cell defaults: the plan read one column, the reader three, and lost `c`). The
  condition that holds at every count is that the two lines are as wide as each other.
  The review of 5.10 found the same mistake twice more, on the reader's arguments rather than its columns. F1: a file the
  one-column rule makes of one column was compacted for the readers at one column alone, though a reader given defaults of
  two cells or more reads it at their count (`a\nc\t1\nc\t5\t6\nc\t3\n`, defaults `[D, '', '']`: written as `a\nc\t1\nc\t3\n`, and `c` lost for every strict
  reader given them, 5.6 to 5.9 and 5.10's first builds). F3b: a strict reader given no defaults takes only the lines as wide as its count (3.39's)
  or at least as wide (5.5's), where the store's defaults, with a value in them, make every line taken (`id\na\t\nb\t\nb\t1\n`,
  `TSVZed(header='id', defaults=[D, 'P'])`, delete `a`: `b\t1` moved up into `b\t`'s place, and 3.39's strict reader lost `b`,
  in a rewrite in 5.4 to 5.9 and in a commit in 5.5 to 5.8; 5.10's first builds did the same). F9 (round 2): the reader given none was planned at the
  caller's count, which a store or scrub with no header takes from its defaults, where a reader given none reads at the
  file's own (`id\tv\tw\nb\t\t1\nb\t1\t2\nb\t1\n`, defaults `[D, 'P']`, a rewrite: `b` from `[b, 1, 2]` to `[b, '', 1]` for 13
  strict readers given none, 5.5 to 5.9 and 5.10's first builds). All three are readers of the compaction now: the defaults' count, and each
  reader again given none, at the count it then reads at.
- Lesson: when one file is read at more than one count (or by more than one rule), a maintenance step keeps what each of
  those readers takes, and changes a line only where all of them agree. A step found right for only one reader is made
  right for every reader, not taken out of the door where the loss showed: it may be what pays for something else. A
  condition checked at one count must be one that holds at every count a reader can take.

## 2026-11-09 TSVZ 5.11: a rule follows its premise to the doors where it holds, and an old reader is modelled as it is
- Changed: the stand-in rule's clause "no row with a value stands in after a line 1 of one cell" (Q3, 5.08) now applies
  only where the deleting program reads line 1 as a row (`tsvz --x-header H delete`, a handler); in a `TSVZed` or a
  `TSVZedLite(strict=False)` given H, a row with a value stands in (the user's answer to Q-A, 2026-11-08). The compaction
  models 3.39's defaults cell by cell (a `#_defaults_#` line cut to the count, filled from the defaults before it), where it
  kept one "strict or not" flag; it plans a `correctColumnNum` the caller gave as one more reader; a key's lines collapse
  when each strict reader takes the moved line where it read the key before; and a widening is refused when a key's only
  line 3.39's strict reader takes holds no value (Q-C).
- Why we had to change: in the store's doors the clause protected 3.39's strict reader at the cost of the deleting store's
  own view (`b` read `[b, 1, Q]` after the delete, and the store's next widening wrote it for everyone, but only in one
  order). The flag kept 3.39's strict reader lenient after a defaults line that made it strict again, and a scrub changed a
  cell for it. The count a caller gave was not planned, and the key order changed for the caller's own readers.
- Original design was wrong because: Q3 was decided for the command's door, where the deleting program reads line 1 as a
  row; the premise is false in a store given H, which counts from the line after line 1, and the rule was applied in every
  door all the same. The flag stood for a list of per-column values, and two states of the list that differ for the reader
  were one state of the flag. The plan listed the readers it could infer from the file and the header, not the one the
  caller named.
- Lesson: a rule bought with one reader's cost holds only where its premise holds; when the premise is about who the
  deleting program is, it is decided per door, from that door's own reading. Model an old reader's state as the reader
  keeps it (a list is not a flag), and plan every reader the caller names, not only those the file implies.

## 2026-11-09 TSVZ 5.12: a choice ranked by one invariant is checked against every invariant, and a reader joins a plan only where it can differ
- Changed: the stand-in for a deleted line that set a reader's count tries line 1's own key first (when its next line
  comes first after the place and is as wide as line 1), where 5.10 and 5.11 tried it last. The compaction plans the
  reader given no header and the caller's defaults at the defaults' count after a line 1 of one cell, under a header line
  1 does not hold, except where it reads every line as the caller's reader does (at the caller's count: a lone key on
  line 1; a bare `#_defaults_#` where no line with a value has another width, or the defaults hold none).
- Why we had to change: the candidates tried before line 1's key come first to a reader that counts from the place, so
  the key order changed for a reopened store and every loose reader given the header (`d\tX\nc\nd\t1\na\t1\n`, delete `c`
  under `d\tX\tz`: `a, d` where memory lists `d, a`), and where the stand-in key's later line is wider than the header,
  5.11's refused widening (Q-C) made that last. The reader given no
  header and the defaults was planned at line 1's one column only, so a line it takes could go: a cell changed for it, and
  after a bare `#_defaults_#` on line 1 a strict one lost a key.
- Original design was wrong because: the candidates were ranked by what each keeps of rows and cells; line 1's key, which
  keeps the order as well (for every reader but 3.39's strict one given a count of one, which gains its row, as the widened
  3.39 entry in docs/limitations.md says), was added later (5.10) and put after the others without asking whether it was safer than they
  are. The deferral's plan moved it before only one of them, and the other reordered the same way. The one-column rule's
  reader stood for "the reader given no header and the defaults", which reads at their count: one entry in the plan stood
  for two readers. The first build of the fix listed it also where it reads as the caller's reader does, and the copy was
  not free: the plans' agreement kept lines no reader needed (14 larger files in 36,000 fuzzed scrubs and rewrites) and
  took about a fifth more time.
- Lesson: rank candidates by every invariant a reader relies on (rows, cells and order); one that keeps them all goes
  first, even when it came later. Plan each reader at the count it reads at, and add a reader to a plan only where an exact
  test says it can read otherwise: a copy of one already planned costs time and keeps lines.

## 2026-11-09 TSVZ 5.13: shutdownEvent is a flag that holds no lock, and no lock another thread waits for is left to a Ctrl-C
- Changed: `TSVZed.shutdownEvent` is a `_Flag`, not a `threading.Event`: the same `is_set()` (and `isSet()`), `set()`,
  `clear()` and `wait()`, with the flag an attribute and each waiter asleep on a lock of its own that `set()` lets go of.
  The store's other signals (a job's done, `_wake`, `_workerDone`) are `_Flag`s too, and the serve writer (a
  `TSVZClient` without a handler, the CLI's one-shot commands, `tsvz serve`) hands its jobs over through a deque and a
  `_Flag` each, where it had one `Condition`. `_memLock` is an RLock that a thread lets go of when it finds itself
  holding it at the start of a section or before it waits for the writer thread. A `.tsvz` store's
  `commitAppendToFile()`, `clear()` and `reload()` run on its writer thread, as a loose store's always did, so no other
  thread takes `writeLock` while the store runs. `_pathLock()` lost its guard lock. 5.07's turning of 3.6's "release
  unlocked lock" `RuntimeError` back into a Ctrl-C is gone: nothing raises it now. The review of the first build added
  three changes: `_SectionLock`'s exit ignores a hold a signal handler already let go of; the 11 sections a main thread
  changes memory in are wrapped in `try`/`except BaseException: letGo(); raise`, so a caught Ctrl-C leaves no hold; and
  the exit lets go of a `.tsvz` store with nothing to write, as it did of a loose one (its commit notes its write).
- Why we had to change: after a Ctrl-C a program could hang at exit, its write done or not, until a second Ctrl-C
  (which abandons what is still queued). 5.12's suite showed it once: the Ctrl-C test hung about 1 run in 250 on 3.6.
  Looking for it found the common case: a Ctrl-C that ended a loop of `store[k] = v` hung the exit in 70 of 960 runs on
  3.14 and 4 of 960 on 3.6, and one that ended a loop of `.tsvz` writes and commits in 27 of 960 on 3.14. A program
  that caught the Ctrl-C and went on hung at its next commit, and a `TSVZClient`'s at its exit.
- Original design was wrong because: it took for granted that a `with` statement always lets go of its lock. In
  CPython a KeyboardInterrupt can be raised just after the lock is taken and before the block is set up (3.14), or
  after the block and before its exit runs (3.6; bpo-29988), and inside `threading.Event` and `Condition`, whose own
  code holds their lock across Python code. The store's threads were built so that the writer thread waits for locks
  that the main thread takes (the Events' and the Condition's, `_memLock`, a `.tsvz` store's `writeLock`) while the
  main thread waits for the writer. 5.07 met the symptom on 3.6 (`RuntimeError: release unlocked lock` out of
  `Condition.wait`) and turned it back into a Ctrl-C without asking what else a Ctrl-C at that point leaves behind.
  The first 5.13 build then took two more things for granted: that a thread which finds a hold at the start of a
  section is a Ctrl-C's (a signal handler that uses the store runs inside the section it interrupted, lets go of
  that section's hold, and the section's exit then raised: a commit or set handler died in 40 of 40 runs, a `SIGTERM`
  handler that closed and exited ended with status 1 in 14 of 40; 5.12 hung in the same cases), and that moving the
  `.tsvz` load to the writer thread cost the exit nothing (it let go of a store with nothing to write only for loose
  stores, so one Ctrl-C during a load waited for the whole load: 6.45 s on a 1.5M-row store, where 5.12 ended in 0.17 s).
  A third: letting go only at the start of the next section left a caught Ctrl-C's hold in place until the program next
  used the store (15 to 17 of 320 write loops on 3.6, 2 to 12 on 3.14), with other threads' sets and a loose store's
  appends held off.
- Lesson: in Python, a Ctrl-C can land between almost any two bytecodes of the main thread, even inside threading's
  own code, and skip a `with` statement's exit; never let another thread wait for a lock the main thread holds across
  Python code. Signal with a flag and a lock per waiter that only `set()` lets go of; hand writes to the thread that owns
  the file; where a lock must be shared, use one that knows its holder, and let the holder drop a hold it finds itself in
  where it can be in no section; let the method a Ctrl-C goes up through drop it too, and let the section's exit accept
  that a signal handler inside it dropped it first (a check of the hold before the release is not enough: the handler
  lands between the check and the release). Work moved to a thread that the exit waits for makes the exit wait for it:
  the exit has to tell a read, which it can leave, from a write, which it finishes. Find such places by raising the
  interrupt at every step of the code in turn, and test the operator's cases too (a Ctrl-C that ends a loop of writes, caught and not; one during a big load; a signal handler that
  uses the store), where the rates are highest. Sharpened in 5.15, when the step sweep went into the suite: step as a
  real signal can land. On 3.12+ that is `sys.monitoring`'s instruction events, which start at a frame's first
  instruction, where settrace's opcode events start late. Up to 3.11 it is settrace's `opcode` events, and only those:
  on 3.9 an opcode event right after a `line` event overwrote the line event's raise, the interrupt ran on, and the next
  C call failed with `SystemError` (CI's 3.9 job, red since 5.13). On 3.11+ the steps the exception table leaves out,
  `NOT_TAKEN` and `NOP`, are skipped: no signal is raised there, and a raise there skipped both the `with` statement's
  exit and the `except` clause, holds a real Ctrl-C never makes. And the sweep keeps no exception object: its traceback
  kept the cut write's open file, whose lock the next write then waited for, a hang no real program meets.

## 2026-11-10 TSVZ 5.15: a job whose waiter left is taken back if it has not begun, and a load stops
- Changed: when a Ctrl-C ends a thread's wait for the store's writer thread, a job the writer thread has not begun
  is taken back (it never runs; the rows it would have written stay queued for the next commit), a load under way
  stops at its next read and changes nothing, and a write under way still runs to its end. At exit nothing is taken
  back. A loose load stops because the waiter swaps the file its reader reads for a stand-in whose every read stops
  it; a `.tsvz` load checks a flag once a 1 MiB chunk. A `.tsvz` `clear()` empties memory inside its job, as it
  lists the queue, where it emptied memory in the calling thread before handing over the file's clear.
- Why we had to change: 5.13's rule was "a KeyboardInterrupt stops the wait, never the write". A `clear()` whose wait
  a Ctrl-C ended ran later all the same, after the rows the program set once it caught the Ctrl-C, and took them out
  of the file (a loose store's memory too; a `.tsvz` store's memory kept them): 63 of 199 steps of a loose `clear()`
  and 153 of 219 of a `.tsvz` one, on 3.14, left the program's next rows apart or gone. And a load nobody waited for
  any more kept the writer thread reading, sharing the interpreter with the main thread's shutdown: a Ctrl-C during a
  1.5M-row load took up to a second or two to end the program.
- Original design was wrong because: it treated two rules as one. "Do not cut a write part way" is about the file:
  a write begun is finished. "Do the write the caller gave up on" is a promise nobody asked for: the caller has
  already gone on, and anything it does next comes before the job, which then runs out of order. And a read whose
  only product is a copy for that caller is work for nobody once the caller left.
- Lesson: when a caller stops waiting, finish only what has begun; take back what has not, and stop work whose only
  product was for that caller (a read into a copy that is swapped in at its end). Stop it from the caller's side, by
  swapping what the work reads, not by a test in its inner loop, which every run pays for.

## 2026-11-10 TSVZ 5.15: a forked child's copy of a store is reset at the fork, by a hook
- Changed: in a child that `fork()` made, each open `TSVZed` drops the rows its parent had queued and gets new locks,
  in an at-fork hook that runs before any of the child's code (`os.register_at_fork` on 3.7+, a wrapper of
  `threading._after_fork` on 3.6). The child's `close()` and normal exit now write the rows it set, except where its
  parent held the file through `get_file_obj()` at the fork, or (3.6) no hook ran; there they write nothing and say so.
- Why we had to change: the child's commit wrote the parent's queued rows too, so they were written twice, and an
  older row could land after the parent's newer one, winning in the file while the parent's memory held the newer.
  Its close and exit wrote nothing at all. And a lock a parent's thread held at the fork (the writer thread holds
  `writeLock` through each commit's write and fsync) stays held in the child, where no thread will let go of it: a
  close or exit that writes would wait for ever on it.
- Original design was wrong because: it treated the child's copy as the store. The queue and the locks belong to the
  parent's threads, which go on in the parent and are not in the child. The first plan for the fix checked the pid at
  every write; that costs a system call per set on every program, for the rare one that forks, and still cannot free
  a lock a dead thread holds.
- Lesson: state a forked child copies from a parent that still owns it (queued work, locks, threads) is reset once,
  before the child's code runs, by the platform's at-fork hook, not by a check at every call. Where the child cannot
  know whether the parent waits for it (a parent holding the file while it waits for the child), it does nothing
  that could wait for the parent, and says what it left undone.
- Missed at first (the 5.15 final review): the mark of a rewrite under way (`_damaged`, set from the write's start until
  the file is on disk). A child forked inside a parent's rewrite took it, and its close wrote its fork-time memory over
  the parent's later rows, as memory "the only good copy". The file's state is the parent's too: list every field a
  write in progress sets, not only the queue and the locks.

## 2026-11-11 TSVZ 5.16: a forked child's store follows the file, whatever monitor_external_changes says
- Changed: in a child that `fork()` made, each `TSVZed`'s `monitor_external_changes` is `True` from the fork on (the
  fork hook, `_afterFork`), whatever the program gave: the child reads the file before a rewrite of its own (a
  one-column delete's or move's commit, `rewrite_on_exit`, a due `rewrite_interval`) and follows what the parent wrote.
  The parent keeps the value it was given.
- Why we had to change: 5.15 made a forked child's `close()` and normal exit write its rows. With monitoring off, a
  one-column delete or move commits by a rewrite that does not read the file first, so a child that deleted or moved a
  key wrote its memory as it was at the fork over the rows its parent set after it: the file ended with `a` alone where
  5.14 kept `a, b, p2` (5.14 lost them only at the child's explicit commit). 5.15 was worse than 5.14 there, found by
  the design check after its release. A child of a store dirty at the fork, with `rewrite_on_exit` or a due
  `rewrite_interval`, did the same at its exit (5.14 too).
- Original design was wrong because: `monitor_external_changes=False` says "no other process writes this file", and a
  fork makes that false: the parent goes on writing it. 5.15's hook reset what the parent's threads own (the queue, the
  locks, a rewrite under way) but kept a setting whose premise the fork broke, and the review rounds found the fork
  state one field at a time. A setting that assumes one writer belongs to the process that gave it, not to the object
  a fork copies.
- Lesson: at a fork, also reset in the child's copy every setting that assumes its process is the file's only writer
  (no monitoring, a rewrite at exit that does not read first): the parent is now another writer. Say so where the
  setting is documented, since the attribute reads otherwise in the child.

## 2026-11-11 TSVZ 5.17: a forked child keeps what it knows of the file and drops what it was doing
- Changed: in a child that `fork()` made, each `TSVZed` now also reads `rewrite_on_exit` `False` and `rewrite_interval`
  `0` from the fork on (as `monitor_external_changes` reads `True` since 5.16), and drops the parent's failed writes to
  try again, their reports and a refused rewrite's back-off. A child's close and exit only append, so a forked child
  no longer compacts the file at its exit unless it sets `rewrite_on_exit` again (the user accepted that cost, picking
  "Take H2 (Recommended)"). A child's own child learns of an ancestor's `get_file_obj()` hold too, and its warning names
  "an ancestor".
- Why we had to change: a child forked after its parent's rewrite failed part way took that rewrite to try again (and
  `rewrite_on_exit`): its close made the file again from its memory, and the parent, still damaged, took that for a
  change it could not merge and wrote over it, losing the child's row (the 5.15 re-review). Each of 5.15's review rounds
  had found one more field the fork hook should reset (`_damaged`, `dirty`, the monitoring setting); the design check
  found the next ones (`_failures`, the rewrite settings) and the grandchild's lost hold.
- Original design was wrong because: the hook reset a list of fields, one per finding, instead of applying a rule; and
  it copied the parent's hold into the child's "parent held" flag only, which the child's own hook then read as its
  own hold, so a grandchild saw none.
- Lesson: decide what a forked copy inherits by a rule, not a list: it keeps what it knows of the shared resource (its
  rows, what it last read of the file, the widths), and drops what the original was doing to it (its queue, its jobs, a
  write under way, failed writes to try again) and every setting that assumes one writer. Carry an ancestor's hold down
  every generation, not only to the first.

## 2026-11-11 TSVZ 5.17: a call made inside a store's own write, on the same thread, runs once that write ends
- Changed: a store with no writer thread (after `close()`, in a forked child) runs one job at a time in the calling
  thread (`_runHere`); a call that a signal handler (or a teeLogger) makes to that store inside such a job, on the same
  thread, is put off until the job ends and returns at once (`self`; `True` for `rewrite()`), where it used to wait for
  ever. A `move_to_end()` that has to follow the file first is put off whole, follow and move. The put-off calls are a
  local of the job's own frame (`_runJob`), not an attribute of the store, and the frame keeps the store's fork mark
  from when the job began (the 5.17 final review: the build had kept them on the store). They are drained from
  `_runHere`, once that frame is gone, only in the process the job began in, and not after a Ctrl-C (the 5.17
  re-review).
- Why we had to change: the job holds `writeLock`, a plain lock, through its write; the handler's commit, `reload()`,
  `clear()` or `rewrite()`, and a move that follows the file, took it again on the same thread and waited for ever
  (5.14 to 5.16: an approved known limitation for forked children and a deferral for closed stores).
- Original design was wrong because: the writer thread gives a store one job at a time, and a store without it ran its
  jobs as plain calls, so a call landing inside one ran inside it: on its lock, a wait for ever; between its locked
  steps, in the middle of its work, which the writer thread never allows. The build then kept the calls it put off on
  the store, and took the stack for exact: the final review found that another thread's call drained them, in the
  middle of the first thread's job (a move ahead of the follow it came after, which undid another writer's change), and
  that a process forked inside the job inherits the stack, so its own commit was taken for a nested call and put off for
  ever. The first fix drained inside the frame, and a call landing after the frame's last look was lost; the next
  drained from `_runHere`, and the re-review found two more holes: a process forked inside the job that returns from the
  handler drains its copy of the parent's line, and makes the parent's calls again (a put-off `clear()` emptied the file
  of what the parent wrote after it), and a Ctrl-C that cut the job just after it took the file lock ended it with the
  traceback still holding the locked file, so the put-off commit made while the Ctrl-C went up waited for ever for that
  lock.
- Lesson: code that serialises work by handing it to a thread must serialise it the same way when it runs the work
  itself. To tell a nested call (a signal handler, a callback) from a new one, ask the call stack: a "busy" flag or a
  lock's owner can be left set by a Ctrl-C, and would then put off every later call with nothing left to make it. The
  stack answers for this thread, and it is the whole story only while the process is the one the job began in: a forked
  process inherits it, so the frame also keeps a mark of the process (the store's fork mark) and the lookup compares
  it, and so does the drain. The mark is the fork hook's: an object the hook does not reach (a closed store) keeps the
  hole, and needs the hook. What the nested call leaves for later belongs to the frame that is waiting for it, not to
  the shared object: kept there, any thread can take it, and a frame that is gone takes it with it. The frame that
  holds it must not be the one that drains it, since a call landing after the frame's last look is lost: drain from
  the caller once the frame is gone. A resource is under its clean-up from the step that takes it: the write took its
  locked file, then ran one more check of the store's own outside the `try` that lets go of it, and its clean-up called
  `_unlockFile` before it closed the file, so a `SystemExit` (`sys.exit()`) or a Ctrl-C landing in either (a real signal
  is taken at a function's entry, as `_unattended()` is called, and at a call) left the file open and locked for as long
  as the exception's traceback lived, and the drain's commit, or a program's own `except SystemExit:` clause that commits
  again, waited for it for ever (the 5.17 round 2 review's N1; the README's handler hung in about 2% of real signals, and
  5.16 hung in a program's clause the same way). Round 2 fixed it where it showed, at the drain, for a Ctrl-C only
  (R1), and the same hang came back with `SystemExit`: the third round's design check found the leak older than 5.17 and
  removed it at its source: the `try` begins where the file is taken, and the file is closed whatever `_unlockFile` did
  (closing the descriptor drops its lock). Put-off work is still dropped after a Ctrl-C, not made in that `finally`: a
  cut can leave `writeLock` held (on Python 3.14, a signal between `with`'s enter and its block), and without the drop a
  Ctrl-C at 5 to 9 steps of a put-off call's sweep hung again. The plan named "a writeLock that knows its holder"; the
  lock is held only through each step of a job, so its owner says nothing between the steps, a holder a Ctrl-C left would
  read as a job under way, and the design check made `writeLock` an `RLock` and swept 5.16 with it: 619 of 1,060 steps
  still hung (the nested commit opens the file again, and waits for the file lock its own write holds). What is left is
  the approved limitation of a lock a cut write leaves held (Python 3.14: 0.4 to 0.6% of real signals), for which a lock
  that knows its holder, letting go of a hold found where the stack shows no job, is a candidate for 5.18, to be measured.
