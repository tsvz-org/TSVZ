# Limitations

Approved known limitations and deferrals. Reviewers: read this first, and don't raise these again.

### Some writers and readers still meet another process's rewrite part way
- Kind: known limitation
- What the operator sees: from 5.3 a reader of a `.tsv`-family file (`readTabularFile`, `tsvz get` and `read`, a
  `TSVZed`'s load, `reload()` and background read, `TSVZedLite`'s load, a `tsvz serve` handler) waits while
  another process rewrites the file under its lock, so a `del` of a key in that moment is no longer lost. It still
  can be in these cases:
  - the other writer empties the file before it takes the lock, or takes no lock: 3.39's and 4.9's rewrite of a
    compressed file and their `clear()` (`get_file_obj('wb')`), `scrubTabularFile`, `clearTabularFile`,
    `tsvz scrub` and `tsvz clear` from 3.39 to 5.0, `TSVZedLite` before 5.1, and other tools. A 5.3 reader can meet
    such a file empty (an empty store) or half written (a compressed one then usually reads as damage and is read
    again under the lock; a plain one does not);
  - the reader is TSVZ 5.2 or older, or tsvz-rs: it takes no lock and can see a 5.3 rewrite part way, as before.
    Restarting the program on 5.3 ends it;
  - where only `lockf` works (on Linux, a kernel or file system that refuses open file description locks: a kernel
    before 3.15, or a RHEL/CentOS 7 kernel older than 3.10.0-704, i.e. 7.0 to 7.4's own: the backport is in
    3.10.0-704, and 7.9's 3.10.0-1160 has them, checked on CentOS 7.9; elsewhere, a file
    system that refuses `flock`), readers take no lock, because a shared `lockf` would replace the process's own
    write lock; there a delete can still be lost, a read in another thread of the process can still drop a write's
    lock, and two threads' writes do not exclude each other (a new file's header can be written twice), as in 5.2;
  - Windows is unchanged: readers take no lock.
- Why: TSVZ cannot change older programs or other tools, and a writer that empties the file first or ignores the
  lock gives a reader nothing to wait on. On `lockf`-only hosts `lockf` is kept so that 3.39 to 5.2 writers stay
  excluded; `flock` would not exclude them on Linux.
- Revisit when: an operator loses a delete with only 5.3 or later programs on a file, still runs 3.39 to 5.0
  writers beside 5.3, or runs on hosts without open file description locks.
- Approved: 2026-11-02, the user chose "One known limitation (Recommended)" for older writers that empty the file
  before or without the lock, readers from 5.1 and earlier, and Python 3.6 forks, and "Keep lockf (Recommended)"
  for Linux without open file description locks; 2026-11-04, "Accept the wider entry (Recommended)" for this
  wording (readers from 5.2 and earlier, `lockf`-only hosts and Windows folded in); 2026-11-04, "Correct the wording
  (Recommended)" for the RHEL/CentOS 7 kernel range found on the EL7 test VM. Narrowed in 5.15: a Python 3.6 child
  that `fork()` made drops the locks it inherits too, since TSVZ's fork hook runs there (the user chose "Wrap it on
  3.6 (Recommended)", 2026-11-10).

### A running TSVZed of 3.39 to 5.2 cuts rows to its header's count when it reads its file again
- Kind: known limitation
- What the operator sees: a program on TSVZ 3.39 to 5.2 keeps a `TSVZed` open on a `.tsv`-family file whose line 1 is not
  the header it was given (it prints "Header mismatch" at the open), and the file holds a row wider than that header:
  `a\na\t\t1\nb\nb\n` under `id\tv`. Its first load shows the row whole (`[a, '', 1]`). When it reads the file again it
  cuts the row to the header's count (`[a, '']`), and its next rewrite writes the cut row, so the value is gone from
  the file. It reads again at its own `reload()` and whenever it follows a change of the file by another process: a
  rewrite or a scrub by any version (4.9 to 5.2), and also an append or a new modification time alone (3.39, 4.1). A
  scrub or rewrite by 5.14 is such a change as one by 5.13 is; because 5.14's compaction drops lines in more files, it
  writes where 5.13 wrote nothing in a few more (44 of 72,000 scrubs in the 5.14 final review's run), and in each the
  running store read 5.14's bytes exactly as it re-read the file before the scrub. A fresh open reads the row whole,
  before and after. 5.4 and later read the file again as they first read it.
- Why: TSVZ cannot change older programs: their reload starts from the column count their last read ended with, where
  their first load takes it from the file. Any write to the file sets it off, so no writer can avoid it.
- Revisit when: someone runs a 3.39 to 5.2 store beside newer writers on a file whose line 1 is not its header.
  Upgrading that program to 5.4 or later ends it.
- Approved: 2026-11-10, the user chose "KL, release 5.14 (Recommended)". The user did not ask for it explicitly; the 5.14
  final review found it.

### Reads by 5.3 programs can hold off a 3.39 to 5.2 writer
- Kind: known limitation
- What the operator sees: a rewrite or an append of a `.tsv`-family file by a 3.39 to 5.2 program takes a lock on
  the whole file, so it waits for the 5.3 reads in flight. 5.3 rewrites pass a gate that stops new reads while
  they wait; the older writers do not know the gate, so reads by 5.3 programs that overlap without a pause
  (several processes reading the file in a loop) hold such a write off for as long as they go on. In a test with
  three 5.3 readers looping, a 5.2 rewrite waited 24 s of a 25 s run and a 5.2 append 23 s. 5.2 readers took no
  lock, so 5.2 writers never waited for reads before.
- Why: TSVZ cannot change older programs, and their whole-file lock is what keeps them excluded from 5.3 writes.
- Revisit when: an operator runs a 3.39 to 5.2 writer beside busy 5.3 readers and cannot upgrade it. Running the
  writer on 5.3 ends it.
- Approved: 2026-11-04, the user chose "Known limitation (Recommended)".

### On macOS and other flock systems, overlapping reads can hold off a rewrite
- Kind: known limitation
- What the operator sees: outside Linux, TSVZ locks with `flock`, which has no byte ranges and so no gate. Reads
  of a `.tsv`-family file that overlap without a pause (several processes reading it in a loop) can hold a rewrite
  off for as long as they go on, and the appends that wait behind the rewrite with it: 18.8 s of a 20 s test run.
  On Linux the gate stops new reads while a rewrite waits.
- Why: a gate needs a second lock on the same file that a reader holds only briefly; `flock` gives one lock per
  open file, and a separate lock file would add a file beside every store.
- Revisit when: an operator on macOS or BSD sees rewrites stall behind readers.
- Approved: 2026-11-04, the user chose "Known limitation (Recommended)".

### A thread holding get_file_obj() waits for ever on another store of the same file
- Kind: known limitation
- What the operator sees: a thread that holds `get_file_obj()` of a `.tsv`-family store and then waits on another
  TSVZ store of the same file in the same process (its `reload()`, `close()`, a write, or its constructor's load)
  hangs: that store's writer thread waits for the lock this thread holds, which is let go of only at
  `release_file_obj()`. 5.2 hung the same way when the other store's job was a write; from 5.3 its reads wait too
  (on Linux only for the rewrite lock: a `get_file_obj()` in an `a` mode holds the append lock, which reads do not
  wait for). A `readTabularFile` of the file in the holding thread itself does not wait.
- Why: the lock is held for the caller across calls, which TSVZ cannot see into; telling another store's thread
  that this thread's lock is its own would need a lock table across stores, the kind of in-process gate that 4.9
  removed after its hangs.
- Revisit when: someone holds `get_file_obj()` while working through a second store of the same file.
- Approved: 2026-11-04, the user chose "Known limitation (Recommended)".

### A second Ctrl-C at exit abandons a write in progress
- Kind: known limitation
- What the operator sees: a program that exits while a `TSVZed` rewrites a `.tsv`-family file waits for the write;
  a Ctrl-C then prints "TSVZ: the write to X is being finished; Ctrl-C again to abandon it". A second Ctrl-C ends
  the program at once and cuts the rewrite where it is: a plain file is left with the rows not yet rewritten
  padded or duplicated, a compressed file can be left unreadable, and memory, the good copy, goes with the
  process. A `kill -9` or a power cut during any in-place rewrite does the same (spec §19.8: an in-place rewrite
  is not crash-atomic).
- Why: an operator who presses Ctrl-C twice wants out now, and an exit that refuses it blocks a clear intent.
  Writing a temporary file and renaming it over the store would survive it, but was rejected for the reasons in
  f329717 (mode, owner, links and other processes' writes lost).
- Revisit when: someone loses a store this way.
- Approved: 2026-10-24, the user chose "One writer thread (Recommended)", whose description said "At exit the
  program finishes a write in progress, and a second Ctrl-C abandons it."

### A line can be written twice, by a signal handler that uses a store inside a set, delete or move, or by a commit a Ctrl-C cuts
- Kind: known limitation
- What the operator sees:
  - A program's signal handler uses a store (a commit, `reload()`, `clear()`, a rewrite, a set, a delete or a
    `move_to_end`) while its main thread sets, deletes or moves rows in it. When the signal lands inside a set, delete
    or move, its rest runs without the store's lock (5.12 waited for ever there), and the writer thread may read the
    file into memory or clear it meanwhile. From 5.15 a set or delete then makes its change again under the lock, so
    memory and the file agree, and queues its line again unless that line is still the last one queued. A
    `move_to_end` makes its move again with the rows and in the order memory holds then, and queues its lines again
    unless the move's own are still the last ones queued and are the lines memory makes then. So a line is written
    twice where the handler wrote or queued something after the section's, on `.tsvz` stores too. With the handler
    run at each step (the suite's sweep and the 5.15 final review's, at its fixes, Python 3.14; 3.6's line steps in
    brackets; ranges over the fixes' runs and the 5.15 re-review's):
    - a set, delete, pop, popitem or defaults change: 35 (a `reload()`) to 47 (a commit, a set or delete of another
      key, a sync with another process's line, a set of the same key) per 1,000 landings (68 to 107); a `rewrite()` of
      a `.tsv` store, 174 per 1,000 (180): 33 of 325 landings of a set, 78 to 84 of about 250 of a delete, pop or
      popitem, whose deletion line goes again after the rewrite took the key out;
    - `move_to_end(key)`: a flush, a `reload()`, a sync, or a set or delete of another key, 10 to 16 of 524 landings,
      about 30 per 1,000 (4 to 7 of 81); a set of the moved key itself, 333 of 524 (55 of 81); a `rewrite()`, none;
    - `move_to_end(key, last=False)`, which sends every other key to the end: a flush or a `reload()`, 9 to 16 of 824
      (5 to 7 of 120); a `rewrite()`, none; a set or delete of another key, or another process's line followed, 606 to
      616 of 824 (91 to 92 of 120): every other key's lines are written again, as their order must follow the
      handler's change, so the file can double until its next rewrite.

    5.14 wrote none twice there, but left memory and the file apart in 3,650 of 33,054 landings. A `clear()` writes no
    row twice inside a set, delete, pop or popitem; it keeps the defaults, so a `#_defaults_#` line or a `.tsvz` marker
    it wrote or kept can be (20 of 1,028 landings of the defaults changes on 3.14, 8 of 194 on 3.6). Inside a move it
    writes the move's deletion lines twice (159 of 524 and 284 of 824 landings on 3.14, 20 of 81 and 31 of 120 on
    3.6): the move's lines can come after the clear, and the redo deletes the keys again.
  - A commit after `close()` or in a forked child that a Ctrl-C cuts just after its write writes its rows again at the
    next commit: 195 of 1,037 steps of such a `.tsvz` commit on 3.14 (from 5.15, which keeps them queued until they
    are on disk), 170 of 1,175 of a loose one (as in 5.14).
  - A handler's `sys.exit()` inside such a commit does the same (widened 2026-11-12; 5.17). The handler sets `h` and
    commits as the write's bytes are on disk and the queue is not popped yet: the commit is put off and made as the
    `SystemExit` goes up, and writes the cut commit's rows, still queued, again: the file's lines are `a b c c h` (`c`
    the cut commit's row; a handler at the commit's fsync, a closed store and a forked child, Python 3.14 and 3.6). The
    last copy wins and every read is right. 5.16 waited for ever there (the handler's commit took the `writeLock` the cut
    commit held).
  - The last copy wins, so every reader reads the same rows, and the file is longer until its next rewrite (a `.tsvz`
    file until a scrub): a line for each copy, or up to two lines for each row after a move to the beginning. The
    exception is a stale copy: when another process writes one of those keys between the two copies (while the
    handler runs), the older copy lands last and the file says it. For a set or delete that is the key it writes; for
    a move to the beginning, any key it moves, since every other key's lines are written again (the kind "A write by
    another process between a move_to_end and its commit can be lost" names). In 5.14 a handler that only committed
    wrote the line once, so this case is new.
- Why: knowing whether the first copy is already on disk, not only whether it is still the last line queued, needs an
  identity check across the queue and a rewrite's snapshot, or a counter on every queued line: more state on every
  write, for a few bytes and a race that needs another writer of the same key while the handler runs. A move to the
  beginning that a handler changed the keys inside can say the new order only by lines after the handler's.
- Revisit when: someone sees a file grow this way, or another process's write lost behind a repeated older copy.
- Approved: 2026-11-10, the user chose "Approve as KL (Recommended)" on the question: "a program's own signal handler
  that writes to a store while the main thread is part way through writing to the same store. 5.12 deadlocked here
  for ever. 5.13 goes on, but the rest of the interrupted write runs without its lock: the file can get a duplicate
  line, or memory and file can differ until the next reload." This entry is the duplicate line; memory and the file
  differing was a deferral, which 5.15 fixed (a loose store's next rewrite could lose the set or undo the delete).
  Widened 2026-11-10 to `.tsvz` stores, item 4's cut commit, and the stale-copy case; the user chose "Build it, skip
  extra copy (Recommended)". (Item 4: the 5.15 plan's fix that keeps a cut `.tsvz` commit's rows queued.) Widened
  2026-11-11 to moves; the user chose "Approve the widening (Recommended)": a handler landing inside a move can leave a
  second copy of the moved lines (a move to the front can double the file) until the next rewrite, and a `clear()`
  handler there can write a deletion line twice; no data or order is wrong. Widened 2026-11-12 to a handler's
  `sys.exit()` inside the store's own commit (the file's lines `a b c c h`; the last copy wins and every read is right;
  5.16 waited for ever there); the user chose "Widen it (Recommended)".

### A Ctrl-C while the writer thread reloads after another process's change waits for that reload
- Kind: deferred
- What the operator sees: a program with a big store open ends up to a second or two after its Ctrl-C when the Ctrl-C
  comes while the store's writer thread reads the file again after another process changed it: a `.tsvz` store's
  sync after another process's append, or a loose store's read of a file another process replaced. 5.15 stops a load
  a Ctrl-C took the waiter from (the constructor's, `reload()`), but this read is the thread's own tick, which no one
  waits for, so it does not stop. A 1.5M-row store, SIGINT 0.3 s after the change, medians of 7 runs (5.14, then
  5.15): on Python 3.14, a `.tsvz` store's sync 1.47 and 1.22 s and a loose store's read of a replaced file 0.97 and
  0.97 s, where one with no read under way ends in 0.41 to 0.57 s; on 3.6, 0.46 to 0.97 s against 0.46 to 0.52 s.
- Why: the tick's reads run inside its sync, which also serves its commits and rewrites; a stop there needs its own
  flag set at exit and its own sweep of the write path, more than this release's load stop.
- Plan (5.18): the tick's reload, both kinds, runs with a stop of its own that `_atExit` sets when it lets go of the
  store (for a loose store, the same swap of its reader's file), and `_appendWork` ends on `_LoadStopped`; with a
  sweep of the tick's write path.
- Revisit when: 5.18.
- Noted: 2026-11-10, found by the 5.15 plan's measurement; a deferral, not asked.

### A Ctrl-C caught inside a set, delete, pop or move can leave that one key apart
- Kind: deferred
- What the operator sees: a program that catches a Ctrl-C and goes on. When the Ctrl-C lands inside a set, delete,
  pop, popitem or `move_to_end`, between its change of memory and its queueing of the line, memory has the change and
  the file never gets it: the interrupted operation, half made. A loose store's next rewrite writes memory's side, so
  the file follows; a `.tsvz` store keeps them apart until a reload. Counted by the suite's step sweep (`allow:opkey`),
  5.15 on Python 3.14: `.tsv` set 20 of 325 steps, delete 78 of 263, pop and popitem 84 of about 250; `.tsvz` set 10
  of 399, delete 184 of 365, pop and popitem 198 of about 360 (5.12 to 5.14 the same).
  - `move_to_end(key, last=False)` moves every other key to the end, so a Ctrl-C between its change of memory and its
    queueing of the lines (one step, from the 5.15 final review's fixes) leaves several keys' order apart (memory has
    the whole move, the file none of it), not one key; the rows themselves agree (by reading: the Ctrl-C sweep compares
    rows, not their order).
  - Two signals: a signal handler's `reload()` landing inside a set or delete (it undoes the change in memory), then a
    caught Ctrl-C between the section's end and its redo (`_again`, which 5.15 added for the handler alone), leaves the
    key apart the same way: memory without the row and the file with it (a delete, by reading: the row back in memory).
    A loose store's next rewrite then drops the row from the file, the loss the redo prevents for the handler alone.
    Checked on Python 3.14 for a set, both kinds (the 5.15 review's probe, with the handler alone as the control).
- Why: a fix finishes the operation in the section's `except BaseException` clause, which changes what a cut set
  means (it completes) and needs its own sweep; 5.15's work on these sections was the signal handler's case.
- Plan (5.18): the clause the sections already have calls the section's redo (`_setAgain`, `_delAgain` and the others
  5.15 added for signal handlers) with its own arguments, under the lock, when the section had begun changing memory
  or found its hold let go of (the two-signal case): a set cut by a Ctrl-C completes, and the KeyboardInterrupt still
  goes up. With its own sweep.
- Revisit when: 5.18.
- Noted: 2026-11-10, found by the 5.15 plan; a deferral, not asked. The two-signal case and the move to the
  beginning's several keys were added 2026-11-11, from the 5.15 review.

### A Ctrl-C at a loop's back jump inside a try or with block can skip its clean-up on Python 3.12 to 3.14, in loops 5.15 did not move
- Kind: deferred
- What the operator sees: Python 3.12 to 3.14 leave a loop's back jump (and the test before it) inside a `try` or `with`
  body out of the exception table, so a KeyboardInterrupt raised there skips the block's exit and every `except` clause
  around it. A real signal lands at a back jump. 5.15 moved the two loops that held a lock or a take-back there
  (`move_to_end`'s, and `_call`'s wait for the writer thread) into methods of their own, whose calls the table covers.
  A static scan of TSVZ.py (the 5.15 review's, an exception-table check: each back jump, call and resume inside a
  `try` or `with` body of a function that the table does not cover) lists 32 more such back jumps on Python 3.12.15 and
  36 on 3.14.8 (and 6 `JUMP_BACKWARD_NO_INTERRUPT` in a `with` statement's own exit on 3.14, where no signal is taken).
  By the review's reading, those a main thread of an open store reaches are:
  - `_appendOpen`'s `while view:` (3.12 and 3.13): a partial append's truncate is skipped;
  - `_specRewriteInPlace`'s put-back loop, on a second Ctrl-C;
  - `_iterPartLines`: the file stays open until the traceback goes;
  - `_ServeConnection.request`'s reply loop (3.14): harmless now, since a cut request drops the connection.

  The rest run on the writer thread, or after `close()` (the Ctrl-C there is "A second Ctrl-C inside a set's clean-up"
  above). None is worse than 5.14. A step sweep cannot reach a loop that iterates only in rare conditions, which is
  why 5.15's sweep missed `_call`'s.
- Why: each loop has its own clean-up, and its own test, so moving them all is a release's work; a check that finds the
  new ones as they are written has to come first.
- Plan (5.18): a static exception-table check in the suite (Python 3.12+), listing the uncovered back jumps in
  functions the main thread of an open store runs; move each loop of those into a method, and write down the rest.
- Revisit when: 5.18.
- Noted: 2026-11-11, found by the 5.15 review; a deferral, not asked.

### A signal handler's change of the store inside popitem or move_to_end can make it raise the wrong exception
- Kind: deferred
- What the operator sees: a program whose signal handler changes a store while its main thread calls `popitem()` or
  `move_to_end()` on it. When the signal lands inside the call, the store changes under it, and the call raises an
  exception that says nothing of what happened, memory and the file agreeing. With the handler run at each step
  (5.15, Python 3.14): `popitem` raises `StopIteration` at 11 of about 245 steps when the handler clears the store, and
  `RuntimeError` ("OrderedDict mutated during iteration") at 1 when it clears, sets, deletes or syncs; `move_to_end`
  raises `TypeError` at 14 of 524 when it clears, and `move_to_end(key, last=False)` raises that `RuntimeError` at 26
  of 824 when the handler changes the keys while the move lists them (a set of a new key, a delete, a move, a clear or
  a sync; 3 of 120 line steps on 3.6; counted at the 5.15 final review's fixes, as in the build before them). (A
  delete, pop or move whose key a handler's `clear()` took raises `KeyError`, as for a missing key: that is right.)
  Two handler calls, the second inside the redo the first one caused, are the known limitation after this entry.
- Why: found by the 5.15 plan's sweep (the move to the beginning's, by the fix round's); the change is small but in two
  hot methods, and wants its own tests.
- Plan (5.18): `popitem` looks for its key again when the store changed between its look and its take, and raises
  `KeyError('dictionary is empty')` when the store went empty; `move_to_end` raises `KeyError(key)` when its key went
  inside it, and a move to the beginning lists the other keys again when the store changed while it listed them.
- Revisit when: 5.18.
- Noted: 2026-11-10, found by the 5.15 plan; a deferral, not asked. The two-handler cases, added 2026-11-11 from the
  5.15 re-review, became the known limitation after this entry the same day.

### Two signal handler calls, the second inside the first one's redo, can make a set, delete, pop or move raise, or leave a key apart
- Kind: known limitation
- What the operator sees: a program whose signal handler uses a store (a commit, `clear()`, `reload()`, a set, delete
  or move) while its main thread sets, deletes, pops or moves rows in it, and two signals land microseconds apart. The
  first handler lets go of the section's lock, so the section makes its change again (its redo, `_again`, from 5.15),
  and the second handler lands inside that redo. In the 5.15 re-review's probes (Python 3.14 and 3.9, one case per
  instruction of the redo):
  - a flush, `clear()` or `reload()` between `queue and` and `queue[-1]` (`_moveAgain`, and `_requeue` for sets and
    deletes) raises `IndexError: deque index out of range` out of the move, set, delete or pop, at 2 to 5 of 89 to 617
    instructions: a kind 5.14 never raised there;
  - a delete of the moved key, or `clear()`, between `dict.__contains__` and `dict.get` in `_moveAgain`'s listing for a
    move to the end hands `_moveLines` a `None` row: `TypeError` in a two-column store (7 of 252 instructions), a `None`
    queued after the drop item in a one-column store (7 of 174), which the drop item's rewrite skips;
  - a `clear()` inside a move to the beginning's redo, after the first handler set a new key `y`, can leave memory
    empty and the file holding `y` (the interrupted pass queued `y`'s lines; the next pass deletes only the keys the
    section captured), and a flush there can leave memory's order apart from the file's.

  After every exception memory and the file agree. Over every pair of handler calls 2, 10, 50 or 200 instructions apart
  (7 handler pairs, 4 operations, 3.14), memory and the file end apart at 220 of 53,873 pairs, against 5.14's 19,094 of
  52,102: 140 are the deferred order of "A set of a new key can leave its place in memory apart from the file's", 37
  the `y` case (5.14: 381), 43 order only (5.14: 418). Exceptions: `IndexError` in 24 pairs (5.14: none), `TypeError`
  in 196 (5.14: 120), `RuntimeError` in 782 (5.14: 628).
- Why: the cases need two handler calls microseconds apart, the second inside the first one's redo, and are far rarer
  than in 5.14. Whatever handles the second signal is itself code a third can land in (as in "A second Ctrl-C inside a
  set's clean-up" below), and the planned patches were a third round on `_moveAgain`, which both of 5.15's fix rounds
  changed.
- Revisit when: someone hits it (a program whose handlers can fire microseconds apart), or the redo is redesigned so
  that an interrupted move keeps no captured rows or lines to go stale (the 5.15 design check's idea, not probed: the
  move is made again in memory and asks for one rewrite, as a one-column delete's drop item does).
- Approved: 2026-11-11, the user chose "Approve as KL (Recommended)", replacing the planned redo patches (`_again`
  running a change again when it raised while its lock was let go of, `_moveAgain` counting the keys a pass queued
  lines for as captured by the next pass, and a two-handler sweep in the suite). The user did not ask for it
  explicitly: the 5.15 re-review found the cases, and the 5.15 design check proposed the known limitation.

### A set of a new key can leave its place in memory apart from the file's
- Kind: deferred
- What the operator sees: a program's signal handler sets another new key, moves a key, or follows another process's
  line, while its main thread sets a new key (`t['x'] = ...`). When the handler lands between the set's change of
  memory and its queued line, memory holds x before the handler's change and the file after it: the rows agree, and
  the order differs until a reload (a loose store's next rewrite writes memory's order). The set's redo sets the row
  again and leaves it where it was. Counted by the suite's handler sweep (`allow:new-key-order`) at the 5.15 final
  review's fixes: 20 of 325 landings on Python 3.14 for each such handler, with or without a header (4 of 59 line
  steps on 3.6), and a `.tsvz` store's 10 of 399 for a set of another key (2 of 72); 5.14 the same 20.
- Why: found by the 5.15 final review's wider sweep; not worse than 5.14, and the fix is in the redo of every set.
- Plan (5.18): `_setAgain` and `_specAgain` move a key that was new to the end when its line is still the last one
  queued; the sweep's allowance goes.
- Revisit when: 5.18.
- Noted: 2026-11-11, found by the 5.15 final review; a deferral, not asked.

### A signal handler's clear() in a store with no header can leave a deleted key as a row of the key alone
- Kind: deferred
- What the operator sees: a store whose file has no header line, and a signal handler that calls `clear()` while the
  main thread deletes, pops or moves a row. After the clear the store has no column count, so the deletion line
  written after it (the delete's own, or a move's redo's) is the lone key, which the emptied file reads as a row:
  the key is in the file, as a row of the key alone, and not in memory. Counted by the suite's handler sweep
  (`allow:lone-key`) at the 5.15 final review's fixes, Python 3.14: a delete 63 of 263 landings, `pop` 69 of 249,
  `popitem` 69 of 245, `move_to_end` 91 of 524 and to the beginning 91 of 824 (3.6: 9 of 49, 10 of 46, 10 of 44, 17
  of 81, 17 of 120). 5.14: 63, 67 and 67 for the first three, and for a move the whole row back at 182 of 532. With a
  header every reader takes the line as a deletion (none in the sweeps with one).
- Why: found by the 5.15 final review's wider sweep; 5.14 the same or worse.
- Plan (5.18): the branch of `__appendEmptyLine` for a store with no column count writes what reads as a deletion
  there (a drop item, as a one-column store's); the sweep's allowance goes.
- Revisit when: 5.18.
- Noted: 2026-11-11, found by the 5.15 final review; a deferral, not asked.

### A '#' key can leave memory when the store follows another process's change while rows are queued
- Kind: deferred
- What the operator sees: a loose store that holds a `#` key (memory only: it is never written). When it follows
  another process's change while it has rows queued, it reads the file whole and replays the queue, and the `#` key,
  which the file does not hold, is gone from memory; with nothing queued it reads only what was appended, and the key
  stays. So whether a `#` key outlives another process's append depends on the timing: 8 of 10 runs lost it on Python
  3.14 (a move queued, another process's line, then the follow; 5.14: 6 of 10). `reload()` drops it always.
- Why: found by the 5.15 final review fixes' sweep of a store with a `#` key; 5.14 the same. Whether a whole read
  keeps `#` keys is a choice of what `reload()` means, as `_readWidened` keeps them and `reload()` does not.
- Plan (5.18): decide whether a whole read keeps the `#` keys memory holds (in their place, as `_readWidened` does), and
  make the follow and `reload()` agree; the sweep's check of a `#` key's place then covers memory without it too.
- Revisit when: 5.18.
- Noted: 2026-11-11, found by the 5.15 final review's fixes; a deferral, not asked.

### Test gaps the 5.15 final review's fixes leave
- Kind: deferred
- What the operator sees: nothing directly; the suite does not yet look at these, which the 5.15 final review's own
  wider sweep (and the fixes' run of it) did, and where it found nothing beyond the deferrals above:
  - the handler sweep runs no one-column store, and not the handlers `rewrite()`, a pop of another key, `popitem()`, a
    delete of the operation's own key and a move of the operation's own key;
  - the Ctrl-C sweep compares rows, not their order.

  (A fork inside a `clear()`'s write, the tick's interval rewrite and a one-column delete's commit, not only inside an
  explicit `rewrite()`, is tested from 5.17.)
- Why: each handler and store adds sweeps to CI's serial run (each under 3.5 s on 3.14; the whole serial 3.14 run took
  13 min 7 s here with the 160 sweeps the fixes added, of the job's 40 minutes); the fixes added the ones that guard
  what they changed.
- Plan (5.18): add them to the suite, measured against CI's time.
- Revisit when: 5.18.
- Noted: 2026-11-11, from the 5.15 final review's test gaps; a deferral, not asked. The fork bullet was done in 5.17.

### The Ctrl-C sweep tests' 120 s limit is too short for suites run side by side
- Kind: deferred
- What the operator sees: nothing in the library. Each Ctrl-C sweep of the suite (`_sweep` in `TSVZ_test.py`) has
  120 s, sized from CI, which runs the suite on its own. On a release run of four suites at once, 32 workers each on 32
  cores, `test_a_ctrl_c_caught_at_any_step_of_an_operation_leaves_no_lock_held_and_memory_and_the_file_agreeing[tsv-oneshot-scrub]`
  timed out at 120 s on Python 3.9 (5.15, 717aea9); alone it passed, in 13.6 s there (6.6 s on 3.14).
- Why: a limit of the tests only, and 5.16 is the hotfix of the one change the user chose.
- Plan (5.18): size the limit from the slowest sweep measured under a release run's load (four suites, 32 workers
  each), and write that reason beside it.
- Revisit when: 5.18.
- Noted: 2026-11-11, from the 5.15 release run; a deferral, not asked.

### Test gaps the review of 5.17's third fix round leaves
- Kind: deferred
- What the operator sees: nothing in the library; two gaps in the suite.
  - The real-signal handler test's `reload` cases
    (`test_a_real_signal_handler_that_uses_the_store_while_the_program_sets_rows_leaves_memory_and_the_file_as_it_set_them[reload-*]`)
    ask for at least 50 handler calls in 30 s. On a loaded host the timer's handler runs fewer times: 24 copies beside 24
    CPU burners, Python 3.11, 27 to 74 calls (every `.tsv` run 27 to 41), memory and the file agreeing in all 48 runs.
  - The round's own test
    (`test_a_signal_that_cuts_a_stores_own_write_after_it_took_the_file_lock_leaves_no_locked_file_for_the_calls_after_it`)
    pins the commit's half of the fix only: a build with the previous `_writeFileStarted`, `_readLocked` and
    `_openLocked` and this round's commit passed the 1,150 related tests. The rewrite's and `clear()`'s half (the design
    check's sweeps: 91 to 9 and 87 to 5 hung steps) has no test.
- Why: the first is a limit sized without load, as in the entry above; the second was found by mutating the fix, after
  the round.
- Plan (5.18): size the count from a release run's load, or count handler landings inside sets instead of calls;
  parametrize the round's test over `rewrite(force=True)` and `clear()` (`_writeFileStarted` calls `_unattended()` after its
  open and `_unlockFile` in its clean-up, so the test's two landing points apply as they are).
- Revisit when: 5.18.
- Noted: 2026-11-12, from the review of the third fix round; a deferral, not asked.

### A write after close(), or in a forked child, can be cut by a Ctrl-C
- Kind: known limitation
- What the operator sees: a `TSVZed` has no writer thread once it is closed, nor in a child that `fork()` made, so
  a `rewrite()`, `mapToFile()` or `hardMapToFile()` called there runs in the caller's thread, as in 4.9. A Ctrl-C
  during it can leave a `.tsv`-family file damaged, a compressed one unreadable; if the program goes on, the next
  rewrite makes it again from memory under the merge rule.
  - Also a `.tsvz` store's `clear()` there (the 5.15 review, 2026-11-11, deferred): it empties memory, then clears the
    file in the caller's thread, and a Ctrl-C between the two leaves memory empty and the file full. `deSynced` is set
    only for an Exception, and after `close()` no tick reloads, so nothing brings the rows back into memory (checked on
    Python 3.14 and 3.6 by raising in `_specClear`). 5.14 had the window in every `.tsvz` `clear()`; 5.15 closes it for a
    running store (the clear runs in its writer thread's job) and narrows it to these two cases.
- Why: calls after `close()` and writes from forked children are rare, and covering them means starting a writer
  thread again in both cases, with its lifetime to get right.
- Revisit when: someone writes through a closed store or from a forked child and hits it. For the `.tsvz` `clear()`:
  5.18, which sets `deSynced` before memory is emptied; a closed store would still need a reload.
- Approved: 2026-10-29, the user chose "Known limitation (Recommended)". (The `.tsvz` `clear()` is deferred, not
  approved.)

### A second Ctrl-C inside a set's clean-up, or a Ctrl-C in a write made after close() or in a forked child, can leave a lock held
- Kind: known limitation
- What the operator sees: a program that catches a Ctrl-C and goes on.
  - Two Ctrl-Cs microseconds apart, the second landing where a set, delete, pop or move lets go of the store's lock
    after the first: the main thread keeps the lock until it next uses the store (its next set, delete, commit or
    `close()`, or the exit, lets go of it). Until then another thread's set or delete waits, and a loose store's
    background appends wait. With interrupts 0.7 ms apart, 4 of 97,000 on 3.6; none in 1.2 million single interrupts
    and none in 1,280 real Ctrl-Cs.
  - After `close()`, or in a child that `fork()` made, a store has no writer thread, and `commitAppendToFile()`,
    `rewrite()`, `mapToFile()`, `clear()` and `reload()` run in the caller's thread. A Ctrl-C there can leave the
    store's lock held (let go of as above), or its `writeLock`, which nothing lets go of: the program's next commit
    or rewrite of that store then waits for ever. The lock left held can be the file's lock too: two signals about 1 µs
    apart, the first cutting `_writeFileStarted`'s unlock and the second landing at the entry of its
    `_closeUnflushed(f)` (a function's entry takes a signal from Python 3.11), leave the file open and locked under the
    traceback (by reading, not run). With an interrupt at each bytecode of one such commit or rewrite: `writeLock` left held at 6 to 13 steps of about 1,000 to 9,400 (3.14 and 3.11; none at 3.6's line steps). 5.12 did
    the same in these two cases, and in every `.tsvz` commit of a running store, which ran in the caller's thread
    there. What the cut write leaves in the file is the entry above. (A cut `.tsvz` commit drops no row from 5.15: its
    rows stay queued until they are on disk.)
  - A signal handler's `sys.exit()` inside such a write, on Python 3.14 (widened 2026-11-12; 5.17). A signal can land
    just after `with self.writeLock:` has taken the lock and before its block begins, and the exception skips the release
    (CPython's own gap). The call the handler put off (a commit, `reload()`, `clear()`, `clear_file()`, `rewrite()`,
    `mapToFile()`, `hardMapToFile()`, `checkExternalChanges()` or `move_to_end()`; 5.17) is made as the exception goes
    up, and waits for ever for the `writeLock` the cut write left held: the process does not end, and the handler's row
    is not written. The README's handler (set, commit, `sys.exit()`) in a
    set-and-commit loop of a closed store and of a forked child, a real SIGUSR1 from another process, 4,000 runs each:
    32 of 8,000 hung on 3.14 (46 in the review of the third fix round), every one waiting at `with self.writeLock:`
    (none in 8,000 on each of 3.11, 3.12 and 3.13). 5.16 waited for ever at 127 and 121 of 160 real signals of that loop (a closed
    store, a forked child), this landing among them: of 8,000 real signals on 5.16 (3.14) 29 landed on that `with`
    line, and every one hung (its handler's commit took the lock the gap left held), and a landing there simulated
    hangs 5.16, in a closed store and in a forked child, on 3.14, 3.11 and 3.6. A program's own `except SystemExit:` or
    `except KeyboardInterrupt:` clause that commits again waits for ever after a signal cut there too (simulated at
    the gap, a closed store and a forked child, 3.14 and 3.6: 5.16 and 5.17 both). Two signals inside one commit (a
    first handler's call put off, then a second handler's `sys.exit()`; the first simulated at the start of each commit
    of the loop, the second a real SIGUSR1, 4,000 runs each): 28 of 8,000 hung on 3.14, all at `writeLock` (none on 3.11), where 5.16 ended in all of them (it made the first
    handler's call at once): this case stays worse than 5.16 on 3.14.
- Why: `writeLock` is held for the caller across calls by `get_file_obj()`, so a thread that finds itself holding
  it cannot take that for a Ctrl-C's leftover; whatever lets go of a lock is itself code a second Ctrl-C can land in;
  calls after `close()` and writes from forked children are rare.
- Revisit when: someone hits it, or a closed store or a forked child gets a writer thread. A candidate for 5.18, to be
  measured: a `writeLock` that knows its holder, used only to let go of a hold a cut write left, in the one job it can
  do: at `_runHere`'s door, where the stack shows no job of this
  store on this thread and `get_file_obj()` holds nothing, a hold by this thread is a leftover and is let go of. It needs
  its own sweep: with a reentrant lock, a nested taker that is not put off would run inside the write where it waits today
  (a plain reentrant `writeLock` hung at 619 of 1,060 steps of 5.16's commit: the nested commit opens the file again, and
  waits for the file lock its own write holds; the 5.17 design check).
- Approved: 2026-11-10, the user chose "Approve as KL (Recommended)" on the question: "One rare case is left, for a
  program that catches Ctrl-C and keeps running: a second Ctrl-C landing inside the cleanup that lets go of the lock,
  or a write after close() or in a forked child, can still leave a store's lock held, and another thread then waits
  for ever (as in 5.12)." The wording is the 5.13 final review's, which adds the `writeLock` case: its hang hits the
  program's own next commit, not only another thread. Widened 2026-11-12 to a handler's `sys.exit()` that lands as a
  write takes `writeLock` on Python 3.14 (the last bullet); the user chose "Widen it (Recommended)".

### On Python 3.6 to 3.9 a signal handler that ends the process inside a store's own write can still hang, in a few of 8,000 signals
- Kind: known limitation
- What the operator sees: in a store with no writer thread (a forked child's, or one after `close()`), a signal handler that
  sets a row, commits and ends by `sys.exit()` (the README's handler) lands inside the store's own write; its commit is
  put off and made as the write ends (5.17). Up to Python 3.9 the interpreter checks for signals after the instruction
  that ends a `try` or `with` block, so one instruction at the end of each block of the write's clean-up is outside
  every clean-up. A signal that lands there leaves the locked file open under the `SystemExit`'s traceback (or `writeLock`
  held, at the end of its `with` block): the handler's commit waits for that lock for ever, and the process does not end.
  The README's handler in a set-and-commit loop, a real SIGUSR1 from another process, a closed store and a forked child,
  4,000 runs each: 2 of 8,000 hung on 3.6 in each of two such runs (three of the four at the file lock, one at
  `writeLock`), 2 on 3.9 and none on 3.10. From 3.10 a signal is taken only after a call and at a loop's back jump (from 3.11 also at a
  function's entry), so none lands between a block's last instruction and the next: a timer's signal into a function
  with a `try` and one with a `with`, 3 million calls each, left the `finally` unrun 367 and 286 times and the lock held
  125 and 189 times on 3.6 and 3.9, and never on 3.10 and 3.11. The handler's runs hung in none on 3.11 and 3.12 (none
  in 8,000 each), and the hangs that are left on 3.14 are the lock-held entry's. The 5.17 build before this fix (a581b13)
  hung at 230, 220 and 195 of 8,000 on 3.6, 3.9 and 3.10, and 5.16 at about 3 of 4 (3.14).
- Why: the interpreters are end-of-life, kept so the tools still run on them. The file-lock part could be closed by
  arranging each write's `try` (the review of the third fix round, by reading the 3.6 and 3.9 bytecode; not built or run): the file
  stored inside the `try` whose `finally` closes it, before it is locked; the unlock the last step of every path out of
  that `try` (the normal end, each early return and each `except` that ends normally); the close in the `finally`; so
  `_openLocked` could no longer be the step that locks and returns the file (on 3.9 its `return f` falls outside its
  `except`). One signal then leaves at most an unlocked open file. That restructures all four functions of the write path
  that 3.11 and later run, every return and `except` path of each, for 2 of 8,000 signals on end-of-life
  interpreters. The `writeLock` part needs more than a `try`: the end of a `with` block is the gap, and a plain `Lock`
  has no owner for a `finally` to test.
- Revisit when: someone hits it where they cannot move to Python 3.11 or later, or a closed store or a forked child gets a
  writer thread.
- Approved: 2026-11-12, the user chose "Known limitation (Recommended)" on the 5.17 design check's question: Python 3.6
  to 3.10 keep a few hangs in 8,000 real signals that no arrangement of `try` removes. Re-approved 2026-11-12 after the
  reason was corrected (3.6 to 3.9, and an arrangement exists for the file lock): the user chose "Known limitation,
  corrected (Recommended)".

### A forked child writes nothing at its close or exit when its parent held get_file_obj() at the fork
- Kind: known limitation
- What the operator sees: a child that `fork()` made while its parent held the store's file through `get_file_obj()`
  sets rows on that store. Its `close()` and its normal exit leave them unwritten, and say so once: "the rows this
  forked child set are not written at its close or exit: its parent held the file (get_file_obj) at the fork;
  commitAppendToFile() writes them". That holds even when both have let go of the file since: the close makes no
  rewrite or sync either, which wait for the file's lock. So let go of the file (`release_file_obj()`) before forking;
  or commit in the child, which writes once the parent lets go of the file (and waits for ever if the parent holds it
  while it waits for that child). A commit in the parent before the fork writes none of the child's rows. (The 5.15
  re-review's probe, Python 3.14 and 3.6, `.tsv` with `rewrite_on_exit` and `.tsvz`: a release before the fork wrote
  the child's row in every case; a commit before it, never.)
  - A store with neither `rewrite_on_exit` nor a sync pending at the close: 5.14 the same, without the message.
  - A store with `rewrite_on_exit` (`.tsv`), or with a sync pending at the close (another process's change not yet
    followed, both kinds): 5.14's close wrote the rows by that rewrite or sync once both had let go of the file; where
    the parent still held it and waited for the child, it waited for ever on 3.14 and wrote through the lock it
    inherited on 3.6. 5.15 writes nothing there and ends (the 5.15 final review's probe, run again on its fixes, on
    Python 3.14 and 3.6).
  - A child that calls `commitAppendToFile()` itself while its parent holds the file and waits for it: on Python 3.6
    5.14's child wrote, through the copy of the parent's lock it inherited; 5.15's waits for ever, as 5.14's already did
    on 3.7 and later, since 5.15's fork hook drops the inherited locks on 3.6 too.
  - With `monitor_external_changes=False` too (5.16), a child's `move_to_end()`, and its `checkExternalChanges()`
    followed by `rewrite()`, wait for ever while the parent holds the file and waits for the child, as a default
    (monitoring-on) child's already did in 5.14 and 5.15; a monitoring-off child's returned before, and left its move
    unwritten. They wait where they follow a change to the file (one since the child's store last read or wrote it)
    and the parent's hold keeps that read or its commit out:
    - a hold in a mode without `a` (`'r+b'`, `'wb'`), which takes the rewrite lock and so holds off the read (a
      `'wb'` hold empties the file, itself a change to follow);
    - any hold where the file lock is a whole-file `flock`: POSIX systems other than Linux (by reading);
    - rows the child queued itself, which the follow commits, waiting for the parent's append lock.

    On Linux a child with nothing queued reads beside the default `'ab'` hold, since an append lock takes byte 1 and a
    read bytes 2 and 0 (`_lockFile`), and the call returns; with no change to follow, nothing waits. (The 5.16 final
    review's `s/held.py`, Python 3.14 and 3.6: 3 of 6 mode and operation cases never end, where 5.15 and 5.14 ended in
    all 6. The 5.16 docs pass's `held2.py`, the same Pythons, with the change made apart: with a change to follow,
    under `'ab'` only the cases with rows queued wait and under `'r+b'` all do; with none, no case waits; `'wb'`, on
    3.14, waits in all 6. macOS: the suite's test of 5.17, below, on macOS 26 with Python 3.12 and 3.14, where the lock is
    a whole-file `flock`: the `'ab'` hold with a change to follow waits there too, as stated.)
  - On Python 3.6 the same holds for a child in which TSVZ's fork hook did not run, because a library replaced
    `threading._after_fork` without calling the one it replaced: that child also keeps 5.14's other behaviour (its
    commit writes the rows the parent had queued too, and it keeps the locks the parent's threads held), and its
    message ends "no fork hook ran in it".
  - From 5.17 the same holds for a child's own child, and any later one, where an ancestor held the file at an earlier
    fork: the message ends "an ancestor held the file (get_file_obj) at an earlier fork". 5.15 and 5.16 took the
    grandchild for one whose parent held nothing, and its exit waited for ever for the grandparent's lock while the
    grandparent waited for the child and the child for it (5.14's children wrote nothing at their exit, and ended).
    That is the first half. The second: a grandchild cannot tell whether its ancestor still holds the file and waits for
    it, so it writes nothing where the ancestor let go of the file long before, and 5.16's wrote its row there (the 5.17
    final review's `gcrel.py`: a process holds `get_file_obj()` across the fork of a child, lets go of it and does not
    wait; the child forks a child of its own, which sets a row and ends normally: the row is in the file in 5.16, and in
    5.17 it is not, and the grandchild says so once; Python 3.14 and 3.6). So let go of the file before forking, as for a
    child.
  - From 5.17 a forked child's `rewrite_on_exit` is False (a child takes none of its parent's single-writer settings),
    so the bullet above about a store with `rewrite_on_exit` is a child that set it again.
  - The suite pins the waits above, in the cases `held2.py` ran
    (`test_a_forked_child_whose_parent_holds_the_file_waits_only_where_its_known_limitation_says`, from 5.17): a change
    that ends one fails there, and this entry changes with it. It pins the grandchild's silence too, both halves
    (`test_a_forked_childs_own_child_writes_nothing_at_its_exit_where_an_ancestor_holds_the_file` and
    `..._even_once_the_ancestor_let_go_of_the_file`).
- Why: the child cannot tell whether its parent waits for it while holding the file. A write there waits for the
  parent's lock, and a parent that waits for the child (`waitpid`) would then never let go: the 5.15 plan's first
  design hung there (measured), as 5.14's close did with `rewrite_on_exit` or a pending sync. A child with no fork
  hook may hold a copy of a lock a parent's thread held, and its close would wait for ever on it.
- Revisit when: someone needs those rows written and cannot commit.
- Approved: 2026-11-10, the user chose "Approve as KL (Recommended)": a forked child writes nothing at close or exit
  when its parent held `get_file_obj()` at the fork, and warns once. The Python 3.6 child with no hook is the case the
  user's pick "Wrap it on 3.6 (Recommended)" named: that child keeps 5.14's behaviour and says so once. Widened
  2026-11-11; the user chose "Widen KL-c (Recommended)" (KL-c: this entry's label in the reviews): a child whose parent
  held the file at the fork writes nothing at its close, even when both have let go of it since, for the stores with
  `rewrite_on_exit` or a pending sync, where 5.14 wrote the row. (The question's advice was "write before forking, or
  close the store in the child explicitly"; a `close()` in such a child writes nothing, and a commit before the fork
  writes none of the child's rows, so the advice here is the 5.15 re-review's: let go of the file before forking, or
  commit in the child once the parent has let go.) The Python 3.6 child whose explicit commit now waits for ever while
  the parent holds the file and waits for it: the user chose "Ship it (Recommended)", 2026-11-11. Widened again
  2026-11-11 (the 5.16 final review); the user chose "Approve the widening (Recommended)": with
  `monitor_external_changes=False` a forked child's `move_to_end()` (and `checkExternalChanges()` followed by
  `rewrite()`) waits there too, as a default child's did. The same day the docs pass after the 5.16 re-review stated
  when it waits (a change to follow, and a hold that keeps the read or the commit out), from the Haiku second opinion:
  that narrows what was approved, and changes no behaviour. Widened again 2026-11-11 (the 5.17 final review); the user
  chose "Approve the widening (Recommended)", with both halves put to them: a grandchild (or a later descendant) writes
  nothing at its close or exit where an ancestor held the file at an earlier fork, so it no longer waits for ever for a
  hold the ancestor still has; and one whose ancestor let go of the file long ago no longer writes its rows at its
  close or exit either, where 5.16's wrote them.

### After a parent's failed rewrite, a forked child no longer mends the file
- Kind: known limitation
- What the operator sees: a store's rewrite fails part way (a full disk, an I/O error) and leaves the file damaged, with
  memory the only good copy. The program forks, and the parent ends without getting a rewrite through (a daemon's
  `os._exit`, a kill). The child sets a row and ends by `close()` or its normal exit.
  - 5.16: the child's close took the parent's failed rewrite to try again and made the file from its memory: all 9 rows
    of the probe read right, in 6 of 6 `.tsv.gz` cases.
  - 5.17: the child only appends after the damage (where the parent appended nothing after it: see the bullet that
    starts "Where the parent appended after the damage"), so a `.tsv.gz` file stays damaged, every read stops at the
    damage, and none of the 9 rows reads right, the child's own included, in 4 of 6 cases (close or exit, with or without
    `rewrite_on_exit` given to the parent's store). The other 2 are a child that sets `rewrite_on_exit` again, which
    mends the file as before. A plain `.tsv` read right in both builds in the probe. (The 5.17 final review's
    `daemon.py`, Python 3.14 and 3.6.)
  - With the parent alive and its rewrite getting through, 5.17 keeps the child's row in 6 of 6 cases (`.tsv` and
    `.tsv.gz`), where 5.16 lost it in 6 of 6 (`alive.py`): the parent, still damaged, took the child's remake for a change
    it could not merge and wrote over it.
  - Where the parent appended after the damage (the same setup, the parent setting `p2` and committing after the fork,
    before the child goes on), the child's exit does not only append. The damaged file is another writer's change to it:
    the child's exit reads the file, which reads as empty (the first member does not decode), and makes the file again
    from that read, so what the read did not get goes. That is the README's documented rule for another writer's damage;
    the child now takes the parent for another writer. The child's memory held `a` to `h` and its own `c2` at its exit
    hook (9 rows); the file ends with `c2` alone, and `a` to `h` are gone, and the parent's `p2` with them. Setting
    `rewrite_on_exit` again gives the same, and a header only (25 bytes) where it ends by `close()`. 5.16 the same or
    worse, header only on close:

    | Child, `.tsv.gz`, the parent appended `p2` after the damage | 5.16 | 5.17 |
    |---|---|---|
    | Sets `rewrite_on_exit` again, ends by close | header only (25 bytes) | header only (25 bytes) |
    | Sets `rewrite_on_exit` again, ends by exit | `c2` alone | `c2` alone |
    | As forked, ends by exit | `c2` alone | `c2` alone |
    | As forked, ends by close | header only (25 bytes) | the damage kept (199 bytes), still unreadable |

    (The 5.17 re-review's `daemon_p2.py`, Python 3.14; a plain `.tsv` read right in every case.)
  - To have a child mend the file, set `rewrite_on_exit` again in the child (the README's advice to a daemon), or open the
    store after the fork. That mends a `.tsv.gz` file only where the parent appended nothing after the damage: with a
    row the parent appended after it, the child's rewrite left it reading as empty (close) or as the child's row alone
    (exit), in 5.16 as well (the bullet above; a variant of `daemon.py`, Python 3.14; a plain `.tsv` read right).
  - The suite pins it (`test_a_forked_child_does_not_mend_a_compressed_file_its_parent_failed_to_rewrite_unless_it_sets_rewrite_on_exit_again`):
    a change that ends it fails there, and this entry changes with it.
- Why: a forked child takes none of its parent's duties to the file (the 5.17 design check; the user's pick "Take H2
  (Recommended)"): the parent writes the file too, and a child that took the failed rewrite to try again made the parent
  overwrite the child's rows. The child cannot tell a parent that will mend the file from one that will not.
- Revisit when: someone needs it. One idea for a design check: a child whose parent is gone (`os.getppid()` changed since
  the fork) is the only heir and takes the duty back. Not built or tested. The next entry, a deferral for 5.18, is the same
  design question for the remake a failed rewrite leaves, and the two are designed together.
- Approved: 2026-11-11, the user chose "Known limitation (Recommended)" on the 5.17 final review's question: after a
  parent's failed rewrite, a forked child no longer mends the file; the README and its upgrade note say so, with the
  workaround. Widened 2026-11-12: the user chose "Widen W1 with the facts (Recommended)". (What the widening added: the
  bullet "Where the parent appended after the damage", the as-forked exit case, and the reach of the workaround in the
  bullet after it.)

### A forked child keeps the remake its parent's failed rewrite left, and makes the file again over a live parent's
- Kind: deferred
- What the operator sees: another writer leaves damage in a `.tsv.gz` file, which every read stops at. The store's commit
  reads it under the lock and takes the file to be made again (`_remake`), and that remake fails part way (a full disk):
  `_damaged` and `_remake` are both set. The program then forks. The child's store drops `_damaged` and the failed-write
  retry (it takes none of its parent's duties, 5.17) but keeps `_remake`, so its commit or close makes the whole file
  again from its memory. The live parent's close, still `_damaged`, takes that for a change it cannot merge ("Overwriting
  external changes") and writes over it: the child's row is lost. 5.16 and 5.17 the same, close and exit alike (the 5.17
  re-review's `g1.py`, Python 3.14: the file reads `a` to `h`, `p2` and `x`, without the child's `c2`); it is the lost
  child row of the 5.15 re-review, which 5.17's H2 fixed for the failed-write state and not for `_remake`.
  - With the parent gone (`os._exit` after the child), the child's remake mends the file, in 5.16 and in 5.17: all rows and
    `c2` read right.
- Why: not worse than 5.16. The fix is one line in `_afterFork`: drop `_remake` where the parent was `_damaged` (a
  prototype: a live parent keeps `c2`, `p2` and every row, close and exit alike). It would also move the dead-parent case,
  which 5.16 and 5.17 mend, into the approved limitation above ("a forked child no longer mends the file"), and that
  needs the user's OK: it is the same design question as that entry's, so it is decided there.
- Plan (5.18): design it with the entry above. A child whose parent is gone (`os.getppid()` changed since the fork) is the
  only heir and takes the duty back, the remake too; a child whose parent lives leaves the remake, and the repair, to it.
  A test from `g1.py` for the live parent (the child's row kept) and the dead parent (the file mended).
- Revisit when: 5.18.
- Noted: 2026-11-12, found by the 5.17 re-review's second opinion (Grok); a deferral, not asked.

### A call a signal handler puts off inside a store's own write is lost when the handler ends the process by os._exit
- Kind: known limitation
- What the operator sees: in a store with no writer thread (a forked child's, or one after `close()`), a signal
  handler's `commitAppendToFile()`, `reload()`, `clear()`, `clear_file()`, `rewrite()`, `mapToFile()`, `hardMapToFile()`,
  `checkExternalChanges()` or `move_to_end()` that lands inside the store's own write is made as soon as that write ends, unless a Ctrl-C ended it (5.17; a Ctrl-C drops it, and a commit's rows stay
  queued for the next commit). A handler that makes such a call and then ends the process by `os._exit()`
  never lets the write end, so the call is not made: set a row, commit and `os._exit(0)` in the handler, and the row is
  not written. By `sys.exit()` the exception unwinds through the write, which makes the calls put off first, and the
  row is written, but for two cases where the call waits for ever and the process does not end: on Python 3.14, a signal
  that lands just after the write has taken `writeLock` leaves it held (32 of 8,000 real signals in a commit loop; the
  entry on a lock left held, above), and on Python 3.6 to 3.9 one that lands at the end of a `try` or `with` block leaves the
  file locked, or `writeLock` held (2 of 8,000 on 3.6; the entry on Python 3.6 to 3.9, above). 5.16 waited for ever at the commit, so
  neither wrote it, and at 127 and 121 of 160 real signals altogether. (The 5.17 final review's `hs.py`, cases `exit` and
  `sysexit`, Python 3.14 and 3.6.) The README says: end by `sys.exit()`, not `os._exit()`.
  - The suite pins it (`test_a_signal_handler_that_ends_the_process_inside_a_stores_own_write_keeps_its_commit_only_by_sys_exit`).
- Why: the call waits for the write it landed in, and that write never returns; running it at once, inside the write,
  is what waited for ever.
- Revisit when: someone ends the process from a handler inside such a write and cannot use `sys.exit()`.
- Approved: 2026-11-11, the user chose "Approve as KL (Recommended)" on the 5.17 final review's question: a put-off call
  is lost when the handler ends the process by `os._exit`. (The 2026-11-12 sentence on the two cases where a call waits
  for ever points to two other entries, the widened lock-held one and the Python 3.6 to 3.9 one; it widens nothing here.)

### A signal handler's set inside a forked child's or a closed store's own write can leave memory apart from the file
- Kind: deferred
- What the operator sees: a store with no writer thread (a forked child's, or one after `close()`) writes in the thread
  that asks, and those writes hold the store's section lock (`_memLock`) where they change memory and the queue together:
  a reload's swap of memory, a commit's listing of the queue. A signal handler that sets a row (or deletes, pops or moves
  one) and lands inside such a section takes the lock from it, as a handler inside a set does (5.13), and the write goes
  on without it. A handler's set of a new key `h` that lands in a `reload()` just before its swap of memory leaves `h`
  out of memory and in the queue: the file gets it, memory does not, until the next reload. 5.16 the same (the 5.17
  build's `setinjob.py`, Python 3.14, a forked child and a closed store; a running store, whose writes run on its writer
  thread while the handler's set waits for the lock, agrees). Not swept: how often each section is hit is not counted.
  - The same family, from the 5.17 final review (`hs.py` and `l1.py`, Python 3.14 and 3.6, a forked child and a closed
    store; 5.16 waited for ever at the first call of each, so none is worse than it): a call a handler makes inside such a
    write is put off and made when the write ends (5.17), but the handler's own next call is made at once, and so comes
    before it.
    - A handler's `clear()` followed by a set of `h`: the set is made, then the put-off clear empties memory and the
      queue, and `h` is in neither memory nor the file (`.tsv` and `.tsvz`). A handler's `reload()` followed by a set
      keeps the set (a reload replays the queued rows on what it read).
    - A handler's `reload()` returns before the reload, so a read right after it sees the old row.
    - A put-off `move_to_end()` of a key that is gone (a `del` after it in the handler) does not raise `KeyError` to the
      handler: the put-off move reports "Failed at _move()" later.
    - A process a handler forks inside a closed store's own write, which sets a row and commits: its commit is put off
      in the line it inherited and is not made (5.16's waited for ever on the writeLock it inherited held). A forked
      child's store gives that process fresh locks and a new fork mark (5.17), and a closed store has no fork hook.
      The same missing hook: a process that returns from the handler into the closed store's write makes the parent's
      put-off calls again (a put-off `clear()` empties the file of the rows the parent wrote after it; the 5.17
      re-review's `forkreturn.py`), where a forked child's store makes none (fixed for it in 5.17). 5.16 waited for ever at
      the first call.
  - From the 5.17 re-review (`sw-*` sweeps over every instruction of `TSVZ.py` on the main thread, Python 3.14 and 3.6;
    5.16 waited for ever at the first call of each, except where it says the same):
    - **Another handler's call between the drained calls runs ahead of them.** Handler 1 calls `move_to_end('a')` inside
      a commit, which puts off three calls (the check, the rewrite, the move). Handler 2 calls `move_to_end('b')` and
      lands in the drain, between those calls. It finds no job's frame (the waiting line lives in `_runHere`'s frame
      between the drained calls, which `_jobHere` does not look at) and is made at once, ahead of the rest of handler 1's:
      memory and the file end `[c, b, a]` where the order of the calls gives `[c, a, b]`; the rows agree (28 of 5,517
      instructions of the drain in the sweep, 86 on Python 3.6). Also a put-off call made by another handler, not only
      by the handler's own next one. A candidate (the re-review's prototype, `proto5`): `_jobHere` also takes a non-empty
      line from a `_runHere` or `_drain` frame of the same store and mark, with identity checks only, so a call made
      while put-off calls wait joins them in order. It costs about 0.5 µs on a call of 1 to 2 µs (1.3 to 1.4 µs becomes
      1.8 to 2.1 µs; 27 to 28 µs becomes 33 to 36 µs at 1,000 frames of stack), which 5.17 does not spend on every call
      for an ordering this rare. A plain set, or a `move_to_end()` with nothing to follow, is never put off, so it comes
      before waiting calls with or without it: that is the plan below.
    - **A handler's set inside the commit's queue scan or a reload's swap makes the main call raise `RuntimeError`.** In
      a store with no writer thread, the main thread runs `commitAppendToFile()` or `reload()`; a handler's plain
      `t['h'] = ...` lands in `_dropQueued`'s scan of the queue or in `_replaceRows`. The main call raises
      `RuntimeError` ("deque mutated during iteration", or "OrderedDict mutated during iteration") to the program. The
      rows stay queued, and the next commit writes them. 5.16 the same: a commit, 17 of 1,060 instructions then and 17 of
      1,177 in 5.17; a reload, 16 of 3,211 and 16 of 3,305; Python 3.6, a commit, 2 of 220. It is the section-lock family
      above, and the plan below, which puts off a set that finds the lock held, covers it.
    - **A process a handler forks inside a forked child's plain commit, which returns from the handler, reports a failed
      write.** It goes on with its copy of the commit, after the fork hook has emptied its queue, so its
      `popleft()` raises and it prints "Failed to write at commitAppendToFile ... pop from an empty deque". Every row is
      fine. 5.16 the same (the re-review's `forkreturn.py` with a handler that only forks). Plan: cover it as 5.17 covers a
      `clear()`'s write, an interval rewrite and a one-column delete's commit, with a fork-inside-a-write test of a plain
      commit.
    - **A handler's `move_to_end()`, or a set, inside the last commit of a close or an exit is never written.** A forked
      child with another writer's change unread sets `c` and calls `close()`; a handler lands at the close commit's fsync
      and calls `move_to_end('b')`. The move is put off and made after the commit, and its 2 lines stay queued: memory
      says `[a, c, b]`, the file `[a, b, c]`. The exit path is the same. A handler's plain set there leaves its line
      queued and not written. 5.16: the set the same, the move waited for ever (the re-review's `g2.py`, Python 3.14; a
      second `close()` of a closed store commits nothing in 5.16 as well, so it does not count). Plan: close and exit of a
      store with no writer thread commit once more when a handler queued lines during their last commit. It needs its
      own design: a handler can land in that commit too, and the cases where a close or an exit writes nothing (the
      held-file limitation above) must stay as they are.
- Why: found by the 5.17 build, by reading, while putting off a handler's commit or reload inside such a write; not worse
  than 5.16, and a fix touches every section of the write path, which wants its own sweep.
- Plan (5.18): a set, delete, pop or move that finds the section lock held by its own thread, in a store with no writer
  thread, is put off until the write below it ends, as a commit is (`_jobHere`, looked up only then: only a handler or a
  Ctrl-C's leftover finds the lock held); with a handler sweep over a forked child's and a closed store's writes. It also
  puts off a set (and a delete or move) made while calls are waiting, so that what a handler does to the store after
  such a call happens after it. Where to find the waiting calls is to design: the line is in the job's frame now (5.17),
  so a stack walk, 0.5 to 40 µs a call (the 5.17 final review's `cost2.py`), on a set that takes 1 to 2 µs; a count on the
  store would read the same for another thread. A call that returns a value (`pop()`, `popitem()`, a read after the
  handler's own set) cannot simply be put off, and a put-off delete of a missing key could no longer raise `KeyError`:
  the sweep decides. A closed store's fork hook (for a process a handler forks inside its write) is part of it.
  - **A design check comes first**, before that put-off is extended to sets, deletes, pops and moves (the 5.17 design check's
    question D). It compares it with the other way to remove this whole class: a store with no
    writer thread stops running its writes on the calling thread. By reading, that is one of two shapes: a forked child's
    store starts its writer thread again at its first write, or `_runHere` runs each job on a helper thread and waits for
    it as `_call` does. Either would remove the put-off line, the frame lookup, the drain, a Ctrl-C's drop, the `os._exit`
    limitation, F4, this entry's other cases, and both cut-write limitations ("A write after close(), or in a forked child, can
    be cut by a Ctrl-C" and "A second Ctrl-C inside a set's clean-up, or a Ctrl-C in a write made after close() or in a
    forked child, can leave a lock held"). Its costs are real: the thread's lifetime in
    a child, a thread hand-off on every commit of a child, and the exit path, where a new thread may not start (a thread
    starts and joins from an `atexit` handler on Python 3.11, 3.12 and 3.14, which is where TSVZ runs its exit hook
    (checked); later in the interpreter's shutdown it may not). Nothing of it was built or run.
  - **A `writeLock` that knows its holder**, used only to let go of a hold a cut write left (the approved limitation of
    a lock left held, Python 3.14), is the other candidate, to be measured: see that entry.
- Revisit when: 5.18.
- Noted: 2026-11-11, found by the 5.17 build; the 5.17 final review's findings added the same day; the design check's
  condition added 2026-11-12; a deferral, not asked.

### A Ctrl-C that a TSVZedLite catches in a .tsvz write can leave its file append-locked until it closes
- Kind: deferred
- What the operator sees: a program keeps a `TSVZedLite` on a plain `.tsvz` file open (it holds the file open for its
  lifetime), sets rows, and catches `KeyboardInterrupt` and goes on. A Ctrl-C that lands in `_specAppendTo`'s `finally`,
  in `_seekRawStart` before the unlock or in `_unlockFile` before its unlock call, leaves the Lite's file append-locked
  until the Lite closes or the process ends. Then the Lite's own `clear_file()` waits for ever (it opens the part again,
  and its rewrite lock waits for the append lock); another process's append (`appendTabularFile`, `tsvz set`, a
  `TSVZed`'s commit) waits until the Lite closes, and so does a scrub or a rewrite by anyone. The Lite's own later sets
  go on (a lock taken again through the same open file description does not conflict; by reading). Real SIGINTs from
  another process 2 to 30 ms into a set loop, caught, 1,920 each: the append lock was still held after 104 on 3.14 (72 in
  `_seekRawStart`, 32 in `_unlockFile`), 91 on 3.11, and 120 on 5.16 (3.14). The Lite's `clear_file()` hung in all 54
  held cases of the 3.14 run (55 of 55 on 5.16) and another process's `appendTabularFile` in all 44 it was tried on (it
  returned once the Lite closed). On macOS and other flock systems, by reading: the held lock is a whole-file exclusive
  `flock`, so other processes' reads wait too.
- Why: not worse than 5.16 (`_specAppendTo`, `_unlockFile`, `_specWrite` and `_specAppendPayload` are the same text in
  5.16 and 5.17); found by the review of the third fix round, after that round put every write of a `TSVZed` under its
  clean-up from the step that takes its file. The Lite's `.tsvz` append is the one site whose file outlives the call
  (`_specAppendTo(f, ...)` with its long-lived `self.fileObj`), so a close cannot drop its lock.
- Plan (5.18): fix at source. The Lite appends through `_specAppendPayload`, which returns the same `(offset, before,
  after)`, so the lock sits on a descriptor of its own that a `with` closes, and a cut clean-up drops the lock with the
  close. A nested `try` in `_specAppendTo` alone would close the 72 landings in `_seekRawStart` and not the 32 in
  `_unlockFile`. Check the Lite's reads at offsets through its kept `fileObj` after appends made through another
  descriptor, and add a test like the third round's.
- Revisit when: 5.18.
- Noted: 2026-11-12, found by the review of the third fix round; a deferral, not asked.

### A Ctrl-C caught in get_file_obj() or release_file_obj() can leave writeLock held, and the store's next commit then waits for ever
- Kind: deferred
- What the operator sees: a program holds the store's file with `get_file_obj('ab')`, writes, lets go of it with
  `release_file_obj(f)` in a `finally`, and catches `KeyboardInterrupt` and goes on. A Ctrl-C that lands in
  `release_file_obj`'s `finally` before its nested `try` (in `getFileUpdateTimeNs()`, or in `_unlockFile`), at the
  function's entry, or in `get_file_obj` just after `writeLock.acquire()`, leaves `writeLock` held, and usually the file
  locked. The store's next commit, on its writer thread, then waits for ever, and so does the commit at exit. Real
  SIGINTs from another process into a `get_file_obj()`/`release_file_obj()` loop of a running `.tsv` store, caught, 960
  each: the next commit hung after 331 on 3.14 and 314 on 3.11 (files on tmpfs), 44 (4.6%) on 3.14 with the files on an
  XFS disk, where an fsync takes real time (16 in `getFileUpdateTimeNs`, 13 in `_unlockFile`, 15 at `get_file_obj`'s
  acquire gap, where the writer thread held `writeLock` for its own fsync), and 311 on 5.16 (3.14, tmpfs).
  `get_file_obj`, `release_file_obj` and `_startWrite` are the same text in 5.16 and 5.17. (A second Ctrl-C, or an error
  and a Ctrl-C, inside `get_file_obj`'s `except` clause leaves the same: `_unlockFile(locked)` there is unprotected.)
- Why: not worse than 5.16, and a Ctrl-C landing in the code that takes or lets go of a lock is the family of the
  approved lock-held limitation; found by the review of the third fix round.
- Plan (5.18): fix at source. Nest `release_file_obj`'s `finally` (the time noted, then the unlock, then the close, then
  `_fileObjHeld = False` and `writeLock.release()` last, innermost), and `get_file_obj`'s `except` path the same: one
  Ctrl-C anywhere in them then still lets go of everything, which closes about 300 of the 331 hangs on tmpfs and 29 of
  the 44 on disk. The acquire gap and the function's entry cannot be closed with a `try` (whatever takes or lets go of a
  lock is itself code a Ctrl-C can land in), and go with the other candidate, a `writeLock` that knows its holder: a
  hold by this thread with `_fileObjHeld` false and no job on the stack is a leftover (see the lock-held entry).
- Revisit when: 5.18.
- Noted: 2026-11-12, found by the review of the third fix round (a second opinion's lead, run); a deferral, not asked.

### A parent that forks and ends by os._exit does not write the rows it queued last
- Kind: known limitation
- What the operator sees: a program sets rows, forks a child that outlives it, and ends by `os._exit`, as a
  daemonizing double fork does. The rows it queued in its last `append_check_delay` before the fork (0.01 s by
  default) are written by neither: the child drops them at the fork, since a parent normally writes them. In 5.14 the
  child's explicit `commitAppendToFile()` wrote them (twice where the parent wrote them too), and its close and exit
  wrote nothing, its own rows included; 5.15 writes the child's own rows there. The README says: commit before
  forking a child that outlives its parent.
- Why: the child cannot tell a parent that will write them from one that will not. Committing in a hook before every
  fork would cost a write and an fsync per `os.fork()`, and would wait for ever where the forking thread holds the
  store's lock or its file (`get_file_obj()`).
- Revisit when: someone loses rows this way.
- Approved: 2026-11-10, the user chose "Approve as KL (Recommended)": a parent that forks and ends by `os._exit`
  loses the rows it queued last; the README says to commit before forking.

### A TSVZClient with no server, used in a forked child, waits for ever at its first write
- Kind: deferred
- What the operator sees: a program opens a `TSVZClient` on a file no `tsvz serve` handler serves (the client then
  works on the file in its own process, through a local engine) and forks; in the child, the first write through that
  client (`c['x'] = ...`) never returns. The engine's writer thread (`_ServeWriter`) is not in the child. Checked on
  5.14 and 5.15, Python 3.14 and 3.6, `.tsv` and `.tsvz` (the 5.15 plan's `probe/forkclient.py`: no return in 10 s).
- Why: 5.15's fork hook resets each `TSVZed`; the client's engine is another object with its own thread, and its
  reset needs its own design and tests.
- Plan (5.18): built on 5.15's fork hook (`_storesAfterFork`): each live `_ServeWriter` (`_SERVE_OPEN_WRITERS`)
  drops its inherited queue in the child and starts its thread again, either at its first `submit` there (a pid check
  once a request) or in the hook; measured to settle which.
- Revisit when: 5.18.
- Noted: 2026-11-10, found by the 5.15 plan; a deferral, not asked.

### A compressed file whose text has no final newline takes an appended member as lines of its own
- Kind: known limitation
- What the operator sees: a `.tsv.gz` (or `.bz2`, `.xz`, `.zst`) written by hand or by another tool whose text
  does not end in a newline: when another process appends a member, a `TSVZed` reading it from where it left off
  takes the member's first line as a row of its own, where a full read joins it onto the file's last line. Memory
  then holds one row more than a reopened store, until its next full read. TSVZ always ends its lines.
- Why: only hand-made files are affected, and TSVZ's own appends to such a file make the same join; fixing it
  means tracking whether the decompressed text ends in a newline, in two places.
- Revisit when: someone keeps compressed stores written by another tool.
- Approved: 2026-10-29, the user chose "Known limitation (Recommended)".

### A multi-member compressed rewrite killed inside a later member's write can bring old rows back
- Kind: known limitation
- What the operator sees: a whole rewrite of a compressed `.tsv` file of several members (more than 1 MiB of
  text) that is killed (kill -9, OOM, power loss) inside the write of its second or a later member leaves its
  new members, that member cut short, and then the old file's bytes from there on. A read takes the cut member
  for a killed append: it leaves it out and reads the old members after it, so rows come back as they were
  before the rewrite, deleted keys included, and the next write of a `TSVZed` that read it keeps them. In probe
  runs of 40 random cuts after the first member: gz 7, bz2 2, xz 5, zst 12. A rewrite killed inside its first
  member stops the read (5.4), as 5.3 did.
- Why: decoding cannot tell a killed append from a killed rewrite: both leave a member cut short followed by
  members that decode. Rewrites write over the file in place (so a reader never meets it emptied), which is
  what leaves the old bytes behind.
- Revisit when: someone loses rows or sees deleted keys back after a crash during a rewrite; for example with
  an end-of-stream check for bz2/xz, or a rewrite that cannot leave old members behind it.
- Approved: 2026-11-04, the user chose "A: stop if first member (Recommended)".

### Appends that do not read the store first land after damage, where reads do not reach them
- Kind: known limitation
- What the operator sees: once a compressed `.tsv` file holds damage that is not a later member cut short
  (bytes that do not decode, a member that fails its check, a first member cut short), every read stops there
  (L5). A running `TSVZed` makes the file again from memory at its next write, so
  its rows stay readable (not while that rewrite fails or is refused: see the README's fault tolerance), but a
  one-shot `tsvz set`, `appendTabularFile()`, a `TSVZedLite` or a `TSVZed` opened with
  `monitor_external_changes=False` (for damage that lands after it loaded the file; from 5.16 not in a child that
  `fork()` made, which follows the file: the suite's test from 5.17) appends without reading the file first, so its rows land
  after the damage, where no read reaches them, and the next `TSVZed` or scrub that makes the file again drops
  them. 4.9 to 5.3 do the same, also after a later member cut short (a writer
  killed mid-append), which from 5.4 reads are no longer stopped by: it is left out and the members after it
  are read. A first member cut short still stops reads. A `TSVZed` that did read the file can meet the same:
  a writer that takes no lock (TSVZ before 5.1, another tool) and dies mid-write in the moment between the
  store's locked read and its append leaves damage the store did not see, and its rows land after it (2 of 400
  stress trials).
- Why: covering them would make every such append read the whole file first; today `tsvz set` is one append.
  A writer that takes no lock cannot be waited for.
- Revisit when: someone mixes one-shot appends with crashing writers on one compressed file.
- Approved: 2026-10-29, the user chose "Known limitation (Recommended)"; 2026-11-04, "Add one line to the entry
  (Recommended)" for a writer that takes no lock and dies between a `TSVZed`'s locked read and its append. Narrowed in
  5.16: a forked child's store follows the file whatever `monitor_external_changes` says (the user chose "Hotfix 5.16
  with M", 2026-11-11).

### A file removed just as a write starts is followed as if rotated
- Kind: known limitation
- What the operator sees: when another process removes a `TSVZed`'s `.tsv`-family file, the store's next write
  makes it again from memory, keeping every row. If the removal lands in the microseconds between a rewrite's or
  an append's check that the file exists and its open, the store instead makes a new empty file there and reads
  it over memory, as it follows a rotation that leaves an empty file in place: the rows already in the file go,
  and the store holds only the rows still waiting to be written when it happened and those set after it.
- Why: closing the window needs a second way of opening the file, and the failure-injection tests all use that
  open as their seam (a prototype broke 33 of them); the window is microseconds wide and needs a removal by
  another process.
- Revisit when: someone removes or rotates store files while they are written and loses rows this way.
- Approved: 2026-10-29, the user chose "Record both (Recommended)".

### A rewrite without --x-header can put a row equal to H on line 1
- Kind: known limitation
- What the operator sees: a header-less `.tsv`-family file whose line 1 a `TSVZed` changes (after
  a delete in a one-column store, or when it makes a damaged, removed or emptied file again) can
  get a row equal to a header H as its line 1 (`a\t1\nid\tv\na\t\n` becomes `id\tv\n`). After that, `tsvz`
  commands with `--x-header H` take that row for the header: `tsvz keys --x-header 'id\tv'` shows nothing, though the
  row stays in the file and reads without `--x-header` show it. Scrubs (`tsvz scrub`, `scrubTabularFile`, a handler)
  and any other `TSVZed` rewrite of a healthy file never change line 1 (a scrub adds a header you pass with
  `verifyHeader=False`, which is not a row; a `TSVZed` rewrite widens a line 1 that is the start of its header).
- Why: a write from memory, or a delete taking line 1 out, does not know the header the CLI is given later.
- Revisit when: an operator meets it after one of those writes.
- Approved: 2026-11-03, the user chose "Known limitation (Recommended)". Narrowed in 5.5 (the scrub and a TSVZed
  rewrite of a healthy file keep line 1).

### Readers given other arguments than the scrub's can see a different file after a scrub or a rewrite
- Kind: known limitation
- What the operator sees: a scrub (`scrubTabularFile`, `tsvz scrub`, a `tsvz serve` handler), and a `TSVZed`
  rewrite of a healthy file (which compacts it the same way), decide which lines are dead with their own reader:
  its header, defaults and column count. A reader given other
  ones can see another file afterwards: one given a header that line 1 is not, which
  takes its column count from a line the scrub may have dropped. From 5.10, under a header that line 1 does not hold
  (line 1 a row, or another program's widening of the header), the compaction also keeps what every reader given no
  header, and every reader given line 1 for its header, takes with the scrub's or the store's defaults (or with none): they
  count from line 1. A `TSVZed` delete's commit compacts the same way from 5.10 (5.9's took out only the deleted keys'
  lines), so a reader given other defaults than the store's can see another file after it too, as after a rewrite. A file of one column is the
  exception, from 5.6: line 1 or the first keyed line after it being one cell, it is compacted as one whatever the scrub
  was given, so a reader that takes it as one column keeps its lone keys. A widening or a rewrite can move a cell's value the same way, with no row lost: a store with
  defaults wider than its header writes them into a row (`id`, `a<TAB>1`, header `id<TAB>v`, three-cell defaults gives
  `a<TAB>1<TAB>Q`, so a reader given no defaults gains Q); a bare `#_defaults_#` in a one-column file becomes a reset once it
  widens, so a plain reader's default-filled cell turns empty; and a store with no header, whose defaults are narrower than the
  file, leaves a plain reader without a default-filled cell. Every reader given the scrub's own arguments, of
  any version from 3.39, sees what it saw before. In fuzzing, about 1% of random files and argument sets, where 5.4
  changed far more of these readers' views. From 5.11 the compaction models 3.39's defaults as that reader keeps them
  (cell by cell; a bare `#_defaults_#` at one column is a row to it and resets nothing), so a line 5.10 kept for it by a
  wrong model can go, and a reader given other arguments can see that: `a\na\na\tX\nb\t2\n#_defaults_#\nb\tX\t2\t1\na\t1\t\n`
  scrubbed with header `a` (line 1 holds it) and defaults `#_defaults_#\t\tQ` drops `a\tX`, and a reader given no header but
  those defaults, which reads at three columns, lists `b, a` where it listed `a, b`; no row or cell changes, and every
  reader given the scrub's arguments reads as before (1 file in 6,000 argument sets of the 5.11 review's fuzz).

  A delete in a one-column file keeps every reader's column count from 5.08 (a stand-in key takes the place of a line
  that set one), with one shape left: under `--x-header H` (`tsvz delete`, `pop`, `popitem`, a handler given H), line 1
  of one cell and not H, when the first keyed line after it goes and only rows with a value could stand in for it. No
  stand-in goes there (the command and every reader given no header read line 1's one column, and a stand-in would be a
  row to 3.39's strict reader), so a reader given H counts from the next keyed line and a cell can change for it:
  `id\na\t\n#_defaults_#\t\tQ\nb\t1\n`, H `id\tv`, delete `a`, and `b` goes from `[b, 1]` to `[b, 1, Q]` for that reader.
- Why: what is dead depends on the reader; keeping every line any reader could need would keep most of the lines a
  compaction exists to drop. For the delete: in that shape, a stand-in that keeps the count for the reader given H gives
  3.39's strict reader a row.
- Revisit when: an operator reads one file with several header or defaults settings and loses rows after a scrub
  or a rewrite. Scrub, and open `TSVZed`s, with the arguments the file's readers use. For the delete: someone hits it, or
  when loose files go.
- Approved: 2026-11-04, the user chose "Known limitation (Recommended)", and "One entry for both (Recommended)"
  for covering `TSVZed` rewrites of a healthy file in the same entry; 2026-11-06, "Keep it (Recommended)" for narrowing it in
  5.6 (a file of one column); 2026-11-06, "Known limitation (Recommended)" for each of the three cell cases above;
  2026-11-07, "Known limitation (Recommended)" for the delete case, then a wider set of shapes. Narrowed in 5.08 to the shape
  the stand-in rule leaves (no stand-in from a row with a value after a line 1 of one cell), which the 2026-11-07 approval
  covers. Narrowed in 5.10 (under a header line 1 does not hold, also kept for the readers that count from line 1). Widened
  in 5.11 (2026-11-09, the user chose "Widen it (Recommended)"): a line 5.10 kept only by its wrong model of 3.39's
  defaults can go, and a reader given no header but the scrub's defaults can then list keys in another order.

### A stdout nobody reads can abort or hold the exit
- Kind: known limitation
- What the operator sees: a verbose `TSVZed` (one that prints) whose program's stdout nobody reads (a full pipe, a stopped
  reader) either ends with `Fatal Python error: _enter_buffered_busy` and exit status -6 (134 in a shell), after about 7 s
  (2 s at 5.6), or, when the writer thread is inside a print just as the exit starts, the exit waits with no limit until
  something reads stdout. A second Ctrl-C then abandons the write in progress, as "A second Ctrl-C at exit abandons a write in
  progress" says. No row is lost. 5.6 does the same. The exit's wait (up to 5 s for a print under way, then the thread writes
  to file descriptor 2) helps only a reader that is slow but still moving.
- Why: nothing safe can free a stream lock held by a frozen thread, and bounding the wait would give up a write that may not
  be on disk yet.
- Revisit when: someone hits it.
- Approved: 2026-11-07, the user chose "Known limitation (Recommended)", first for the abort and again for the corrected
  wording that includes the wait.

### A file whose first keyed line is a bare #_defaults_# reads as one column without defaults
- Kind: known limitation
- What the operator sees: a `.tsv`-family file whose first line with a key is a bare `#_defaults_#` (an empty
  defaults line, for example `\n#_defaults_#\na\t1\t2`), read without defaults of two or more cells and without a
  header that line 1 holds (`readTabularFile(p)`, `tsvz read`, a `TSVZed` given no defaults), takes its column count
  from that line: one. The reader then shows a row named `#_defaults_#`; a strict read (`readTabularFile`'s default)
  drops every row of more than one cell, and a loose one shows them whole. Read with the defaults the file's
  program uses (or its header) it reads right. Scrubs and rewrites keep such a file as it is. (A late `setDefaults()` on
  such a file makes a rewrite grow it: see the entry after the move-growth one, "A late setDefaults() on a file whose
  first keyed line has one cell grows it at each rewrite".) A delete of the file's first key (`K\n#_defaults_#\t\n`,
  or `K\n<wider row>\n#_defaults_#\n`) takes that row, or the bare line with it, out of the read: the bare line was the
  row `#_defaults_#` at one column and is a reset at the width the file reads at after the delete. No key's row changes.
- Why: the column count comes from the first keyed line in every version from 3.39, and old and new readers must
  agree on it; skipping a bare defaults line there would make 5.5 readers see other rows than older readers of the
  same bytes.
- Revisit when: an operator meets such a file read without its defaults, or when every reader of these files can be
  changed at once.
- Approved: 2026-11-04, the user chose "Known limitation (Recommended)".

### A write by another process between a move_to_end and its commit can be lost
- Kind: known limitation
- What the operator sees: `move_to_end` follows the file first, so a change another process made before the call is
  kept (5.5 without that read lost it on 8 of 8 tries). A write that lands after the call and before the move's
  lines are committed (one tick, `append_check_delay`, 10 ms by default) can still lose: the commit order decides,
  as for any queued line. Measured on 3.14 (40 tries each, another process writing `o2` while the store moved
  another key to the front): a write 4 ms and 1 ms before the call, none lost; at the call, 26 lost; 1, 3, 5, 8 and
  10 ms after it, 5, 7, 9, 13 and 11 lost; 11 and 13 ms after it, none lost. So the window is about one
  tick, and a write in it lost in 45 of 200 tries after the call, and 26 of 40 at the call.
- Why: the moved rows are queued from memory and written at the next tick, and a write that lands in between is
  not read before them; closing the window would mean holding the file from the call to the commit.
- Revisit when: someone loses a row this way.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### A one-column delete can undo another process's write that lands just before it
- Kind: known limitation
- What the operator sees: in a one-column `.tsv`-family file a `TSVZed` delete is not a line: the next commit
  takes the key's lines out of the file (a whole rewrite). Another process's write of the same key that lands after
  the delete reached the file survives; one that lands before that, within one tick (`append_check_delay`, 10 ms by
  default), is undone by the delete, and a refused rewrite stretches that window to the refusal. After another process's
  whole rewrite the order cannot be told. This is the rule for a queued deletion line at two or more columns too.
- Why: a one-column file cannot write a deletion as a line (a lone key is a row there), so the delete is carried out
  on the file as it is when the commit takes it.
- Revisit when: someone loses a row to another process's delete or write this way.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### Repeated move_to_end of a row read before the defaults grows the file
- Kind: known limitation
- What the operator sees: a row that holds an empty cell the file's defaults would fill (it was read before the
  `#_defaults_#` line, so it reads empty) adds a reset and a defaults line to the file with each `move_to_end`: 300
  moves took a 28-byte file to 11,728 bytes and 600 moves to 23,428. A rewrite takes out about a quarter of that
  (9,037 and 18,037 bytes), and a scrub nothing further. Reads stay correct. Rows filled when the file is opened
  are stable.
- Why: the compaction keeps every `#_defaults_#` line, and changing that rule for this corner adds risk to every
  file.
- Revisit when: someone sees a file grow this way.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### A late setDefaults() on a file whose first keyed line has one cell grows it at each rewrite
- Kind: known limitation
- What the operator sees: a program opens a `.tsv`-family file with no defaults, the file's first keyed line has one
  cell (a bare `#_defaults_#`, or a lone key: `a\nb\t\n`), and the file has a row with an empty cell that the
  defaults would fill. The program then calls `setDefaults()`. Each `hardMapToFile()` after that adds one more reset
  and defaults pair, 28 bytes with one default `P`: 60, 88, 116, 144, 172 bytes over five rewrites of the bare-line
  file, and 50 to 162 of `a\nb\t\n`. In a probe, 59 of 76 files of that shape grew and none of 3,124 others did.
  Reads never changed across the rewrites. Defaults given to the constructor never grew (0 of 5,716). 5.4's size
  stays flat, but after its rewrite a reopen does not match memory. Automatic rewrites are off by default, so it
  takes a program that rewrites.
- Why: the cause is not verified. It looks like the compaction keeping every `#_defaults_#` line, as in the
  move-growth entry above.
- Revisit when: a real store shows the growth.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)" on the wider shape (a first keyed line of one
  cell, not only a bare `#_defaults_#`).

### A wider row that a move writes again is lost to old strict readers
- Kind: known limitation
- What the operator sees: for a file `a\t1\na\t1\t2\nb\t3\n`, `move_to_end('a')` appends `a\t` then `a\t1\t2`. 5.5's
  strict readers see `a`; 3.39 to 5.4 strict readers (which take only a line exactly as wide as the column count)
  lose it. Upgrading the readers cures it.
- Why: only 5.5's readers take a row wider than the column count whole; the move writes the row as it is.
- Revisit when: a mixed fleet of 5.5 writers and older strict readers hits it.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### Deleting many keys of a big one-column file rewrites it once per commit
- Kind: known limitation
- What the operator sees: a `TSVZed` on a one-column file does one whole-file rewrite per commit however many
  deletes that commit holds: about 1 s per commit at 100,000 keys. A program deleting all the time pays that once
  per `append_check_delay`.
- Why: a one-column file cannot write a deletion as a line.
- Revisit when: someone deletes often in a big one-column store.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### A store emptied at one column, then given a wider row first, loses the lone keys after it
- Kind: known limitation
- What the operator sees: after every key of a one-column store was deleted, `t['x'] = ['x', '1']; t['y'] = ['y']`
  writes `x\t1\ny\n`, and a reopened store takes two columns from the first line, so `y` is a deletion there and is
  gone. A lone cell in a two-column file has meant delete in every version. From an empty file 5.4 and 5.5 both drop
  `y` the same way.
- Why: the first keyed line sets the column count, and a lone key at two columns is a deletion in every version.
- Revisit when: someone hits it.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### move_to_end follows the file first, so it waits for the lock and slows beside a busy writer
- Kind: known limitation
- What the operator sees: `move_to_end` reads what another process changed before it queues the rows it moves, so it
  waits for as long as another process holds the file's lock (the read lock has no timeout: 3.000 s in a test where
  the other process held it for 3 s, against 0.000 s with no holder), and slows when another process writes: on
  100,000 rows with a writer at 20 Hz, 28 moves/s and a slowest call of 1.774 s (5.4: 1.2 million moves/s and
  0.008 s). That cost beside a writer is bounded, and nothing livelocks. It also raises KeyError for a key another process
  has deleted, as its docstring says (5.4 succeeded).
- Why: without that read the rows it appends again undo the other process's change (the entry on a write between a
  `move_to_end` and its commit).
- Revisit when: a program starts calling it on a busy store.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### A same-size change by a writer that takes no lock can be missed by a running TSVZed
- Kind: known limitation
- What the operator sees: a running `TSVZed` notices another process's change by the file's modification time and
  size. A change that keeps the size, made by a writer that takes no lock (another tool, TSVZ before 5.1), can go
  unseen. At a 1 ms tick: 39 of 6000 changes in one run and 90 of 4000 in another (43 of 6000 on 5.4, so it is not
  new). At the default 10 ms tick: 44 of 4000 on 5.5 and 42 on 5.4 in one run, 0 of 2500 in another, so the rate
  moves with the state of the machine. Memory then stays stale until the file changes again. TSVZ's own writers
  were not exposed where readers take the read lock, since their rewrites run behind it: 0 of 8000 on 5.5
  and on 5.4, on Linux with open file description locks. Where readers take no read lock (`lockf`-only hosts and
  Windows, see `_readLock`) that reason does not hold: forced `lockf` and `flock` gave 0 of 1500; Windows was not
  measured.
- Why: the cause is not verified. The misses checked had a file time that had moved, so "the time did not move" is ruled
  out for them; the likely cause is that the time moves before the new bytes land, so a read in between takes the old bytes
  with the new time, but that is not proved. Seeing it for certain would mean reading the file again at every
  tick.
- Revisit when: a real store is edited by such a tool.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)"; 2026-11-05, "Known limitation
  (Recommended)" again on the default-tick figures.

### setDefaults() to another cell count changes how the file reads, not the rows memory holds
- Kind: known limitation
- What the operator sees: `setDefaults()` with another cell count than the file's rows (two cells or more set the
  column count a reader takes) leaves memory's rows as they are, and the file unchanged at the commit. A rewrite
  then keeps those rows and adds the defaults line. At two columns a lone key is a deletion, so a store opened with
  such defaults, or given them by `setDefaults()` and then `reload()`, does not hold a one-column file's lone keys
  (`a\nb\n` reads `{}`; a plain open reads the rows). A rewrite of that store (`mapToFile()`, `hardMapToFile()`,
  `rewrite(force=True)`, `rewrite_on_load`, `rewrite_on_exit`, `rewrite_interval` once the store is dirty) keeps every
  key of the file and adds the defaults line: with `defaults=['#_defaults_#', 'P']`, `a\nb\n` becomes
  `a\nb\n#_defaults_#\tP\n` and `a\nb\nc\t1\n` becomes `a\nb\nc\t1\n#_defaults_#\tP\n`; a plain open reads the same keys
  before and after, and the rewrite says nothing. (5.5, and 5.6 until its second fix round, took all but the first lone
  key out of the file with no output: a file is now compacted at its own width, its first keyed line's, never at the
  store's.) `TSVZedLite`'s delete asks the file with its own width too, so `setDefaults()` after the load does not turn
  its lone keys into rows the delete leaves. 5.4 read such a file as `{}` with no defaults at all, and its rewrite took
  out the first lone key too.
- Why: the user chose to allow a `setDefaults()` that changes the column count.
- Revisit when: someone needs the store to hold the lone keys under such defaults (see "A file of lone keys does not
  widen").
- Approved: 2026-11-05, the user chose "Allow it, document it (Recommended)" when asked whether `setDefaults()` may
  change the column count; 2026-11-06, "Fix in 5.6 (Recommended)" on the rows a rewrite took out.


### After a line 1 of one cell, a delete under a header can change a cell for readers given it
- Kind: known limitation
- What the operator sees: two shapes are left, one through the command's door and one with a line 1 of two cells; 5.11
  fixed the store's own doors (Noted, below).
  - **The command's delete, then a store's rewrite that widens line 1.** `tsvz --x-header 'id\tv' delete` (or a handler
    given that header) on `id\na\n#_defaults_#\t\tQ\nb\t1\n`, delete `a`: the command reads line 1 as a row, so no row
    with a value stands in after it (Q3, the approved case in "Readers given other arguments"), and the file is
    `id\n#_defaults_#\t\tQ\nb\t1\n`. A `TSVZed` given `id\tv` reads `b` there as `[b, 1, Q]`, and its next rewrite
    widens line 1 and writes that view for every reader: `id\tv\n#_defaults_#\t\tQ\nb\t1\nb\t1\tQ\n`, and every 5.5 and
    later reader, the command, a `TSVZed` and tsvz-rs read `b` as `[b, 1, Q]`; 3.39's readers keep `[b, 1]` (3 file-header
    cases at depth 3 of the 5.09 sweep).
  - **A line 1 that already names the key (shape 2).** `d\tX\na\nd\tY\tZ\n`, delete `a` under header `q`, in every door:
    every key that could stand in is already named by a valued row before the gap (line 1's own `d\tX`), so no stand-in
    goes there, and 3.39's loose reader given `q`, which counts from the gap's place, goes from `[d]` to `[d, Y, Z]`. From
    5.10 line 1's key stands in when the first keyed line after the gap is a row of it as wide as line 1
    (`d\tX\na\nd\tY\n` becomes `d\tX\nd\nd\tY\n`), and from 5.14 also past defaults lines whose first value is in their
    second cell, or in their third where line 1 has two cells (`d\tX\na\n#_defaults_#\tP\nd\tY\n` becomes
    `d\tX\nd\n#_defaults_#\tP\nd\tY\n`); left are that line wider or narrower than line 1 (as here), another key's line,
    or a defaults line first that stops the search (a reset, or a first value that comes later).
  - No row is lost to any reader in these shapes but the old line-1 row that the widening by a rewrite asked for takes
    (README, "Upgrading to 5.8", item 3).
- Why: no layout serves both readers. A reader given H counts from the line after line 1, so only a one-cell line keeps
  its count there, and any one-cell keyed line is a row to a reader at one column: 3.39's strict reader, which drops
  `b\t1` there, would show `b`. Where the command deletes it reads line 1 as a row, so 5.08 kept 3.39's strict reader's
  view (Q3, approved for the command's door); a store given H that rewrites later writes its own view, as a widening does.
  In shape 2 a stand-in of line 1's key is a row with a value to a reader at line 1's count, and a stand-in from a row
  with a value that is not as wide gives some reader a cell it did not read.
- Revisit when: someone reads such a file with 3.39 or a loose reader given the header and hits one of the two
  shapes, or when loose files go.
- Noted: 2026-11-07, a deferral under the user's standing rule that deferrals are noted, not asked. 2026-11-08: 5.09
  fixes one of the shapes (3.39's strict reader in the `TSVZed` delete + commit door); 5.10 fixes shape 2 when line 1's key's
  next line is as wide as line 1. 2026-11-09: 5.11 builds the user's answer of 2026-11-08 to Q-A, "A: stand-in in the
  store's own delete": in the `TSVZed` and `TSVZedLite(strict=False)` doors given H, a row with a value stands in after a
  line 1 of one cell (`id\nb\n#_defaults_#\t\tQ\nb\t1\n`), the store and readers given H keep `[b, 1]`, the store's rewrite
  writes the same bytes whether the delete was committed first or not (N1), and so does a `TSVZedLite` delete followed by a
  store's rewrite; 3.39's strict reader given no header or line 1 gains `b` there (the kind approved as Q4). The rest is
  deferred to 5.12 under the same rule.
- Approved: 2026-11-09, the user chose "Known limitation (Recommended)" for the two shapes left after 5.11 (no layout
  serves both readers; the command's door is the approved Q3).

### After a line 1 of two or more cells, a delete under a header can reorder keys for readers given it
- Kind: known limitation
- What the operator sees: under a header that line 1 is not (`q`, or line 1 and one more cell), a delete of the first keyed
  line after line 1 (`tsvz --x-header`, `TSVZedLite(strict=False)`, a `TSVZed` delete and commit) leaves a one-cell
  stand-in. From 5.12 line 1's key stands in where its next line comes first after the place and is as wide as line 1,
  and the readers given the header keep the order. Where line 1's key comes next at another width, the stand-in is chosen
  as in 5.11 and comes first to the readers given the header: `d\tX\nc\nd\t1\t2\na\n` (wider) and `d\tX\tW\nc\nd\t1\na\n`
  (narrower), delete `c`, write `d\tX\na\nd\t1\t2\na\n` and `d\tX\tW\na\nd\t1\na\n`. The store's memory lists `d, a`; a
  reopened store, `TSVZedLite` and every loose `readTabularFile` given the header list `a, d`. No row or cell changes, and
  5.11 writes the same bytes. From 5.14 another key's row first stands in where the deleted line had one cell (past
  defaults lines and resets too), and line 1's key is looked for past defaults lines whose first value is in their second
  cell, or in their third where line 1 has two cells (README, "Upgrading to 5.14 from 5.13"). The bytes shown are the
  command's and `TSVZedLite`'s; from 5.14 a `TSVZed` delete and commit also compacts away the stand-in key's later line
  without a value (`d\tX\na\nd\t1\t2\n`), which every reader reads as it reads the command's output. Three shapes are
  left, with the same effect (5.12 and 5.13 choose the same stand-in in each):
  - **Line 1's key past a reset (a defaults line without a value).** `d\tX\nc\n#_defaults_#\t\nd\t1\na\n`, delete `c` under
    `q`, writes `d\tX\na\n#_defaults_#\t\nd\t1\na\n`; a reopened store lists `a, d` where memory lists `d, a`.
  - **Line 1's key past a defaults line whose first value comes later.** `d\tX\tW\nc\n#_defaults_#\t\tQ\nd\t1\t2\na\n` writes
    `d\tX\tW\na\n#_defaults_#\t\tQ\nd\t1\t2\na\n`, listed `a, d`. Where no other key has a later line without a value,
    nothing stands in, and the store's readers given the header count from the defaults line, so a cell changes (a count
    change): `d\tX\tW\nc\n#_defaults_#\t\t\tR\nd\t1\t2\n` writes `d\tX\tW\n#_defaults_#\t\t\tR\nd\t1\t2\n`, and a reopened
    store reads `d` as `[d, 1, 2, R]` where it read `[d, 1, 2]`.
  - **Line 1's key at another width past a valued defaults line.** `d\tX\nc\n#_defaults_#\tP\nd\t1\t2\na\n` writes
    `d\tX\na\n#_defaults_#\tP\nd\t1\t2\na\n`, listed `a, d`.
- Why: a one-cell line of line 1's key is a deletion of that key to every reader that counts from line 1, so it can stand
  in only where the key's next line is one they all take. At another width a strict reader given no header drops that line
  and loses the key: `d\tX\nd\nd\t1\t2\na\n` loses `d` for 3.39's, `d\tX\tW\nd\nd\t1\na\n` for every version's. Any other
  one-cell stand-in comes first to the readers given the header. The same holds past a defaults line, and no stand-in of
  `d` serves these shapes: after a reset, a strict reader given no header and defaults of three cells with a value takes
  line 1, and the reset clears the defaults it took it by, so it drops the row of `d` that follows as narrower than its
  count, and the stand-in's deletion of `d` stays: 5.14's, 5.5's, 3.39's and `TSVZed`'s strict readers lose `d` (the review
  checked them with the real readers). For a first value that comes later, 3.39 cuts a defaults line to its count: given a
  count below that value's cell and defaults whose values lie beyond it (which made it take line 1), it holds no value
  after the line, reads strictly and drops a row of another width than its count, so `d\tX\tW\nd\n#_defaults_#\t\tQ\nd\t1\t2\n` loses `d` for its strict
  `readTabularFile(correctColumnNum=2, defaults=['#_defaults_#', '', 'Q'])`; at another width it is the shape above.
- Revisit when: a layout that serves both readers turns up.
- Approved: 2026-11-09, the user chose "Approve (Recommended)". Not asked for by the user: it remains from the 5.11
  deferral "A stand-in after a line 1 of two or more cells can reorder keys for readers given the header", which 5.12
  fixes where line 1's key comes next as wide as line 1. The question also named a defaults line first; that shape was
  then found fixable and moved to a deferral, which narrows this entry (5.14 builds it).
- Noted: 2026-11-09, the 5.14 review (r514a): the part of "a defaults line first" that 5.14 builds is a defaults line whose
  first value is in the second cell (the third where line 1 has two cells). The three shapes above are the rest, and no
  stand-in can fix them: a `d` stand-in loses `d` for strict readers given no header and three-cell defaults with a
  value (a reset), for 3.39's strict reader given a count below the value's cell (a first value that comes later), and
  at another width as above. They are the part of the approved question that the 5.12 docs had moved out as fixable, folded
  back in here, as its question named "a defaults line first". A reset before another key's row is not among them: that
  key stands in past it (5.14).

### A compaction can keep a strict reader's first line of a key where no other key competes for its place
- Kind: deferred
- What the operator sees: a scrub, a `TSVZed` rewrite or a delete's commit can keep a line that no reader needs, once:
  `a\na\tX\na\t2\t\na\t\t2\n` scrubbed with header `id\tv`, defaults `[#_defaults_#, '', '']` and count 2 keeps all four
  lines; 5.11 wrote `a\na\tX\na\t\t2\n`, which every reader (5.14's, 5.5's and 3.39's, strict and loose, given no header,
  the header or line 1, with and without the defaults and the count, a `TSVZed`, `TSVZedLite`, the command) reads as it
  reads the file. The reader planned at the defaults' three columns (5.12) keeps `a\t2\t`, the first line of `a` its
  strict variant takes, as that reader's place, though no other key's row comes after it before `a\t\t2`. Compacting the
  output again never grows it.
- Why: a plan keeps a strict reader's first line of a key it takes as the key's place among the keys that reader reads,
  without asking whether another key's row it takes comes before it. Keeping it only keeps lines in their places, so it
  never loses a row. The 5.15 plan's rule (keep that line only where the planned strict reader takes a row between it
  and its last line of the key) passed the exhaustive differential of its planned readers (0 of 1,310,400 compactions
  read otherwise), but the random differential of the writers (5,000 `.tsv` and 1,500 `.csv` files, 4 argument sets
  each) found readers given other arguments than the writer's that it moved, where 5.14 kept the line, in 4 `.tsv` and
  1 `.csv` argument sets: a reader given line 1 for its header where the writer was given none read `a, d` as `d, a`
  (`a\t1\na\t\t1\nd\t1\n\na\t1\t2\n`, defaults `[#_defaults_#, '', '']`), a strict reader given a count of 3 that the
  writer was not given lost a cell (`c` from `[c, 2, X]` to `[c, 2, '']`), and tsvz-rs read `c, b` as `b, c`. So 5.15
  keeps 5.14's compaction; the suite holds the first two files as a test.
- Plan (5.18): drop that line only where no reader that can read the file otherwise takes a line between: a reader
  given line 1 for its header (also where the writer has no header) and a reader given a count the writer was not
  given, as the random differential lists them; then the exhaustive differential with an empty header and count readers
  for a rewrite, and the random one.
- Revisit when: 5.18.
- Noted: 2026-11-09, found in the 5.12 review (the second half of 5.12's deferral "A compaction keeps a moved-up line only
  from the deletion that ends the other reader's life of the key", whose first half 5.14 builds); a deferral, not asked.
  2026-11-10: the 5.15 plan's rule was built and taken back, as above.

### A TSVZedLite delete looks at the file's first lines every time
- Kind: deferred
- What the operator sees: unless the program gave a column count of two or more, each `delete`, `pop`
  and `popitem`, whatever was read first, takes the file's rewrite lock to read its first lines, which say the width; at two columns or more the
  delete is then the appended line. Measured on a `.tsv` of 200k rows: 29 us (5.5: 6.3 us, 5.6 before its second fix round:
  17.7 ms, the whole file read under the exclusive lock); of 2M rows: 31 us (before: 171 ms). On a `.tsv.gz` the first
  member is read whole, so the cost follows its size: 2.5 ms for 200k rows in one member, 30 ms for 2M (before: 42 ms and
  411 ms; 5.5: 12 us). In a file of one column the delete is a whole rewrite (see "A one-column delete rewrites the whole
  file").
- Why: the width is a fact of the file and another writer can change it, so the delete asks the file; a cached width
  would be a second answer to the same question. A member of a compressed file cannot be read in part by this reader.
- Revisit when: a store deletes in a hot loop on a big compressed file, or on a plain file where 30 us is too much.

### mapToFile() at 20 Hz beside an appender loses rows
- Kind: deferred
- What the operator sees: a `TSVZed` calling `mapToFile()` 20 times a second while another process appends to the file with
  `appendTabularFile` (which takes the lock) loses some of the appended rows (733 of 4068 in the review's test), the same as
  5.5.
- Why: the cause found is that `mapToFile()` runs with `sync=False`: it does not read what was appended since its last
  look, and writes deletion lines for the rows it has not read yet. Not fixed here: a forced rewrite that reads first is a
  change to every route that calls it.
- Revisit when: a program mixes forced rewrites at a high rate with appenders.

### No deterministic test of the read gate
- Kind: deferred
- What the operator sees: nothing; the gate that keeps a reader out while a rewrite is mid-way (byte 2 of the lock layout)
  is covered by stress runs and the conformance cases, not by a test that forces the interleaving.
- Why: nobody has written it yet. It needs no hook: a child process can hold byte 2 of the lock layout, and the test checks
  that a read blocks until the child lets go.
- Revisit when: the lock layout changes or a stress run fails in the gate.

### legacyfuzz does not know the widening exception
- Kind: deferred
- What the operator sees: nothing in the library; the reviewer's fuzzer reports a mismatch when a rewrite widens line 1,
  because it still holds the 5.5 rule that a rewrite of a healthy file only drops lines.
- Why: the fuzzer is a development tool; the exception is documented in the README and tested directly.
- Revisit when: the fuzzer is next used for a release.

### A writer with no lock loses rows to a rewrite
- Kind: known limitation
- What the operator sees: `rewrite(force=True)` at 10 Hz on 10k rows beside a shell `>>` lost 229 of 1174 appended rows,
  and a hot loop 4495 of 13313. 5.5 lost 0 on a clean store and 181 of 1175 on a dirty one. Writers that lock lost 0.
- Why: a rewrite replaces the file, so it cannot see an append that takes no lock.
- Revisit when: someone needs it.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)".

### A one-column delete rewrites the whole file
- Kind: known limitation
- What the operator sees: `tsvz delete` and `TSVZedLite`'s delete on a one-column file cost one full rewrite each, because such
  a file cannot hold a deletion line (a lone key is a row there); two-column files still append.
- Why: correctness; a batch delete is a new API nobody needs yet.
- Revisit when: someone deletes in bulk from a big one-column file.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)".

### A file of lone keys does not widen
- Kind: known limitation
- What the operator sees: with a wider header or defaults on a one-column file with lone keys, a rewrite leaves it one
  column and keeps every key. With defaults of two cells or more, a store reads none of the lone keys (they are deletions
  at its width) and the rewrite adds the defaults line. With a wider header, line 1 is not widened (a warning says the file
  holds rows with no value), and the store prints "Header mismatch" at each open; it reads the lone keys as rows unless its
  defaults or the first row after line 1 give it a wider count (`id\na\t1\nb\n`), when it reads them as deletions and the
  file keeps them all the same. No row is lost. The file widens once every row holds a value. (From 5.08 a `TSVZedLite`
  delete of that first row keeps the store's count: a stand-in, `id\nb\t\nb\n`, so b stays a deletion to it.)
  From 5.11 a key whose only line 3.39's strict reader takes holds no value also keeps line 1 one column, with the same
  warning, when a later line gives it a value but is wider than the header (`id\nb\t\nb\t\t1\n` under `id\tv`): at the
  header's two columns that reader would drop the wider line and take `b\t` as a deletion, and lose `b` (5.6 to 5.10 widened).
- Why: a value-less row at two columns is a deletion, so marking one needs a format change that every reader, the spec and
  tsvz-rs would have to learn.
- Revisit when: the format next changes.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)". The Lite delete case: the same answer, the same day
  (5.08 closes that case). The widening refused for 3.39's strict reader: 2026-11-08, Q-C, the user chose "Refuse the
  widening (Recommended)".

### A row deleted from a one-column file under wider defaults or a wider header stays as its key
- Kind: known limitation
- What the operator sees: on a one-column file opened by a `TSVZed` with wider defaults (`a\nb\nc\t1\n`,
  `defaults=['#_defaults_#', 'P']`), a deleted row that held a value stays as `c<TAB>` after a rewrite
  (`a\nb\nc\t\n#_defaults_#\tP\n`): the store shows it gone, a plain reader shows c with an empty value. So with a wider
  header: `id\na\t1\nb\n`, `header='id\tv'`, `del t['a']` leaves `id\na\t1\nb\na\t\n`. `tsvz delete`, a handler and a
  `TSVZedLite` ask the file, so they take the key's lines out and leave nothing, whatever was read first. Two exceptions for a
  `TSVZedLite`: a program that gave `correctColumnNum` of two or more is not asked about, so the delete is the appended line; and
  under a header that line 1 isn't, a copy of the key on line 1 stays, in the header's place. From 5.11 the deleted key's own value line goes at a rewrite
  when it is as wide as the deletion (5.10 kept `c\t1` before the `c\t`: one more line per deleted key); one of another
  width stays with it (`c\t1` and `c\t\t` under defaults of three cells).
- Why: a file that stays one column has no way to mark that deletion, so it needs a format change.
- Revisit when: the next format change.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)", and "Widen the entry (Recommended)" for the wider
  header.

### 3.39's strict reader of a one-column file sees a new row when its last lone key is deleted
- Kind: known limitation
- What the operator sees: a one-column file whose only lone key is deleted and only wider rows are left: `a\nb\t1\n`,
  `tsvz delete F a` (or a `TSVZedLite` or `TSVZed` delete). 3.39's strict `readTabularFile` reads at one column and drops
  `b\t1` as wider, so it showed only `a`; after the delete it shows `b`. 5.5 and later readers show `b` as `[b, 1]` before
  and after. 5.6 does exactly the same (its bytes `b\t1\n`, 5.8's `b\nb\t1\n`), in the same 5,235 cases of the depth-4
  delete sweep. From 5.11 the same reader also gains a stand-in key after a store's own delete under a header that line 1
  is not: `TSVZed(p, header='id\tv')` (or `TSVZedLite(strict=False)`) on `id\na\n#_defaults_#\t\tQ\nb\t1\n`, delete `a`,
  writes `id\nb\n#_defaults_#\t\tQ\nb\t1\n`, and 3.39's strict reader given no header or line 1 shows `b` as `[b]`, where it
  showed `a`; the store and readers given the header keep `[b, 1]`. When `b`'s later line is wider than the header
  (`b\t1\t2`), the stand-in also keeps line 1 one column at a rewrite (Q-C, "A file of lone keys does not widen").

  From 5.12 the same reader given a count of one (`readTabularFile(p, strict=True, correctColumnNum=1)` with no header, or
  with a header and `verifyHeader=False`) can also gain line 1's key after a delete under a header line 1 is not, where line
  1 has two cells or more and line 1's key now stands in: `d\tX\nc\nd\t1\na\n`, delete `c` under `q` or `d\tX\tz`
  (`tsvz --x-header`, `TSVZedLite(strict=False)`, a `TSVZed` delete and commit), writes `d\tX\nd\nd\t1\na\n` (5.11:
  `d\tX\na\nd\t1\na\n`), and that reader, which takes only the one-cell lines, shows `d` as `[d]` beside `a`, where 5.11
  left it `a` alone. In the 5.12 review's exhaustive delete sweep (123,480 files, each under two headers, through
  `tsvz --x-header H delete`), 1,690 outputs differ from 5.11's: for this reader 720 gain a key under 5.12 and not under
  5.11, 930 gain one under both (`d\tX\nc\nd\t1\na\t1\n`: 5.11 gives it `a`, 5.12 `d`), and 40 keep their keys but list
  them in the store's order instead of their own (`d\tX\nc\nd\t1\na\nd\n`: `a, d` becomes `d, a`). Every other reader given
  a count of one (3.39's loose, 5.5's and later strict or loose) reads all 1,690 as before.

  From 5.14 the same reader given a count of one can also gain the key of another key's row that stands in, after a line 1
  of two cells or more, where the deleted line had one cell and that row, which holds a value, comes first after it (past
  defaults lines and resets too): `d\tX\nc\nb\t1\t2\na\n`, delete `c` under `q` or `d\tX\tz`, writes `d\tX\nb\nb\t1\t2\na\n`
  (5.13: `d\tX\na\nb\t1\t2\na\n`), and that reader shows `b` as `[b]` beside `a`, where 5.13 showed it `a` alone. The
  5.14 build's own sweep of 434,340 `tsvz --x-header H delete` runs found 3,600 where this reader gains the stand-in's key and
  180 where it lists its keys in the store's order instead of its own; the stand-in past a reset
  (`d\tX\nc\n#_defaults_#\t\nb\t1\t2\na\n`) adds about 2,016 more reader runs of the review's second sweep (116,676
  files, each under two headers, 233,352 runs). In that second sweep 5.14's output differs from 5.12's in 20,536 runs, and
  this reader, given no header or a header and `verifyHeader=False`, and any of five defaults sets, gains a key in
  88,112 reader runs (5.14's total, line 1's key past a defaults line included) and lists the store's order in 3,840. It
  loses no key, and no cell changes for it. Every reader from 5.5 on reads these files as 5.12 did or better.

  "3.39's strict reader" in this entry stands for every strict reader before 5.5: 4.1 to 5.4 take a line only when it
  is exactly as wide as their count, as 3.39 does, and given a count of one they gain the same keys in the same files,
  from 5.12 (line 1's key) and from 5.14 (the stand-in's key). The 5.14 final review ran 3.39's, 4.9's, 5.2's and
  5.4's on every output of its delete sweep that differs from 5.13's (65,070 files under three headers, seven doors,
  2.19 million runs): 3.39's, 4.9's and 5.2's each gain a key in 41,088 reader runs and list the store's order in
  1,080; 5.4's does when given no defaults (25,088 and 1,080; given defaults with a value it takes wider rows and
  gains nothing). None loses a key or reads a cell otherwise.
- Why: no layout serves both: 5.5's and later readers keep `[b, 1]` only if the file reads at one column or at two, and
  either needs a keyed line that 3.39's strict read takes as a row of `b`. A bare `#_defaults_#` would keep one column, but
  it is a row named `#_defaults_#` to every reader at one column. For the 5.12 delete: the stand-in must be one cell to
  keep the count of the readers given the header, and the only keys whose one-cell line gives this reader nothing new are
  keys with a later line without a value, which come first to the store's own readers (5.11's order change). The key it
  gains is one the store has, and it loses none. The same holds for the 5.14 stand-in: another key's row that comes first
  after the deleted line is the one key no reader that counts from line 1 has read, so its one-cell line deletes nothing
  they read and keeps the store's own order; the only reader that can see it takes a one-cell line as a row. Any other
  one-cell stand-in is a key that comes first to the store's readers (5.13's order change, which only a stand-in of this
  kind avoids).
- Revisit when: nothing in the format can fix it; upgrading the reader to 5.5 or later cures it.
- Approved: 2026-11-07, the user chose "Known limitation (Recommended)". The store's own delete: 2026-11-08, Q-A, the user
  chose "A: stand-in in the store's own delete (Recommended)". The delete under a header after a line 1 of two cells or
  more (5.12): 2026-11-09, the user chose "Keep, widen the KL (Recommended)". The stand-in of another key's row (5.14):
  2026-11-09, the user chose "Widen it, keep the fix (Recommended)". Every strict reader before 5.5 (4.1 to 5.4) named in
  this entry: 2026-11-10, the user chose "Name them in the entry (Recommended)".

### pip 9 cannot upgrade from or uninstall 5.5 and earlier
- Kind: known limitation
- What the operator sees: With pip 9, the pip of EL7 and EL8 (`pip3 --version`), upgrading from 5.5 (5.05) or earlier and `pip3 uninstall TSVZ` of those releases stop with `configparser.DuplicateOptionError: ... option 'tsvz' in section 'console_scripts' already exists`, and nothing changes: the installed release keeps working. Run `pip3 install -U pip` once, then upgrade or uninstall. With `--user` the same happens, except that `pip3 install --user -U TSVZ` with no version does not stop: it installs the new release, which works, and leaves the old release's metadata behind. `pip3 list` then shows the old release after `pip3 uninstall TSVZ`, and the next uninstall stops with that error. To avoid this, run `pip3 install --user -U pip` once first. If it has already happened, run that, run `pip3 uninstall TSVZ` until it says TSVZ is not installed, and install again.
- Why: pip 9 parses the installed release's `entry_points.txt` with a parser that folds case, and nothing in a new release can
  change the old metadata.
- Revisit when: EL's stock pip stops being 9, or never.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)".

### The `TSVZ` spelling in a venv with a space in its path
- Kind: known limitation
- What the operator sees: With pip 10 or later, in a venv whose path has a space or is too long for the kernel's `#!` line, the `TSVZ` spelling does not start (the error varies: "bad interpreter", `import: command not found`), because pip gives a script file a plain `#!` line: use `tsvz`. With pip 9 no spelling starts in such a venv, as in 5.5. uv and system or `--user` installs are not affected.
- Why: pip writes a plain `#!<python>` line into a script file and a `/bin/sh` wrapper only for an entry point. A launcher that delegates to the lowercase twin overwrites it on a case-insensitive disk under uv. One that execs `mcmd` or `mssh` works, but prints that name for `-V`, breaks `python3 $(which multiCMD)`, and TSVZ has no such sibling.
- Revisit when: someone hits it.
- Approved: 2026-11-06, the user chose "Known limitation (Recommended)".
