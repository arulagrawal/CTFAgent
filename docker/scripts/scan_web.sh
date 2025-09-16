#!/bin/bash
# Web application scanning script for CTFs

URL="$1"
if [ -z "$URL" ]; then
    echo "Usage: $0 <URL>"
    exit 1
fi

echo "=== Web Application Scan for $URL ==="
echo

echo "1. Basic HTTP response:"
curl -I "$URL"
echo

echo "2. Full page content:"
curl -s "$URL" | head -50
echo

echo "3. Robots.txt check:"
curl -s "$URL/robots.txt" || echo "No robots.txt found"
echo

echo "4. Directory enumeration (common paths):"
for path in admin login wp-admin phpmyadmin .git .env config.php; do
    status=$(curl -s -o /dev/null -w "%{http_code}" "$URL/$path")
    if [ "$status" != "404" ]; then
        echo "Found: $URL/$path (HTTP $status)"
    fi
done
echo

echo "5. Common file extensions:"
for ext in php asp aspx jsp txt bak old; do
    status=$(curl -s -o /dev/null -w "%{http_code}" "$URL/index.$ext")
    if [ "$status" != "404" ]; then
        echo "Found: $URL/index.$ext (HTTP $status)"
    fi
done

echo "=== Scan complete ==="
