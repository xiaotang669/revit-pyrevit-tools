# -*- coding: utf-8 -*-
# ============================================================
#  Author  : xiaotang669
#  GitHub  : https://github.com/xiaotang669/revit-pyrevit-tools
#  Bilibili: https://space.bilibili.com/1562942194
#  本项目开源免费。如果你是在其他地方付费购买的，说明你被骗了。
# ============================================================
"""
Extend a batch of walls and beams to one target element.
Environment: pyRevit 6.5.3 / IronPython 2.7 / Revit 2020 API

FLOW
  1. Box-select the elements to extend.
  2. Pick one target element (wall or beam, straight location line).
  3. Every selected element is solved and extended on its own, in its own
     SubTransaction, so one failure cannot affect the others.

WHAT IS ACCEPTED
  walls with WallType.Kind == Basic, and structural framing (OST_StructuralFraming).
  Everything else - MEP, openings, furniture, grids, ... - is reported as
  [skipped] and never touched.

GEOMETRY
  The intersection is solved in 2D (Z ignored), but the end point is built
  along the element's real 3D direction:  hit = a0 + d_a * t
  A horizontal element is unaffected; a sloped one keeps its slope.

  If the intersection lies on the target's own segment (u in 0..1) it is used
  directly; otherwise EXTEND_FALLBACK = "line" extends to the target's
  infinite line, so the element keeps its own direction.

OPENINGS  (walls only - beams have no hosted elements)
  Changing a wall's location curve makes Revit re-place its hosted doors and
  windows by their distance from the wall's START. Their world positions are
  recorded before the change and restored afterwards. If one disappears, that
  wall is rolled back and reported as failed.

KNOWN LIMITATION
  The LocationCurve setter is verified for walls only. If Revit refuses it for
  beams in your model, every beam shows up under [failed] with the real error
  and nothing else is affected.
"""

__title__ = "Extend walls and beams to target"

import math

from pyrevit import revit, DB, UI

doc = revit.doc
uidoc = revit.uidoc

# ----------------------------------------------------------------------
# parameters
# ----------------------------------------------------------------------
TOL_PARALLEL = 1e-12           # singularity guard for the intersection solve
MIN_LENGTH = 10.0 / 304.8      # 10 mm: shortest element Revit accepts
REFUSE_TO_SHORTEN = True       # never shorten, only extend
EXTEND_FALLBACK = "line"       # extend to the target's infinite line


# ----------------------------------------------------------------------
# category ids, resolved one by one so a bad name cannot kill the script
# ----------------------------------------------------------------------
def _bic(name):
    v = getattr(DB.BuiltInCategory, name, None)
    return None if v is None else int(v)


WALL_BIC = _bic("OST_Walls")
FRAMING_BIC = _bic("OST_StructuralFraming")


# ----------------------------------------------------------------------
# classification
# ----------------------------------------------------------------------
def category_label(element):
    try:
        cat = element.Category
        return cat.Name if cat is not None else "no category"
    except Exception:
        return "unknown"


def classify(element):
    """'wall', 'beam' or None. Category can be null - always guard it."""
    try:
        cat = element.Category
        if cat is None:
            return None
        cid = cat.Id.IntegerValue
        if WALL_BIC is not None and cid == WALL_BIC:
            return "wall"
        if FRAMING_BIC is not None and cid == FRAMING_BIC:
            return "beam"
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------
# geometry helpers
# ----------------------------------------------------------------------
def straight_line_of(element):
    """The element's LocationCurve when it is a straight Line, else None."""
    loc = element.Location
    if not isinstance(loc, DB.LocationCurve):
        return None
    c = loc.Curve
    return c if isinstance(c, DB.Line) else None


def intersect_2d(p1, d1, p2, d2):
    """
    Intersection of two infinite 2D lines, Z ignored.
        line 1: p1 + t * d1        line 2: p2 + u * d2
    Returns (t, u) or None when parallel. The parameter is valid for the 3D
    line as well, because a straight line is parametrised the same way in 2D.
    """
    denom = d1.X * d2.Y - d1.Y * d2.X
    if abs(denom) < TOL_PARALLEL:
        return None
    dx = p2.X - p1.X
    dy = p2.Y - p1.Y
    return ((dx * d2.Y - dy * d2.X) / denom,
            (dx * d1.Y - dy * d1.X) / denom)


def point_at(p0, d, t):
    """Point on the line - keeps the real 3D direction, so slopes survive."""
    return DB.XYZ(p0.X + d.X * t, p0.Y + d.Y * t, p0.Z + d.Z * t)


def pick_element(prompt):
    try:
        ref = uidoc.Selection.PickObject(
            UI.Selection.ObjectType.Element, prompt)
    except Exception:
        return None
    return doc.GetElement(ref.ElementId) if ref is not None else None


# ----------------------------------------------------------------------
# openings (walls only)
# ----------------------------------------------------------------------
def centre_of(element):
    bb = element.get_BoundingBox(None)
    if bb is None:
        return None
    return DB.XYZ((bb.Min.X + bb.Max.X) / 2.0,
                  (bb.Min.Y + bb.Max.Y) / 2.0,
                  (bb.Min.Z + bb.Max.Z) / 2.0)


def snapshot_openings(wall):
    """[(ElementId, world centre)] for every FamilyInstance hosted by wall."""
    out = []
    for fi in (DB.FilteredElementCollector(doc)
               .OfClass(DB.FamilyInstance)
               .WhereElementIsNotElementType()):
        try:
            host = fi.Host
        except Exception:
            host = None
        if host is None or host.Id.IntegerValue != wall.Id.IntegerValue:
            continue
        c = centre_of(fi)
        if c is not None:
            out.append((fi.Id, c))
    return out


def restore_openings(snaps):
    """Move every recorded opening back. Returns (moved, [ids that are gone])."""
    moved, missing = 0, []
    for oid, was in snaps:
        fi = doc.GetElement(oid)          # ElementId: safe to reuse
        if fi is None:
            missing.append(oid.IntegerValue)
            continue
        now = centre_of(fi)
        if now is None:
            missing.append(oid.IntegerValue)
            continue
        delta = DB.XYZ(was.X - now.X, was.Y - now.Y, was.Z - now.Z)
        if abs(delta.X) + abs(delta.Y) + abs(delta.Z) < 1e-6:
            continue
        # the shift is along the host wall, so moving is allowed
        DB.ElementTransformUtils.MoveElement(doc, oid, delta)
        moved += 1
    return moved, missing


# ----------------------------------------------------------------------
# extend one element - raises on failure, returns a result dict otherwise
# ----------------------------------------------------------------------
def extend_one(element, kind, c_target):
    # ---- guards -------------------------------------------------------
    if element.GroupId != DB.ElementId.InvalidElementId:
        return {"skip": "inside a group"}
    if element.Pinned:
        return {"skip": "pinned"}
    if kind == "wall":
        try:
            if element.WallType.Kind != DB.WallKind.Basic:
                return {"skip": "not a basic wall (%s)"
                                % element.WallType.Kind}
        except Exception:
            pass

    c = straight_line_of(element)
    if c is None:
        return {"skip": "location curve is not a straight line"}

    a0 = c.GetEndPoint(0)
    a1 = c.GetEndPoint(1)
    d_a = a1 - a0

    b0 = c_target.GetEndPoint(0)
    b1 = c_target.GetEndPoint(1)
    d_b = b1 - b0

    # ---- solve in 2D --------------------------------------------------
    res = intersect_2d(a0, d_a, b0, d_b)
    if res is None:
        return {"skip": "parallel to the target"}

    t, u = res

    # hit keeps the element's own 3D direction, so a sloped beam stays sloped
    hit = point_at(a0, d_a, t)
    mode = "target" if (0.0 <= u <= 1.0) else "extension line"

    # ---- which end moves ----------------------------------------------
    old_len = a0.DistanceTo(a1)
    if a0.DistanceTo(hit) <= a1.DistanceTo(hit):
        fixed_end, moved_name = a1, "start"
    else:
        fixed_end, moved_name = a0, "end"

    new_len = fixed_end.DistanceTo(hit)

    if REFUSE_TO_SHORTEN and new_len < old_len - 1e-6:
        return {"skip": "would shorten the element (%.0f -> %.0f mm)"
                        % (old_len * 304.8, new_len * 304.8)}
    if new_len < MIN_LENGTH:
        return {"skip": "the result would be too short for Revit"}

    # ---- openings: walls only, beams have no hosted elements -----------
    snaps = snapshot_openings(element) if kind == "wall" else []

    # ---- write the new curve -------------------------------------------
    element.Location.Curve = DB.Line.CreateBound(fixed_end, hit)
    if snaps:
        # only regenerate when there is dependent geometry to read back
        doc.Regenerate()

    moved, missing = restore_openings(snaps)
    if missing:
        # never let an opening disappear silently
        raise Exception("opening(s) %s disappeared while extending" % missing)

    return {"ok": True,
            "moved_end": moved_name,
            "mode": mode,
            "old_len": old_len,
            "new_len": new_len,
            "openings_moved": moved}


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    # ---- 1. box-select the elements to extend -------------------------
    try:
        refs = uidoc.Selection.PickObjects(
            UI.Selection.ObjectType.Element,
            "Box-select the walls and beams to extend, then press Enter")
    except Exception:
        return

    elements, seen = [], set()
    for r in refs:
        if r.ElementId in seen:
            continue
        seen.add(r.ElementId)
        e = doc.GetElement(r.ElementId)
        if e is not None:
            elements.append(e)

    if not elements:
        UI.TaskDialog.Show("Extend", "Nothing selected.")
        return

    # ---- 2. classify ----------------------------------------------------
    todo, skips = [], []
    for el in elements:
        kind = classify(el)
        if kind is None:
            skips.append("%d: unsupported category (%s)"
                         % (el.Id.IntegerValue, category_label(el)))
            continue
        if el.GroupId != DB.ElementId.InvalidElementId:
            skips.append("%d: inside a group" % el.Id.IntegerValue)
            continue
        if el.Pinned:
            skips.append("%d: pinned" % el.Id.IntegerValue)
            continue
        if straight_line_of(el) is None:
            skips.append("%d: location curve is not a straight line"
                         % el.Id.IntegerValue)
            continue
        todo.append((el, kind))

    if not todo:
        UI.TaskDialog.Show("Extend", "No wall or beam could be used.")
        for line in skips:
            print("[skipped] %s" % line)
        return

    # ---- 3. pick the target ---------------------------------------------
    target = pick_element("Pick the TARGET element (wall or beam)")
    if target is None:
        return

    if classify(target) is None:
        UI.TaskDialog.Show("Extend",
                           "The target must be a wall or a beam.")
        return

    c_target = straight_line_of(target)
    if c_target is None:
        UI.TaskDialog.Show("Extend",
                           "The target has no straight location line.")
        return

    tid = target.Id.IntegerValue
    todo = [(el, kind) for (el, kind) in todo if el.Id.IntegerValue != tid]
    if not todo:
        UI.TaskDialog.Show("Extend", "Only the target itself was selected.")
        return

    # ---- 4. extend -------------------------------------------------------
    extended = 0
    onto_target = 0
    onto_extension = 0
    openings_moved = 0
    fails, detail = [], []

    transaction = DB.Transaction(doc, "Extend walls and beams to target")
    transaction.Start()
    try:
        for element, kind in todo:
            sub = DB.SubTransaction(doc)
            committed = False
            try:
                sub.Start()
                info = extend_one(element, kind, c_target)
                sub.Commit()
                committed = True
            except Exception as ex:
                # a failing rollback must never hide the real error
                if not committed:
                    try:
                        sub.RollBack()
                    except Exception:
                        pass
                fails.append("%d: %s" % (element.Id.IntegerValue, ex))
                continue

            if "skip" in info:
                skips.append("%d: %s" % (element.Id.IntegerValue,
                                         info["skip"]))
                continue

            extended += 1
            openings_moved += info["openings_moved"]
            if info["mode"] == "target":
                onto_target += 1
            else:
                onto_extension += 1

            detail.append("[extended] %d: moved the %s end, %.0f -> %.0f mm, "
                          "target: %d (%s)"
                          % (element.Id.IntegerValue, info["moved_end"],
                             info["old_len"] * 304.8, info["new_len"] * 304.8,
                             tid, info["mode"]))

        transaction.Commit()
    except Exception as ex:
        try:
            transaction.RollBack()
        except Exception:
            pass
        UI.TaskDialog.Show("Extend", "Rolled back:\n%s" % ex)
        raise
    finally:
        # belt and braces: never leave the transaction hanging
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass

    # ---- 5. report --------------------------------------------------------
    for line in skips:
        print("[skipped] %s" % line)
    for line in fails:
        print("[failed ] %s" % line)
    for line in detail:
        print(line)

    UI.TaskDialog.Show(
        "Extend",
        "Extended            : %d\n"
        "  onto the target   : %d\n"
        "  onto its ext. line: %d\n"
        "Openings moved back : %d\n"
        "Skipped             : %d\n"
        "Failed              : %d"
        % (extended, onto_target, onto_extension, openings_moved,
           len(skips), len(fails)))


if __name__ == "__main__":
    main()
