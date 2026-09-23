"""Synthetic images and fakes shared by the imaging tests."""
import numpy as np
from PIL import Image

H, W = 720, 400


def geo(men=250, bottom=650, x0=150, x1=250, cap0=100, cap1=150):
    return {"meniscus": {"y": men},
            "liquid": {"bottom_y": bottom, "fill_fraction_of_body": 0.7},
            "body": {"x0": x0, "x1": x1, "width_px": x1 - x0},
            "cap": {"y0": cap0, "y1": cap1},
            "vial": {"x0": x0 - 20, "x1": x1 + 20, "width_px": x1 - x0 + 40},
            "image_size": {"height": H, "width": W}}


def linear_to_srgb(lin):
    lin = np.clip(np.asarray(lin, dtype=np.float64), 0, 1)
    v = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.round(v * 255).astype(np.uint8)


def save_rows(path, row_values_srgb, channel=None):
    """PNG (whatever the suffix) whose every row is uniform; row_values_srgb
    has H entries. channel=None fills all three channels."""
    img = np.zeros((H, W, 3), dtype=np.uint8)
    col = np.asarray(row_values_srgb, dtype=np.uint8)[:, None]
    if channel is None:
        img[:] = col[..., None]
    else:
        img[..., channel] = col
    Image.fromarray(img).save(path, format="PNG")
    return path


class FakeLight:
    name = "NEEWER-FAKE"

    def __init__(self, fail_cct=False):
        self.calls = []
        self.fail_cct = fail_cct

    def power(self, on):
        self.calls.append(("power", on))

    def set_cct(self, b, k, gm=50):
        self.calls.append(("cct", b, k))
        if self.fail_cct:
            raise RuntimeError("BLE write failed")

    def set_hsi(self, h, s, i):
        self.calls.append(("hsi", h, s, i))

    def close(self):
        self.calls.append(("close",))
