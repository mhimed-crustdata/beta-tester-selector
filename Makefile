.PHONY: setup run demo test
setup:
	@test -f .env || cp .env.example .env; echo "Edit .env and set CRUSTDATA_API_KEY"
run:
	python3 selector.py --users examples/users.example.csv
demo:
	python3 selector.py --users examples/users.example.csv --dry-run --all
test:
	python3 -m unittest discover -s tests -v
