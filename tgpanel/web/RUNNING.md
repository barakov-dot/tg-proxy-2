# Running the panel (notes for the service runner)

The app is exposed as `tgpanel.web.app.create_app(ctx, root_path)`; the runner builds `WebContext`
and starts uvicorn with these arguments:

```python
uvicorn.run(
    app,
    host="127.0.0.1",  # loopback only; Caddy is the only client
    port=8090,
    access_log=False,  # URLs/ids must not be logged anywhere
    proxy_headers=False,  # X-Forwarded-For is parsed by the app, only from trusted proxies
    server_header=False,
    limit_concurrency=64,
    timeout_keep_alive=5,
    h11_max_incomplete_event_size=16 * 1024,
)
```

Environment:

- `TGPANEL_SECRET_KEY` - session/CSRF signing key, at least 32 characters (pass it as
  `WebContext.secret_key`).
- `TGPANEL_ENV_FILE` - env file (default `/etc/tgpanel/tgpanel.env`). The panel never stores the bot
  token in the database: `WebContext.write_env(key, value)` must write `TGPANEL_BOT_TOKEN=<token>`
  into this file atomically with mode 0600. The bot picks it up after a restart.

Built-in guards: request bodies over 2 MB are refused with 413 (Caddy also enforces
`request_body max_size 2MB`); the `Host` header must be the `panel_hostname` setting, `127.0.0.1`
or `localhost`; sessions are signed cookies (`Path=/<RANDOM>/`, 12 h) and the login limiter is
in memory (per process).
