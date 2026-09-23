# Development

**English** | [한국어](DEVELOPMENT.ko.md)

Develop in an isolated worktree with Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check
make check-packages
bash examples/fake_orca_smoke/run.sh
```

`src/orca_auto` is the only source root. Keep the `orca` → `core` direction
and CLI boundaries. Do not alter an environment serving active calculations.
Use disposable inputs/fake engines in tests; changes to real engine behavior
require the acceptance in [VALIDATION](VALIDATION.md). Follow [RELEASE](RELEASE.md)
for versions and publication, and [RUNTIME](RUNTIME.md) for deployment.
