# Consumer setup

## Dependency ownership

Install mise and native KiCad 8 or later, including the standard libraries and Python bindings.
Linux headless fabrication builds also need `xvfb-run`.
Install Fabrication Toolkit through KiCad's Plugin and Content Manager, or set `FABRICATION_TOOLKIT_DIR` to an installed toolkit directory.

KKH installs and loads its own runtime toolchain when you invoke `kkh` or `kkh-analyze-schematic`.
Consuming repositories do not need Java, Clojure or Babashka pins for these commands.
The standalone analyzer uses the same pinned runtime as the project tasks.

`KICAD_CLI` can select the native KiCad CLI executable.
`KICAD_PYTHON` can select a Python interpreter that can import `pcbnew`.
These overrides are optional when the normal installation is discoverable.

## Pin KKH in a repository

Add this definition to the consuming repository's `mise.toml`, replacing `<full-commit-sha>` with the desired revision:

```toml
[tools."http:kkh"]
version = "<full-commit-sha>"
url = "https://github.com/lynaghk/kevins-kicad-helpers/archive/{{ version }}.tar.gz"
strip_components = 1
bin_path = "bin"
```

Run `mise trust`, `mise install` and `mise lock` from that repository.
Commit its `mise.toml` and `mise.lock`.
Load the environment with `eval "$(mise env -s bash)"` before invoking KKH in a noninteractive shell.

The installed `bin/` directory provides `kkh`, `kkh-analyze-schematic`, `fabrication-toolkit` and the other helpers.
Call these commands directly instead of shadowing them with local adapters.

## Install JLCPCB libraries

Open KiCad once to create its user configuration, then close KiCad and its editors.
Run:

```sh
kkh setup-kicad-libraries --jlcpcb-version 2026.08.08
```

The command works outside a Git repository and does not discover or modify PCB projects.
It detects the installed KiCad major version and installs the selected release for that version on native macOS or Linux.
Flatpak and Windows library setup are not supported.

The installer downloads the release ZIP from CDFER/JLCPCB-Kicad-Library and checks its pinned SHA-256 digest before extraction.
Version 2026.08.08 is currently the only supported release.
Adding another release requires an explicit version and checksum entry in `kicad-libraries/setup_kicad_libraries.py`.

The installer registers `PCM_JLCPCB-*` symbol libraries and the `PCM_JLCPCB` footprint library globally.
It preserves unrelated registrations and the standard libraries.
Library and model references use the installed KiCad version's `${KICAD<major>_3RD_PARTY}` variable, so placed footprints do not embed a user's home directory.
The installer honors that variable's override in the environment or in `kicad_common.json`.

Default installation roots are:

- macOS: `~/Documents/KiCad/<major>.0/3rdparty/kkh-jlcpcb/2026.08.08/`
- Linux: `~/.local/share/kicad/<major>.0/3rdparty/kkh-jlcpcb/2026.08.08/`

Linux uses `XDG_CONFIG_HOME` and `XDG_DATA_HOME` when set.
`KICAD_CONFIG_HOME` overrides the configuration base directory; KiCad's version directory is appended to it.
The verified archive is cached under the KiCad user data directory at `libraries/jlcpcb/2026.08.08.zip`.

Rerunning setup repairs the installed files without changing correct library tables.
Run it again after installing a new KiCad major version.
KiCad can then be opened normally from the desktop without mise in its environment.

### Existing installations and backups

The libraries installed by this command are manually managed, not PCM-managed.
If you switch from PCM, uninstall its JLCPCB package before running setup.
Fabrication Toolkit remains a separate dependency.

The command stops before changing registrations that point to another JLCPCB installation.
Inspect the reported conflicts, then explicitly approve replacement if appropriate:

```sh
kkh setup-kicad-libraries --jlcpcb-version 2026.08.08 --yes
```

Each changed existing library table is copied to a uniquely named `<table>.kkh-<timestamp>.bak` file beside the original.
The command prints each backup path.
To restore a table, close KiCad and copy its backup over the original file.
Existing installations at other paths are not deleted.

## Thin repository entry points

A consuming repository can keep `scripts/setup` for discovery and select its JLCPCB version there:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
eval "$(mise env -s bash)"
exec kkh setup-kicad-libraries --jlcpcb-version 2026.08.08 "$@"
```

A `scripts/build.sh` entry point can use the same shell setup and finish with `exec kkh build "$@"`.
Project-specific tests can invoke `kkh check` before running their own checks.

Builds always emit `bom.csv` and `positions.csv`.
Boards with nothing to assemble receive header-only files.
Populated BOMs require placement output, and placement rows are filtered to the parts listed in the BOM.
