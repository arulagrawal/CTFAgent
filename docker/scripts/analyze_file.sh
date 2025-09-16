#!/bin/bash
# File analysis script for CTFs

FILE="$1"
if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
    echo "Usage: $0 <file>"
    echo "File must exist"
    exit 1
fi

echo "=== File Analysis for $FILE ==="
echo

echo "1. File type:"
file "$FILE"
echo

echo "2. File size:"
ls -lh "$FILE"
echo

echo "3. Strings (first 20):"
strings "$FILE" | head -20
echo

echo "4. Hexdump (first 256 bytes):"
hexdump -C "$FILE" | head -16
echo

echo "5. Entropy check:"
if command -v ent >/dev/null 2>&1; then
    ent "$FILE"
else
    echo "ent not available, checking for binary content..."
    if file "$FILE" | grep -q "text"; then
        echo "File appears to be text"
    else
        echo "File appears to be binary"
    fi
fi
echo

echo "6. Binwalk analysis:"
if command -v binwalk >/dev/null 2>&1; then
    binwalk "$FILE" | head -10
else
    echo "binwalk not available"
fi

echo "=== Analysis complete ==="
