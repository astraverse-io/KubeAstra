# Resilience Pilot demo GIF

`docs/media/resilience-pilot-demo.gif` replays a real run of the published CLI
against `fixtures/rendered/checkout.yaml`: a scan that fails the gate on two
high findings (a PDB that stalls node drains at the HPA floor, an HPA scaling on
CPU the container never requests), then `--fix` and the generated PDB patch.

```bash
PYTHON=python3.11 scripts/demo-recorder/resilience-pilot/make-gif.sh 0.3.0
```

`capture.py` runs every command for real from a throwaway venv with
`kubeastra` installed from PyPI, refuses to record if the resilience core didn't
load from the installed package, and checks the output still tells the story
(RES001 + RES003, exit 1, `minAvailable: 1` in the patch). `timeline.json` is
what it captured; the GIF shows nothing else. The frame renderer and assembler
are shared with `../upgrade-pilot/` — this directory only adds its own player
page (title, timings, closing card).
