# Limitations

Approved known limitations and deferrals. Reviewers: read this first, and don't raise these again.

### A read in another thread can drop a lockf write lock
- Kind: deferred
- What the operator sees: where TSVZ cannot take an open file description lock (macOS and other systems that
  are not Linux, or a Linux file system that refuses those locks), it locks a file with `lockf`, and closing
  any descriptor of that file in the process drops the lock. A read of the store in another thread of the same
  process (a handler's, another `TSVZed`'s, a `readTabularFile`) can then drop a write's lock while the write
  holds it, and another process's write can land in the middle of it. A `lockf` lock belongs to the process, so
  two writes from threads of one process (two stores on one file, or a store and `appendTabularFile`) do not
  exclude each other either: one starting on a missing file can write its header twice, read as a row `key`
  (main does the same). On Linux with open file description locks (the usual case) none of this happens.
- Why: 4.9 tried a close gate (a close waits while another thread holds the lock) and took it out again
  (cbe1829): five review rounds kept finding hangs in it. A fix needs its own design, for example `flock` on
  macOS, checked on a Mac.
- Revisit when: someone runs TSVZ writers on macOS with threads or a handler, or a Mac is at hand to check a
  `flock` design.
- Approved: 2026-10-24, the user chose "Record as deferred (Recommended)".

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

### A delete during another process's whole rewrite can be lost
- Kind: deferred
- What the operator sees: while another process rewrites a `.tsv`-family file, a `TSVZed`'s background read takes
  no lock and can see it part way: a key whose line the rewrite has moved may be missing for a moment (an in-place
  rewrite of a plain `.tsv` writes its lines one by one), and a writer that empties the file first (3.39, 4.9,
  5.0's scrub and clear, another tool) shows it empty. A `del` of a key in that moment finds no key and writes
  nothing, so the key is live again once the rewrite ends. 4.9 and main do the same. A `TSVZed`'s own whole
  rewrites (from 5.0) write over the file and then cut it, and so do (from 5.1) `scrubTabularFile`,
  `clearTabularFile`, `tsvz scrub`, `tsvz clear` and `TSVZedLite.clear_file()` of a `.tsv`-family file, so a
  reader never meets them emptied (it can meet, for a moment, a key's old row after its new one), and a read cut
  short by damage is read again under the lock when the store can open the file for writing.
- Why: the fix is for reads to take a shared lock and wait out another process's rewrite, which changes how every
  read locks (spec §18.6 asks for such a lock); that is a design change of its own.
- Revisit when: someone runs two writers that both rewrite the same file.
- Approved: 2026-10-29, the user chose "Defer (Recommended)".

### Appends that do not read the store first land after damage, where reads do not reach them
- Kind: known limitation
- What the operator sees: once a compressed `.tsv` file holds damage that is not a member cut short (bytes that
  do not decode, a member that fails its check, the old bytes a whole rewrite cut short leaves after its new
  members), every read stops there (L5). A running `TSVZed` makes the file again from memory at its next write, so
  its rows stay readable (not while that rewrite fails or is refused: see the README's fault tolerance), but a
  one-shot `tsvz set`, `appendTabularFile()`, a `TSVZedLite` or a `TSVZed` opened with
  `monitor_external_changes=False` (for damage that lands after it loaded the file) appends without reading the
  file first, so its rows land after the damage, where no read reaches them, and the next `TSVZed` or scrub that
  makes the file again drops them. 4.9 to 5.3 do the same, also after a member cut short (a writer killed
  mid-append), which from 5.4 reads are no longer stopped by: it is left out and the members after it are read.
- Why: covering them would make every such append read the whole file first; today `tsvz set` is one append.
- Revisit when: someone mixes one-shot appends with crashing writers on one compressed file.
- Approved: 2026-10-29, the user chose "Known limitation (Recommended)".
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

### A scrub run with other defaults than the store's program can change how early rows read
- Kind: known limitation
- What the operator sees: when a `.tsv` holds an empty `#_defaults_#` line (one that turns defaults off) and rows
  with empty cells before any defaults line, a scrub run with other defaults than the program that reads the store
  (for example `tsvz scrub f.tsv`, with none, for a program that opens it with constructor defaults) can change how
  those rows read for that program: before the scrub its own defaults fill their empty cells; after it, the reset
  line the scrub writes before the rows keeps them empty.
- Why: a scrub fills rows with its own caller's defaults and does not keep where each row sat relative to a reset;
  keeping that exactly would mean recording, per row, whether it was read under a reset. Run the scrub with the
  defaults the store's program uses.
- Revisit when: an operator hits it.
- Approved: 2026-11-01, the user chose "Known limitation (Recommended)".

### Scrubs and rewrites read rows at one column width (deferred to the width design)
- Kind: deferred
- What the operator sees: a scrub (`scrubTabularFile`, `tsvz scrub`, a `tsvz serve` handler), and a `TSVZed`'s
  rewrite, read every row of a `.tsv`-family file at one column count, and write the rows back at it: cells past it
  are lost, and when the cut leaves only empty cells the row is written as a deletion line, so the key is gone
  (`id\tv\na\t\t7` scrubbed with its header becomes `id\tv\na\t\n`; `b\t2\nc\t\t\t2` becomes `b\t2\nc\t\n`;
  `a\t\t\t1` scrubbed with defaults `[D, '']` becomes `a\t\n`). Where the count comes from, and so what is cut:
  - Defaults given to the scrub (`defaults=`, `--x-defaults`) narrower than the rows, with no header to take the
    count from: the rows are cut to their width (`a\t1\t2\t3` with `[D, 'D']` becomes `a\t1`). A strict scrub
    drops the rows of another width only when the given defaults are all empty; with a non-empty cell it cuts as a
    loose one does. A `#_defaults_#` line of the file narrower than the rows, after the first row, cuts no row
    and is written back as it is (as the file's first line it does cut them: see below). Same in 5.1.
  - A file with no header and no live row, scrubbed with defaults narrower than its own `#_defaults_#` line: the
    line is cut to the width of the given defaults (`#_defaults_#\tX\tY\tZ\na\t\n` with `[D, 'D']` is written
    `\n#_defaults_#\tX\n`). New in 5.2: 5.1 left such a file as it was.
  - A row wider than the header (written by hand or another tool), at a scrub or a `TSVZed` rewrite (after a
    change, or `hardMapToFile()`): `id\tv\na\t1\t2` becomes `id\tv\na\t1`. A strict scrub drops the row, and the
    other rows are written. `tsvz scrub --x-strict` of `id\tv\na\t1\t2` with no `--x-header` writes `id\tv\n`, the
    header line being read as a key. Same in 5.1 at a scrub, at a rewrite after a change, and at a `TSVZed` open
    of a file that holds a key with two values. New in 5.2: a `TSVZed` open (`rewrite_on_load`, the default) also
    rewrites a file that holds a key's row and its deletion, so the cut becomes permanent at a plain open, with
    nothing changed: `id\tv\na\t1\t2\nb\t1\nb\t\n` becomes `id\tv\na\t1  \n`, and `id\tv\na\t\t7\nb\t1\nb\t\n`
    becomes `id\tv\na\t  \n`, the 7 lost (the spaces pad the in-place rewrite). With `rewrite_on_load=False` the
    next exit or periodic rewrite does it.
  - A header-less row wider than the first row of the file: `a\t1\nb\t1\t2` becomes `a\t1\nb\t1`. Same in 5.1.
  - A file of one-column keys that holds one line of two cells (`a\nb\nc\nb\t\n`, which
    `appendTabularFile(p, ['b', ''])` writes): every key is written as a deletion line, `a\t\nb\t\nc\t\n`, and all
    are gone. Same in 5.1.
  - A narrow `#_defaults_#` first line that is not bare (`#_defaults_#\tX\na\t1\t2\t3`): the reader takes its
    count from it, and the rows are cut (`a\t1`). Same in 5.1.
  - A bare `#_defaults_#` first line, or one behind an empty first line (`\n#_defaults_#\na\t1\t2`), read without
    defaults: the count is 1, `readTabularFile(p)` returns a row named `#_defaults_#` and drops the rows
    (`strict=True`, the default) or cuts them to their key (`strict=False`); a scrub writes that (`a`). Defaults of
    two or more cells read it right. 5.2's scrub never turns a file that reads right into one; scrubbing one keeps
    it one (`\n#_defaults_#\na\t1\t2` becomes `\n#_defaults_#\na\n`). Same in 5.1.
- Why: one column-width rule for reads, scrubs and rewrites gets its own design after 5.2, tested with the
  differential scrub fuzzer. Patching one trigger at a time moves the cut around.
- Revisit when: the width design (after 5.2 ships).
- Approved: 2026-11-03, the user chose "bb3e697 + small fix (Recommended)", "In the width design (Recommended)" and
  "One umbrella entry (Recommended)".

### A strict scrub leaves a file whose read dropped a line for its width
- Kind: deferred
- What the operator sees: a strict scrub (`strict=True`, `tsvz scrub --x-strict`) of a `.tsv`-family file whose
  strict read finds no live row, and dropped a line for its width (`id\tv\nb\t\t2` with header `id\tv`; `a\t1`
  with defaults of three cells), leaves it as it is. A writer that appends keys, deletes them all and scrubs over
  and over then sees the file grow without limit, as in 5.1. A scrub that is not strict compacts it, and cuts the
  line: `id\tv\nb\t\t2` becomes `id\tv\nb\t\n`, and b is lost (see the width entry above). The headed example is
  `tsvz scrub`'s only with `--x-header id\tv`; without it line 1 is a row, so the strict read finds the live key
  `id`, drops the wider line, and writes `id\tv\n`.
- Why: compacting it would write the file as an empty store and lose the dropped line, which is data (d9bf316).
- Revisit when: the width design (after 5.2 ships).
- Approved: 2026-11-03, the user chose "Deferred to width design (Recommended)".

### A rewrite without --x-header can put a row equal to H on line 1
- Kind: known limitation
- What the operator sees: a header-less `.tsv`-family file rewritten by `tsvz scrub` without `--x-header`, by
  `scrubTabularFile`, or by a `TSVZed` rewrite can get a row equal to a header H as its line 1
  (`a\t1\nid\tv\na\t\n` becomes `id\tv\n`). After that, `tsvz` commands with `--x-header H` take that row for the
  header: `tsvz keys --x-header 'id\tv'` shows nothing, though the row stays in the file and reads without
  `--x-header` show it. Only `tsvz scrub --x-header H` guards against it (it puts an empty first line in front).
- Why: the library's rewrites do not know the header the CLI is given later.
- Revisit when: the library's rewrites get the same guard (for example with the column-width design).
- Approved: 2026-11-03, the user chose "Known limitation (Recommended)".
