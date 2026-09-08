PY    ?= .venv/bin/python
ROBOT ?= .venv/bin/robot

.PHONY: test atest coverage report

test:
	$(PY) -m pytest tests/ -q

atest:
	$(ROBOT) --outputdir results atest/

# Combined branch coverage (pytest + Robot) and the docs/coverage.html report.
# Robot keyword-adapter lines only count when Robot itself runs under coverage.
coverage:
	rm -f .coverage .coverage.*
	$(PY) -m coverage run --branch --source=cdpbrowser -m pytest tests/ -q
	mv .coverage .coverage.pytest
	$(PY) -m coverage run --branch --source=cdpbrowser -m robot --outputdir results atest/
	mv .coverage .coverage.robot
	$(PY) -m coverage combine
	$(PY) -m coverage json -o coverage.json --quiet
	$(PY) tools/generate_coverage_report.py

report:
	$(PY) tools/generate_coverage_report.py
