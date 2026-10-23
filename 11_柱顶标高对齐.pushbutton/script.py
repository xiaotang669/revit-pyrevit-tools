# -*- coding: utf-8 -*-
"""
按绝对顶标高批量驱动结构柱。
环境：Revit 2020 / pyRevit / IronPython 2.7

实现要点：
  1. 输入的目标顶标高是"米"，先换算成英尺（Revit 内部单位）。
  2. 柱子的顶部有两种驱动方式，脚本会自动识别：
       - 顶部标高驱动：设置「顶部偏移」= 目标标高 - 顶部标高
       - 无连接高度：  设置「实例长度」= 目标标高 - 底部标高
     这两种情况的数值不一样，不能一律用"高度"去填「顶部偏移」，
     否则柱子顶部会跑到错误的位置。
  3. 每根柱子独立 SubTransaction，单根失败不影响其他柱。
  4. 修改后调用 doc.Regenerate() 强制刷新几何。
"""

__title__ = "结构柱顶标高批量驱动"

from pyrevit import revit, DB, UI, forms

doc = revit.doc
uidoc = revit.uidoc

# ----------------------------------------------------------------------
# 参数区
# ----------------------------------------------------------------------
MIN_HEIGHT_FT = 10.0 / 304.8     # 柱高小于 10mm 视为不合理，跳过
TOL_FT = 1e-4                    # 复核顶标高时的允许偏差
M_TO_FT = 1.0 / 0.3048           # 1 米 = 1/0.3048 英尺


# ----------------------------------------------------------------------
# 工具：安全解析 BuiltInParameter / StructuralType，写错不会让脚本崩掉
# ----------------------------------------------------------------------
def _bip(name):
    return getattr(DB.BuiltInParameter, name, None)


def _get_structural_type_enum():
    """
    StructuralType 在 Autodesk.Revit.DB.Structure，
    直接写 DB.StructuralType 会 AttributeError（2020 实测）。
    """
    try:
        return DB.Structure.StructuralType
    except Exception:
        pass
    try:
        from Autodesk.Revit.DB.Structure import StructuralType
        return StructuralType
    except Exception:
        return None


BIP_BASE_LEVEL  = _bip("FAMILY_BASE_LEVEL_PARAM")
BIP_BASE_OFFSET = _bip("FAMILY_BASE_LEVEL_OFFSET_PARAM")
BIP_TOP_LEVEL   = _bip("FAMILY_TOP_LEVEL_PARAM")
BIP_TOP_OFFSET  = _bip("FAMILY_TOP_LEVEL_OFFSET_PARAM")
BIP_LENGTH      = _bip("INSTANCE_LENGTH_PARAM")

STRUCTURAL_TYPE = _get_structural_type_enum()
ST_COLUMN = STRUCTURAL_TYPE.Column if STRUCTURAL_TYPE is not None else None


# ----------------------------------------------------------------------
# 读取柱子的底部标高（绝对 Z，英尺）
# ----------------------------------------------------------------------
def base_elevation_of(element):
    """
    优先用「底部标高 + 底部偏移」计算；
    取不到时退回 Location 点的 Z。
    """
    if BIP_BASE_LEVEL is not None:
        try:
            p = element.get_Parameter(BIP_BASE_LEVEL)
            if p is not None and p.StorageType == DB.StorageType.ElementId:
                lvl = doc.GetElement(p.AsElementId())
                if lvl is not None:
                    elev = lvl.Elevation
                    if BIP_BASE_OFFSET is not None:
                        p_off = element.get_Parameter(BIP_BASE_OFFSET)
                        if p_off is not None and \
                                p_off.StorageType == DB.StorageType.Double:
                            elev += p_off.AsDouble()
                    return elev
        except Exception:
            pass

    try:
        loc = element.Location
        if isinstance(loc, DB.LocationPoint):
            return loc.Point.Z
    except Exception:
        pass

    return None


def top_level_of(element):
    """返回柱子的顶部标高元素；无连接高度时返回 None。"""
    if BIP_TOP_LEVEL is None:
        return None
    try:
        p = element.get_Parameter(BIP_TOP_LEVEL)
        if p is None or p.StorageType != DB.StorageType.ElementId:
            return None
        lvl_id = p.AsElementId()
        if lvl_id is None or lvl_id == DB.ElementId.InvalidElementId:
            return None
        return doc.GetElement(lvl_id)
    except Exception:
        return None


# ----------------------------------------------------------------------
# 修改单根柱子
# ----------------------------------------------------------------------
def drive_column(element, z_target):
    """
    把柱顶驱动到 z_target（英尺）。
    返回 ("ok", 说明) 或 ("skip", 原因)；失败抛异常由调用方回滚。
    """
    if element.Pinned:
        return ("skip", u"已锁定（Pinned）")

    z_base = base_elevation_of(element)
    if z_base is None:
        return ("skip", u"读不到底部标高")

    top_level = top_level_of(element)

    if top_level is not None:
        # ---- 情况 A：顶部标高驱动，改「顶部偏移」 ----
        if BIP_TOP_OFFSET is None:
            return ("skip", u"没有「顶部偏移」参数")
        p_off = element.get_Parameter(BIP_TOP_OFFSET)
        if p_off is None:
            return ("skip", u"取不到「顶部偏移」参数")
        if p_off.IsReadOnly:
            return ("skip", u"「顶部偏移」只读")
        if p_off.StorageType != DB.StorageType.Double:
            return ("skip", u"「顶部偏移」不是数值型")

        new_offset = z_target - top_level.Elevation
        p_off.Set(new_offset)

        # 复核
        now = top_level.Elevation + p_off.AsDouble()
        if abs(now - z_target) > TOL_FT:
            return ("skip", u"复核偏差过大（%.1f mm）" % ((now - z_target) * 304.8))

        return ("ok", u"顶部偏移 → %.0f mm（顶部标高 %s）"
                      % (new_offset * 304.8, top_level.Name))

    # ---- 情况 B：无连接高度，改「实例长度」 ----
    if BIP_LENGTH is None:
        return ("skip", u"没有「实例长度」参数，且顶部未绑定标高")
    p_len = element.get_Parameter(BIP_LENGTH)
    if p_len is None:
        return ("skip", u"取不到「实例长度」参数")
    if p_len.IsReadOnly:
        return ("skip", u"「实例长度」只读")
    if p_len.StorageType != DB.StorageType.Double:
        return ("skip", u"「实例长度」不是数值型")

    new_len = z_target - z_base
    if new_len < MIN_HEIGHT_FT:
        return ("skip", u"算出的高度过小（%.0f mm）" % (new_len * 304.8))

    p_len.Set(new_len)

    now = z_base + p_len.AsDouble()
    if abs(now - z_target) > TOL_FT:
        return ("skip", u"复核偏差过大（%.1f mm）" % ((now - z_target) * 304.8))

    return ("ok", u"实例长度 → %.0f mm" % (new_len * 304.8))


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main():
    # ---- 1. 取当前选择 ------------------------------------------------
    try:
        sel_ids = uidoc.Selection.GetElementIds()
    except Exception:
        sel_ids = None

    if not sel_ids or len(sel_ids) == 0:
        UI.TaskDialog.Show(u"结构柱顶标高", u"请先选中结构柱，再运行本脚本。")
        return

    if ST_COLUMN is None:
        UI.TaskDialog.Show(
            u"结构柱顶标高",
            u"无法解析 StructuralType 枚举（它在 Autodesk.Revit.DB.Structure 下）。")
        return

    # ---- 2. 输入目标顶标高 --------------------------------------------
    raw = forms.ask_for_string(
        default="",
        prompt=u"请输入目标顶标高（单位：米，例如 10.32）",
        title=u"结构柱顶标高批量驱动")

    if raw is None:
        return                                  # 用户点了取消

    text = (raw or u"").strip().lower()
    # 允许用户顺手带上单位
    if text.endswith("m"):
        text = text[:-1].strip()
    elif text.endswith(u"米"):
        text = text[:-1].strip()

    try:
        target_m = float(text)
    except Exception:
        UI.TaskDialog.Show(
            u"结构柱顶标高",
            u"输入格式不正确：「%s」\n请输入一个数字，例如 10.32" % raw)
        return

    # 地下室标高为负是合理的，这里只做一个宽松的合理性检查
    if target_m < -1000.0 or target_m > 2000.0:
        UI.TaskDialog.Show(
            u"结构柱顶标高",
            u"标高数值超出合理范围：%.2f m" % target_m)
        return

    z_target = target_m * M_TO_FT

    # ---- 3. 筛选结构柱 ------------------------------------------------
    columns = []
    skipped = []                    # [(构件ID, 原因)]

    for eid in sel_ids:
        e = doc.GetElement(eid)
        if e is None:
            continue

        if not isinstance(e, DB.FamilyInstance):
            skipped.append((eid.IntegerValue, u"不是族实例"))
            continue

        try:
            if e.StructuralType != ST_COLUMN:
                skipped.append((eid.IntegerValue, u"不是结构柱"))
                continue
        except Exception:
            skipped.append((eid.IntegerValue, u"读不到结构类型"))
            continue

        columns.append(e)

    if not columns:
        UI.TaskDialog.Show(u"结构柱顶标高", u"选中的构件里没有结构柱。")
        return

    # ---- 4. 逐根驱动 --------------------------------------------------
    modified = 0
    detail = []

    tr = DB.Transaction(doc, u"结构柱顶标高驱动 %.3f m" % target_m)
    tr.Start()
    try:
        for col in columns:
            eid_int = col.Id.IntegerValue
            sub = DB.SubTransaction(doc)
            committed = False
            try:
                sub.Start()
                status, message = drive_column(col, z_target)
                if status == "skip":
                    sub.RollBack()
                    committed = True        # 已主动回滚，视作结束
                    skipped.append((eid_int, message))
                    continue
                sub.Commit()
                committed = True
            except Exception as ex:
                if not committed:
                    try:
                        sub.RollBack()
                    except Exception:
                        pass
                skipped.append((eid_int, u"修改失败：%s" % ex))
                continue

            modified += 1
            detail.append((eid_int, message))

        # 强制刷新几何
        doc.Regenerate()
        tr.Commit()
    except Exception as ex:
        try:
            tr.RollBack()
        except Exception:
            pass
        UI.TaskDialog.Show(u"结构柱顶标高", u"整体回滚：\n%s" % ex)
        raise
    finally:
        try:
            if tr.GetStatus() == DB.TransactionStatus.Started:
                tr.RollBack()
        except Exception:
            pass

    # ---- 5. 报告 ------------------------------------------------------
    body = [u"目标顶标高：%.3f m" % target_m,
            u"成功修改：%d 根" % modified,
            u"跳过：%d 根" % len(skipped)]

    if detail:
        body.append(u"")
        body.append(u"修改明细：")
        for eid_int, message in detail[:15]:
            body.append(u"  %d：%s" % (eid_int, message))
        if len(detail) > 15:
            body.append(u"  ……（还有 %d 根）" % (len(detail) - 15))

    if skipped:
        body.append(u"")
        body.append(u"跳过明细：")
        for eid_int, reason in skipped[:15]:
            body.append(u"  %d：%s" % (eid_int, reason))
        if len(skipped) > 15:
            body.append(u"  ……（还有 %d 根）" % (len(skipped) - 15))

    UI.TaskDialog.Show(u"结构柱顶标高", u"\n".join(body))


if __name__ == "__main__":
    main()