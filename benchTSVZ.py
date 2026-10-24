#!/usr/bin/env python3
"""Throughput benchmark for TSVZ (TSVZed / TSVZedLite)."""
import argparse
import time

import TSVZ

RESOURCE_LIB_AVAILABLE = True
try:
	import resource
except ImportError:
	RESOURCE_LIB_AVAILABLE = False

version = '4.1'


def format_bytes(size):
	"""A byte count in binary units, e.g. 1536 -> '1.50 Ki' (the caller adds the 'B')."""
	n = 0
	while size >= 1024 and n < 5:
		size /= 1024
		n += 1
	return f"{size:.2f} {('', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi')[n]}"


def get_resource_usage(return_dict=False):
	try:
		if RESOURCE_LIB_AVAILABLE:
			raw = resource.getrusage(resource.RUSAGE_SELF)
			resource_dict = {
				'user mode time': f'{raw.ru_utime} seconds',
				'system mode time': f'{raw.ru_stime} seconds',
				'max resident set size': f'{format_bytes(raw.ru_maxrss * 1024)}B',
				'shared memory size': f'{format_bytes(raw.ru_ixrss * 1024)}B',
				'unshared memory size': f'{format_bytes(raw.ru_idrss * 1024)}B',
				'unshared stack size': f'{format_bytes(raw.ru_isrss * 1024)}B',
				'cached page hits': f'{raw.ru_minflt}',
				'missed page hits': f'{raw.ru_majflt}',
				'swapped out page count': f'{raw.ru_nswap}',
				'block input operations': f'{raw.ru_inblock}',
				'block output operations': f'{raw.ru_oublock}',
				'IPC messages sent': f'{raw.ru_msgsnd}',
				'IPC messages received': f'{raw.ru_msgrcv}',
				'signals received': f'{raw.ru_nsignals}',
				'voluntary context sw': f'{raw.ru_nvcsw}',
				'involuntary context sw': f'{raw.ru_nivcsw}',
			}
			if return_dict:
				return resource_dict
			return '\n'.join('\t'.join(line) for line in resource_dict.items())
	except Exception as e:
		print(f"Error: {e}")
	return {} if return_dict else ''


def _open_store(path, *, lite=False, flush_interval=0.01, verbose=False):
	if lite:
		return TSVZ.TSVZedLite(path, strict=False, verbose=verbose)
	return TSVZ.TSVZed(path, append_check_delay=flush_interval, verbose=verbose)


def _print_usage(verbose):
	if verbose:
		print(get_resource_usage())


if __name__ == '__main__':
	parser = argparse.ArgumentParser(description='Benchmark for TSVZ')
	parser.add_argument('file_name', type=str, help='File to benchmark (.tsv for the 3.39 format, .tsvz for spec v1)')
	parser.add_argument('-n', '--number', type=int, default=1_000_000,
						help='Number of entries to write (default: 1M)')
	parser.add_argument('--lite', action='store_true',
						help='Use TSVZedLite instead of TSVZed')
	parser.add_argument('--scrub', action='store_true',
						help='Run scrubTabularFile after the writes')
	parser.add_argument('--compare-old', action='store_true',
						help='Also time a full readTabularFile load with TSVZ 3.39 (TSVZ_old) and this TSVZ')
	parser.add_argument('--flush-interval', type=float, default=0.01,
						help='TSVZed append_check_delay in seconds')
	parser.add_argument('-v', '--verbose', action='store_true',
						help='Print resource usage and extra detail')
	parser.add_argument('-V', '--version', action='version', version=f'%(prog)s {version}')
	args = parser.parse_args()

	store_label = 'TSVZedLite' if args.lite else 'TSVZed'
	start = time.perf_counter()
	store = _open_store(args.file_name, lite=args.lite, flush_interval=args.flush_interval)
	print(f'Time to create / load {store_label}: {time.perf_counter() - start:.3f} seconds')
	_print_usage(args.verbose)

	start = time.perf_counter()
	for i in range(args.number):
		store[str(i)] = [str(i)] + [str(id(i))] * 19
	store.close()
	del store
	elapsed = time.perf_counter() - start
	print(f'Time to write {args.number} entries: {elapsed:.3f} seconds')
	if args.number:
		print(f'Rate: {args.number / elapsed:,.0f} entries/s')
	_print_usage(args.verbose)

	if args.scrub:
		start = time.perf_counter()
		TSVZ.scrubTabularFile(args.file_name)
		print(f'Time to scrub {args.number} entries: {time.perf_counter() - start:.3f} seconds')
		_print_usage(args.verbose)

	if args.compare_old:
		import TSVZ_old
		for label, module in (('TSVZ 3.39', TSVZ_old), ('TSVZ ' + TSVZ.version, TSVZ)):
			start = time.perf_counter()
			module.readTabularFile(args.file_name, verifyHeader=False, strict=False)
			print(f'Time to load with {label}: {time.perf_counter() - start:.3f} seconds')
