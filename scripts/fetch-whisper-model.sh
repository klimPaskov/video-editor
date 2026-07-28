#!/usr/bin/env bash
set -euo pipefail

model="small"
destination=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --model)
      [ "$#" -ge 2 ] || { echo "--model requires a value" >&2; exit 2; }
      model="$2"
      shift 2
      ;;
    --destination)
      [ "$#" -ge 2 ] || { echo "--destination requires a value" >&2; exit 2; }
      destination="$2"
      shift 2
      ;;
    *)
      echo "usage: $0 [--model MODEL] --destination PATH" >&2
      exit 2
      ;;
  esac
done

[ -n "$destination" ] || { echo "--destination is required" >&2; exit 2; }

case "$model" in
  tiny) file="tiny.pt"; sha256="65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9" ;;
  tiny.en) file="tiny.en.pt"; sha256="d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03" ;;
  base) file="base.pt"; sha256="ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e" ;;
  base.en) file="base.en.pt"; sha256="25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead" ;;
  small) file="small.pt"; sha256="9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794" ;;
  small.en) file="small.en.pt"; sha256="f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872" ;;
  *) echo "unsupported model: $model" >&2; exit 2 ;;
esac

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 2; }
if command -v sha256sum >/dev/null 2>&1; then
  hash_command="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
  hash_command="shasum -a 256"
else
  echo "sha256sum or shasum is required" >&2
  exit 2
fi

destination_directory=$(dirname "$destination")
mkdir -p "$destination_directory"
destination=$(cd "$destination_directory" && pwd)/$(basename "$destination")
url="https://openaipublic.azureedge.net/main/whisper/models/$sha256/$file"

hash_file() {
  # shellcheck disable=SC2086
  $hash_command "$1" | awk '{print tolower($1)}'
}

if [ -f "$destination" ]; then
  existing_hash=$(hash_file "$destination")
  [ "$existing_hash" = "$sha256" ] || { echo "existing model hash mismatch: $existing_hash" >&2; exit 1; }
  printf '{"status":"reused","model":"%s","path":"%s","sha256":"%s","source":"%s"}\n' "$model" "$destination" "$existing_hash" "$url"
  exit 0
fi

staged=$(mktemp "$destination.download.XXXXXX")
cleanup() { rm -f -- "$staged"; }
trap cleanup EXIT
curl --fail --location --proto '=https' --tlsv1.2 --retry 2 --output "$staged" "$url"
downloaded_hash=$(hash_file "$staged")
[ "$downloaded_hash" = "$sha256" ] || { echo "downloaded model hash mismatch: $downloaded_hash" >&2; exit 1; }
mv -- "$staged" "$destination"
trap - EXIT
printf '{"status":"downloaded","model":"%s","path":"%s","sha256":"%s","source":"%s"}\n' "$model" "$destination" "$downloaded_hash" "$url"
