.PHONY: install run demo test clean

install:
	python -m pip install -r requirements.txt

run:
	uvicorn app.main:app --reload

demo:
	python seed_demo.py
	uvicorn app.main:app --reload

test:
	python -m pytest

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache data/*.db data/*.sqlite data/*.sqlite3
