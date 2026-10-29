# Limitations

Approved known limitations and deferrals. Reviewers: read this first, and don't raise these again.

### A read in another thread can drop a lockf write lock
- Kind: deferred
- What the operator sees: where TSVZ cannot take an open file description lock (macOS and other systems that
  are not Linux, or a Linux file system that refuses those locks), it locks a file with `lockf`, and closing
  any descriptor of that file in the process drops the lock. A read of the store in another thread of the same
  process (a handler's, another `TSVZed`'s, a `readTabularFile`) can then drop a write's lock while the write
  holds it, and another process's write can land in the middle of it. On Linux with open file description
  locks (the usual case) this does not happen.
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
  another tool) shows it empty. A `del` of a key in that moment finds no key and writes nothing, so the key is live
  again once the rewrite ends. 4.9 and main do the same. TSVZ 5.0's own whole rewrites write over the file and
  then cut it, so a reader never meets them emptied (it can meet, for a moment, a key's old row after its new one),
  and a read cut short by damage is read again under the lock when the store can open the file for writing.
- Why: the fix is for reads to take a shared lock and wait out another process's rewrite, which changes how every
  read locks (spec §18.6 asks for such a lock); that is a design change of its own.
- Revisit when: someone runs two writers that both rewrite the same file.
- Approved: 2026-10-29, the user chose "Defer (Recommended)".

### get_file_obj('ab') on a compressed store unlocks before the trailer
- Kind: deferred
- What the operator sees: `get_file_obj('ab')` and `release_file_obj()`, the 3.39 API, hand a program the store's
  file through its codec; on release the lock is let go of before the codec writes its trailer (and, for bz2 and
  xz, its data), so another writer appending in that gap can corrupt the stream. 3.39, 4.9 and main do the same.
  `TSVZed`'s own writes do not use them.
- Why: only programs that call this API on a compressed store while another process appends are affected; the
  fix (close the codec writer first, keeping the lock through a duplicated descriptor) touches the `lockf`
  bookkeeping.
- Revisit when: someone uses `get_file_obj()` on compressed stores.
- Approved: 2026-10-29, the user chose "Defer (Recommended)".

### A compressed member cut short by a writer killed mid-append can end its cut line with garbage
- Kind: deferred
- What the operator sees: a writer killed part way through an append to a `.tsv.gz` (more rarely `.xz`) leaves a
  member cut short; once another member is appended after it, Python's GzipFile decodes some of that member's bytes
  as the rest of the cut line before it fails at the damage. A read then holds that one row with a garbage value
  (in 72 of 200 random cases on gzip, 2 of 200 on xz, none on bz2; 4.9 and main do the same), and the first write
  of a `TSVZed` that read it (a rewrite, or its first append, which makes the damaged file again) writes it back.
- Why: the fix is a reader of its own for compressed full reads (`readTabularFile`, `TSVZed`, the CLI) that
  decodes member by member, uses a member only once it checks out and keeps only the whole lines of a cut one,
  holding each member's text until it is checked: a reader redesign of its own.
- Revisit when: someone finds a garbage value in a compressed store after a crash.
- Approved: 2026-10-29, the user chose "Defer (Recommended)".

### Appends that do not read the store first land after damage, where reads do not reach them
- Kind: known limitation
- What the operator sees: once another writer has left a compressed `.tsv` file damaged, every read stops at the
  damage (L5). A running `TSVZed` makes the file again from memory at its next write, so its rows stay readable
  (not while that rewrite is refused: see the README's fault tolerance), but a one-shot `tsvz set`, `appendTabularFile()` or a `TSVZedLite` appends without reading the file first, so its
  rows land after the damage, where no read reaches them, and the next `TSVZed` or scrub that makes the file again
  drops them. 4.9 and main do the same.
- Why: covering them would make every such append read the whole file first; today `tsvz set` is one append.
- Revisit when: someone mixes one-shot appends with crashing writers on one compressed file.
- Approved: 2026-10-29, the user chose "Known limitation (Recommended)".
