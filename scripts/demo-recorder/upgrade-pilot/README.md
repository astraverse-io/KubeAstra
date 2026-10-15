# Upgrade Pilot demo GIF

Builds [`docs/media/upgrade-pilot-demo.gif`](../../../docs/media/upgrade-pilot-demo.gif):
a ~20-second terminal clip of `kubeastra upgrade plan` finding three removed
APIs in a rendered chart and exiting 1 — the exit code that fails a CI check.

```bash
cd scripts/demo-recorder && npm install        # once: Playwright
upgrade-pilot/make-gif.sh                      # latest kubeastra from PyPI
upgrade-pilot/make-gif.sh 0.2.3                # or pin a version
```

Needs Python 3.11+ (set `PYTHON=/path/to/python3.11+` if `python3` is older),
Node 20+, and Google Chrome. Everything else goes into a throwaway venv.

## The output is real

The GIF never shows text someone typed in: it **replays** what the commands
printed.

| Step | What it does |
|---|---|
| `capture.py` | Runs each command for real against `fixtures/rendered/` and saves the output verbatim to `timeline.json` (committed, so you can audit exactly what the GIF shows). `$?` in the last step is the previous command's real exit code. |
| `frames.mjs` | Loads `player.html` in Chrome and renders one frame per 80 ms of the animation — typing the commands, then printing the captured output. |
| `assemble.py` | Shared palette, no dithering, identical frames merged → ~94 frames, ~0.6 MB. |

`capture.py` **refuses to record from a repo checkout**: it requires the
upgrade core bundled in the installed package (`kubeastra._vendor`), because the
clip is evidence that `pip install kubeastra` works. That is exactly the case
that was broken before 0.2.3 while every checkout-based test passed.

The fixture is a demo input (like `demo/`), shaped like `helm template`
output. The findings come from the tool's real deprecation map.

## When to rebuild

After a CLI release that changes `kubeastra upgrade` output, or when the
deprecation map changes what the fixture reports. `capture.py` fails loudly if
the plan no longer shows 3 blocking APIs and exit 1, so a silent change in
behaviour can't produce a misleading clip.
