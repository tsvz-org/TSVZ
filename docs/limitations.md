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
  `verifyHeader=False`, which is not a row).
- Why: a write from memory, or a delete taking line 1 out, does not know the header the CLI is given later.
- Revisit when: an operator meets it after one of those writes.
- Approved: 2026-11-03, the user chose "Known limitation (Recommended)". Narrowed in 5.5 (the scrub and a TSVZed
  rewrite of a healthy file keep line 1).

### Readers given other arguments than the scrub's can see a different file after a scrub or a rewrite
- Kind: known limitation
- What the operator sees: a scrub (`scrubTabularFile`, `tsvz scrub`, a `tsvz serve` handler), and a `TSVZed`
  rewrite of a healthy file (which compacts it the same way), decide which lines are dead with their own reader:
  its header, defaults and column count. A reader given other
  ones can see another file afterwards: one that takes the file as one column (its first keyed line is one cell),
  for which lone keys are rows the scrub dropped as deletions, and one given a header that line 1 is not, which
  takes its column count from a line the scrub may have dropped. Every reader given the scrub's own arguments, of
  any version from 3.39, sees what it saw before. In fuzzing, about 1% of random files and argument sets, where 5.4
  changed far more of these readers' views.
- Why: what is dead depends on the reader; keeping every line any reader could need would keep most of the lines a
  compaction exists to drop.
- Revisit when: an operator reads one file with several header or defaults settings and loses rows after a scrub
  or a rewrite. Scrub, and open `TSVZed`s, with the arguments the file's readers use.
- Approved: 2026-11-04, the user chose "Known limitation (Recommended)", and "One entry for both (Recommended)"
  for covering `TSVZed` rewrites of a healthy file in the same entry.

### A file whose first keyed line is a bare #_defaults_# reads as one column without defaults
- Kind: known limitation
- What the operator sees: a `.tsv`-family file whose first line with a key is a bare `#_defaults_#` (an empty
  defaults line, for example `\n#_defaults_#\na\t1\t2`), read without defaults of two or more cells and without a
  header that line 1 holds (`readTabularFile(p)`, `tsvz read`, a `TSVZed` given no defaults), takes its column count
  from that line: one. The reader then shows a row named `#_defaults_#`; a strict read (`readTabularFile`'s default)
  drops every row of more than one cell, and a loose one shows them whole. Read with the defaults the file's
  program uses (or its header) it reads right. Scrubs and rewrites keep such a file as it is, so it does not get
  worse.
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

### tsvz delete and TSVZedLite's delete in a one-column file do not delete
- Kind: deferred to 5.6
- What the operator sees: `tsvz delete FILE b` on a file `a\nb\nc\n` exits 0 and the file grows to `a\nb\nc\nb\n`:
  `b` stays, since a lone key is a row in a one-column file. `TSVZedLite`'s `del` appends the same line. 5.4 writes
  the same bytes. `TSVZed` now deletes in such a file (it takes the key's lines out), so the two differ. `tsvz
  serve` does the same on 5.5 and 5.4 (exit 0, the key stays, `has` exits 0).
- Why: both append a deletion line, as in 5.4, and a lone key is a row in a one-column file; 5.5's change was to
  `TSVZed`.
- Revisit when: right after 5.5.
- Approved: 2026-11-05, the user chose "Deferred to 5.6 (Recommended)".

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

### A header wider than the file's line 1 is not written into line 1
- Kind: deferred to 5.6
- What the operator sees: a program gives a header wider than the file's line 1 (it added a column: line 1
  `id\tv`, header `id\tv\tw`). 5.5 keeps the file's width and prints "Header mismatch" at every open. Rows already in
  the file, and rows whose last cell is empty, raise IndexError when the program reads the new column. Even
  `hardMapToFile()` leaves line 1 as it is, so the way out today is editing line 1 by hand. 5.4 widened line 1 at a
  rewrite. No data is lost.
- Why: 5.5 no longer writes its header over a line 1 that is not that header (5.4 did, and a reader given no header
  lost the row line 1 was).
- Revisit when: 5.6, which lets a rewrite widen line 1 to the header the program gives.
- Approved: 2026-11-05, the user chose "README now, fix in 5.6 (Recommended)".

### move_to_end follows the file first, so it waits for the lock and slows beside a busy writer
- Kind: known limitation
- What the operator sees: `move_to_end` reads what another process changed before it queues the rows it moves, so it
  waits while another process holds the file's lock (3.000 s against 0.000 s in a test), and slows when another
  process writes: on 100,000 rows with a writer at 20 Hz, 28 moves/s and a slowest call of 1.774 s (5.4: 1.2 million
  moves/s and 0.008 s). The wait is bounded and nothing livelocks. It also raises KeyError for a key another process
  has deleted, as its docstring says (5.4 succeeded).
- Why: without that read the rows it appends again undo the other process's change (the entry on a write between a
  `move_to_end` and its commit).
- Revisit when: a program starts calling it on a busy store.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### A same-size change by a writer that takes no lock can be missed by a running TSVZed
- Kind: known limitation
- What the operator sees: a running `TSVZed` notices another process's change by the file's modification time and
  size. A change that keeps the size, made by a writer that takes no lock (another tool, TSVZ before 5.1), can go
  unseen: 39 of 6000 changes at a 1 ms tick (43 of 6000 on 5.4, so it is not new). Memory then stays stale until the
  file changes again. TSVZ's own writers are not exposed: their rewrites run behind the read lock.
- Why: the cause is not verified; the likely one is that the file's time moves before the new bytes land, so a read
  in between takes the old bytes with the new time. Seeing it for certain would mean reading the file again at every
  tick.
- Revisit when: a real store is edited by such a tool.
- Approved: 2026-11-05, the user chose "Known limitation (Recommended)".

### A one-cell set after another process empties a one-column file acts as a delete
- Kind: deferred to 5.6
- What the operator sees: another process empties a one-column file while the store has nothing queued. The store's
  column count becomes unknown, and its next one-cell `set` acts as a delete with a warning, so that write is lost.
  1 of 8000 fuzz cases (`bdel o0; async; aset a2` at one column); 5.4 does the same.
- Why: a lone key is a deletion unless the column count says one column, and it says nothing after the file is
  emptied.
- Revisit when: 5.6, with the one-column delete fix deferred there.
- Approved: 2026-11-05, the user chose "Deferred to 5.6 (Recommended)".

### setDefaults() to another cell count changes how the file reads, not the rows memory holds
- Kind: known limitation
- What the operator sees: `setDefaults()` with another cell count than the file's rows (two cells or more set the
  column count a reader takes) leaves memory's rows as they are, and the file unchanged at the commit. A rewrite
  keeps the data and adds the defaults line. A reopen given those defaults then reads the file at the new count,
  which can read `{}` (a one-column file's lone keys are deletions at two columns), while a plain reopen reads the
  rows. 5.4 was worse: memory was `{}` after the call, and its rewrite wiped the file.
- Why: the user chose to allow a `setDefaults()` that changes the column count.
- Revisit when: someone loses rows to it.
- Approved: 2026-11-05, the user chose "Allow it, document it (Recommended)" when asked whether `setDefaults()` may
  change the column count.
