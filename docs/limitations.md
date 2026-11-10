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
  - Python 3.6: a child that `fork()` made while a thread held a TSVZ lock keeps that lock until it exits, so the
    parent's later writes of the file wait for the child (an inherited read lock holds off only rewrites, and on
    `flock` systems appends too; an inherited write lock holds off reads as well). 3.7 and later drop it in the
    child;
  - where only `lockf` works (on Linux, a kernel or file system that refuses open file description locks: a kernel
    before 3.15, or a RHEL/CentOS 7 kernel older than 3.10.0-704, i.e. 7.0 to 7.4's own: the backport is in
    3.10.0-704, and 7.9's 3.10.0-1160 has them, checked on CentOS 7.9; elsewhere, a file
    system that refuses `flock`), readers take no lock, because a shared `lockf` would replace the process's own
    write lock; there a delete can still be lost, a read in another thread of the process can still drop a write's
    lock, and two threads' writes do not exclude each other (a new file's header can be written twice), as in 5.2;
  - Windows is unchanged: readers take no lock.
- Why: TSVZ cannot change older programs or other tools, and a writer that empties the file first or ignores the
  lock gives a reader nothing to wait on. Python 3.6 has no at-fork hook. On `lockf`-only hosts `lockf` is kept so
  that 3.39 to 5.2 writers stay excluded; `flock` would not exclude them on Linux.
- Revisit when: an operator loses a delete with only 5.3 or later programs on a file, still runs 3.39 to 5.0
  writers beside 5.3, or runs on hosts without open file description locks.
- Approved: 2026-11-02, the user chose "One known limitation (Recommended)" for older writers that empty the file
  before or without the lock, readers from 5.1 and earlier, and Python 3.6 forks, and "Keep lockf (Recommended)"
  for Linux without open file description locks; 2026-11-04, "Accept the wider entry (Recommended)" for this
  wording (readers from 5.2 and earlier, `lockf`-only hosts and Windows folded in); 2026-11-04, "Correct the wording
  (Recommended)" for the RHEL/CentOS 7 kernel range found on the EL7 test VM.

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

### A Ctrl-C at exit can leave the program waiting after the write is done
- Kind: deferred
- What the operator sees: rarely, a program that exits while a `TSVZed` still has a write to finish prints "TSVZ: the
  write to X is being finished; Ctrl-C again to abandon it" after a Ctrl-C, the write completes and is on disk, and the
  program then never ends; a second Ctrl-C ends it. On any Python: estimated at about 1 in 30,000 such Ctrl-Cs on 3.6 and
  fewer on 3.14 (an estimate from the injection runs, not a field rate). 5.11 does the same. In the suite,
  `TSVZ_lock_test.py::test_ctrl_c_at_exit_of_a_store_with_a_queued_row_keeps_waiting_and_writes_it` fails this way in
  0.1–0.4% of runs on 3.6 (9 of 6,000 at 5.12, 5 of 6,000 at 5.11, `-n 64`) and 1 of 4,000 on 3.14: its Ctrl-C comes
  0.7 s in, exactly 7 of the exit's 0.1 s wait steps, when the main thread runs Python code between two waits.
- Why: the store's threads signal each other with `threading.Event`, which holds its Condition's lock while Python code
  runs. A KeyboardInterrupt can land where the `with` block's exit never runs (CPython bpo-29988), so the main thread keeps
  the lock; the writer's next `set()` waits for it, and the main thread waits for the writer. Reproduced by raising the
  interrupt at each step of one wait round: 7 of 90 points hang on 3.6, 4 of 60 on 3.14 (scratchpad r512flake, 2026-11-09).
- Plan (5.13): a small flag that never holds a lock across Python code (a plain attribute, and a lock that `set()`
  releases) for the job's done signal, `_workerDone`, `_wake` and `shutdownEvent`; `_waited` becomes a plain wait. The
  patch is written and passes both suites with 0 hangs in 6,000 + 4,000 test runs and 0 of 150 injection points.
  `shutdownEvent` stops being a `threading.Event` (same methods), so that change gets its lesson. Check `_ServeWriter`,
  which uses the same pattern, in the same round.
- Revisit when: 5.13.
- Noted: 2026-11-09, found when the 5.12 suite failed once on 3.6 (passed on reruns); a deferral, not asked.

### A write after close(), or in a forked child, can be cut by a Ctrl-C
- Kind: known limitation
- What the operator sees: a `TSVZed` has no writer thread once it is closed, nor in a child that `fork()` made, so
  a `rewrite()`, `mapToFile()` or `hardMapToFile()` called there runs in the caller's thread, as in 4.9. A Ctrl-C
  during it can leave a `.tsv`-family file damaged, a compressed one unreadable; if the program goes on, the next
  rewrite makes it again from memory under the merge rule.
- Why: calls after `close()` and writes from forked children are rare, and covering them means starting a writer
  thread again in both cases, with its lifetime to get right.
- Revisit when: someone writes through a closed store or from a forked child and hits it.
- Approved: 2026-10-29, the user chose "Known limitation (Recommended)".

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
  `monitor_external_changes=False` (for damage that lands after it loaded the file) appends without reading the
  file first, so its rows land after the damage, where no read reaches them, and the next `TSVZed` or scrub that
  makes the file again drops them. 4.9 to 5.3 do the same, also after a later member cut short (a writer
  killed mid-append), which from 5.4 reads are no longer stopped by: it is left out and the members after it
  are read. A first member cut short still stops reads. A `TSVZed` that did read the file can meet the same:
  a writer that takes no lock (TSVZ before 5.1, another tool) and dies mid-write in the moment between the
  store's locked read and its append leaves damage the store did not see, and its rows land after it (2 of 400
  stress trials).
- Why: covering them would make every such append read the whole file first; today `tsvz set` is one append.
  A writer that takes no lock cannot be waited for.
- Revisit when: someone mixes one-shot appends with crashing writers on one compressed file.
- Approved: 2026-10-29, the user chose "Known limitation (Recommended)"; 2026-11-04, "Add one line to the entry
  (Recommended)" for a writer that takes no lock and dies between a `TSVZed`'s locked read and its append.

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
    (`d\tX\na\nd\tY\n` becomes `d\tX\nd\nd\tY\n`); left are that line wider or narrower than line 1 (as here), another
    key's line or a defaults line first.
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
  5.11 writes the same bytes. Another key's row first, and a defaults line first, are deferred (the next two entries).
- Why: a one-cell line of line 1's key is a deletion of that key to every reader that counts from line 1, so it can stand
  in only where the key's next line is one they all take. At another width a strict reader given no header drops that line
  and loses the key: `d\tX\nd\nd\t1\t2\na\n` loses `d` for 3.39's, `d\tX\tW\nd\nd\t1\na\n` for every version's. Any other
  one-cell stand-in comes first to the readers given the header.
- Revisit when: a layout that serves both readers turns up.
- Approved: 2026-11-09, the user chose "Approve (Recommended)". Not asked for by the user: it remains from the 5.11
  deferral "A stand-in after a line 1 of two or more cells can reorder keys for readers given the header", which 5.12
  fixes where line 1's key comes next as wide as line 1. The question also named a defaults line first; that shape was
  then found fixable and moved to a deferral (below), which narrows this entry.

### After a line 1 of two or more cells, a delete before a defaults line reorders keys for readers given the header
- Kind: deferred
- What the operator sees: `d\tX\nc\n#_defaults_#\t\tQ\nd\t1\na\n` under header `q` (or `d\tX\tz`), delete `c` through a
  `TSVZed` delete and commit, `tsvz --x-header` or `TSVZedLite(strict=False)`: 5.11 and 5.12 write
  `d\tX\na\n#_defaults_#\t\tQ\nd\t1\na\n`. The store's memory lists `d, a`; a reopened store, `TSVZedLite` and every loose
  `readTabularFile` given the header list `a, d`. No row or cell changes.
- Why: 5.12 tries line 1's key only where its next line comes first after the place, and does not look past a defaults
  line. Past one it depends on that line and the reader's defaults: `d\tX\nd\n#_defaults_#\nd\t1\na\n` loses `d` for every
  strict reader given no header and defaults such as `[#_defaults_#, P, Q]` (the bare line resets them, so `d\t1` is too
  narrow there). In the example file the defaults line keeps `d\t1` read: by hand, `d\tX\nd\n#_defaults_#\t\tQ\nd\t1\na\n`
  serves every reader the 5.12 docs round ran (eight defaults sets, and four- and five-cell defaults), except that 3.39's
  strict `readTabularFile(correctColumnNum=1)` given no header gains `d`, which the widened 3.39 entry covers.
- Plan (5.13): let line 1's key stand in past defaults lines when its next line is as wide as line 1 and every planned
  reader still takes that line after them; then run the exhaustive delete-door shapes again, with defaults lines.
- Revisit when: 5.13.
- Noted: 2026-11-09, found in the 5.12 docs round (5.11 writes the same bytes); a deferral, not asked.

### After a line 1 of two or more cells, a delete before another key's row reorders keys for readers given the header
- Kind: deferred
- What the operator sees: `d\tX\nc\nb\t1\t2\na\n` under header `q` (or `d\tX\tz`), delete `c` through a `TSVZed` delete and
  commit, `tsvz --x-header` or `TSVZedLite(strict=False)`: 5.11 and 5.12 write `d\tX\na\nb\t1\t2\na\n`. The store's memory
  lists `b, a`; a reopened store, `TSVZedLite` and every loose `readTabularFile` given the header list `a, b`, and the
  readers given a count of one but 3.39's strict one list `d, a, b` where they listed `d, b, a`. No row or cell changes.
- Why: where another key's row comes first after the place, the stand-in is a key whose later line holds no value (`a`),
  as in 5.11, and it comes first to the readers that count from the place.
- Plan (5.13): in the header doors, let the first row after the place stand in before a later key without a value. By
  hand, `d\tX\nb\nb\t1\t2\na\n` keeps the order for every reader the 5.12 docs round ran (headers `q`, `d\tX\tz` and
  `id\tv`, eight defaults sets): `b` comes first to the readers given the header anyway, and a reader that counts from line
  1 reads it as the deletion of a key not yet read. It costs 3.39's strict `readTabularFile(correctColumnNum=1)` given no
  header a row (`b` as `[b]`, where 5.11's `a` gave it nothing new), the kind "3.39's strict reader of a one-column file
  sees a new row when its last lone key is deleted" covers since its 5.12 widening. Then run the exhaustive delete-door
  shapes again.
- Revisit when: 5.13.
- Noted: 2026-11-09, found in the 5.12 final review (5.11 writes the same bytes); a deferral, not asked.

### A compaction keeps a moved-up line only from the deletion that ends the other reader's life of the key
- Kind: deferred
- What the operator sees: a scrub, a `TSVZed` rewrite or a delete's commit can keep a line that no reader needs, once.
  `id\nb\tX\nb\t\nb\t\na\t1\t\tX\na\t2\t\t2\n#_defaults_#\n` scrubbed with header `a`, defaults `[#_defaults_#, P, Q]` and count 2
  keeps `b\tX` (`id\nb\tX\nb\t\nb\t\na\t2\t\t2\n#_defaults_#\n`; 5.11 wrote `id\nb\t\na\t2\t\t2\n#_defaults_#\n`). A reader's plan can also
  keep a strict reader's first line of a key where no other key competes for its place. The delete door is the same
  family: `d\tX\nc\nd\t1\na\na\n` under header `q` (or `d\tX\tz`, `id\tv`), a `TSVZed` delete of `c` and its commit write `d\tX\nd\nd\t1\na\na\n` (5.11
  wrote `d\tX\na\nd\t1\na\n`): it keeps the `a\na` pair, since `agrees()` rejects moving the last `a` up for a reader given no
  header, which reads both lines as deletions of a key it never had. Every reader reads both files alike. The file is a
  line or two larger, and compacting a scrub's output again never grows it (5.12 review: in the delete doors 405 of
  123,480 files' outputs were larger than 5.11's and 355 smaller, no reader worse).
- Why: `agrees()` in `_legacyCompactOnce` accepts a line moved up into another line's place only when the deletion that
  ends the other reader's life of the key is the one the line came from. A later deletion of the same key with no row of
  it between would do as well: that reader reads the key deleted at the moved line, and read it dead from the first
  deletion on. A move it rejects only keeps lines in their places, so it never loses a row.
- Plan (5.13): accept that later deletion in `agrees()`, then measure it on the exhaustive planned-reader and delete-door
  shapes again (the 5.12 review's relaxed variant made 408 of 1.31M outputs smaller and no reader worse; on the first
  trace above it writes one line more than 5.11, so the build needs its own measurement).
- Revisit when: 5.13.
- Noted: 2026-11-09, found in the 5.12 review; a deferral, not asked.

### A one-column compaction given a count equal to the defaults' count plans the same reader twice
- Kind: deferred
- What the operator sees: a scrub or a `TSVZed` rewrite of a file the one-column rule reads as one column, given defaults
  of two cells or more and a count (`correctColumnNum`, a store's given column count) equal to the defaults' count, plans
  the reader at that count twice: once from the one-column rule's branch and again from the loop over the counts given.
  `_legacyCompactReaders([b'a', b'c'], 1, True, [D, 'P'], '\t', 'utf8', False, (2,))` returns `(2, True, False, [D, 'P'])`
  twice. The bytes and every reader's view are the same; the cost is the second plan's time. 3,600 of 97,200 argument
  sets of the 5.12 review's fuzz do it, in 5.11 and 5.12 alike.
- Why: the two branches each add the reader, and neither looks at the other's.
- Plan (5.13): in `_legacyCompactReaders`, do not add a reader the list already holds (the loop over the counts given does
  that already for the readers given no defaults); one line, then the argument sets again.
- Revisit when: 5.13.
- Noted: 2026-11-09, found in the 5.12 review (pre-existing in 5.11); a deferral, not asked.

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
- Why: no layout serves both: 5.5's and later readers keep `[b, 1]` only if the file reads at one column or at two, and
  either needs a keyed line that 3.39's strict read takes as a row of `b`. A bare `#_defaults_#` would keep one column, but
  it is a row named `#_defaults_#` to every reader at one column. For the 5.12 delete: the stand-in must be one cell to
  keep the count of the readers given the header, and the only keys whose one-cell line gives this reader nothing new are
  keys with a later line without a value, which come first to the store's own readers (5.11's order change). The key it
  gains is one the store has, and it loses none.
- Revisit when: nothing in the format can fix it; upgrading the reader from 3.39 cures it.
- Approved: 2026-11-07, the user chose "Known limitation (Recommended)". The store's own delete: 2026-11-08, Q-A, the user
  chose "A: stand-in in the store's own delete (Recommended)". The delete under a header after a line 1 of two cells or
  more (5.12): 2026-11-09, the user chose "Keep, widen the KL (Recommended)".

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
