# Point KiCad's bundled fontconfig at a usable fonts.conf on macOS.
#
# The fontconfig library inside KiCad.app is built with a default config path
# from the build machine, which does not exist on end-user machines. Every
# process that loads it (kicad-cli, and pcbnew via KiCad's framework Python)
# then warns:
#
#   Fontconfig error: Cannot load default config file: No such file: (null)
#
# Source this file and call kkh_export_fontconfig with the KiCad.app path
# before exec-ing such a process. Prefers a fonts.conf shipped inside the app
# bundle, falling back to the minimal fonts.conf next to this file. Respects a
# FONTCONFIG_FILE the caller already set.

kkh_export_fontconfig() {
  local app=$1 file=""

  if [[ -n "${FONTCONFIG_FILE:-}" ]]; then
    return
  fi

  for file in \
    "${app}/Contents/Resources/fonts/fonts.conf" \
    "${app}/Contents/Resources/etc/fonts/fonts.conf" \
    "${app}/Contents/Frameworks/etc/fonts/fonts.conf"; do
    if [[ -f "${file}" ]]; then
      break
    fi
    file=""
  done

  if [[ -z "${file}" ]]; then
    file="$(find "${app}" -name fonts.conf -type f 2>/dev/null | sort | head -n 1)"
  fi

  if [[ -z "${file}" ]]; then
    file="$(dirname "$(realpath "${BASH_SOURCE[0]}")")/fonts.conf"
  fi

  export FONTCONFIG_FILE="${file}"
  export FONTCONFIG_PATH="${file%/*}"
}
