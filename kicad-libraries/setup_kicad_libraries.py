import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

JLCPCB_RELEASES = {
    "2026.08.08": "b42dee2bc083675756eb08e05ce47b1651b441c1e0c1bda6e04bbf8109b01cf6",
}


def main():
    parser = argparse.ArgumentParser(
        prog="kkh setup-kicad-libraries",
        description="Install a pinned JLCPCB release in the user's global KiCad libraries.",
        epilog="Close KiCad first. Requires native KiCad 8 or later on macOS or Linux. "
        "Libraries are manually managed, not installed through Content Manager.",
    )
    parser.add_argument(
        "--jlcpcb-version",
        default="2026.08.08",
        choices=sorted(JLCPCB_RELEASES),
        help="JLCPCB release to install (default: 2026.08.08)",
    )
    parser.add_argument("--yes", action="store_true", help="replace conflicting JLCPCB registrations")
    args = parser.parse_args()
    ensure_kicad_closed()
    version = installed_kicad_version()
    config, data, templates = installation_paths(version)
    if not config.is_dir():
        sys.exit(f"Open KiCad {version} once first; configuration is missing: {config}")

    variable = f"KICAD{version.split('.')[0]}_3RD_PARTY"
    destination = third_party_path(config, data, variable) / "kkh-jlcpcb" / args.jlcpcb_version
    library_uri = Path(f"${{{variable}}}/kkh-jlcpcb/{args.jlcpcb_version}")
    cache = data / "libraries/jlcpcb" / f"{args.jlcpcb_version}.zip"
    archive_bytes = release_archive(args.jlcpcb_version, cache)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        validate_archive(archive)
        libraries = {
            "sym-lib-table": {
                f"PCM_{Path(name).stem}": library_uri / name
                for name in sorted(archive.namelist())
                if name.startswith("symbols/") and name.endswith(".kicad_sym")
            },
            "fp-lib-table": {"PCM_JLCPCB": library_uri / "footprints/JLCPCB.pretty"},
        }
        updates = {}
        conflicts = []
        for filename, entries in libraries.items():
            path = config / filename
            source = path if path.exists() else templates / filename
            if not source.is_file():
                sys.exit(f"Open KiCad's library manager once first; no library table found: {path}")
            original = source.read_text()
            updated, replaced = update_table(original, entries, args.jlcpcb_version)
            conflicts.extend(f"{filename}: {name}" for name in replaced)
            if updated != original or not path.exists():
                updates[path] = updated
        if conflicts and not args.yes:
            sys.exit(
                "Existing JLCPCB registrations would be replaced:\n  "
                + "\n  ".join(conflicts)
                + "\nRerun with --yes to replace them with the requested release."
            )

        destination.mkdir(parents=True, exist_ok=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(archive_bytes)
        archive.extractall(destination)
    fix_model_paths(destination, library_uri)
    for path, updated in updates.items():
        if path.exists():
            backup = path.with_name(f"{path.name}.kkh-{time.time_ns()}.bak")
            shutil.copy2(path, backup)
            print(f"Backup: {backup}")
        path.write_text(updated)
    print(f"JLCPCB {args.jlcpcb_version} installed for KiCad {version}: {destination}")
    print("Symbols: PCM_JLCPCB-*; footprints: PCM_JLCPCB")


def ensure_kicad_closed():
    for process in ("kicad", "eeschema", "pcbnew", "kicad-manager"):
        result = subprocess.run(["pgrep", "-ix", process], capture_output=True, check=False)
        if result.returncode == 0:
            sys.exit("Close KiCad and its editors before installing libraries.")
        if result.returncode != 1:
            sys.exit(f"Could not check whether KiCad is running: {result.stderr.decode().strip()}")


def installed_kicad_version():
    output = subprocess.check_output(["kicad-cli", "version"], text=True).strip()
    match = re.match(r"(\d+)\.\d+", output)
    if not match or int(match[1]) < 8:
        sys.exit(f"Requires KiCad 8 or later; got {output!r}.")
    return f"{match[1]}.0"


def installation_paths(version):
    home = Path.home()
    if sys.platform == "darwin":
        config = home / "Library/Preferences/kicad" / version
        data = home / "Documents/KiCad" / version
        templates = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/template")
    elif sys.platform == "linux":
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "kicad" / version
        data = Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "kicad" / version
        templates = Path("/usr/share/kicad/template")
    else:
        sys.exit("Library setup supports native macOS and Linux installations.")
    if "KICAD_CONFIG_HOME" in os.environ:
        config = Path(os.environ["KICAD_CONFIG_HOME"]) / version
    return config, data, templates


def third_party_path(config, data, variable):
    common = config / "kicad_common.json"
    variables = json.loads(common.read_text()).get("environment", {}).get("vars", {}) if common.exists() else {}
    configured = os.environ.get(variable) or variables.get(variable)
    if configured:
        return Path(os.path.expandvars(configured)).expanduser()
    return data / "3rdparty"


def release_archive(version, cache):
    if cache.exists():
        archive_bytes = cache.read_bytes()
    else:
        url = (
            "https://github.com/CDFER/JLCPCB-Kicad-Library/releases/download/"
            f"{version}/JLCPCB-KiCad-Library-{version}.zip"
        )
        print(f"Downloading JLCPCB {version}...")
        with urllib.request.urlopen(url, timeout=60) as response:
            archive_bytes = response.read()
    if hashlib.sha256(archive_bytes).hexdigest() != JLCPCB_RELEASES[version]:
        sys.exit(f"Archive checksum mismatch. Remove {cache} if present and retry.")
    return archive_bytes


def validate_archive(archive):
    names = archive.namelist()
    for name in names:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            sys.exit(f"Unsafe archive path: {name}")
    if not any(name.startswith("symbols/") and name.endswith(".kicad_sym") for name in names):
        sys.exit("The release contains no symbol libraries.")
    if not any(name.startswith("footprints/JLCPCB.pretty/") for name in names):
        sys.exit("The release contains no JLCPCB footprints.")


def update_table(text, libraries, version):
    updated = text
    conflicts = []
    for name, uri in libraries.items():
        entry = (
            f'(lib (name {json.dumps(name)}) (type "KiCad") '
            f'(uri {json.dumps(uri.as_posix(), ensure_ascii=False)}) (options "") '
            f'(descr "JLCPCB {version}"))'
        )
        spans = library_spans(updated, name)
        for start, end in reversed(spans):
            old = updated[start:end]
            if old != entry and uri.as_posix() not in old:
                conflicts.append(name)
            replacement = entry if start == spans[0][0] else ""
            updated = updated[:start] + replacement + updated[end:]
        if not spans:
            end = updated.rfind(")")
            if end < 0:
                raise ValueError("Invalid KiCad library table.")
            updated = updated[:end].rstrip() + "\n  " + entry + "\n)\n"
    return updated, conflicts


def library_spans(text, name):
    spans = []
    pattern = r"\(lib\s+\(name\s+" + re.escape(json.dumps(name)) + r"\)"
    for match in re.finditer(pattern, text):
        depth = 0
        for token in re.finditer(r'"(?:\\.|[^"\\])*"|[()]', text[match.start() :]):
            value = token.group()
            if value == "(":
                depth += 1
            elif value == ")":
                depth -= 1
                if depth == 0:
                    spans.append((match.start(), match.start() + token.end()))
                    break
        else:
            raise ValueError(f"Unbalanced library entry: {name}")
    return spans


def fix_model_paths(destination, library_uri):
    # @ Built-in KiCad variables keep placed footprints portable between users.
    model_root = (library_uri / "3dmodels").as_posix()
    for footprint in destination.glob("footprints/**/*.kicad_mod"):
        text = re.sub(
            r"\$\{KICAD\d+_3RD_PARTY\}/3dmodels/com_github_CDFER_JLCPCB-Kicad-Library",
            lambda _: model_root,
            footprint.read_text(),
        )
        for model in re.findall(r'\(model "([^"]+)"', text):
            resolved = model.replace(library_uri.as_posix(), destination.as_posix())
            if not Path(resolved).is_file():
                raise ValueError(f"Missing 3D model: {model}")
        footprint.write_text(text)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
