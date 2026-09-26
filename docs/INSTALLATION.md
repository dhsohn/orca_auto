# Installation

**English** | [한국어](INSTALLATION.ko.md)

ORCA_auto is a queue runner and execution manager for ORCA on Linux and WSL2.

---

## System Requirements

- **Operating System**: Linux or WSL2 (Ubuntu 20.04 LTS or newer recommended)
- **Python**: 3.11+
- **Service Manager**: `systemd` (for supervised background execution)
- **ORCA Engine**: Separately installed ORCA executable (compatible with ORCA 5.x and 6.x)

> **Upgrading**: If you are upgrading from 6.x or earlier, consult the [7.0 Upgrade Guide](RELEASE.md#upgrading-to-70).

---

## 1. PyPI Installation

Install the package into an isolated virtual environment:

```bash
# Create and activate a virtual environment
python3 -m venv ~/.local/share/orca_auto/venv
source ~/.local/share/orca_auto/venv/bin/activate

# Install ORCA_auto
pip install --upgrade pip
pip install orca_auto==8.0.1

# Verify installation
orca_auto --version
```

---

## 2. Configuration & Service Setup

After installation, initialize your configuration file:

```bash
# Initialize configuration
orca_auto init --config ~/orca_auto.yaml
```

### Background Execution with systemd
To supervise workers in the background, register systemd worker units. The installer requires `--repo` pointing to either a local repository checkout (containing `.venv`) or a prepared runtime root:

```bash
# Register systemd worker units for the current user (from checkout or prepared runtime)
orca_auto systemd install --user "$(id -un)" --repo /path/to/orca_auto --config ~/orca_auto.yaml

# Verify worker and runtime status
orca_auto service status
```

> **Note**: For interactive sessions or direct command-line use without systemd, you can enqueue jobs with `orca_auto run-dir` and run the supervisor in the foreground via `orca_auto queue worker`.

Continue with the [Quickstart Guide](QUICKSTART.md) to submit and inspect calculations.

---

## 3. Production Deployment (Optional)

For production workstations or shared lab machines, see [Prepared Production Runtimes](RUNTIME.md) for immutable wheel-based deployment with offline verification and idle cutover procedures.

---

## 4. Development Installation (Source Checkout)

To develop or contribute to ORCA_auto:

```bash
git clone https://github.com/dhsohn/orca_auto.git
cd orca_auto

python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Run linters, type checks, and tests
make check
```

See the [Development Guide](DEVELOPMENT.md) for architecture rules and contribution guidelines.
