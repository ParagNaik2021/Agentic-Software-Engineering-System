.PHONY: install test lint format demo demo-greenfield demo-brownfield demo-ambiguous demo-faults demo-all clean

install:
	pip install -e ".[dev,target]"

test:
	pytest

lint:
	ruff check src tests
	mypy src

format:
	ruff format src tests
	ruff check --fix src tests

demo: demo-greenfield

demo-greenfield:
	agentic run greenfield --mode replay

demo-brownfield:
	agentic run brownfield --mode replay

demo-ambiguous:
	agentic run ambiguous --mode replay

demo-faults:
	agentic run brownfield --mode replay --inject-fault "node=verify.unit,kind=quality,persist=true"

demo-all: demo-greenfield demo-brownfield demo-ambiguous demo-faults

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} +
