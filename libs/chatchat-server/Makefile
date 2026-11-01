.PHONY: all format lint test tests test_watch integration_tests docker_tests help extended_tests

# Default target executed when no arguments are given to make.
all: help

######################
# TESTING AND COVERAGE
######################

# Define a variable for the test file path.
TEST_FILE ?= tests/unit_tests/

# 解释器。仓库没有 poetry.lock，本机约定是直接用 Conda 环境的 python，所以这里不再
# 硬写 `poetry run`（实测这台 Windows 机器上既没有 poetry 也没有 make，
# `make test` 连启动都做不到）。默认取 PATH 上的 python，需要指定环境时显式传：
#   make test PYTHON=/home/lab239/anaconda3/envs/Langchain-Chatchat/bin/python
PYTHON ?= python

# 测试期间的网络封锁。上游写的是 `--disable-socket --allow-unix-socket`，在 Linux 上
# 成立，在 Windows 上不成立：asyncio 的 self-pipe 用 socketpair()，Windows 上走的是
# AF_INET 回环，于是被一起拦掉。实测 test_spa_static + test_auth + test_api_contract
# 三个文件在该口径下 26 failed / 16 errors，全是 SocketBlockedError；去掉封锁后
# 单是 test_spa_static 就 30 passed。改成只放行回环：外部连接仍然被拦（实测连
# pypi.org 报 SocketConnectBlockedError: allowed "127.0.0.1,::1,localhost"），
# 而这三个文件恢复 88 passed。封锁的目的（测试不许偷跑外网）保留，平台可用性修好。
SOCKET_GUARD = --allow-hosts=127.0.0.1,localhost,::1

# Run unit tests and generate a coverage report.
coverage:
	poetry run pytest --cov \
		--cov-config=.coveragerc \
		--cov-report xml \
		--cov-report term-missing:skip-covered \
		$(TEST_FILE)

test tests:
	$(PYTHON) -m pytest $(SOCKET_GUARD) $(TEST_FILE)

extended_tests:
	$(PYTHON) -m pytest $(SOCKET_GUARD) --only-extended tests/unit_tests

test_watch:
	ptw --snapshot-update --now . -- -x $(SOCKET_GUARD) tests/unit_tests

test_watch_extended:
	ptw --snapshot-update --now . -- -x $(SOCKET_GUARD) --only-extended tests/unit_tests

integration_tests:
	poetry run pytest tests/integration_tests

scheduled_tests:
	poetry run pytest -m scheduled tests/integration_tests


######################
# LINTING AND FORMATTING
######################

# Define a variable for Python and notebook files.
PYTHON_FILES=.
MYPY_CACHE=.mypy_cache
lint format: PYTHON_FILES=.
lint_diff format_diff: PYTHON_FILES=$(shell git diff --relative=libs/langchain --name-only --diff-filter=d master | grep -E '\.py$$|\.ipynb$$')
lint_package: PYTHON_FILES=chatchat
lint_tests: PYTHON_FILES=tests
lint_tests: MYPY_CACHE=.mypy_cache_test

lint lint_diff lint_package lint_tests:
	./scripts/check_pydantic.sh .
	./scripts/lint_imports.sh
	poetry run ruff .
	[ "$(PYTHON_FILES)" = "" ] || poetry run ruff format $(PYTHON_FILES) --diff
	[ "$(PYTHON_FILES)" = "" ] || poetry run ruff --select I $(PYTHON_FILES)
	[ "$(PYTHON_FILES)" = "" ] || mkdir -p $(MYPY_CACHE) && poetry run mypy $(PYTHON_FILES) --cache-dir $(MYPY_CACHE)

format format_diff:
	[ "$(PYTHON_FILES)" = "" ] || poetry run ruff format $(PYTHON_FILES)
	[ "$(PYTHON_FILES)" = "" ] || poetry run ruff --select I --fix $(PYTHON_FILES)

spell_check:
	poetry run codespell --toml pyproject.toml

spell_fix:
	poetry run codespell --toml pyproject.toml -w

######################
# HELP
######################

help:
	@echo '-- LINTING --'
	@echo 'format                       - run code formatters'
	@echo 'lint                         - run linters'
	@echo 'spell_check               	- run codespell on the project'
	@echo 'spell_fix               		- run codespell on the project and fix the errors'
	@echo '-- TESTS --'
	@echo 'coverage                     - run unit tests and generate coverage report'
	@echo 'test                         - run unit tests'
	@echo 'tests                        - run unit tests (alias for "make test")'
	@echo 'test TEST_FILE=<test_file>   - run all tests in file'
