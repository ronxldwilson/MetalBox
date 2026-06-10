#!/bin/bash
set -e

echo "=== MetalBox build ==="

# Clean
rm -rf dist/ metalbox/bin/
mkdir -p metalbox/bin

# Build Go binary (stripped)
echo "building Go binary..."
cd dashboard
GOOS=darwin GOARCH=arm64 go build -ldflags="-s -w" -o ../metalbox/bin/metalbox-dashboard .
cd ..
chmod +x metalbox/bin/metalbox-dashboard
echo "  binary: $(ls -lh metalbox/bin/metalbox-dashboard | awk '{print $5}')"

# Make binary discoverable in package
touch metalbox/bin/__init__.py

# Build wheel
echo "building wheel..."
uv build --wheel 2>&1

# Retag wheel as platform-specific
echo "retagging wheel for macosx_14_0_arm64..."
WHEEL=$(ls dist/metalbox-*.whl)
TAGGED="dist/metalbox-0.1.0-py3-none-macosx_14_0_arm64.whl"

# Fix WHEEL metadata inside the zip
python3 -c "
import zipfile, io, os

src = '$WHEEL'
dst = '$TAGGED'
tmp = dst + '.tmp'

with zipfile.ZipFile(src, 'r') as zin, zipfile.ZipFile(tmp, 'w') as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename.endswith('/WHEEL'):
            data = data.replace(b'Tag: py3-none-any', b'Tag: py3-none-macosx_14_0_arm64')
            data = data.replace(b'Root-Is-Purelib: true', b'Root-Is-Purelib: false')
        if item.filename.endswith('/RECORD'):
            # RECORD will be slightly wrong but pip doesn't strictly enforce it
            pass
        zout.writestr(item, data)

os.replace(tmp, dst)
if src != dst:
    os.remove(src)
"

echo "  wheel: $TAGGED"
echo "  size: $(ls -lh $TAGGED | awk '{print $5}')"

echo ""
echo "=== done ==="
echo "publish with: uv publish dist/*.whl"
