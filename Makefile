.PHONY: harness-unit rust-quality android-quality framework-fixtures integration benchmark quality

PYTHON ?= python3

harness-unit:
	$(PYTHON) -m unittest discover -s tests -v

rust-quality:
	cargo fmt --check
	cargo clippy --all-targets -- -D warnings
	cargo test --locked
	cargo build --release --locked

android-quality:
	cd agent-android && ./gradlew test lint assembleDebug

framework-fixtures:
	cd fixtures/reactnative && npm ci
	cd fixtures && ./gradlew :compose:assembleDebug :reactnative:assembleDebug

integration:
	$(PYTHON) scripts/integration.py

benchmark:
	cargo build --release --locked
	ASHNAZG_BIN="$(CURDIR)/target/release/ashnazg" $(PYTHON) scripts/benchmark.py --iterations 10 --output artifacts/benchmark.json

quality: harness-unit rust-quality android-quality framework-fixtures
