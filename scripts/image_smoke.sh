#!/bin/sh
# Builds the image and starts it with the example config: the check a base
# image update has to pass before it may merge, since the test suite runs on
# the CI runner's own Python and never touches the image.
set -eu

tag=tunebox:smoke
name=tunebox-smoke-$$
port=${SMOKE_PORT:-18099}
data=$(mktemp -d)

cleanup() {
    docker rm -f "$name" >/dev/null 2>&1 || true
    rm -rf "$data"
}
trap cleanup EXIT INT TERM

docker build --quiet -t "$tag" . >/dev/null
cp tunebox.example.toml "$data/tunebox.toml"
# The image runs as 1000:1000, which the runner's user is not.
chmod 0777 "$data"
docker run -d --name "$name" -p "127.0.0.1:$port:8080" -v "$data:/data" "$tag" >/dev/null

up=no
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
        up=yes
        break
    fi
    sleep 1
done
if [ "$up" != yes ]; then
    echo "image smoke: /healthz never answered" >&2
    docker logs "$name" >&2
    exit 1
fi

curl -fsS "http://127.0.0.1:$port/api/channels" | python3 -c '
import json, sys
body = json.load(sys.stdin)
channels = body if isinstance(body, list) else body["channels"]
assert channels, "no channels"
print(f"image smoke: {len(channels)} channels")
'
curl -fsS "http://127.0.0.1:$port/" | grep -q "<html" || {
    echo "image smoke: / gave no page" >&2
    exit 1
}

logs=$(docker logs "$name" 2>&1)
if printf '%s\n' "$logs" | grep -Eq 'Traceback|"level": "error"'; then
    echo "image smoke: errors in the log" >&2
    printf '%s\n' "$logs" >&2
    exit 1
fi
echo "image smoke: passed"
