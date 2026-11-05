#!/bin/bash
# RecLight - sestavení aplikace a instalačního obrazu (.dmg) pro macOS.
# Spusťte na Macu v terminálu:  bash build_mac.sh      (potřebuje Python 3.11+ s Tk, např. z python.org nebo "brew install python-tk")
# Výsledek: dist/RecLight-<verze>-macOS-<arch>.dmg
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(python3 - <<'PY'
import re
print(re.search(r'APP_VERSION = "([^"]+)"', open("reclight.py", encoding="utf-8").read()).group(1))
PY
)
ARCH=$(uname -m)   # arm64 nebo x86_64

echo "=== RecLight $VERSION pro macOS ($ARCH) ==="
python3 -m venv .venv-mac
source .venv-mac/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python make_icons.py

rm -rf build dist
python -m PyInstaller --noconfirm --windowed --name RecLight --icon reclight.icns \
  --osx-bundle-identifier eu.c2coder.reclight \
  --collect-submodules mido --hidden-import mido.backends.rtmidi --hidden-import rtmidi \
  --hidden-import pystray._darwin --hidden-import gui --hidden-import vmidi \
  --collect-all customtkinter reclight.py

# Info.plist: aplikace s oknem, ikona se zobrazuje i v Docku, vysoké rozlišení
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $VERSION" dist/RecLight.app/Contents/Info.plist 2>/dev/null || \
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" dist/RecLight.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" dist/RecLight.app/Contents/Info.plist 2>/dev/null || true

# ad-hoc podpis (bez Apple Developer ID); bez něj se aplikace na Apple Silicon nespustí
codesign --force --deep --sign - dist/RecLight.app

STAGE=dist/dmg
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R dist/RecLight.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp -R firmware "$STAGE/Firmware"
cp mac/PRECTI-ME.txt "$STAGE/PRECTI-ME.txt"
DMG="dist/RecLight-$VERSION-macOS-$ARCH.dmg"
hdiutil create -volname "RecLight $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo
echo "Hotovo: $DMG"
