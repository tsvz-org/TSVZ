# TSVZ

**[tsvz.org](https://tsvz.org)**: the format, its specification and its implementations.

TSVZ is a small, dependency-free library and CLI that keeps an **ordered
key-value store in a delimiter-separated text file**. The first column of each
row is the key; the file behaves like an ordered dictionary that is persisted to
disk as you change it.

**Version 5.8**, Python **3.6+**, one self-contained module (`TSVZ.py`): drop it
next to your code and `import TSVZ`, or install it.

```bash
pip install tsvz          # from PyPI
pip install -e .          # editable, from this directory
```

- With pip 9, the pip of EL7 and EL8 (`pip3 --version`), upgrading from 5.5 (5.05) or earlier and `pip3 uninstall TSVZ` of those releases stop with `configparser.DuplicateOptionError: ... option 'tsvz' in section 'console_scripts' already exists`, and nothing changes: the installed release keeps working. Run `pip3 install -U pip` once, then upgrade or uninstall.
- With `--user` the same happens, except that `pip3 install --user -U TSVZ` with no version does not stop: it installs the new release, which works, and leaves the old release's metadata behind. `pip3 list` then shows the old release after `pip3 uninstall TSVZ`, and the next uninstall stops with that error. To avoid this, run `pip3 install --user -U pip` once first. If it has already happened, run that, run `pip3 uninstall TSVZ` until it says TSVZ is not installed, and install again.
- With pip 10 or later, in a venv whose path has a space or is too long for the kernel's `#!` line, the `TSVZ` spelling does not start (the error varies: "bad interpreter", `import: command not found`), because pip gives a script file a plain `#!` line: use `tsvz`. With pip 9 no spelling starts in such a venv, as in 5.5. uv and system or `--user` installs are not affected.

## Two file dialects, one API

| Extension | Rules |
|---|---|
| `.tsv` `.csv` `.nsv` `.psv` (and any other) | The TSVZ **3.39** format, unchanged. A 3.39 host can read every file this version writes. |
| `.tsvz` `.csvz` `.nsvz` `.psvz` | The **[tsvz-spec-v1](tsvz-spec-v1.md)** append-only key-value log. |

The delimiter follows the extension: tab, comma, NUL, pipe. A trailing
compression suffix (`.gz`, `.bz2`, `.xz`, `.zst`) is allowed on both; `.zst`
needs Python 3.14. For important or sensitive data, keep the part you write to
uncompressed and compress parts once they no longer change: a damaged
compressed member can lose every record in it (spec §16.4.2).

`TSVZed`, `TSVZedLite`, `readTabularFile`, `appendTabularFile`,
`appendLinesTabularFile`, `clearTabularFile`, `scrubTabularFile` (and the
`*TSV` aliases) work on both dialects. `t.dialect` tells you which one a store
uses.

## Quick start

```python
import TSVZ

t = TSVZ.TSVZed('people.tsvz', header='id\tname\tscore')
t['alice'] = ['alice', 'Alice', '10']
t['bob'] = 'bob\tBob\t20'
t['alice'] = ['alice', 'Alice', '11']   # last write wins
del t['bob']                            # appends a tombstone
print(t['alice'])                       # ['alice', 'Alice', '11']
t.close()
```

```bash
tsvz set people.tsvz carol Carol 5        # add or update a row (append is the same)
tsvz get people.tsvz carol                # one row; exit 3 if carol is missing
tsvz read people.tsvz                     # every row: a table on a terminal, records in a pipe
tsvz delete people.tsvz carol             # tombstone
tsvz read people.tsvz | tsvz set copy.tsvz -   # copy every row
tsvz clear people.tsvz                    # empty it, keeping the header and markers
tsvz scrub people.tsvz                    # compact it (for .tsvz: archival maintenance)
tsvz verify people.tsvz                   # check #_checksum_*_# segments; exit 4 on a mismatch
tsvz parts events.tsvz                    # list the parts of a multi-part store
tsvz has people.tsvz carol                # exit 0 if carol is live, 3 if not
tsvz pop people.tsvz carol                # print carol's row and delete it
tsvz serve people.tsvz &                  # keep the store loaded; tsvz commands then use it
tsvz stop people.tsvz
tsvz people.tsvz append carol Carol 5     # the 3.39 form still works
tsvz -V
```

## What TSVZ does with `.tsvz` files

- **Reads** everything a conformant writer can produce: markers
  (`#_defaults_#`, `#_strip_trailing_whites_#`, `#_fill_empty_with_default_#`,
  `#_return_defaults_when_missing_#`, ...), escapes (`<sep>`, `<LF>`, `<lt>`,
  `<#>`), tombstones, `#_checksum_<algo>_#` segments (crc32 and every
  fixed-length hashlib algorithm), compressed parts, and **multi-part stores**
  (`store.tsvz.<hex>`, part 0 first, `.rotated` parts skipped). A read starts
  at the last *snapshot part* (one that begins with `#_snapshot_#`, spec
  §19.10, and holds the store's whole state) and skips the history before it;
  `verify` and `parts` still read every part. A store whose parts change while
  they are read (another tool's in-place scrub) is read again, up to three
  times; a read still changing then is incomplete, as an unreadable part is.
- **Writes** one file: the named file, or the highest-numbered part of a
  multi-part store. Rows are written as given (no padding); a lone key is a
  tombstone; a row of empty cells (`k\t\t`) is a live row.
- **Compaction is manual.** `scrubTabularFile` / `tsvz scrub f.tsvz` rewrites a
  single-part store in place (same inode). If another writer commits while it
  runs, it reads the store again; after three tries it writes nothing, with a
  warning (`clearTabularFile` does the same). The automatic rewrite options of
  3.39 (`rewrite_on_load`, `rewrite_on_exit`, `rewrite_interval`, `rewrite()`,
  `mapToFile()`, `hardMapToFile()`) are ignored for `.tsvz`, with a warning.
- **Missing keys:** `t[key]` returns the defaults row while
  `#_return_defaults_when_missing_#` is true (its default); `in`, `get`,
  `setdefault` and `pop` behave like a dict.
- **`#` keys** are stored (`<#>key`). Keys of the form `#_name_#` are marker
  writes: `t['#_defaults_#'] = [...]` sets the defaults, and so does
  `setDefaults([...])` once the object is constructed. `#_snapshot_#` is
  refused (spec §19.10.2).
- **Other writers:** a `TSVZed` notices another process's change by the active
  part's inode, size and times and by the list of parts, and reloads. Rows it
  has queued but not yet written stay in memory across that reload, and a live
  key is never missing while it runs.

## Command line

`tsvz` follows [tsvz-spec-v1 §20](tsvz-spec-v1.md#20-command-line-interface):
`tsvz [OPTION ...] OPERATION STORE [ARG ...]`, with options anywhere before a
lone `--`. The 3.39 form `tsvz STORE [OPERATION] [ARG ...]` works too; a store
named like an operation then needs a path prefix (`./read`).

- **One engine.** Every operation runs through the engine `tsvz serve` uses,
  in its own process when no server runs, so a command prints the same with or
  without a server.
- **stdout carries only rows.** It gets a table on a terminal and records
  otherwise; `--format table|records` overrides. Records use the store's
  delimiter and escaping, so `tsvz read A | tsvz set B -` copies a store of
  the same variant.
- **stderr carries every message.** `-q` prints errors only; `-v` adds detail.
- **Exit status:**
  - 0: done.
  - 1: failed or refused. Also a missing store for `read`, `get`, `scrub`, `verify` and `parts`,
    a store that `set`, `append` or `delete` could not create, and a `.tsvz` part that could not
    be read (the rows of the other parts are still printed).
  - 2: usage error.
  - 3: `get` found a missing key.
  - 4: `verify` found a checksum mismatch.
- **Arguments are literal.** `tsvz set s.tsvz k 'a<b'` stores `a<b`. Values may
  begin with `-` (`-5`); after `--` every argument is positional. `set KEY`
  alone deletes KEY, as in 3.39. `set KEY ''` stores an empty cell in a `.tsvz`
  store; in a `.tsv` file it deletes KEY, as in 3.39.
- **Bulk input.** `set STORE -` and `delete STORE -` read records or keys from
  stdin and append them in one write. An unterminated last line is ignored,
  with a warning.
- **TSVZ extensions:**
  - `--x-header H` and `--x-defaults KEY<d>D1<d>...` decode backslash escapes such as `\t`;
  - `--x-header H` makes line 1 of a `.tsv`-family file its header for every operation
    when line 1 is H: the same number of cells, each equal to H's once the whitespace
    around it is taken off (a trailing empty cell counts: `id\tv\t` is not `id\tv`; a UTF-8
    byte order mark before line 1, as Excel writes one, is skipped). It
    is then never a key, row or count, `scrub` and `clear` keep it, and
    `read --format table` shows it above the rows. When line 1 differs, the file has no
    header and line 1 is a row, with 3.39's "Header mismatch" warning (`--x-strict`: an
    error, exit 1, nothing written). A missing or empty file gets H first. Without
    `--x-header`, line 1 is a row, as in 3.39. (The library's own header check, a
    `header=` given to `readTabularFile`, `appendTabularFile`, `clearTabularFile`,
    `scrubTabularFile` or `TSVZed`, is 3.39's, looser one: each cell of H within the
    cell of line 1 under it, so `idaho\tvalue` passes for `id\tv`.) A scrub never changes
    line 1; a `TSVZed` that writes its memory over a header-less file can put a row equal to
    H there
    ([limitations](docs/limitations.md#a-rewrite-without---x-header-can-put-a-row-equal-to-h-on-line-1)).
  - `--x-strict` / `--x-force` turn 3.39's column and header checks on or off.

## Serving a store

`tsvz serve STORE` keeps a store loaded in one process and answers requests on a
local socket ([tsvz-spec-v1 §21](tsvz-spec-v1.md#21-write-handler-protocol)).
`tsvz` commands and `TSVZ.TSVZClient` find it through the pointer file
`STORE.serve` and use it instead of re-reading the store. Their output and exit
status stay the same.

- **One appender.** The server batches the writes it receives into single appends.
  Other writers may still append to the files; the server reads what they add
  before it answers.
- **Acknowledgement.** A write is acknowledged once it is queued (`#_write_ack_#`
  `memory`, the default), or after `fsync` (`#_write_ack_#` `disk`, or
  `TSVZClient(..., sync=True)`). Reads always see acknowledged writes.
- **Access.** Only the user who started the server can connect (socket mode 0600).
  `--x-group G` or `--x-mode 0660` opens it on purpose. The server listens on a
  Unix-domain socket only; reaching it from other hosts is left to a separate
  adapter (for example TCP with TLS) that would relay to the socket.
- **Trust.** Clients use `STORE.serve` only when it is a regular file owned by
  them or by the store's owner, not writable by anyone else, naming a socket that
  belongs to that owner. Otherwise they warn and use the
  files. `serve` never writes through a symbolic link or into a file it did not
  create.
- **Lifetime.** It runs in the foreground until `tsvz stop STORE`, SIGINT or
  SIGTERM, or `--x-idle-timeout S` seconds without connections. It then writes
  and `fsync`s everything and removes `STORE.serve`.
- **Bypassing it.** `--x-direct` makes a `tsvz` command use the files. Command
  lines with `--x-header`, `--x-defaults` or `--x-strict`, or another delimiter
  than the server's, use the files too. A server started with `--x-header H`
  answers every request by the `--x-header` rule above, and one started with
  `--x-strict` too answers as a direct `--x-strict` command does: a line 1 that is
  not H fails the request (status 1, nothing written), and reads and scrubs drop
  the rows of another width. It does not start when line 1 is not H then. The
  table of a routed `read --format table` shows no H line: its first row is set
  as the table's header, as with no `--x-header`.

```python
import TSVZ

with TSVZ.TSVZClient('people.tsvz') as people:  # served, or in this process when no server runs
    people['dave'] = ['dave', 'Dave', '41']
    print(people['dave'], len(people), 'alice' in people)
    print(people.pop('dave'))
```

`TSVZClient` has `TSVZed`'s semantics for reading and setting keys, `del`, `in`,
`len`, iteration, `pop`, `popitem`, `setdefault`, `update`, `clear` and
`setDefaults`. Its writes return once they are written (raising `OSError` if they
cannot be), one client may be shared between threads, and without a server it
warns once and works on the files in-process, finishing its writes when the
program exits. A server of a `.tsv`-family file names its delimiter in
`STORE.serve`, and `TSVZClient` splits string values on it.

## Fault tolerance

TSVZ keeps your data and keeps going when a file is not what it expects, and
says so on stderr (or through `teeLogger`) with one summarised line per problem:

```
TSVZ warning: data.tsvz: invalid UTF-8 replaced with U+FFFD (3 occurrences, first at line 17)
```

Examples: bytes after the last newline (ignored on read; truncated before the
next append, and quoted), invalid UTF-8, a byte order mark, a newer
`#_version_#`, a marker with a bad value, a checksum mismatch, a damaged
compressed member or a zero-filled line (a member cut short by a crash keeps
the whole lines it decoded; a corrupt member or a zero-filled line is
recovered, skipped or stops the read, as the store's `#_on_corruption_#` says,
`recover` by default, spec §4.9, §4.10, §16.6; a scrub that rewrites such a
part keeps the original as `<part>.damaged-<time>`), an unreadable part, and a
delimiter or encoding argument that conflicts with the extension.

Writing is stricter than reading: a key, value, header or marker that cannot be
encoded as UTF-8, such as the lone surrogate `os.fsdecode` gives for a
non-UTF-8 file name, raises a `ValueError` (`TSVZEncodeError`) that names it
and writes nothing, instead of storing `?`. Pass `errors='replace'` (or any codec
error handler) to the writing functions or to `TSVZed`, `TSVZedLite` and `TSVZClient`
to get the old `?`. `TSVZed` and `TSVZedLite` refuse such a row when it is set. A
rewrite that meets such text leaves the file as it is: `mapToFile()` and
`hardMapToFile()` raise the error, and the rewrites `TSVZed` makes itself report it
once. With a non-UTF-8 `encoding=`, U+FFFD (what the reader puts for a byte it could
not decode) is text that encoding may lack, and it is refused like any other: such a
store is not rewritten (`rewrite_on_load` leaves the file as it is, without failing),
appends of rows that can be written still work, and the delete or update of a key that
holds U+FFFD raises. Such a key stays until the file is edited, the
store cleared, or the store opened with the encoding it was written in; `errors='replace'`
would write `?` for U+FFFD, losing the bytes the file holds there. The `tsvz` command
exits 1 with the message.

A `TSVZed` writes a `.tsv`, `.csv`, `.nsv` or `.psv` file (compressed or not) from
its background thread only: `rewrite()`, `mapToFile()`, `hardMapToFile()`,
`commitAppendToFile()`, `clear()`, `clear_file()`, `reload()`, `close()` and the
constructor's load hand it the job and wait, so a Ctrl-C stops the wait, never the
write (the call still raises `KeyboardInterrupt`). At exit a write in progress is
finished: a Ctrl-C then says so and keeps waiting, and a second one abandons it. A
closed store is freed. (`get_file_obj()` and `release_file_obj()`, kept from 3.39,
hand the locked file to the calling thread; while it holds it, that thread must not wait
on another store of the same file, whose thread waits for this lock.) A file another process removed is made
again from memory by the store's next write, an append included (a removal in the
microseconds as a write starts is followed as a rotation instead); while that rewrite
fails or is refused, the rows are appended to a new file there, and memory keeps all of
them for the rewrite that gets through. A compressed file is read member by member, and a
member's rows are used only once it passes its codec's check (the gzip CRC-32 and length,
the bzip2 CRCs, the xz check, the zstd content checksum TSVZ writes): a member after the
first cut short, as a writer killed mid-append leaves it, is left out whole and the
members after it are read; a first member cut short and any other damage (bytes that do
not decode, a member that fails its check) stop the read there (L5). A rewrite of a
multi-member file killed inside a later member's write can make a read take its old
members back ([docs/limitations.md](docs/limitations.md)). A
compressed file another writer left damaged is made again too: what the read did not get
goes, the damaged member and, after damage that stops the read, everything after it (rows
appended there would be read by no one). While that
rewrite fails, the rows are appended after the damage and memory keeps them for the
next rewrite that gets through; while it is refused (a row changed in place to text
the encoding cannot hold), they are appended after it as in 4.9, and the next read
drops them. Either way `close()` tries the rewrite once more, whatever
`rewrite_on_exit` says. Appends that do not read the store first (`tsvz set`,
`appendTabularFile`, `TSVZedLite`, and a `TSVZed` with `monitor_external_changes=False`
for damage that lands after it loaded the file) still land after damage that stops the
read ([docs/limitations.md](docs/limitations.md)).

It knows the size and SHA-256 of the bytes it last read or wrote. When another process
has changed the file, and it is the same file (device and inode) and still begins with
those bytes, nothing this store set is still waiting to be written, and
`setDefaults()` has not changed its defaults since, only what follows is read and
applied to memory, as a read of the file applies it: memory keeps an order set by
`move_to_end`, and its `#` keys. Any other change reloads the file, changing memory
in place, so a live key is never missing meanwhile.

A rewrite writes over the file and then cuts it to its new length, so a reader never
meets it emptied. One that fails part way (a full disk, an I/O error), or that cannot
make again a file that was removed, leaves the file damaged: the new bytes it wrote,
then the old ones after them. Memory then holds the only good copy, as it does when
the store appends after damage another writer left that it cannot make the file again
over yet (that rewrite failed, or waits out a failed one's back-off). Until a rewrite gets through, the file is not
reloaded over memory; the background writer still appends the rows set meanwhile, and
the next rewrite (the background writer's, `rewrite()`, `close()`) makes the file
again from memory. A change another process
made meanwhile is merged into memory first, as above, when the file is still the one
the damage was left in (for a file that was removed, any file made there since) and
still begins with exactly the bytes it left; after a damage that left the file empty
(or removed it), what follows must also begin with this store's header.
Otherwise that change is overwritten, with the warning `Overwriting external changes`.
What cannot be told from an append is merged as one: another process's rewrite of the
same file in place that begins with exactly the bytes the damage left, and, for a store
with no header (or `verifyHeader=False`), anything written to a file the damage left
empty; the keys it sets or deletes then take its values, and the keys it left out keep
memory's. An explicit `reload()` while the file is damaged replaces memory with the
damaged file, as asked, and warns.

## Upgrading to 5.8 from 5.7

The file formats are unchanged.

- **A delete leaves the other rows as every reader read them: no line moves.** In a loose
  file of one column a delete takes the key's lines out (`tsvz delete`, `pop` and `popitem`,
  a `tsvz serve` handler, a `TSVZedLite` delete, a `TSVZed` delete with its commit or
  rewrite). When one of those lines set a reader's column count (the first keyed line, and
  under `--x-header H` with line 1 not H also the first keyed line after line 1, which a
  reader given H counts from) and the next line has another cell count, 5.6 and 5.7 moved a
  row up into its place, and a cell or a key changed for some readers. Now the key of a later
  row, with as many empty cells as the line had, takes the place (a stand-in), and every
  other line stays where it was: `a\n#_defaults_#\t\tQ\nb\t1\n`, delete `a`, becomes
  `b\n#_defaults_#\t\tQ\nb\t1\n` (5.7: `b\t1\n#_defaults_#\t\tQ\n`, where a `TSVZedLite` then read
  `b` as `[b, 1, Q]`). A stand-in reads as nothing to every reader: at two columns or more it is
  the deletion of a key not yet read, at one a row the key's own later line replaces. Under
  `--x-header id`, `a\nb\t1\n` becomes `b\nb\t1\n` (a reader given `id` read `b` before and
  still does), `id\tv\n# n\n` with `zz` deleted is left as it is (5.7 wrote `zz`, a row to
  that reader), and when the next line has the same cell count a bare `#` keeps line 1's place
  (`a\nb\n` becomes `#\nb\n`).

  What shows: a file can hold a key twice after a delete (the stand-in and the key's own
  line), so a store that reads it is dirty; readers that keep the file's order list that
  key earlier; a `TSVZed`'s memory keeps its order (5.7 moved the row to the front), and a
  one-column store keeps one column (5.7 took the count of the row it moved up). Under
  `--x-header H` with line 1 not H, the command now rewrites the file where 5.7 appended a
  line, when a reader given H reads at one column. One shape is left, after a line 1 of one
  cell, where a reader given H can see a cell change: see
  [docs/limitations.md](docs/limitations.md), "Readers given other arguments".

- **A store emptied after a rewrite follows another process's append at the count its file
  says.** After a rewrite, a store whose rows were then all deleted read the next append at
  its header's count where a reopen takes the file's (`# n\n` under `header='id\tv'`, a
  row `a\t\t1`: an appended `b\t2` read as `[b, 2]` at two columns, where a reopen reads three).

## Upgrading to 5.7 from 5.6

The file formats are unchanged.

- **A program no longer dies at exit beside a store's writer thread, unless nobody reads its
  stdout.** A `TSVZed`'s writer thread, a daemon, could be inside a print as the interpreter
  ended, and the program then ended with `Fatal Python error: _enter_buffered_busy` and exit
  status 134 (no row was lost). Now the exit waits for a print under way, up to 5 s, and from
  then to the interpreter's end that thread writes what it has to say straight to file
  descriptor 2: not to `sys.stdout`, `sys.stderr` or a `teeLogger`. That helps a reader that
  is slow but still reading. A verbose store whose stdout nobody reads (a full pipe, a
  stopped reader) either still ends with that error, now after about 7 s (2 s at 5.6), or,
  when the writer thread is inside a print just as the exit starts, makes the exit wait with
  no limit until something reads stdout (a second Ctrl-C abandons the write in progress, as
  on any exit); 5.6 does the same. No row is lost. See [docs/limitations.md](docs/limitations.md).

- **On Python 3.6 a Ctrl-C at exit no longer drops a queued row.** One that landed as the exit's
  wait took its lock back came out as "Failed to stop append thread ... release unlocked lock",
  and the program ended without the row, or hung; it now reads as any Ctrl-C at exit does.
  The same holds for a Ctrl-C during any other wait for a store's writer thread (a
  `checkExternalChanges()`, `rewrite()`, `clear()` and the like): a `KeyboardInterrupt` where
  3.6 raised `RuntimeError: release unlocked lock`. A `RuntimeError` of another kind is raised
  as it is.

- **"External changes detected" and "Time anomalies detected" print only with
  `verbose=True`.** They printed to stdout at every change another process made, whatever
  `verbose` said. A program that read them needs `verbose=True`. Warnings and errors print
  as before.

- **A strict store follows a widened line 1 with a byte order mark or empty cells at its
  end.** `\ufeffid\tv\tw` and `id\tv\tw\t` under `header='id\tv'` raised "Header mismatch",
  though a rewrite widens such a line 1 the other way. One rule now answers both, and the
  line stays as it is in the file. A loose store reads such a file padded, as wide as line 1
  counts its cells, empty ones at its end too (`id\tv\tw\t` is four columns wide), where it took
  its count from the rows; a row it adds is written that wide, so a strict reader given no header
  keeps it.

- **A store that holds no row reads what another process appends as a reopen does.** The
  count comes from the first keyed line: `id\n` under `header='id\tv'`, then another
  process's `a\n`, shows `a` (it read as a deletion, and memory stayed empty). Each change is
  read whole only while the store holds no row and the last whole read took no column count
  from the file (no keyed line yet, or a file emptied at one column); with a count or a row,
  only what follows is read, as before. A store's own whole write that leaves it with no row
  (`clear()`, or a rewrite of a store whose rows were all deleted) is followed the same way,
  and the store then takes the column count and the rows a reopen would from the file it
  wrote, so a row set after the clear is shaped at that count. This costs a few reads of the
  file. One visible change: under `verifyHeader=False`, the header line a clear writes is then
  held as a row, as a newly opened store holds it, so `len(t)` is 1 after `clear()` where 5.6
  gave 0.

- **"Header mismatch" is said once for a line 1, across all of a store's reads.** A `TSVZed`
  whose line 1 is not its header used to say it at every whole read of the file (46 times for
  45 appends beside it); it now says it once for that line 1, an explicit `reload()` included,
  and again if line 1 becomes another line. A strict store still raises at every read.

- **Deletes write what 5.6 writes.** `tsvz delete`, `pop` and `popitem` (with or without
  `--x-header`, `--x-strict` or `--x-defaults`), a `tsvz serve` handler, a `TSVZedLite` delete
  and a `TSVZed` delete with its commit or rewrite leave the same bytes as in 5.6, but for a
  `TSVZed` on a file whose line 1 starts with a byte order mark (it now writes what 5.6 writes
  for the same file without the mark) or ends in empty cells (it now writes its deletion lines
  and padded rows as wide as line 1 counts its cells: see the item above). So what 5.6 gets wrong in a loose one-column file
  stays for this version: a row moved up to keep a reader's column count can change a cell or
  a key for some readers. 5.8 moves no line: see [Upgrading to 5.8](#upgrading-to-58-from-57).

## Upgrading to 5.6 from 5.5

The file formats are unchanged.

- **The readers take text defaults, and leave the caller's list alone.**
  `readTabularFile`, `readTSV`, `scrubTabularFile` and `scrubTSV` split `defaults=` given as
  text as `TSVZed` does (they took it as a sequence of characters, one cell each). They read
  a copy: the file's `#_defaults_#` lines no longer go into the caller's list, of a `.tsv`
  or a `.tsvz` file, so a second read with that list reads as the first did. To know the
  file's defaults, open a `TSVZed` and read `.defaults`. A list that does not start with
  `#_defaults_#` still differs: a reader takes its first cell for the key's column
  (`['', 'X']` fills cell 1), as every reader since 3.39 has, and `TSVZed` puts
  `#_defaults_#` in front of it (`['', 'X']` fills cell 2).

- **`rewrite(force=True)` always writes.** On a clean store it returned True and wrote
  nothing, so a row changed in place (`t['a'][1] = 'X'`, which does not make the store
  dirty) was gone after `close()`. It now writes as `mapToFile()` does, and compacts the file,
  whatever `dirty` says. `rewrite_on_load=True` and `rewrite_on_exit=True` do the same, each
  at its one moment, with one exception: over damage in a compressed file that no row was
  written after, they leave the file as it is, since the bytes past the damage hold rows no
  read reached (`rewrite(force=True)`, `mapToFile()` and `hardMapToFile()` write there too).
  A program that called `rewrite(force=True)` only to follow another process's change now
  pays a whole rewrite for it: `rewrite()` follows it and writes nothing. `rewrite_interval`
  still writes only a dirty store. A `.tsvz` still ignores all of these. A store that has
  deleted no key and changed no row it held (setting a new key leaves `dirty` False: the
  writer appends it, and it is in the file) takes in another process's change before it
  writes, also with `monitor_external_changes=False` (which is for overwriting what the
  store changed itself: a row another process appended is kept, not deleted), and one given
  `createIfNotExist=False` makes no file where there is none (a row set makes it, as
  before). A writer that takes no lock (a shell `>>`) can still lose rows to a rewrite
  ([entry](docs/limitations.md#a-writer-with-no-lock-loses-rows-to-a-rewrite)): at 10 Hz on
  10k rows 229 of 1174 appended rows were lost.

- **A one-column store stays one column when another process empties its file.** Its
  column count was lost, so the next one-cell `set` acted as a delete and the write was
  gone. The count now holds until a line in the file says otherwise; a lone key in a store
  of two columns or more emptied this way stays a delete.

- **`tsvz delete` and `TSVZedLite` delete in a one-column file.** `tsvz delete FILE b` on
  `a\nb\nc\n` exited 0 and appended `b`, a row there; `TSVZedLite`'s `del` did the same, and
  `tsvz serve`. `delete`, `pop` and `popitem` of a `tsvz` command, a handler or a
  `TSVZedLite` now take the key's lines out, as `TSVZed` does: the file keeps its one
  column (a row that must move up, because it is narrower than the first line left, moves
  with the cells it read, and the other rows read as before) and only those lines go, the
  rest copied as they are. Each delete there is a whole rewrite, and `TSVZedLite` then reads
  the file again for its offsets, as a fresh open would; at two columns or more a delete is
  still one appended line, after a look at the file's first lines under the rewrite lock
  (29 us on a `.tsv` of 200k rows or 2M, 2.5 ms on a 200k-row `.tsv.gz` of one member). A file with no keyed line yet and no header of two columns or
  more takes no line either: a `delete` used to write the lone key, which then read as a
  row, and it now writes nothing (a store that did not exist is still created; under
  `--x-header` with two columns or more a lone key is a deletion, and `id\tv\n` becomes
  `id\tv\na\t\n`). The file's own lines decide this, never the defaults a command is given:
  `tsvz delete --x-defaults 'K\tP' F b` takes b's line out of `a\nb\nc\t1\n`, and in a file with
  no keyed line it writes nothing. The delete reads the file under the rewrite lock it writes under, as
  `TSVZed` does, so a writer beside it neither makes it fail nor loses its rows (it failed at
  every try beside an appender at 200 Hz); `TSVZedLite` changes its memory only once the
  file is written, so a delete that fails (an `OSError`) leaves the key where it was. A
  damaged file that cannot be read gets the line, as before, and a warning on stderr: in a
  file of one column the line deletes nothing.
- **A rewrite keeps the lone keys of a one-column file under defaults of two cells or more.**
  `a\nb\nc\n` opened with `defaults=['#_defaults_#', 'P']` read as `{}` and any rewrite left
  `a\n#_defaults_#\tP\n`: b and c were deleted as deletion lines at the store's width, with no
  output. A file is now compacted at its own width: its header's when line 1 holds it, else one
  column when line 1 is one cell, else its first keyed line's. So it stays one column, keeps every
  key (`a\nb\nc\n#_defaults_#\tP\n`) and gets the defaults line; a plain open reads the same keys
  before and after, on every route (`mapToFile()`, `hardMapToFile()`, `rewrite(force=True)`,
  `rewrite_on_load`, `rewrite_on_exit`), and so does a scrub. The same holds under a wider header:
  `id\na\nb\n` with `header='id\tv'` and those defaults keeps a and b, and `id\na\t1\nb\n` with
  that header keeps b. The store itself still reads none of the lone keys its width makes
  deletions, and the file does not widen to the defaults or to a wider header until every row
  holds a value ([entry](docs/limitations.md#a-file-of-lone-keys-does-not-widen)). A row with a
  value that such a store deletes stays in the file as its key and an empty cell
  ([entry](docs/limitations.md#a-row-deleted-from-a-one-column-file-under-wider-defaults-or-a-wider-header-stays-as-its-key)).
- **`TSVZedLite` knows a one-column file from its load.** So a one-cell row is a row there
  (it was a delete of a key it did not hold), and a row read at its offset has its cell
  (`t['a']` read as `[]`).

- **A rewrite widens line 1 to the header the program gives.** A program that adds a column
  (line 1 `id\tv`, `header='id\tv\tw'`) no longer has to edit line 1 by hand: a `TSVZed`
  rewrite of any kind (`mapToFile()`, `hardMapToFile()`, `rewrite(force=True)`, the automatic
  ones, and one that makes a removed or damaged file again) writes the header as line 1, pads
  every narrower row and `#_defaults_#` line to it, and the store's rows and column count
  follow at once, so `t['a'][2]` holds `''` (a default the file already holds for the new
  column, if it has a `#_defaults_#` line that reaches it, is what a row below that line reads;
  the rows above it read `''`) and the "Header mismatch" at each open ends. The padding is the one thing besides dropping lines
  that a rewrite does to a line: without it a strict reader, which drops a narrower line,
  would lose the rows. The rule is exact: line 1 must be the header's beginning, the same
  cells without the whitespace around them, and fewer of them. `idaho\tvalue`, which the
  read's looser check takes for `id\tv`, stays, as does a line 1 that is a row, or one wider
  than the header (writes never cut), and a header given with `verifyHeader=False`. A strict
  `TSVZed` takes a line 1 that extends its header (the same leading cells, then named ones)
  for another program's widening, at open and whenever it follows the file, and its column
  count becomes the wider header's; it used to raise "Header mismatch" and stop following. It
  reads the rows of such a file loose, padded, what another process appends to it too, and a
  rewrite leaves the rows and the file's defaults lines as they are, adding only the store's own
  defaults if it was given others (a strict read dropped every row narrower than the new count,
  and a rewrite then deleted them). Its `set`s that are narrower than the file are padded and
  written, where a strict store that never met a widening still refuses them. Each whole read
  decides this afresh: once line 1 no longer extends its header, such a `set` is refused again.
  A wider row or a header-less file is no widening. Any other line 1 that is not its header is
  refused as before. A program still on the old
  header then sees "Header mismatch" in its turn, and a strict reader given it raises; it
  does not narrow line 1. A one-column file
  with a row that holds no value is not widened, with a warning, and keeps every row: a lone
  key is a deletion at two columns or more, so widening would delete it. A key's earlier line
  with no value does not count when a later line gives the key one; that key then moves after the
  rows between its two lines, no value lost (`id\nd\na\t1\nd\tx\n` under `header='id\tv'` becomes
  `id\tv\na\t1\nd\tx\n`). A scrub still never changes line 1.

## Upgrading to 5.5 from 5.4

The file formats are unchanged, and a 3.39 host still reads every `.tsv`-family file 5.5
writes.

- **`TSVZed` no longer rewrites a `.tsv`-family file when it opens it.** `rewrite_on_load`
  now defaults to `False`, as `rewrite_on_exit` and `rewrite_interval` already did; 5.4
  rewrote a legacy file at open only when it held a superseded line (a key restated with
  another row, a deletion of a live key). Compact a file explicitly: call `mapToFile()` or
  `hardMapToFile()`, or scrub it (`scrubTabularFile`, `tsvz scrub`), or pass
  `rewrite_on_load=True`. A store still makes again a file that
  is damaged or removed, since its memory is then the only copy. The three automatic-rewrite options
  (`rewrite_on_load`, `rewrite_on_exit`, `rewrite_interval`) are planned to be removed in
  a future version in favour of those explicit calls.
- **Nothing else rewrites a file unless the program asks.** A store that follows another
  process's change no longer rewrites the file at its next tick because it holds changes
  of its own; `rewrite()` without `force=True` only follows such a change, and rewrites
  when `rewrite_interval` says so. A rewrite happens on `mapToFile()` and `hardMapToFile()`
  (always), on `rewrite(force=True)` and the options above set explicitly (in 5.5 only when
  the store is dirty: `dirty` is True for a key restated with another row or a deletion of a
  live key; from 5.6 `rewrite(force=True)`, `rewrite_on_load` and `rewrite_on_exit` always
  write, see [Upgrading to 5.6](#upgrading-to-56-from-55)), and on its own in two cases:
  making again a damaged, removed or emptied file, and the next commit after a delete in a
  one-column store (below).
- **`move_to_end` appends instead of rewriting.** `t.move_to_end(k)` appends `k`'s deletion
  line, then its row, so every reader sees the new order at once; it no longer turns on the
  rewrite at exit or warns "Need to resync", and it refuses a row the encoding cannot hold
  when called. `last=False` cannot be an append for one key: it moves every other key to
  the end instead, two lines for each, until a compaction takes them out. A key whose row
  holds no value moves in memory only. In a one-column store a move is a delete (below)
  and an append. A move follows another process's change to the file first, so the rows it
  appends again do not undo it; a write that lands between the move and its commit (one
  tick) can still lose, as any queued line can
  ([limits](docs/limitations.md#a-write-by-another-process-between-a-move_to_end-and-its-commit-can-be-lost)).
- **Rows wider than the file's column count are read whole.** `readTabularFile` (also
  `lastLineOnly`), `read_last_valid_line`, `TSVZed`, `TSVZedLite`, `tsvz read`/`get` and
  their table output, and `tsvz serve`: a row is padded to the column count as before,
  and only its empty cells past it go (`a\t1\t\t` at two columns is `[a, 1]`); 3.39 to
  5.4 cut it, and a scrub or rewrite then erased the cells (and a row whose cut left only
  empty cells became a deletion). `len(row)` can now exceed the header's width; a
  `correctColumnNum=` a caller gives no longer cuts. A program that adds a column sees it.
  While 5.4 and older processes share the file, they still see such rows cut, and their
  scrubs and rewrites still erase the cells: upgrade every process that rewrites the file.
- **A shorter `#_defaults_#` line keeps the defaults before it for the cells it lacks.**
  `#_defaults_#\tX\tY` then `#_defaults_#\tZ` leaves `Z, Y` in effect (5.4: `Z` and, past
  the column count, nothing).
- **`TSVZed` reads a one-column file as `readTabularFile` does**: lone keys are rows (3.39
  to 5.4 showed it empty, and the next rewrite erased the keys). A `del` (or `pop`,
  `popitem`) in a one-column store appends nothing (a lone key would read back as a row
  there). The next commit (the background writer's next tick, `commitAppendToFile()`,
  `close()`) compacts the file with that key's lines left out: for 100,000 keys about
  1 s and 23 MiB (Python 3.14), once per commit however many deletes it holds, so a
  program deleting in a loop in such a file pays that once per `append_check_delay`.
  Until then the key stays deleted for the store: a reload, or following another
  process's rewrite, leaves it out. A write another process makes after that commit stays;
  one that lands before it loses to this store's delete, as a queued deletion line does
  at two columns or more. While that rewrite is refused (a row changed in place to text
  the encoding cannot hold), the store's later writes wait behind it; `close()` appends
  them and reports the delete as not written. The file keeps its one column: when taking a
  key's line out would leave a wider line first, the next one-cell line moves up into its
  place, so that key's position changes. With no one-cell line left, the narrowest line
  moves up and the file now reads at that many columns (a line with no value moves up as
  the key alone).
- **`setDefaults()` can change a header-less store's column count.** Defaults of two cells
  or more set the column count a reader takes, so defaults with another cell count change
  how the file reads: a reader pads the rows narrower than that and fills them with the
  defaults, and a strict reader's check moves with them. The store itself leaves memory's
  rows as they are and does not change the file at the commit. In a one-column file the
  lone keys are deletions at two columns: a store opened with such defaults, or given them
  by `setDefaults()` and then `reload()`, does not hold them, where a plain open does. 5.5
  took all but the first lone key out of the file at any rewrite of that store, with no
  output (`defaults=['#_defaults_#', 'P']` on `a\nb\nc\t1\n`: `hardMapToFile()` left
  `a\nc\t1\n#_defaults_#\tP\n`); from 5.6 the file stays one column and keeps every key
  (`a\nb\nc\t1\n#_defaults_#\tP\n`: a rewrite compacts a file at its own width, its first keyed
  line's, never at the store's). 5.4 did worse: its `TSVZed` read such a file as `{}` with
  no defaults at all, and its rewrite took out the first lone key too.
  The last `setDefaults()` wins, and is what a whole reload starts from. A rewrite changes
  nothing this store's reader reads, except what memory changed; pass the same defaults to
  every reader of the file, as text or as a list that starts with `#_defaults_#` (from 5.6
  those read alike; a list that does not start with it does not: see
  [Upgrading to 5.6](#upgrading-to-56-from-55))
  ([entry](docs/limitations.md#setdefaults-to-another-cell-count-changes-how-the-file-reads-not-the-rows-memory-holds)).
- **A cell that is whitespace once unescaped (`<LF>`) is still filled with the defaults
  when a `TSVZed` loads it** (as 5.4): only the cells its right-strip empties;
  `id\tv\n#_defaults_#\tNA\nk\t<LF>` loads `[k, NA]`.
- **Writes never cut a row.** `TSVZed[k] = row`, `TSVZedLite[k] = row`,
  `appendTabularFile`, `appendLinesTabularFile`, `tsvz set`/`append` and the handler pad a
  shorter row and keep a wider one (dropping only empty cells past the count), so every
  cell written is checked against the encoding.
- **Strict reads and stores take a wider row whole.** A strict read (`readTabularFile`'s
  default, `TSVZedLite`, a strict `TSVZed`, `--x-strict`) drops only a line narrower than
  the column count (unless defaults with a value are in effect there, as before), and
  takes a wider one whole; 3.39 to 5.4 dropped every line of another width. A strict
  `TSVZed` or `TSVZedLite` refuses only a row narrower than the column count, judged after
  its empty cells past the count are dropped (`['a', '1', '']` fits two columns), and
  keeps a wider one.
- **`TSVZed.correctColumnNum` after a load is the column count the read took** (the
  header's, the defaults', or the first keyed line's), not the first row's length, which
  a wider row can now exceed.
- **`TSVZed(correctColumnNum=N)` no longer pads.** The store's column count is the one the
  read takes (above), whatever `N` says. In 5.4 a loose store padded every row to an `N`
  above the file's width (`N=5` wrote `a\t1` as `a\t1\t\t\t`), but a strict one refused
  those rows: memory was empty and a rewrite emptied the file. An `N` below the file's
  width cut rows in memory at the open and in the file at a rewrite in a loose store (`N=1`:
  `a\t1` to `a`; `N=2`: `a\t1\t2` to `a\t1`) and emptied the file in a strict one. 5.5
  does neither. A program that relied on
  `N` to pad its rows gives defaults with `N` cells, counting the `#_defaults_#` key
  (`['#_defaults_#', '', '', '', '']` for 5; `['']*5` collapses to no defaults), or pads
  them itself. A `header=` that line 1 holds sets the width instead.
  `readTabularFile(correctColumnNum=N, strict=False)` still pads to `N`; a strict read takes
  `N` as its column count (strict reads, above).
- **A scrub compacts by dropping lines.** `scrubTabularFile`, `tsvz scrub` and a handler
  `scrub` drop comments, empty lines, superseded lines and dead keys' lines, and keep
  every other line byte for byte where it was. A `#_defaults_#` line the same as the one
  before it, with nothing between them changing the defaults, goes too. The exceptions: a
  key's last line can move up into its first line's place (the key then is one line:
  when no defaults line lies between them, or when the last line is exactly as wide as the
  column count with a value in every cell, so no defaults reach it), a dead key's line that
  readers take their column count from is kept emptied (`a\t` for `a\t1`), and a header
  you pass with `verifyHeader=False` is added as line 1 when the file's line 1 is not it.
  Otherwise line 1 never changes (`tsvz scrub --x-header` no longer puts an empty line
  first: no row can move onto line 1), the `#_defaults_#` lines stay where they are (5.1
  to 5.4 moved them and wrote resets), the caller's defaults are never written into rows,
  a strict scrub writes what a loose one writes and keeps the lines strict drops, and
  text the encoding cannot hold no longer refuses a scrub (its bytes are copied). A file
  whose keys were all deleted keeps its line 1, and in a header-less file one emptied
  line where needed, the same size after every scrub (5.2 to 5.4 shrank it to the bare
  header). A scrub ends the file's last line with a newline, and writes the file again
  even when it drops nothing (a compressed one is recompressed), as before.
- **A `TSVZed` rewrite of a healthy file compacts it the same way and appends what memory
  adds**: the store's defaults when the file does not already end with them (a line as
  wide as the column count, with a bare `#_defaults_#` reset in front when one line cannot
  say them, or the reset alone when the store has none and the file ends with some;
  neither when the store's own reader would not take it), then rows memory holds that the
  file does not read the same (changed in place, put in memory directly; a row with an
  empty cell the defaults in effect would fill goes between a reset and those defaults
  again; a row narrower than the column count is padded to it, so a strict reader takes
  it), and deletion lines for keys memory no longer holds. Only a file that is damaged,
  missing or empty is written from memory, and it keeps its reading: a header-less store
  writes first the first row memory holds exactly as wide as the column count (moved to
  the front), or at two columns or more with no such row, a `#_defaults_#` reset padded to
  that width; rows with an empty cell the defaults would fill go after a reset, and the
  store's defaults line goes after the rows (before them, in a one-column file, it read as
  the file's column count and every key read as a deletion). Every rewrite is written
  whole: `mapToFile()` no longer
  rewrites in place, which padded a shorter line with spaces. It no longer writes its header over a line
  1 that is not that header (5.4 did, and a reader given no header lost the row line 1
  was); such a file keeps warning "Header mismatch" until it is fixed. It no longer
  refuses a rewrite for a row it read but cannot encode again; only rows memory must
  write are checked. Its `#_defaults_#` lines are kept, so a program that sets
  `t['#_defaults_#']` over and over keeps a line for each change (5.4's rewrite
  collapsed them). It costs a second parse of the compacted lines (one flag per key, not
  the rows): for 100,000 two-cell rows with one changed in place, `hardMapToFile()`
  peaks at 25 MiB and takes about 0.8 s, where 5.4 peaks at 19 MiB and takes about 0.2 s
  (Python 3.14). A scrub of that file takes about 0.6 s (5.4: 0.5 s).
- **A whole reload starts from the defaults and column count the program gave.** A
  `TSVZed` that follows another process's rewrite (any rewrite, not an append) now reads
  the file as its constructor did (or with the defaults of its last `setDefaults()`), not
  with the defaults its last read ended with. Release note: a `TSVZed` of 3.39 to 5.4
  that shares a file with any process that rewrites it (a scrub, another `TSVZed`'s
  periodic or open-time rewrite) can misread a file whose meaning depends on its
  defaults (a bare `#_defaults_#` first line, defaults the program gives):
  `#_defaults_#\na\tX\nb` read with defaults `['', 'P']` shows `{a: [a, X]}`, and 5.2
  showed `{}` after following such a rewrite. Upgrade those processes. A `setDefaults()`
  made during such a read stays: the read no longer puts back the defaults it started with.
- **Readers given other arguments than the scrub's can see a different file after a scrub
  or a `TSVZed` rewrite**: see [docs/limitations.md](docs/limitations.md#readers-given-other-arguments-than-the-scrubs-can-see-a-different-file-after-a-scrub-or-a-rewrite).
- **A file whose first keyed line is a bare `#_defaults_#` reads as one column without
  defaults**: pass the defaults the file's program uses
  ([docs/limitations.md](docs/limitations.md#a-file-whose-first-keyed-line-is-a-bare-_defaults_-reads-as-one-column-without-defaults)).
- **A `TSVZed` given a header that line 1 is not takes its column count as readers do**:
  from the defaults when they have two cells or more, else from the first keyed line; with
  no keyed line yet, the header's, even with wider defaults (it took the header's, and a
  delete in a one-column file was written as a row). A `.nsv` file's compaction keeps the empty cells at the end of a
  line, which its reader keeps (it took them off and could drop a row).
- **Known limitations of one-column files and moves** (each has an entry in
  [docs/limitations.md](docs/limitations.md)):
  - a delete in a one-column file can undo another process's write that lands just
    before it ([entry](docs/limitations.md#a-one-column-delete-can-undo-another-processs-write-that-lands-just-before-it));
  - deleting many keys of a big one-column file rewrites it once per commit, about 1 s
    at 100,000 keys
    ([entry](docs/limitations.md#deleting-many-keys-of-a-big-one-column-file-rewrites-it-once-per-commit));
  - a store emptied at one column and then given a wider row first loses the lone keys
    after it: `t['x']=['x','1']; t['y']=['y']` reopens without `y`
    ([entry](docs/limitations.md#a-store-emptied-at-one-column-then-given-a-wider-row-first-loses-the-lone-keys-after-it));
  - repeated `move_to_end` of a row read before the defaults grows the file
    ([entry](docs/limitations.md#repeated-move_to_end-of-a-row-read-before-the-defaults-grows-the-file));
  - a wider row that a move writes again is lost to 3.39 to 5.4 strict readers
    ([entry](docs/limitations.md#a-wider-row-that-a-move-writes-again-is-lost-to-old-strict-readers));
  - a one-column delete rewrites the whole file
    ([entry](docs/limitations.md#a-one-column-delete-rewrites-the-whole-file));
  - a file of lone keys does not widen to a wider header or defaults: every key stays, and a
    store given such defaults reads none of them
    ([entry](docs/limitations.md#a-file-of-lone-keys-does-not-widen)).
- **A late `setDefaults()` on a file whose first keyed line has one cell grows it at each
  rewrite** (a bare `#_defaults_#` or a lone key, and a row with an empty cell the defaults
  fill): 28 bytes per `hardMapToFile()` with one default. Reads do not change
  ([entry](docs/limitations.md#a-late-setdefaults-on-a-file-whose-first-keyed-line-has-one-cell-grows-it-at-each-rewrite)).
- **A header wider than the file's line 1 is not written into line 1** (5.4 widened it at a
  rewrite, and from 5.6 a rewrite does again, see
  [Upgrading to 5.6](#upgrading-to-56-from-55)). A program that adds a column to its header
  finds that 5.5 keeps the file's width: "Header mismatch" prints at every open, and rows
  already in the file (and rows whose last cell is empty) raise IndexError on the new
  column. Nothing is lost.
- **`move_to_end` follows the file first**, so it waits while another process holds the
  file's lock and slows beside a busy writer (100,000 rows and a writer at 20 Hz: 28
  moves/s, 5.4: 1.2 million), and it raises `KeyError` for a key another process has
  deleted, as its docstring says (5.4 succeeded)
  ([entry](docs/limitations.md#move_to_end-follows-the-file-first-so-it-waits-for-the-lock-and-slows-beside-a-busy-writer)).
- **A running `TSVZed` can miss a same-size change by a writer that takes no lock** (another
  tool, TSVZ before 5.1): memory stays stale until the file changes again. TSVZ's own writers
  were not exposed where readers take the read lock
  ([entry](docs/limitations.md#a-same-size-change-by-a-writer-that-takes-no-lock-can-be-missed-by-a-running-tsvzed)).

## Upgrading to 5.4 from 5.3

The file formats are unchanged.

- **A compressed `.tsv`-family file is read member by member, each member once its check
  passes.** `readTabularFile`, `scrubTabularFile`, `clearTabularFile`, `TSVZed`,
  `TSVZedLite`, `openFileAsCompressed()` for reading, `get_file_obj('rb')`, the CLI and
  `tsvz serve` read a `.gz`, `.bz2`, `.xz` or `.zst` file so. Python's codec readers hand
  a member's text over before they check it: a member cut short by a writer killed
  mid-append, once another member was appended after it, could end its last line with a
  garbage value (gzip, more rarely bz2 or xz), which a `TSVZed` then wrote back. Now:
  - a member cut short is left out whole, also when it is the last one (5.3 and earlier
    kept the whole lines it decoded): its writer was never told those rows were saved. A
    `.tsvz` part still keeps a cut member's whole lines, as spec §16.6 says, so the two
    differ there.
  - after a member cut short that is not the first, the members after it are read, where
    5.3 and earlier stopped at it, so the appends that followed a killed append are read.
    A `TSVZed` that read it makes the file again at its next write, without the cut member.
  - a first member cut short stops the read, as before, and now gives none of its rows
    (5.3 and earlier handed over the lines gzip decoded of it): a whole rewrite killed in
    its first member leaves the old file's bytes after it, whose rows are as they were
    before the rewrite (deleted keys too). Its line 1 is not taken unchecked either: an
    append that checks the header (`tsvz set --x-header`, `appendTabularFile` with a
    header) raises and writes nothing, and a scrub or clear makes the file again.
    A rewrite killed inside a later member's write of a multi-member file is not told
    from a killed append ([docs/limitations.md](docs/limitations.md)).
  - a member that fails its check gives none of its rows (5.3 and earlier used them, then
    stopped).
  - other damage stops the read there, as before: bytes that do not decode, also after a
    complete `.bz2` or `.xz` stream, which 5.3's and earlier versions' `readTabularFile` (and so a scrub, a clear
    and `tsvz serve`'s reads) passed over silently, where a `TSVZed` already reported
    them; xz stream padding (zero bytes after a stream) is not damage.
  - the damage warning names each member left out and where it begins.
  - a member's text is held until its check passes: TSVZ writes at most 1 MiB of text a
    member, and a file another tool compressed whole is one member, held whole while it is
    read (the read builds the rows from it anyway). A read of line 1 alone (an append's
    header check, a clear, a scrub) decodes the first member in pieces and keeps only that
    line until the member's check passes. It takes the read lock (5.3), so it waits out
    another process's rewrite rather than reading a first member it has half written.

## Upgrading to 5.3 from 5.2

The file formats are unchanged, and a 3.39 host still reads every `.tsv`-family file
5.3 writes. What changed is how the file is locked.

- **Readers wait while another process rewrites the file.** `readTabularFile`,
  `tsvz read` and `tsvz get`, a `TSVZed`'s load, `reload()` and background read,
  `TSVZedLite`'s load and a `tsvz serve` handler's reads of a `.tsv`-family file take a
  shared lock while they read, so a rewrite another process (or thread) holds the file's
  lock for (a `TSVZed` rewrite, a scrub, a clear) is waited out, never met part way. The
  wait is as long as the rewrite holds the lock, and Ctrl-C ends it; a writer stopped
  with Ctrl-Z stalls them until it continues. This is what makes a `del` during another
  process's rewrite land: 5.2 and earlier could read the file part way, find no key and
  write nothing, so the key came back once the rewrite ended.
- **Linux:** a read also waits for an append by 3.39 to 5.2, which locks the whole file.
  A 5.3 append does not wait for readers, and readers do not wait for it. A rewrite waits
  for the reads in flight, and while it waits no new append or read starts (it holds the
  append lock and a gate first), so reads that overlap without a pause cannot hold it off.
- **macOS and other systems:** locks are `flock`. Reads and appends wait for each other
  there, and `flock` has no gate: reads that overlap without a pause (several processes
  reading the file in a loop) can hold a rewrite off for as long as they go on, and the
  appends behind it with it
  ([limitations](docs/limitations.md#on-macos-and-other-flock-systems-overlapping-reads-can-hold-off-a-rewrite)).
  A read in another thread no longer drops a write's lock, and two threads' writes
  exclude each other (5.2 could write a new file's header twice).
- **The lock layout changed.** Reads lock byte 0 shared, passing through a gate at byte 2
  (held shared only until byte 0 is); appends lock byte 1; rewrites lock byte 1, then the
  gate, then the whole file (Linux open file description locks); `flock` on other
  POSIX systems. Older programs' whole-file locks still exclude every 5.3 lock, and 5.3
  excludes them, so 5.2 and 5.3 programs can share a file; 3.39 to 5.2 writers do not
  pass the gate, so 5.3 reads that overlap without a pause can hold them off
  ([limitations](docs/limitations.md#reads-by-53-programs-can-hold-off-a-339-to-52-writer)).
  Where only `lockf` works (a
  Linux kernel without open file description locks, a file system that refuses them and
  `flock`), writers use it and readers take no lock, as in 5.2.
- A child that `fork()` made drops the TSVZ locks it inherits (Python 3.7 and later): a
  copy of a `get_file_obj()` holder gets EBADF on its writes.
- A thread holding `get_file_obj()` reads that file (`readTabularFile`) without waiting.
  One that waits on another TSVZ store of the same file (its `reload()`, `close()` or a
  write) can wait for ever: that store's thread waits for this thread's lock
  ([limitations](docs/limitations.md#a-thread-holding-get_file_obj-waits-for-ever-on-another-store-of-the-same-file)).
- At exit a `TSVZed` with nothing to write no longer waits for its writer thread, so one
  Ctrl-C ends a program whose store waits in its load for another process's rewrite. A
  store with rows queued, or a write handed to its thread (`hardMapToFile()`, say), is
  still finished at exit.
- **Fixed: on Python 3.9 to 3.12 one Ctrl-C at exit could drop a store's queued rows.**
  A Ctrl-C in `Thread.join()` marks the running thread as stopped there, so 5.2 stopped
  waiting at once while it printed "is being finished; Ctrl-C again to abandon it". 5.3
  waits for its writer thread another way: the first Ctrl-C keeps waiting, the second
  abandons the write, on every Python.
- Still not covered: writers that empty the file before or without the lock (3.39's and
  4.9's compressed rewrites and `clear()`, 3.39 to 5.0 scrub and clear, other tools),
  readers from 5.2 and earlier and tsvz-rs, Python 3.6 forks, `lockf`-only hosts and
  Windows
  ([limitations](docs/limitations.md#some-writers-and-readers-still-meet-another-processs-rewrite-part-way)).

## Upgrading to 5.2 from 5.1

The file formats are unchanged, and a 3.39 host still reads every `.tsv`-family file
5.2 writes.

- **A scrub of a `.tsv` whose rows were all deleted shrinks it** (5.5: see
  [Upgrading to 5.5](#upgrading-to-55-from-54); it now keeps line 1). `scrubTabularFile`,
  `tsvz scrub` (also through a `tsvz serve` handler) of a `.tsv`-family file (`.tsv`,
  `.csv`, `.nsv`, `.psv` and their compressed forms) in which every key has been deleted
  leaves the file as an empty store: the header line it has, and its `#_defaults_#`
  lines as a scrub of a file with rows keeps them (an empty one after the header, the
  last non-empty one after it, both as wide as the header, or as the column count the
  read took, which caller defaults set when given). A file with no header line keeps its
  empty first line, or becomes 0 bytes when its first line was a row; a first line that
  is no row (a comment, say) becomes an empty line. A defaults line it keeps follows an
  empty first line, which the scrub then writes. Comment lines go, as a scrub of a file
  with rows already drops them. A scrub that is given
  a header (and checks it, the default) keeps the file's first line as its header, an
  empty one included, with a "Header mismatch" warning when it differs; the header it is
  given is written only with `verifyHeader=False`, or when the file's first line cannot be
  decoded (plain text named `.gz`); `tsvz scrub --x-header` follows the `--x-header` rule
  below instead. A scrub of a path that is not there
  makes no file and returns `{}`, as in 5.1, unless `createIfNotExist=True`. A compressed
  file whose read decodes no row (plain text named `.gz`, a first member cut short) is
  made again as an empty store, as a scrub of a damaged file with rows already was in
  5.1: what the read did not get goes, the bytes after the damage and also the lines of the
  damaged member before it that the codec never handed over (Python's gzip gives none of
  a first member cut short, where `zcat` shows them), and
  `tsvz clear --x-header H --x-strict` (`clearTabularFile(verifyHeader=True)`) now clears
  such a file, where 5.1 exited 1 with the codec's error.
  5.1 and 5.0 left such a file as it was, deletion lines included, so a writer that
  appended keys, deleted them all and scrubbed, over and over, saw the file grow without
  limit (205, 405, 605 bytes, ... for 20 keys a cycle); now it is the same size after
  every scrub. A file with no byte (or a compressed one with only empty streams) is
  still left alone, and so is a file whose strict scrub (`strict=True`, `--x-strict`) read
  dropped a line for its width and found no row: that line is not a deletion, as 5.1 left
  it. A scrub that finds the file changed since its read still reads
  again and writes nothing if it keeps changing, as for a file with rows.
- **`get_file_obj('ab')` on a compressed store writes its trailer before the lock is let
  go.** A program that calls it on a `.tsv.gz`, `.bz2`, `.xz` or `.zst` store and writes
  through the file now has all of it, the gzip trailer and the bz2, xz and zstd data included,
  in the file when `release_file_obj()` unlocks it. 5.1 and earlier wrote them after the
  unlock, where another writer could append in between and corrupt the stream.
- **A `TSVZed` no longer lets a file grow with every session of sets and deletes** (5.5:
  the open-time rewrite is off by default; see [Upgrading to 5.5](#upgrading-to-55-from-54)). It
  rewrites a `.tsv`-family file at open when the file holds a key's row and the deletion
  of it, as 3.39 did only for a key with two values, so a program that sets keys and
  deletes them in each session (default settings, or `rewrite_interval`) keeps the file
  the same size (205, 405, 605 bytes, ... for 20 keys a session before). Every session
  that opens such a file now rewrites it once. With `rewrite_on_load=False` the open
  does not, and the next exit or periodic rewrite does it (`rewrite_on_exit=True`,
  `rewrite_interval`). What such a rewrite does, it now does at a plain open:
  - it cuts a row wider than the header, and the cut stays (5.5: no longer; see
    [Upgrading to 5.5](#upgrading-to-55-from-54)):
    `id\tv\na\t1\t2\nb\t1\nb\t\n` becomes `id\tv\na\t1  \n`, and
    `id\tv\na\t\t7\nb\t1\nb\t\n` becomes `id\tv\na\t  \n`, the 7 lost;
  - it writes over the file in place and pads with spaces (`a\t1  ` above; 5.5 writes a
    compaction whole): TSVZ readers
    strip them, tools that read the raw bytes (`awk`, `cut`) see them;
  - on a file it cannot write (read-only), it prints `Failed to open file … Permission
    denied` twice and leaves the file as it is, as 5.1 did for a key with two values.
- **`--x-header H` makes line 1 the header for every `tsvz` operation.** On a
  `.tsv`-family file whose line 1 is H (an exact match: the same cells, each without the
  whitespace around it, a trailing empty cell included), line 1 is the header: `keys`,
  `len`, `read`, `get`, `has`, `pop`, `popitem` and `delete` never list, count, print,
  pop or delete it, and `scrub` and `clear` keep it. When line 1 differs, the file has no
  header and line 1 is a row, with 3.39's "Header mismatch" warning, for the reads and
  the scrub alike, so a scrub keeps the rows a read shows (but for the column-width cases
  below), and in such a file it never writes a row that is H as line 1: it puts an empty
  line first (`a\t1\nid\tv\na\t\n` becomes `\nid\tv\n`), so the reads still take that row
  for a row (5.5: a scrub keeps line 1, so it leaves that file as it is). With `--x-strict` a line 1 that is not H is an error (exit 1, nothing
  written) for every operation, through a handler too. A missing or empty file gets H
  first at its first write, as in 5.1. A `tsvz serve STORE --x-header H` handler applies
  the rule to every request it answers, routed `tsvz` commands included; with
  `--x-strict` it is strict for them as above (5.1 ignored `--x-strict` at `serve`): such
  a handler refuses to start on a file whose line 1 is not H, its routed `.tsv` reads and
  scrubs drop lines of another column count, and its `.tsvz` reads are strict, as for a
  direct `--x-strict` command. The library's own header check (`header=` to
  `readTabularFile`, `appendTabularFile`, `clearTabularFile`, `scrubTabularFile`,
  `TSVZed`) is unchanged, 3.39's containment check. The CLI's writes check line 1 more
  strictly than 5.1's did: 5.1's `set`, `append`, `delete` and `clear` with `--x-header`
  silently took a line 1 that merely held H's cells (`id\tvalue` for `id\tv`) for the
  header and fitted rows to H's width; now they warn and write the row at its own width,
  and with `--x-strict` exit 1. A rewrite without `--x-header` (a scrub without it,
  `scrubTabularFile`, a `TSVZed` rewrite) can still put a row equal to H on line 1 of a
  header-less file (5.5: only a `TSVZed` writing its memory;
  [limitations](docs/limitations.md#a-rewrite-without---x-header-can-put-a-row-equal-to-h-on-line-1)).
  In 5.1 only `set`, `append`, `delete` and `clear` used `--x-header`: the reads and
  `scrub` took line 1 as a row (`tsvz keys`
  printed `id a b` for two keys, `len` said 3, `popitem first` popped the header), and
  so did every request to a handler. Without `--x-header` nothing changed: line 1 is a
  row, as in 5.1 and 3.39. In a `.tsvz` store `pop`, `popitem` and `setdefault` now check
  `--x-header` as `set` does; nothing else changed there.
- **Known, mostly as in 5.1: a scrub, a rewrite or a read takes one column count for every row,
  and can lose cells or keys.** A scrub given defaults narrower than the rows, a row wider
  than the header, and a `.tsv` that begins with a bare `#_defaults_#` line (read without
  defaults) are examples. A strict scrub of a file whose read dropped a line for its width
  and found no row leaves it, so it grows again over append, delete-all and scrub cycles.
  Both waited for the column-width design, which 5.5 is: see
  [Upgrading to 5.5](#upgrading-to-55-from-54).

## Upgrading to 5.1 from 5.0

The file formats are unchanged, and a 3.39 host still reads every `.tsv`-family file
5.1 writes. Most of the changes are in scrub and clear.

- **Scrub and clear of a `.tsv` write over the file.** `scrubTabularFile`,
  `clearTabularFile`, `tsvz scrub` and `tsvz clear` (on the command line or through a
  `tsvz serve` handler), `TSVZClient.clear()` and `TSVZedLite.clear_file()` of a
  `.tsv`-family file write the new content over the file and then cut it, as
  `TSVZed`'s rewrites do, so a reader that takes no lock never meets the file emptied.
  They take the file's lock, so they wait while another writer holds it. 5.0 emptied
  the file first, with no lock (`TSVZedLite` truncated its own descriptor).
- **A scrub keeps another process's rows.** A scrub of a `.tsv` writes only if the
  file is still what its read saw. Otherwise it reads again, up to three times, and if
  the file kept changing it writes nothing and warns: `tsvz scrub` exits 1 (`scrub
  refused; nothing was written`), as for a `.tsvz` (C6), and a handler answers refused.
  `scrubTabularFile` still returns the rows it read. 5.0 wrote over a row appended
  after its read.
- **`scrubTabularFile(lastLineOnly=True)` is a read.** It returns the last row and
  writes nothing, as for a `.tsvz`. 5.0 and 3.39 wrote that row back as deletion lines
  (`id\tv\na\t\n3\t\n`), and the store was gone.
- **A scrub keeps the file's `#_defaults_#` lines** (5.5: where they are; see
  [Upgrading to 5.5](#upgrading-to-55-from-54)). The defaults in effect at the end
  of the read are written after the rows: the last non-empty defaults line, its empty
  cells filled from the line before it as a reader fills them. An empty defaults line
  that was read (it turns defaults off) is written before the rows, padded to their
  width. Every row reads as it did to a reader given the scrub's defaults and
  `strict`. Defaults a caller passes still fill the rows' empty cells, as in 5.0, but
  are not written as a defaults line, and the caller's list is not changed. 5.0 and
  3.39 dropped the lines, so rows appended later got no defaults.
- **The header stays on line 1.** A scrub given no header (`tsvz scrub` of a `.tsv`,
  `scrubTabularFile(path)`) and a `TSVZed` given none, of a file
  that has a header line, keep that line first, with a `TSVZed`'s defaults line right
  after it. 5.0 and 3.39 put an empty line in front of it, so a reader given the header
  read the header as a row. A header-less file keeps the empty first line it begins
  with, and new files and clears given no header still get one, as in 5.0.
- **Interrupts.** A scrub or clear of either dialect keeps a copy of only the bytes it
  overwrites, to put back if it is interrupted before the file is cut to its new
  length; once the cut is made the rewrite is done and the new content stays. 5.0 kept
  a copy of the whole `.tsvz` part and put it all back. (A scrub still reads the whole
  store, and a `.tsvz` clear reads it too, to keep its markers.)
- **A compressed store cleared to nothing is one empty stream.** A `.tsvz.gz` (or
  `.bz2`, `.xz`, `.zst`) cleared or scrubbed to nothing holds one empty compressed
  stream, which `bzcat`, `xzcat` and Python's `bz2` and `lzma` can read; 5.0 left 0
  bytes.
- **A header-checking clear of an empty file writes the header it was given.** A clear
  that checks the header (`verifyHeader=True`, `tsvz clear --x-header H --x-strict`) of a
  0-byte `.tsv`, or of a compressed file with no byte or only empty streams (another
  tool's), takes it for an empty store with that header, as every reader does (L9), and
  writes H without a "Header mismatch" warning. 5.0 wrote an empty line and warned of a
  mismatch, so a strict `TSVZed` given H then refused the file, and raised on a 0-byte
  `.bz2`, `.xz` or `.zst`.
- **`tsvz --help`** says what `clear` keeps: a `.tsvz` its header and markers, a
  `.tsv` the `--x-header` it is given. (With `--x-strict` too, a `.tsv` clear keeps the
  file's own header line.)
  `TSVZed`'s message for a refused rewrite lists `hardMapToFile()` among the rewrites
  a program asks for.
- **Known in 5.1, fixed in 5.2: a scrub of a `.tsv` whose rows were all deleted did not
  compact it** when the scrub was given the file's header (`scrubTabularFile(path, header=H)`)
  or the file had no header line: it left the file as it was, deletion lines included;
  reads were right, as in 3.39 and 5.0. (`tsvz scrub` of a headed file read the header
  line as a row, so it did compact it.)
- **Known: a scrub run with other defaults than the store's program can change how
  early rows read.** In a `.tsv` with an empty `#_defaults_#` line and rows with empty
  cells before any defaults line, the reset line the scrub writes before the rows keeps
  those cells empty for a program that opens the store with its own defaults; run the
  scrub with the defaults that program uses (5.5: no longer; a scrub writes no reset).
- **Running programs keep 5.0** until they are restarted: long-running programs that
  import TSVZ, and running `tsvz serve` handlers.

## Upgrading to 5.0 from 4.9

- **Writes refuse text they cannot encode.** A key, value, header, defaults line or
  marker the file's encoding cannot hold (UTF-8 for `.tsvz`; `encoding=` for a
  `.tsv`), such as the lone surrogate `os.fsdecode` gives for a non-UTF-8 file name,
  raises `TSVZEncodeError` (a `ValueError`) and writes nothing; 4.9 stored `?` (its
  `tsvz` stored U+FFFD for a non-UTF-8 argument, and its `.tsvz` file functions raised
  `UnicodeEncodeError`).
  `errors='replace'` brings `?` back. Under a non-UTF-8 `encoding=`, a key read as
  U+FFFD cannot be deleted or updated. `tsvz` exits 1 with the message. See Fault
  tolerance.
- **A Ctrl-C no longer cuts a `.tsv` write.** Every write of a `.tsv`-family file
  runs on the store's background thread; a call waits for it, a Ctrl-C raises in the
  caller and the write still finishes, and at exit a write in progress is finished
  (a second Ctrl-C abandons it). In 4.9 a Ctrl-C while `TSVZed()` opened a
  compressed store (its rewrite on load) could leave the file with no readable row.
  A closed `TSVZed` or `TSVZedLite` is freed; 4.9 kept every one until exit.
- **Another process's change is taken in, not reloaded over memory.** An append to
  a `.tsv`-family file is read from where the store left off, so an order set by
  `move_to_end` and `#` keys survive it; any reload (both dialects) changes memory
  in place, so another thread never finds a live key missing. After a rewrite of
  the store's own fails part way, memory is kept and another process's appends are
  merged into it (Fault tolerance).
- **`.tsvz` stores** see another process's append made right after their load, a
  commit or a clear (4.9 missed it until the next change), keep the rows set while
  they reload, and raise a strict header mismatch at the first read.
- **`.tsv` files:** an append to an existing empty file writes the header first, and
  reading one gives an empty store; see L8 to L14 for the rest.
- **`tsvz`** runs every operation through the engine `tsvz serve` uses, with the same
  output. A server of a `.tsv` (or other tab- or NUL-delimited) file is now found by
  `tsvz` commands, `TSVZClient` and `tsvz stop`; 4.9's clients rejected its pointer
  file. The 3.39 warning that a file name does not end in `.tsv` is printed only
  when a command creates such a file, and a write that cannot be made reports the
  operating system's error.
- **Running programs keep 4.9.** Long-running programs that import TSVZ, and running
  `tsvz serve` handlers, keep 4.9 until they are restarted. 5.0's `tsvz stop` cannot
  stop a 4.9 handler of a `.tsv` (or other tab- or NUL-delimited) file: 4.9 wrote the
  delimiter into the pointer file's `x-delimiter` as `<sep>` (a NUL as `<NUL>`), which
  5.0 decodes to the TAB or NUL itself and refuses as a control character. Send that
  handler SIGTERM instead: its pid is on the `pid` line of its pointer file
  (`STORE.serve`), and it finishes its writes and exits.
- **macOS and other systems that lock with `lockf`:** as in 4.9, a read in another
  thread of the process can still drop a write's lock (5.3 locks with `flock` there:
  fixed).

## Breaking changes from 3.39

### `.tsv`, `.csv`, `.nsv`, `.psv`: the format is unchanged

| # | Change |
|---|---|
| L1 | Appending after an unterminated last line writes `\n` first; 3.39 merged the two lines. |
| L2 | Setting a row whose values are all empty deletes it in memory too (it already meant delete on disk), with a warning. |
| L3 | `TSVZedLite` on a compressed file keeps rows in memory; 3.39 read compressed bytes as text. Rows held in memory (also `#` keys) are returned directly. |
| L4 | New warnings on stderr (invalid encoding, lines dropped by `strict`); data unchanged. |
| L5 | A damaged compressed file is read up to the damage instead of raising; from 5.4 a member cut short after the first is left out and the members after it are read (a damaged first member stops the read). |
| L6 | `mapToFile` no longer glues a row it adds at the end of the file to the next one. |
| L7 | `TSVZed`'s `in` and `get` find a key as `t[key]` does: the live key equal to it, else the key right-stripped. 3.39 matched them exactly, so `'x ' in t` was false while `t['x ']` read `x`, and `t.setdefault('x ', …)` overwrote `x`. |
| L8 | Appending (`TSVZed`, `TSVZedLite`, `appendTabularFile`, `appendLinesTabularFile`) to an existing empty file (no byte, or a compressed file holding only empty streams) writes the store's header first, as creating the file does; 3.39 wrote the rows alone (a read then took the first one for the header), and `appendTabularFile` with `strict` refused the file. |
| L9 | Reading such an empty file with a header (and `verifyHeader`) gives an empty store with that header: `readTabularFile`, `TSVZed` and `TSVZedLite` with `strict` no longer refuse it with `Header mismatch` (a `TSVZed` restarted after a rewrite that left the file empty failed in its constructor), and without `strict` no longer print a header mismatch. |
| L10 | Writing text the file's encoding cannot hold (a lone surrogate from `os.fsdecode`, a character the `encoding=` lacks) raises `TSVZEncodeError`, a `ValueError`, and writes nothing; 3.39 and 4.9 wrote `?`, which `errors='replace'` brings back. |
| L11 | `pop()` and `popitem()` of a `#` key, which lives in memory only, write no tombstone line to the file. |
| L12 | A rewrite of a file whose header line has no newline writes the rows on lines of their own; 3.39 wrote the first one onto the header line. |
| L13 | A compressed file is rewritten in members of at most 1 MiB, as appends already were: the bytes differ, the rows read are the same, and 3.39 reads them. |
| L14 | `TSVZed` opening a compressed file in a codec this Python lacks (zstd before Python 3.14) raises `ImportError` instead of reading the compressed bytes as text. |
| L15 | `scrubTabularFile`, `clearTabularFile`, `tsvz scrub`, `tsvz clear` and `TSVZedLite.clear_file()` write the new content over the file and then cut it, under the file's lock; 3.39 emptied the file first, with no lock, so a reader could meet it empty. |
| L16 | A scrub writes only if the file is still what its read saw: it reads again up to three times, then writes nothing, with a warning (`tsvz scrub` exits 1, C6); 3.39 wrote over rows appended after its read. |
| L17 | `scrubTabularFile(lastLineOnly=True)` returns the last row and writes nothing; 3.39 wrote that row back as deletion lines, and the store was gone. |
| L18 | A scrub keeps every `#_defaults_#` line as the file has it, where it was, so every row reads as it did; 3.39 dropped them, so rows appended later got no defaults. |
| L19 | A scrub never changes line 1, except that a header you pass with `verifyHeader=False` is added; a `TSVZed` rewrite of a healthy file never changes it, unless a delete in a one-column file takes line 1 out or (5.6) line 1 is the start of a wider header the program gives; 3.39 put an empty line in front of a header line, and a reader given the header then read the header as a row. |
| L20 | A row wider than the column count is read whole (only its empty cells past the count go); 3.39 cut it. |
| L21 | `TSVZed` reads a one-column file's lone keys as rows, as `readTabularFile` does (3.39 showed none); a delete there takes the key's lines out at the next commit, not as a line; from 5.6 so do `tsvz delete`, `pop`, `popitem`, a handler and `TSVZedLite`, at once. |
| L22 | Writes never cut a row: a wider row is written whole; 3.39 cut it to the column count. A strict read takes a wider row whole and drops only a narrower one; a strict store refuses only a narrower row. |
| L23 | A scrub, and a `TSVZed` rewrite of a healthy file, drop lines and keep the lines they keep byte for byte, in place (a key's last line may move up into its first line's place; a dead key's line readers take their column count from is kept emptied); 3.39 wrote the rows it read. |
| L24 | A `TSVZed`'s whole reload starts from the defaults the program gave, not those its last read ended with. |
| L25 | A `TSVZed` rewrite writes its defaults line after the rows: a compacting rewrite after the file's lines and before the rows memory adds, only when the file does not already end with those defaults; a rewrite from memory after every row. 3.39 wrote it after the header. |
| L26 | `TSVZed` no longer rewrites the file when it opens it: `rewrite_on_load` defaults to `False`; every rewrite is written whole. |
| L27 | `move_to_end` appends a deletion line and the row; 3.39 reordered memory and rewrote the file. |

### `.tsvz`, `.csvz`, `.nsvz`, `.psvz`: now tsvz-spec-v1

| # | Change |
|---|---|
| S1 | `.csvz/.nsvz/.psvz` use `, \0 \|` (3.39: tab). A conflicting `delimiter=` or `encoding=` is overridden, with a warning. |
| S2 | A file written by 3.39 under one of these names reads differently (header line becomes data, `key\t\t` deletes come back, `</sep/>` stays literal, an unterminated last line is dropped, single-column files read as empty, empty cells are not defaulted, NULs are not stripped). **Rename such a file to `.tsv`.** |
| S3 | `t[missing]` returns the defaults row instead of raising `KeyError` (see above). |
| S4 | `#` keys are stored; `#_name_#` keys are marker writes. |
| S5 | Automatic rewrites are ignored with a warning; `rewrite_on_load` effectively defaults to `False`; `move_to_end` only reorders memory. |
| S6 | Rows are never trimmed or dropped for their width; `strict` no longer drops rows; `appendLinesTabularFile` does not pad. |
| S7 | The header is written as a `#` comment; a new file without a header is empty. |
| S8 | An unterminated tail is ignored on read and truncated on append. |
| S9 | `clear` keeps the active markers; scrub keeps the header comment and drops other comments, custom markers and checksums. |
| S10 | Files named `x.tsvz.<hex>` beside `x.tsvz` are read as parts of the store. |
| S11 | `lastLineOnly` reads forward (slower on large files). |
| S12 | `TSVZedLite.clear()` and `clear_file()` raise `OSError` when the clear is refused (a part could not be read, the path names one part of a store, or the store kept changing). Memory is reloaded from the unchanged store first. |
| S13 | Compressed parts are written in members of at most 1 MiB of records (`#_member_size_#`), Zstandard frames with a checksum; an append never reads or repairs the part. |
| S14 | A zero byte in a value is written `<NUL>`, and a raw zero byte in a record is read as zero-fill damage (spec §4.9): under the default `recover` only the bytes after it are read. **A value that 4.2 or 4.3 wrote with a raw zero byte therefore reads differently in 4.4.** Such values are rare; before upgrading, `grep -c -P '\x00' store.tsvz` (any `.tsvz`, `.csvz` or `.psvz` part) finds them. Stop 4.2 and 4.3 handlers before upgrading: they read `<NUL>` as text, as they read `<CR>`. |
| S15 | A lookup (`t[key]`, `in`, `get`, `del`, `pop`, `setdefault`) takes the live key equal to the one given before the right-stripped one, so a key ending in whitespace (written while `#_strip_trailing_whites_#` was false) can be read and deleted by name (spec §20.2.7). Setting a key still right-strips it. |

### The `tsvz` command

3.39 command lines still parse, including abbreviated long options (`--delim`)
and `-d=,`, except the ones C5 and C9 list.

| # | Change |
|---|---|
| C1 | `read` into a pipe or a file prints records instead of the table; `--format table` gives the table. |
| C2 | Every message goes to stderr: "Created …", "File not found", header mismatches, warnings. |
| C3 | `read` and `scrub` of a missing store exit 1 with a message (3.39: exit 0, and `read` printed "File not found" on stdout). |
| C4 | `delete` deletes every KEY it is given (3.39: only the first). |
| C5 | `append` or `delete` without a KEY, or with an empty KEY, is a usage error with exit 2 (3.39 wrote an empty line). |
| C6 | A `clear` or `scrub` that writes nothing (one part of a multi-part store, a store that kept changing, a part that declares a spec version newer than 1, per spec §19.4 and §19.8) exits 1. |
| C7 | `-c/--header`, `--defaults`, `-s/--strict` and `-f/--force` are undocumented aliases of the `--x-` options. `--x-header` and `--x-defaults` keep non-ASCII text. |
| C8 | Usage errors print tsvz's own message, exit 2; the `-h` text is new. |
| C9 | Arguments after STORE for `read`, `clear` and `scrub` are a usage error, exit 2 (3.39 ignored them, so `clear junk` cleared the file). |
| C10 | The table (a terminal, or `--format table`) of `get`, `keys`, `pop`, `popitem`, `setdefault`, and of `read` on a `.tsvz`-family store, shows the rows only: no row is drawn as a header, and no row is cut to the first row's or the terminal's width. `read` of a `.tsv`-family file keeps 3.39's table, whose first row is the file's header line (5.5: padded to the widest row, so no row is cut to it). |
| C11 | The table shows control characters, bidi controls and line separators as escapes (`\r`, `\n`, `\t`, `\xNN`, `\uNNNN`), so no value can move the cursor, reorder the line or restyle the terminal. It sizes columns in terminal columns, so CJK text (two columns a character) lines up. The table is for reading: a backslash in a value is shown as is, so use records for exact values. |
| C12 | While stripping is on, `get`, `has`, `pop`, `delete` and `setdefault` take the live key equal to a KEY ending in a space or tab before the stripped one (spec §20.2.7), so `tsvz keys s \| tsvz delete s -` deletes every key. `delete` of such a KEY reads the store first, and exits 1 without writing when the store cannot be read whole. |

## Spec deviations and interpretations

None of these fails a conformance case: `conformance/reference-baseline.json`,
which lists the cases this implementation does not pass, is empty (see
Conformance testing).

- §17.5: a numbered part opened directly is read alone, with a warning.
- §14.5: the defaults-on-missing read applies to `t[key]` only.
- §18.6: `.tsv`-family readers hold the access lock shared only while they read (byte 0, through the gate at byte 2; a `TSVZed` checks what it read before by identity, size and bytes), appends take the append lock (byte 1), and rewrites take it, the gate, then the whole file; on systems other than Linux both are one whole-file `flock`. What remains a deviation: `.tsvz` part readers take no lock (they read again until the parts keep still); where only `lockf` works, writers use it and readers do not lock; Windows readers do not lock; and one-shot appends, the making of a file, scrubs, clears and `.tsvz` part writes also take an in-process lock per path (`_pathLock`), which a `TSVZed`'s own writes do not. See [docs/limitations.md](docs/limitations.md).
- §19.4: a scrub whose rows would read differently under the final markers writes them with stripping and fill-empty off, then restores the final markers after the rows.
- §20.7: `-q` also silences informational messages ("Created …"); only errors remain.
- §20.2: `scrub` of a multi-part store, and `clear` of one part of one, are refused with exit 1; `read`, `get` and `parts` of a numbered part read that part alone, with a warning.
- §20.2.4: on `.tsv`-family files `get` prints nothing for a missing key, `verify` checks nothing, `parts` lists the file, and in a file of one column (where a lone key is a row, so a deletion line deletes nothing) `delete`, `pop` and `popitem` take the key's lines out of the file, a whole rewrite, not an appended line.
- §20.3: a bulk input line with an empty key is skipped, with a warning.
- §20.6: a write whose store cannot be created, and a `read`, `get` or `verify` that cannot read every part, or whose parts kept changing through three reads, exit 1 with an error that `-q` keeps.
- §21: a served `.tsv` file with a header line shows that line as a row, as `tsvz read` does (`TSVZed` hides it when given the header).
- §21.8: the server notices changes with `stat()`. It re-reads the part list while the store's directory changed less than 2 s ago, and reloads a part whose size stayed the same but whose inode, modification time or change time changed. The change time catches a rewrite whose modification time was set back (`cp -p`, `rsync --inplace -a`); a rewrite in place that keeps a part's size within one clock tick is not seen until the part changes again.
- §21.9: `tsvz` commands and `TSVZClient` ask the server to answer writes once they are written (`--x-written`), so a write the server cannot make fails as it would on the files. While writes fail, every answer carries a warning.
- §21.3: `tsvz serve` needs Unix-domain sockets. Where Python has none (Windows), it exits 1, and `tsvz` commands and `TSVZClient` work on the files.
- §20.7: on `.tsv`-family files a `-d` longer than one character is used, as in 3.39, with a warning; a `-d` that does not decode is a usage error.

## Website

The tsvz.org site lives in [tsvz-org/tsvz.org](https://github.com/tsvz-org/tsvz.org).
It renders `tsvz-spec-v1.md` from a checkout of this repository next to its own,
and its tests also check the spec: every § reference names a real section, and
the spec uses only markdown the site renders. Run them after editing the spec
(see Tests); CI runs them on every push here, and the site publishes `main`'s
spec each morning.

## Conformance testing

`tsvzconform.py` tests how closely an implementation of tsvz-spec-v1, in any
language, follows the spec. Its cases are JSON files in `conformance/cases/`,
each naming the § clauses it tests, and it drives an implementation only
through the spec's command line (§20) and handler socket (§21).

```bash
python3 tsvzconform.py --impl ./tsvz --feature crc32,sha256,handler
python3 tsvzconform.py --impl ./tsvz --class reader --only 13
```

Each failure is printed with its input, what was expected, what came back and
a command line that reruns it alone; the summary gives a verdict for each
conformance class (reader, writer, cli, handler). `conformance/README.md`
explains the options and the features to claim, and how to load the cases into
another language's tests.

## Benchmarking implementations

`tsvzbench.py` compares implementations of tsvz-spec-v1, in any language, side
by side on one machine. It drives each one only through the spec's command
line (§20) and, when it has one, its handler socket (§21), so any conformant
`tsvz` tool can be measured as it is.

```bash
python3 tsvzbench.py --impl ref='python3 TSVZ.py' --impl new='../tsvz-rs/target/release/tsvz' \
                     --suite quick --work-dir /mnt/fast-disk/tsvzbench
```

- `--impl NAME=COMMAND`, repeatable: the first is the baseline, and every other
  implementation gets a `×` ratio against it (below 1 is faster). A relative
  COMMAND runs from the directory you start tsvzbench in.
- `--suite smoke|quick|standard|large`: `smoke` checks a setup in seconds,
  `quick` takes about a minute, `standard` (the default, up to 1M rows) about
  five, and `large` (up to 10M rows) twenty or more and ~5 GB of memory for the
  harness itself.
- `--only cold,bulk,perop,maint` picks families: one-shot replays, `set -` and
  `delete -` batches, single operations (CLI and handler), and scrub/clear.
- `--work-dir DIR` keeps the generated fixtures between runs. Put it on the disk
  you mean to measure: not a tmpfs, and not a FUSE file system such as mergerfs
  (the harness warns about those). The default is a `tsvzbench-*` directory here.
- `--json PATH` also writes every raw sample.

The harness writes its own input stores, so every implementation reads the same
bytes, and checks every result against its own replay model: `✓` right, `✗`
wrong output or files, `! N` an unexpected exit status, `⏱` timed out, `refused`
an allowed refusal. It exits 1 when any cell failed. Each command runs under a
small launcher that measures it with `wait4`; a peak RSS shown as `≤8.0 MiB` is
the launcher's own size, so the true peak is no higher. Compare an
implementation with itself to see the noise: a few percent per cell, but the
socket rows, whose latencies are tens of microseconds, can move 10–20% between
runs, because a single run can land in a slower mode as a whole.

## Tests

```bash
python3 -m pytest TSVZ_test.py -q                         # everything, incl. 3.39 differential tests
python3 -m pytest TSVZ_lock_test.py -q                    # file locks between processes and threads (POSIX)
python3 -m pytest ../tsvz.org/tsvz_site_test.py -q        # the spec as tsvz.org renders it (needs the tsvz.org checkout next to this one)
python3 -m pytest tsvzbench_test.py -q                    # the benchmark harness
python3 -m pytest tsvzconform_test.py -q                  # the conformance runner and cases, against the reference
python3 benchTSVZ.py /tmp/b.tsv -n 100000 --compare-old    # benchmark (not a test)
python3 legacyfuzz.py pair --ver OLD/TSVZ.py --ver-b TSVZ.py  # the legacy scrub and rewrite differential fuzzer; see its docstring; run it in a container
```

`TSVZ_old.py` is the frozen 3.39 module the differential tests compare against;
it is not installed. CI (`.github/workflows/test.yml`) runs the four test files on
Python 3.6, 3.9, 3.12 and 3.14. Accepted limitations are listed in
[docs/limitations.md](docs/limitations.md).

## License

GPL-3.0-or-later © Yufei Pan (pan@zopyr.us)
