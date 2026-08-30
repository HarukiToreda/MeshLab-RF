#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

architecture="${1:-$(uname -m)}"
case "$architecture" in
  arm64|x86_64) ;;
  *)
    echo "Unsupported macOS architecture: $architecture" >&2
    exit 1
    ;;
esac

version="$(python3 -c 'from mesh_simulator import __version__; print(__version__)')"

python3 tools/create_icon.py
python3 -m unittest discover -s tests -v
python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name MeshLabRF \
  --icon assets/meshlab.ico \
  --osx-bundle-identifier com.harukitoreda.meshlab-rf \
  --target-architecture "$architecture" \
  main.py

plist="dist/MeshLabRF.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" "$plist" \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $version" "$plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $version" "$plist" \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $version" "$plist"

# Ad-hoc signing keeps the bundle internally consistent. A future Developer ID
# can replace this step to remove Gatekeeper's unsigned-app warning.
codesign --force --deep --sign - dist/MeshLabRF.app
codesign --verify --deep --strict --verbose=2 dist/MeshLabRF.app

output="dist/MeshLabRF-macOS-${architecture}.dmg"
hdiutil create \
  -volname "MeshLab RF" \
  -srcfolder dist/MeshLabRF.app \
  -ov \
  -format UDZO \
  "$output"

echo "Built: $output"
