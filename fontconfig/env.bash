# Point KiCad's bundled fontconfig at a usable fonts.conf on macOS.
#
# The fontconfig library inside KiCad.app is built with a default config path
# from the build machine, which does not exist on end-user machines. Every
# process that loads it (kicad-cli, and pcbnew via KiCad's framework Python)
# then warns:
#
#   Fontconfig error: Cannot load default config file: No such file: (null)
#
# A fonts.conf shipped inside the app bundle is not enough on its own: it does
# not list the macOS font directories, so fonts the user installed (e.g. under
# ~/Library/Fonts) resolve in the KiCad GUI but get silently substituted in
# kicad-cli, which changes rendered text geometry and DRC results.
#
# Source this file and call kkh_export_fontconfig with the KiCad.app path
# before exec-ing such a process. It generates a config that includes the
# bundle's fonts.conf (when one exists) and always adds the macOS system and
# user font directories. Respects a FONTCONFIG_FILE the caller already set.

kkh_export_fontconfig() {
  local app=$1 bundle_conf="" file

  if [[ -n "${FONTCONFIG_FILE:-}" ]]; then
    return
  fi

  for file in \
    "${app}/Contents/Resources/fonts/fonts.conf" \
    "${app}/Contents/Resources/etc/fonts/fonts.conf" \
    "${app}/Contents/Frameworks/etc/fonts/fonts.conf"; do
    if [[ -f "${file}" ]]; then
      bundle_conf="${file}"
      break
    fi
  done

  if [[ -z "${bundle_conf}" ]]; then
    bundle_conf="$(find "${app}" -name fonts.conf -type f 2>/dev/null | sort | head -n 1 || true)"
  fi

  local generated="${TMPDIR:-/tmp}/kkh-fontconfig-${UID}.conf"
  {
    echo '<?xml version="1.0"?>'
    echo '<fontconfig>'
    if [[ -n "${bundle_conf}" ]]; then
      echo "  <include ignore_missing=\"yes\">${bundle_conf}</include>"
    fi
    echo '  <dir>/System/Library/Fonts</dir>'
    echo '  <dir>/System/Library/Fonts/Supplemental</dir>'
    echo '  <dir>/Library/Fonts</dir>'
    echo '  <dir>~/Library/Fonts</dir>'
    echo '  <cachedir>~/Library/Caches/fontconfig</cachedir>'
    echo '</fontconfig>'
  } > "${generated}"

  export FONTCONFIG_FILE="${generated}"
  export FONTCONFIG_PATH="${generated%/*}"
}
