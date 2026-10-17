.PHONY: check lint test examples

check: lint test examples

lint:
	ruff check examples
	ruff format --check examples

test:
	cd examples/ch01 && pytest -q
	cd examples/ch02 && pytest -q
	cd examples/ch03 && pytest -q

# Each chapter's example must run from a clean checkout. Chapter 1's checker
# exits 1 on purpose (N7 is left incomplete), so only its --markdown mode is
# required to succeed here.  Chapter 3's validate also exits 1 on purpose
# (two sample records are bad), so the other three tools stand for it.
examples:
	cd examples/ch01 && python never_list.py never_list.json --markdown > /dev/null
	cd examples/ch02 && python data_path.py place computations.json > /dev/null
	cd examples/ch02 && python data_path.py traffic paths.json > /dev/null
	cd examples/ch03 && python telemetry.py decode 018507ee1b263e3ee40e75 > /dev/null
	cd examples/ch03 && python telemetry.py settings samples/settings_desired.json > /dev/null
	cd examples/ch03 && python telemetry.py growth streams.json > /dev/null
