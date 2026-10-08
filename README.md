# Tapo watcher

Watches a TP-Link Tapo camera and, whenever it reports movement, takes a few pictures and
asks the [OpenAI Decisions API](https://developers.openai.com/api/docs/guides/decisions)
whether anyone in them is using a mobile phone. Results show up on a small local dashboard.

```
Tapo camera ── ONVIF motion event ──▶ app takes SNAPSHOTS pictures over RTSP (ffmpeg)
                                        │
                                        ▼
          OpenAI Decisions API (gpt-6-luna), per picture:
            • "Is a person visible?"            (yes/no probability)
            • "Is anyone using a mobile phone?" (phone / no_phone / unclear)
                                        │
                                        ▼
          events/<id>/  (pictures + event.json)  ──▶  dashboard: http://localhost:8765
```

Captures are fully automatic. The dashboard shows each event with its pictures and verdict,
and has a live view, a "Capture now" button for testing, and a diagnostics panel listing
the events your camera sends.

## Requirements

- Linux, or Windows with WSL2. Tested on Ubuntu 24.04 under WSL2.
- Python 3.10 or newer, with `venv`.
- `ffmpeg`.
- A Tapo camera with a **Camera Account** set up (see below), on the same network.
- An OpenAI API key with access to the Decisions API. The API is in public beta.

```bash
sudo apt install python3 python3-venv ffmpeg
```

## Setup

1. **Create the camera account.** In the Tapo app, open the camera, then **Settings >
   Advanced Settings > Camera Account**, and set a username and password. ONVIF (events)
   and RTSP (video) use this account, not your TP-Link cloud login.
2. **Check that motion detection is on.** In the Tapo app, under the camera's detection
   settings, turn on **Motion Detection**, and make sure **Privacy Mode** is off.
3. **Find the camera's IP address.** Look under **Settings > Device Info** in the Tapo app,
   or in your router's device list. A DHCP reservation keeps the address from changing.
4. **Get the code and configure it.**
   ```bash
   git clone https://github.com/AntonieGeerts/tapo-watcher.git
   cd tapo-watcher
   cp .env.example .env
   ```
   Edit `.env` and replace the placeholders:
   ```ini
   CAMERA_HOST=your-camera-ip
   CAMERA_USER=your-camera-account-username
   CAMERA_PASS=your-camera-account-password
   OPENAI_API_KEY=your-openai-api-key
   ```
   Until they are all filled in, the dashboard says which ones are missing. Changes to the
   three camera settings are picked up without a restart. Keep `.env` private:
   `chmod 600 .env`. It is in `.gitignore`, so it is never committed.
5. **Start it.**
   ```bash
   ./run.sh
   ```
   The first run creates a virtualenv in `.venv` and installs the dependencies. Then open
   **http://localhost:8765**. On WSL2 that address also works from Windows.
6. **Test it.** Walk past the camera, or press **Capture now**. An event should appear
   within a few seconds. **Diagnostics** shows the raw camera messages if nothing happens.

Stop the server with `Ctrl+C`.

## Configuration

Every setting lives in `.env`, and `.env.example` explains each one. The main ones:

| Setting | Default | What it does |
|---|---|---|
| `TRIGGER_MODE` | `motion` | `motion`, `person` (the camera's own person detection, not sent by every model) or `auto` |
| `SNAPSHOTS` | `3` | Pictures per event. Each one is sent to the API. |
| `SNAPSHOT_INTERVAL` | `1.5` | Seconds between pictures |
| `COOLDOWN_SECONDS` | `20` | Minimum time between two automatic captures |
| `PERSON_MIN_PROB` | `0.5` | Minimum probability for a picture to count as showing a person |
| `VERDICT_MIN_PROB` | `0.6` | Minimum probability for "using phone" or "no phone". Below it the event is "unclear". |
| `KEEP_DAYS` | `14` | Delete events older than this. `0` keeps them forever. |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8765` | Dashboard address |

The camera's motion sensitivity and activity zones, set in the Tapo app, decide what counts
as movement. Lower the sensitivity or draw zones if trees, cars or lights trigger too many
captures. Movement without anyone in it gets the verdict "no person", and the dashboard
hides those events while **Only people** is ticked.

## How the verdict is made

Each picture is sent to the Decisions API separately, with two questions: whether a person
is visible, and whether anyone is using a mobile phone. Pictures that probably show a person
are averaged into one verdict, weighted by how sure the model is that a person is there.
The dashboard shows the picture that makes the verdict clearest. The model can be wrong, so
check the pictures for anything that matters.

To ask something else, edit `QUESTIONS` (and `OUTCOMES`) in `app/decisions.py`. To re-run
saved events with the new question:

```bash
.venv/bin/python -m app.reanalyze              # all events
.venv/bin/python -m app.reanalyze <event-id>   # specific events
```

## Project layout

```
app/
  main.py        web server, event pipeline, API routes
  watcher.py     follows the camera's ONVIF events and decides when to capture
  onvif.py       minimal ONVIF client (PullPoint subscription, WS-Security digest)
  capture.py     ffmpeg: event pictures and the live view
  decisions.py   OpenAI Decisions API questions, calls, and the verdict
  store.py       events on disk (events/<id>/)
  reanalyze.py   re-run the API on saved events
  static/index.html  the dashboard
tests/
  smoke.sh         end-to-end test against a fake camera (makes real API calls)
  mock_camera.py   fake Tapo ONVIF service
  probe_events.py  check how your camera answers event requests
```

## Testing without a camera

`tests/smoke.sh` runs the whole pipeline against a fake ONVIF camera and a generated test
video. It needs `OPENAI_API_KEY` set, either in `.env` or in the environment, and makes a
couple of real Decisions API calls. The test video has no person in it, so the expected
verdict is `no_person`.

```bash
./run.sh            # once, to create .venv; then stop it with Ctrl+C
bash tests/smoke.sh
```

## Troubleshooting

- **"NotAuthorized" error.** The username or password is wrong, or it's the TP-Link cloud
  login instead of the Camera Account.
- **Connected, but nothing is captured.** Check that Motion Detection is on and Privacy Mode
  is off in the Tapo app. Then watch **Diagnostics > Latest ONVIF messages** while you
  move in front of the camera: `CellMotionDetector/Motion` with `IsMotion: true` should
  appear.
- **"cannot reach http://…:2020".** `CAMERA_HOST` is wrong, or the machine can't reach the
  camera. Try `ping <camera-ip>`.
- **`localhost:8765` doesn't open.** The server isn't running. Start it with `./run.sh`.
- `python -m tests.probe_events` checks the camera's event service directly, using your
  `.env`.

Two Tapo quirks the code works around: the camera drops an event request that waits longer
than about 10 seconds, and it marks every event update as `Initialized`. The app polls every
5 seconds and reacts to the motion value itself, whatever the label.

## Privacy and security

- Pictures of anyone the camera sees are sent to OpenAI for analysis. Make sure that's
  acceptable where you live and for the people the camera covers.
- Pictures are stored locally in `events/` and deleted after `KEEP_DAYS`.
- The dashboard has no login, so by default it listens only on this machine (`127.0.0.1`).
  Don't expose it to a network without adding authentication.
- Never commit `.env` or API keys. `.gitignore` already excludes `.env`,
  `decisionsapi.txt` and `events/`.

## License

MIT. See [LICENSE](LICENSE).
