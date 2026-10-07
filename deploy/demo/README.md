# Demo server

A public, read-only Kalinka server that anyone can try from the app's "Try demo server" button. It runs the Kalinka server and the Jamendo plugin in one container, with no renderer and no music of its own.

## What visitors can and cannot do

- Browse, search and play Jamendo's catalogue. Mood search works too.
- Use the queue: add, remove, reorder, play, pause, seek, skip, shuffle and repeat. The queue holds up to 100 tracks.
- Change the volume number. Nothing is heard, so it only moves the slider.
- Look at every setting. Saving, restarting, upgrading, favourites, playlists, collections and renderer changes are all refused with `403 {"detail": {"code": "demo_read_only", ...}}`.

Playback is simulated inside the server by an output named "Demo output". It keeps time like a real renderer, so tracks end and the next one starts. No renderer can connect from outside: the server refuses `/renderer/ws` in demo mode.

Every visitor shares the same queue and the same volume number.

Each visitor's changes are rate-limited: a burst of 20, then one every 3 seconds. Going over is answered `429` with `Retry-After`. The server tells visitors apart by address, so behind a reverse proxy it must trust the proxy's `X-Forwarded-For` header: the compose file sets uvicorn's `FORWARDED_ALLOW_IPS` for that. Reads, including search, are not limited.

## The demo flag

`base_config.server.demo_mode` turns all of this on. It is read-only: only the config file sets it, and `PUT /server/config` refuses it on every server.

## Running it

You need a Jamendo client id from the [Jamendo developer portal](https://devportal.jamendo.com). Keep it out of git.

1. Clone the repository with its tags, so the packages know their version.
2. Create `deploy/demo/.env` with `JAMENDO_CLIENT_ID=<your id>`, and `DEMO_HOST=<your host>` if it is not `demo.kalinkaplayer.com`.
3. Point the host's DNS at the machine, then run `docker compose -f deploy/demo/compose.yaml up -d --build`.

Caddy terminates TLS and obtains the certificate. The server itself speaks plain HTTP on port 8000 inside the compose network.

The entrypoint writes the client id into `/etc/kalinka/kalinka_conf.cfg` inside the container, readable only by the server's user. Do not bind-mount `/etc/kalinka` from the host.

The `kalinka-state` volume keeps the Jamendo mood index and the text model, which download on the first mood search, and the queue across restarts.

## Behind another reverse proxy

The app needs WebSockets on `/queue/ws` and `/device/ws`, and unbuffered streaming on `/queue/events` and `/device/events`. For nginx that means `proxy_http_version 1.1`, the `Upgrade` and `Connection` headers, `proxy_buffering off`, and a long `proxy_read_timeout`.

The proxy must set `X-Forwarded-For` to the visitor's address, replacing any value the visitor sent, and the server must trust it through `FORWARDED_ALLOW_IPS`. Otherwise every visitor shares one rate limit.

## Trying it locally

Run the demo from a source checkout without a container:

```sh
mkdir -p ~/kalinka-demo/etc/kalinka
cp deploy/demo/kalinka_conf.cfg ~/kalinka-demo/etc/kalinka/
# put your client id into input_modules.jamendo.client_id in that copy
make dev-run KALINKA_PREFIX=$HOME/kalinka-demo
```

Then start the app with `--dart-define=KALINKA_DEMO_SERVER=http://127.0.0.1:8000` and press "Try demo server".
