#!/bin/sh
# Build and copy the app to https://xavierdamman.com/projects/osv-app/ (nginx on this box serves /var/www/blog).
# Old hashed assets are kept so phones still running the previous version don't break.
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
DEST=${DEST:-/var/www/blog/projects/osv-app}
cd "$SRC"
npx tsc -b
npx vite build
mkdir -p "$DEST"
cp -R dist/. "$DEST/"
chmod -R u+rwX,g+rwX,o+rX "$DEST"
echo "Deployed to https://xavierdamman.com/projects/osv-app/"
