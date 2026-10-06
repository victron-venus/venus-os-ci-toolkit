#!/usr/bin/env bash
# Manual infrastructure probe; never registers a GitHub runner or changes routing.
set -euo pipefail
[[ "$(id -u)" == 1001 ]]
[[ "$(uname -r)" == *-gvisor ]]
[[ ! -e /var/run/secrets/kubernetes.io/serviceaccount/token ]]
node --version
npm --version
python3 --version
java -version
git --version
gh --version
pkg-config --exists dbus-1 glib-2.0
./bin/Runner.Listener --version
sudo -n true
curl --fail --silent --show-error --max-time 20 https://github.com/robots.txt >/dev/null
work=$(mktemp -d)
trap 'docker rm -f reserve-postgres >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
cat > "$work/Dockerfile" <<'DOCKERFILE'
FROM busybox@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
RUN printf 'nested-build-ok\n' > /proof
CMD ["cat", "/proof"]
DOCKERFILE
docker buildx build --load --tag reserve-smoke:local "$work"
[[ "$(docker run --rm reserve-smoke:local)" == nested-build-ok ]]
password=$(openssl rand -hex 24)
docker run --detach --name reserve-postgres \
  --publish 127.0.0.1:55432:5432 --env POSTGRES_PASSWORD="$password" \
  postgres@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
for attempt in {1..60}; do
  pg_isready --host=127.0.0.1 --port=55432 --username=postgres && break
  [[ "$attempt" != 60 ]] || exit 1
  sleep 1
done
[[ "$(PGPASSWORD="$password" psql --host=127.0.0.1 --port=55432 --username=postgres --no-psqlrc --tuples-only --no-align --command='SELECT 1')" == 1 ]]
printf 'PASS: sandbox, HTTPS, toolchains, nested BuildKit and PostgreSQL published port\n'
