.PHONY: check lint test examples

check: lint test examples

lint:
	ruff check examples
	ruff format --check examples

test:
	cd examples/ch01 && pytest -q
	cd examples/ch02 && pytest -q
	cd examples/ch03 && pytest -q
	cd examples/ch04 && pytest -q
	cd examples/ch05 && pytest -q
	cd examples/ch06 && pytest -q
	cd examples/ch07 && pytest -q
	cd examples/ch08 && pytest -q

# Each chapter's example must run from a clean checkout. Chapter 1's checker
# exits 1 on purpose (N7 is left incomplete), so only its --markdown mode is
# required to succeed here.  Chapter 3's validate also exits 1 on purpose
# (two sample records are bad), so the other three tools stand for it.
# Chapter 7's scan exits 1 on purpose over samples/ (four planted secrets),
# so that line is negated.
examples:
	cd examples/ch01 && python never_list.py never_list.json --markdown > /dev/null
	cd examples/ch02 && python data_path.py place computations.json > /dev/null
	cd examples/ch02 && python data_path.py traffic paths.json > /dev/null
	cd examples/ch03 && python telemetry.py decode 018507ee1b263e3ee40e75 > /dev/null
	cd examples/ch03 && python telemetry.py settings samples/settings_desired.json > /dev/null
	cd examples/ch03 && python telemetry.py growth streams.json > /dev/null
	cd examples/ch04 && python firmware_sim.py table > /dev/null
	cd examples/ch04 && python firmware_sim.py interlock > /dev/null
	cd examples/ch04 && python firmware_sim.py compare --outage 120:420 > /dev/null
	cd examples/ch04 && python firmware_sim.py run --hose-burst > /dev/null
	cd examples/ch05 && python layers.py coverage layers.json > /dev/null
	cd examples/ch05 && python layers.py compare > /dev/null
	cd examples/ch05 && python layers.py run --fail ssr-short --layers interlock,gate > /dev/null
	cd examples/ch05 && python layers.py sizing > /dev/null
	cd examples/ch06 && python link.py airtime > /dev/null
	cd examples/ch06 && python link.py protocol > /dev/null
	cd examples/ch06 && python link.py budget nodes.json links.json > /dev/null
	cd examples/ch06 && python link.py select nodes.json links.json > /dev/null
	cd examples/ch06 && python link.py backoff > /dev/null
	cd examples/ch07 && python identity.py inventory inventory.json > /dev/null
	cd examples/ch07 && python identity.py inventory inventory_real.json > /dev/null
	cd examples/ch07 && python identity.py blast > /dev/null
	cd examples/ch07 && python identity.py provision > /dev/null
	cd examples/ch07 && python identity.py rotate > /dev/null
	cd examples/ch07 && ! python identity.py scan samples > /dev/null
	cd examples/ch08 && python cloud.py fit requirements.json > /dev/null
	cd examples/ch08 && python cloud.py cost fleet.json prices.json > /dev/null
	cd examples/ch08 && python cloud.py cost --status-period-s 300 --history-retention-days 30 > /dev/null
	cd examples/ch08 && python cloud.py fanout fleet.json prices.json > /dev/null
	cd examples/ch08 && python cloud.py rules firebase > /dev/null
	cd examples/ch08 && python cloud.py rules aws > /dev/null
	cd examples/ch08 && python cloud.py rules mosquitto > /dev/null
	cd examples/ch08 && python cloud.py rules map > /dev/null
