#!/usr/bin/env bash
# Run only against a disposable local Docker daemon, as an unprivileged user.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
temp=$(mktemp -d)
export COMPOSE_PROJECT_NAME="ivcheck-$RANDOM-$$"
export IV_UID=$(id -u) IV_GID=$(id -g)
export IV_DATA="$temp/state" IV_SECRETS="$temp/tls" IV_ACCESS_DIR="$temp/access"
export IV_SOCKET_DIR="$temp/socket" IV_BEARER_FILE="$temp/bearer" IV_WEIGHTS="$temp/model"
export IV_ALLOWED_NETWORKS="127.0.0.1/32,172.16.0.0/12" IV_PUBLISH=127.0.0.1 IV_PORT=0
export IV_RETAINED_TERMS="synthetic recording" IV_ENABLE=1 IV_REVISION=fixture IV_MODEL=tiny IV_IMAGE="$COMPOSE_PROJECT_NAME-gateway"
export IV_ENGINE=openai-whisper IV_FP16=false IV_BEAM=1 IV_PREVIOUS=false IV_COMPILE=false IV_IDLE=0 IV_DIGEST=fixture
export IV_BOT='{}' TELEGRAM_USER_ID=12345678 BOT_TOKEN="12345678:$(openssl rand -hex 20)"
mkdir -m 700 "$IV_DATA" "$IV_SECRETS" "$IV_ACCESS_DIR" "$IV_SOCKET_DIR"
touch "$IV_WEIGHTS"
openssl rand -hex 32 > "$IV_BEARER_FILE"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=fixture -addext subjectAltName=IP:127.0.0.1 \
  -keyout "$IV_SECRETS/tls.key" -out "$IV_SECRETS/tls.crt" 2>/dev/null
# Build the real Gateway and bot. Exercise worker request handling with a CPU fixture model.
cat > "$temp/worker.yml" <<YAML
services:
  whisper:
    container_name: $COMPOSE_PROJECT_NAME-worker
    build: !reset null
    group_add: !reset []
    devices: !reset []
    volumes:
      - "$PWD/whisper/whisper_server.py:/app/whisper_server.py:ro"
      - "$PWD/tests/docker_worker_fixture.py:/app/fixture.py:ro"
    command: [python, /app/fixture.py]
  bot:
    command: [python, -c, "import time; time.sleep(900)"]
YAML
base=(docker compose --env-file /dev/null -f docker-compose.yml -f compose.private.yml)
stack=("${base[@]}" -f compose.telegram.yml -f "$temp/worker.yml")
cleanup() {
  "${stack[@]}" down -v --remove-orphans >/dev/null || true
  rm -rf "$temp"
}
trap cleanup EXIT
[[ "$("${base[@]}" config --services | sort | xargs)" == 'gateway whisper' ]]
"${stack[@]}" build gateway bot
"${stack[@]}" up -d --no-build --pull never
port=$("${stack[@]}" port gateway 9443 | awk -F: '{print $NF}')
export PORT="$port"
python3 - <<'PY'
import hashlib, http.client, io, json, os, secrets, ssl, time, wave
from pathlib import Path
access = Path(os.environ["IV_ACCESS_DIR"])
tls = ssl.create_default_context(cafile=os.environ["IV_SECRETS"]+"/tls.crt")
def call(method, path, headers=None, data=None):
    connection = http.client.HTTPSConnection("127.0.0.1", int(os.environ["PORT"]), context=tls, timeout=4)
    try:
        connection.request(method, path, data, headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()
token = secrets.token_hex(32)
def rotate(key):
    tmp = access/"new"
    tmp.write_text(json.dumps({"test":{"scope":"both","digest":hashlib.sha256(key.encode()).hexdigest()}}))
    tmp.replace(access/"clients.json")
rotate(token)
for attempt in range(60):
    try:
        if call("GET","/v1/capabilities")[0] == 401:
            break
    except OSError:
        pass
    time.sleep(1)
else:
    raise RuntimeError("Gateway TLS endpoint did not become ready")
headers = {"Authorization":"Bearer "+token, "Idempotency-Key":"fixture"}
status, result = call("POST","/v1/sessions",headers)
assert status == 201, (status,result)
path = "/v1/sessions/"+result["session_id"]
audio = io.BytesIO()
with wave.open(audio,"wb") as stream:
    stream.setparams((1,2,16000,0,"NONE","not compressed"))
    stream.writeframes(bytes((1, 0))*16000)
assert call("PUT",path+"/fragments/0",headers | {"Content-Type":"audio/wav"},audio.getvalue())[0]==201
assert call("POST",path+"/finalize",headers | {"Content-Type":"application/json"},b'{"last_index":0}')[0]==202
for attempt in range(30):
    result = call("GET",path,headers)[1]
    if result["state"]=="succeeded":
        assert result["segments"][0]["text"] == "synthetic recording"
        break
    time.sleep(1)
else:
    raise RuntimeError("Synthetic session did not finish")
rotate(secrets.token_hex(32))
assert call("GET",path,headers)[0]==401
assert not any(f.suffix in (".pcm",".upload",".part",".wav") for f in Path(os.environ["IV_DATA"]).iterdir())
PY
for svc in gateway whisper bot; do
  cid=$("${stack[@]}" ps -q "$svc")
  [[ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.Config.User}}' "$cid")" == "true $IV_UID:$IV_GID" ]]
  "${stack[@]}" exec -T "$svc" python -c 'import os; assert os.geteuid()>0 and os.getegid()>0; assert int(next(x.split()[1] for x in open("/proc/self/status") if x.startswith("CapEff:")),16)==0; assert any(l.split()[4]=="/tmp" and " - tmpfs " in l for l in open("/proc/self/mountinfo"))'
done
"${stack[@]}" exec -T bot python - <<'PY'
import asyncio, socket
import bot
async def check():
    async with bot.gateway_session() as session:
        async with session.get("http://localhost/health", headers={"Authorization":"invalid"}) as result:
            assert result.status == 401
        async with session.get("http://localhost/health") as result:
            assert result.status == 200 and (await result.json())["status"] == "healthy"
    from types import SimpleNamespace
    for uid, chat_type, expected in ((bot.AUTHORIZED_USER,"private",True), (bot.AUTHORIZED_USER+1,"private",False), (bot.AUTHORIZED_USER,"group",False)):
        event = SimpleNamespace(from_user=SimpleNamespace(id=uid), chat=SimpleNamespace(type=chat_type))
        permitted, _ = await bot.dp.message.check_root_filters(event)
        assert bool(permitted) == expected
    for name in ("gateway","whisper"):
        try:
            socket.getaddrinfo(name,9000)
        except socket.gaierror:
            continue
        raise AssertionError(name)
asyncio.run(check())
PY
"${stack[@]}" restart gateway
[[ -z "$("${stack[@]}" exec -T gateway sh -c 'find /tmp -name "*.pcm" -print -quit')" ]]

if [[ -n "${IV_TEST_SCANNER:-}" ]]; then
  result=0
  for image in "$COMPOSE_PROJECT_NAME-gateway" "$COMPOSE_PROJECT_NAME-bot"; do
    "$IV_TEST_SCANNER" "docker:$image" --only-fixed --fail-on high || result=1
  done
  exit "$result"
fi
