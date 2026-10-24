# Installation

**English** | [한국어](INSTALLATION.ko.md)

Install ORCA_auto in a virtual environment, then connect it to your ORCA executable and background worker.

---

## System Requirements

- **Operating System**: Linux or WSL2 (Ubuntu 20.04 LTS or newer recommended)
- **Python**: 3.11+
- **Service Manager**: `systemd` (for supervised background execution)
- **ORCA Engine**: Separately installed ORCA executable (acceptance-tested with ORCA 6.1.1; other versions are unverified)

For upgrades from 6.x or earlier, start with the [7.0 Upgrade Guide](RELEASE.md#upgrading-to-70).

---

## 1. PyPI Installation

Install the package into an isolated virtual environment:

```bash
# Create and activate a virtual environment
python3 -m venv ~/.local/share/orca_auto/venv
source ~/.local/share/orca_auto/venv/bin/activate

# Install ORCA_auto
pip install --upgrade pip
pip install orca_auto==10.2.3

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

To receive job notifications, configure [Discord](DISCORD_SETUP.md) or [Slack](SLACK_SETUP.md). Slack is available from 10.0.0.

### Background Execution with systemd

The package includes service templates. Run these commands from the activated virtual environment to use its Python. To use a source checkout or prepared runtime, add `--repo PATH`.

~~~bash
orca_auto systemd install --user "$(id -un)" --config ~/orca_auto.yaml
orca_auto service status
~~~

For a foreground session without systemd, submit jobs with `orca_auto run-dir` and run the supervisor with `orca_auto queue worker`.

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
