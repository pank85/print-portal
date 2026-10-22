"""The uniform shrink factor that makes an upload fit the selected machine's bed.

`mesh.py` measures, `profiles.bed_limits` reads the bed; this module decides whether the two
go together and, when they can, by how much the model has to shrink. It never enlarges a
model (the factor is capped at 1.0) and never rounds up (it is floored to three decimals),
because both mistakes eat the clearance the margin exists to keep.

The margin is XY only. Brim and skirt print beside the part on the plate, so nothing has to
be reserved above it: the Z limit is the bare `printable_height`. The comparison is done on
raw spans, so the answer does not depend on the margin.

Every refusal is a `FitError` whose message a user can act on — how big the model is, what
was judged against, and what to change — because the API writes it verbatim into the job's
`error`. `mesh.MeshError` and `prof.ProfileError` are not swallowed: a file that cannot be
measured and a preset that cannot be read are different problems from a model that is
merely too large.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import mesh
import profiles as prof

FIT_MARGIN_MM = 16.0
"""Clearance kept on each of X and Y for brim and skirt, in millimetres."""

MIN_FIT_SCALE = 0.25
"""Below this factor a shrink is refused: the part would no longer print as designed."""

_DECIMALS = 3


class FitError(RuntimeError):
    """The model cannot be sliced inside the machine's limits."""


@dataclass(frozen=True, slots=True)
class FitPlan:
    """The measurement a job is sliced from: the model's box, the bed, and the scale to apply."""

    bbox: mesh.BoundingBox
    bed: prof.BedLimits
    scale: float                 # 1.0 == the caller passes no --scale


def exceeds_bed(bbox: mesh.BoundingBox, bed: prof.BedLimits) -> bool:
    """True when the model is larger than the bed in any axis, margin excluded.

    A different question from `scale_factor`: this one answers "could this ever fit", so the
    clearance plays no part. A 210 mm cube on a 220 mm bed does not exceed it; a 300 mm one
    does.
    """
    sx, sy, sz = bbox.size
    bx, by, bz = bed.size
    return sx > bx or sy > by or sz > bz


def scale_factor(bbox: mesh.BoundingBox, bed: prof.BedLimits,
                 margin: float = FIT_MARGIN_MM) -> float:
    """The largest uniform factor <= 1.0 that brings ``bbox`` inside ``bed``, floored.

    Raises `FitError` when there is no factor to speak of: a degenerate box has no size to
    divide by, and a bed no wider than the margin leaves nothing to shrink into.
    """
    if bbox.is_degenerate:
        raise FitError("the model has no printable geometry (degenerate bounding box)")
    bed_x, bed_y, bed_z = bed.size
    usable_x = bed_x - margin
    usable_y = bed_y - margin
    if usable_x <= 0 or usable_y <= 0:
        raise FitError(
            f"the bed of '{bed.machine}' is {bed_x:g}x{bed_y:g}x{bed_z:g} mm, too small to "
            f"hold a {margin:g} mm fit margin"
        )
    sx, sy, sz = bbox.size
    k = min(1.0, usable_x / sx, usable_y / sy, bed_z / sz)
    return math.floor(k * 10 ** _DECIMALS) / 10 ** _DECIMALS


def _too_big_message(name: str, bbox: mesh.BoundingBox, bed: prof.BedLimits) -> str:
    sx, sy, sz = bbox.size
    bx, by, bz = bed.size
    return (f"{name} is {sx:g}x{sy:g}x{sz:g} mm and does not fit the {bx:g}x{by:g}x{bz:g} mm "
            f"bed of '{bed.machine}' — turn on shrink to fit (uniform scale) or upload a "
            f"smaller model")


def _below_floor_message(name: str, bbox: mesh.BoundingBox, bed: prof.BedLimits,
                         scale: float) -> str:
    sx, sy, sz = bbox.size
    bx, by, bz = bed.size
    minx, miny, minz = (round(span * MIN_FIT_SCALE, 1) for span in (sx, sy, sz))
    return (f"{name} is {sx:g}x{sy:g}x{sz:g} mm and needs a uniform scale of {scale:.2f}x to "
            f"fit the {bx:g}x{by:g}x{bz:g} mm bed of '{bed.machine}', below the "
            f"{MIN_FIT_SCALE:.2f}x floor — scale the model to at least "
            f"{minx:g}x{miny:g}x{minz:g} mm yourself and upload it again")


def plan_fit(model: Path, machine: Path, *, shrink_to_fit: bool) -> FitPlan:
    """Measure ``model`` against the bed of the ``machine`` preset and pick the scale.

    The box is measured either way, because the caller records it with the job. With
    ``shrink_to_fit`` off the model is refused when it is larger than the bed; with it on,
    the model is refused when the factor needed falls under `MIN_FIT_SCALE`.
    """
    model = Path(model)
    bbox = mesh.model_bounding_box(model)
    bed = prof.bed_limits(machine)
    name = model.name
    if bbox.is_degenerate:
        raise FitError(f"'{name}' has no printable geometry (degenerate bounding box)")
    if not shrink_to_fit:
        if exceeds_bed(bbox, bed):
            raise FitError(_too_big_message(name, bbox, bed))
        return FitPlan(bbox=bbox, bed=bed, scale=1.0)
    scale = scale_factor(bbox, bed)
    if scale < MIN_FIT_SCALE:
        raise FitError(_below_floor_message(name, bbox, bed, scale))
    return FitPlan(bbox=bbox, bed=bed, scale=scale)
