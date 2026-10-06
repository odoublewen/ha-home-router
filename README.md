# Home Router

A Home Assistant integration for controlling a home router: port forwards you define
ahead of time become switches, and the router gets a restart button.

Today it supports one model, the **Quantum Fiber C5500XK**, but the device logic sits
behind a `Router` interface (`custom_components/home_router/router/`) so other models
can be added later.

It talks to the router's CGI API directly over HTTP, the same way the router's own web
UI does. Nothing runs on the router.

## Install

Through [HACS](https://hacs.xyz):

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/odoublewen/ha-home-router`
   with type **Integration**.
2. Install **Home Router**, then restart Home Assistant.
3. **Settings → Devices & services → Add integration → Home Router**.

Or by hand: copy `custom_components/home_router` into your Home Assistant
`config/custom_components/` and restart.

Requires Home Assistant 2025.10 or newer.

## Configure

The setup form asks for the router's address (default `192.168.1.1`), the admin
username (default `admin`) and password, and the model. The router serves a self-signed
certificate, so TLS verification is off unless you turn it on.

If the router's password changes, Home Assistant asks for the new one.

Under **Configure** you can change how often the router is polled (default 60 seconds).

## Port forwards

Go to **Settings → Devices & services → Home Router**. On the router's entry, open the
**⋮** menu and choose **Add port forward** to define a rule:

| Field | |
| --- | --- |
| Name | Stored on the router as the rule's description. Letters, digits, `.`, `-` and `_`. |
| Internal IP address / port | Where traffic is sent on your LAN. |
| External (WAN) port | Defaults to the internal port. |
| Protocol | TCP, UDP, or both. |

Each rule gets a switch, on a small device of its own that is linked to the router.
**On** creates the rule on the router, **off** deletes it. The
switch reads the router's actual state, matching rules by name, so a rule created or
deleted in the router's web UI shows up too. If a rule with that name forwards
somewhere other than the definition says, the switch's `mismatch` attribute is `true`.

Things to know:

* Turning a switch on fails, with an error in the UI, if another rule already forwards
  that WAN port on the same protocol. The router itself does *not* enforce this. Its
  web UI checks in the browser only, so the API will happily store two rules competing
  for one port.
* A TCP-and-UDP rule is stored on the router as two entries with the same name, as the
  web UI does too. Turning the switch off removes both.
* A rule can't be edited while its switch is on. Turn it off first.
* Deleting a port forward definition removes its switch and deletes the rule from the
  router, if it is there. If the router can't be reached at that moment, the rule stays
  and a warning is logged.
* The source address is always "All IP Addresses". Restricting it is not exposed.

The **Port forwards** sensor counts every forwarding entry on the router (a TCP-and-UDP
rule counts twice), including ones Home Assistant doesn't manage. Its `rules`
attribute lists them.

## Rebooting

The **Restart** button returns as soon as the router accepts the command. The
integration then watches the router go offline and come back, and fires a
`home_router_reboot_finished` event whose `result` is one of:

| `result` | Meaning |
| --- | --- |
| `recovered` | Seen to go offline and come back. |
| `never_went_down` | Kept answering, so the reboot can't be confirmed. |
| `still_down` | Went offline and wasn't back within 7 minutes. |

Two things make that harder than it sounds, both learned the hard way:

* The C5500XK keeps serving for another 5-10 seconds after it accepts the reboot, so
  polling only for "is it up again" reports success having never seen it leave. Hence
  the two phases, and three consecutive probes must agree before either one settles.
* The router's web server answers long before the device is usable. During a reboot,
  `/` returns a 302 and `/login.html` a 200 while the UI is still dead, and the CGI
  endpoint itself returns 503 for most of the outage. So the probe asks
  `/cgi/cgi_get` and accepts only 444 (alive, not logged in) or 200.

A full reboot takes just over two minutes. Entities show as unavailable while the
router is down.

## Clearing every port forward

The `home_router.clear_port_forwards` action deletes every rule on the router,
including ones Home Assistant doesn't manage, and responds with `{"removed": <count>}`.
`config_entry_id` picks the router, and can be left out when only one is set up.

### Nightly clean-up

Clear any ports opened during the day, then reboot the router to keep the fiber link
healthy:

```yaml
automation:
  - alias: Nightly router reset
    triggers:
      - trigger: time
        at: "03:00:00"
    actions:
      - action: home_router.clear_port_forwards
      - action: button.press
        target:
          entity_id: button.quantum_fiber_c5500xk_restart
```

## Develop

```sh
uv sync
uv run pytest
uv run ruff check
```

## How it works

The C5500XK admin interface is a React app over a small CGI API:

| Request | Purpose |
| --- | --- |
| `POST /cgi/cgi_action` with `username=...&password=...` | log in (session cookie) |
| `GET /cgi/cgi_get?Object=<TR-181 path>` | read objects |
| `POST /cgi/cgi_set` with an operation string | add or delete objects |
| `POST /cgi/cgi_action` with `Action=Reboot` | reboot |

Every request needs `X-Requested-With: XMLHttpRequest`. Without it, the device returns
the single-page-app shell. HTTP 444 means the session has expired, and the integration
logs in again.

Port forwards are `Device.NAT.PortMapping` instances, with this model's vendor prefix
being `X_AXON_`.

## License

MIT — see [LICENSE](LICENSE).
