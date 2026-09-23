# Third-party notices

This repository's own code is released under the MIT License (see `LICENSE`).
It builds on the following third-party components, which keep their own licenses.

## pydobot (MIT)

`src/devices/dobot/pydobot_patch.py` monkey-patches and extends
[pydobot](https://github.com/luismesas/pydobot); its replacements of
`Dobot._send_command`, `_send_message`, `_read_message`,
`_get_queued_cmd_current_index` and the `__init__` wrapper are derived from pydobot 1.3.2.

```
Copyright 2017 Luis Mesas

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be included in all copies
or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

## Speech-recognition models

Voice input downloads `whisper-large-v3-turbo` weights (OpenAI, MIT) in the
CTranslate2 conversion `deepdml/faster-whisper-large-v3-turbo-ct2`, and optionally
`kotoba-tech/kotoba-whisper-v2.0` (Apache-2.0), from Hugging Face at first use. They
are not redistributed here.

## Browser libraries loaded by the sensor dashboard

`src/monitoring/dashboard/static/index.html` loads [Chart.js](https://www.chartjs.org/)
(MIT), [Hammer.js](https://hammerjs.github.io/) (MIT) and
[chartjs-plugin-zoom](https://www.chartjs.org/chartjs-plugin-zoom/) (MIT) from the
jsDelivr CDN at page load. They are not redistributed in this repository.

## Python packages

Runtime dependencies listed in `requirements.txt` are used unmodified under their
respective licenses (Streamlit: Apache-2.0; FastAPI, pydantic, python-dotenv,
pyserial, bleak, opencv-python, numpy, pandas: MIT/BSD/Apache-2.0;
google-generativeai, openai: Apache-2.0; faster-whisper: MIT).

## Ultralytics YOLOv8 (AGPL-3.0) — `detection/` only

The optional object-detection module in `detection/` imports
[Ultralytics YOLOv8](https://github.com/ultralytics/ultralytics), which is licensed
under the **GNU Affero General Public License v3.0**. Ultralytics' own code is *not*
vendored here; it is installed from PyPI via `detection/requirements.txt`.

Because of that dependency, **everything under `detection/` is itself distributed under
AGPL-3.0**. The full license text is included at
[`detection/LICENSE`](detection/LICENSE) — a verbatim copy of
<https://www.gnu.org/licenses/agpl-3.0.txt> — and the scope is restated in
[`detection/README.md`](detection/README.md#license-notice).

The AGPL applies to `detection/` and nowhere else. **The rest of this repository is
MIT** (code, see [`LICENSE`](LICENSE)) **and CC BY 4.0** (documentation and CAD), and
**does not import `ultralytics`**; `detection/` is an isolated, optional module and
nothing in `src/` or `examples/` depends on it. Verified with:

```bash
grep -rn "ultralytics\|from ultralytics\|YOLO(" src examples   # no matches
```

If you modify the code in `detection/` and then distribute it, or make it available to
users over a network, AGPL-3.0 (in particular §13) requires you to offer them the
corresponding source of your modified version. Using `detection/` privately, or using
the MIT-licensed rest of the platform without `detection/`, carries no such obligation.

## Not included

The vendor SDK for Dobot (DobotDll) is intentionally not part of this repository, and
neither is the Ultralytics source itself (see the section above); see `README.md` →
Licensing and `detection/README.md`.
