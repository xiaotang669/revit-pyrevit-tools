# -*- coding: utf-8 -*-
# ============================================================
#  Author  : xiaotang669
#  GitHub  : https://github.com/xiaotang669/revit-pyrevit-tools
#  Bilibili: https://space.bilibili.com/1562942194
#  本项目开源免费。如果你是在其他地方付费购买的，说明你被骗了。
# ============================================================
"""
================================================================================
 机电管线移动后，一键同步墙 / 楼板洞口位置（支持链接模型）
 Revit 2020 + pyRevit + IronPython 2.7
================================================================================

 工作原理
 --------
 1. 用户框选：已移动的管线（Duct / Pipe / CableTray，即 MEPCurve）
              + 相关墙 / 楼板 / 天花
              + 需要同步的洞口（原生 Opening 或宿主于墙板的 FamilyInstance）
              + 如果墙/板/洞口在链接模型里，把「链接实例 RevitLinkInstance」也框进来
 2. 所有几何统一换算到「当前文档的世界坐标」后再计算：
      · 链接元素：link_instance.GetTotalTransform().OfPoint(...)
      · 管线：curve.CreateTransformed(transform)
    这样宿主平面、洞口中心、管线轴线都处在同一个坐标系下，位移才不会错。
 3. 先做只读计算，再在一个 Transaction 里用「每个洞口一个 SubTransaction」逐个移动。
 4. doc.Regenerate() 后弹窗报告。

 ⚠️ 重要限制（Revit API 层面，不是脚本能绕过的）
 -------------------------------------------------
 Revit 的链接文档是只读的。ElementTransformUtils.MoveElement 只能移动
 「当前活动文档」里的元素。因此：

   ✅ 洞口在当前模型、宿主墙/板在链接里  → 完全支持，位移按世界坐标计算
   ✅ 管线在链接里、洞口在当前模型        → 完全支持（只读链接管线，移动当前模型洞口）
   ❌ 洞口本身在链接模型里                → 脚本会算出正确的位移量并报告，
                                          但 API 无法写入链接文档

 遇到第 3 种情况时，请改用下面的工作方式：
   在「土建模型」里打开项目，把机电模型作为链接载入，再运行本脚本。
   此时洞口属于活动文档（可移动），管线来自链接（只读），脚本可直接完成同步。

 匹配规则（按优先级）
 --------------------
 a. 洞口中心到管线中心线的距离最小者胜出；
 b. 优先「中心线真正穿过宿主平面、且交点在宿主范围内」的管线；
 c. 距离必须 <= MAX_MATCH_DIST_MM（默认 1000mm）+ 管线半截面尺寸；
 d. 位移一律投影到宿主平面内（去掉法向分量）。
================================================================================
"""

__title__ = u"同步洞口"
__doc__ = u"根据已移动的机电管线中心线，批量对齐所选墙 / 楼板洞口的位置（支持链接模型）。"

import traceback
import System

from Autodesk.Revit.DB import (
    XYZ, Line, Transform, Outline,
    LocationCurve, LocationPoint,
    Wall, Floor, Ceiling, MEPCurve, Opening, FamilyInstance,
    RevitLinkInstance, FilteredElementCollector, BoundingBoxIntersectsFilter,
    BuiltInCategory, BuiltInParameter, StorageType,
    Transaction, SubTransaction, ElementTransformUtils,
    TransactionStatus,
    IFailuresPreprocessor, FailureProcessingResult, FailureSeverity,
)
from Autodesk.Revit.UI.Selection import ObjectType

from pyrevit import revit, forms, script

active_doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# ============================== 配置区 ==============================

# 洞口中心到管线中心线的最大匹配距离（mm）
MAX_MATCH_DIST_MM = 1000.0

# 单次允许的最大移动距离（mm），超过视为异常并跳过
MAX_MOVE_DIST_MM = 5000.0

# 小于该位移视为「已对齐」，不再移动（mm）
MIN_MOVE_MM = 0.5

# 位移超过该值时在报告中提示人工复核（mm）
WARN_DIST_MM = 300.0

# 用坐标反推宿主时，宿主包围盒的向外扩张量（mm）
HOST_BBOX_MARGIN_MM = 300.0

# 选中链接实例后，从链接里扫描候选元素的范围：
# 以当前模型中所选元素的包围盒向外扩张这么远（mm）
LINK_SCAN_MARGIN_MM = 5000.0

# 是否用管线半截面尺寸放宽匹配距离
USE_SIZE_TOLERANCE = True

# 试运行：只计算、不移动（强烈建议第一次先设 True 看匹配结果）
DRY_RUN = False

# 是否允许尝试直接移动「链接模型中的洞口」。
# Revit API 不允许修改链接文档，打开后大概率抛异常；默认关闭，
# 脚本会在报告里给出正确的位移量，供你在土建模型里处理。
ALLOW_LINKED_MOVE_ATTEMPT = False

# 原生 Opening 移动失败时是否尝试「删旧建新」（风险高，默认关闭）
ENABLE_RECREATE_FALLBACK = False

# 默认不当作洞口处理的 FamilyInstance 类别（按 BuiltInCategory 名称）
# 族名带 OPENING_NAME_KEYWORDS 关键字的会绕过这张黑名单。
EXCLUDE_FI_CATEGORY_NAMES = (
    "OST_Doors",
    "OST_Windows",
    "OST_Furniture",
    "OST_FurnitureSystems",
    "OST_Casework",
    "OST_CurtainWallPanels",
    "OST_CurtainWallMullions",
    "OST_DuctTerminal",
    "OST_Sprinklers",
    "OST_LightingFixtures",
    "OST_LightingDevices",
    "OST_CommunicationDevices",
    "OST_DataDevices",
    "OST_FireAlarmDevices",
    "OST_SecurityDevices",
    "OST_TelephoneDevices",
    "OST_NurseCallDevices",
    "OST_MechanicalEquipment",
    "OST_ElectricalEquipment",
    "OST_ElectricalFixtures",
)

# 族 / 类型名称里带这些关键字时，无视上面的类别黑名单，强制当作洞口
OPENING_NAME_KEYWORDS = (
    u"预留",
    u"洞口",
    u"开洞",
    u"套管",
    u"opening",
    u"sleeve",
    u"penetration",
)

# ====================================================================

_FT_PER_MM = 304.8

# 当前文档里的链接实例列表（main 里初始化）
_LINKS = []


def mm(v):
    """毫米 -> Revit 内部单位（英尺）"""
    return float(v) / _FT_PER_MM


def to_mm(v):
    """Revit 内部单位（英尺）-> 毫米"""
    return float(v) * _FT_PER_MM


MAX_MATCH_DIST = mm(MAX_MATCH_DIST_MM)
MAX_MOVE_DIST = mm(MAX_MOVE_DIST_MM)
MIN_MOVE = mm(MIN_MOVE_MM)
WARN_DIST = mm(WARN_DIST_MM)
HOST_BBOX_MARGIN = mm(HOST_BBOX_MARGIN_MM)
LINK_SCAN_MARGIN = mm(LINK_SCAN_MARGIN_MM)


# ============================== 小工具 ==============================


def _short(ex):
    """异常 -> 一行短文本"""
    try:
        return unicode(ex)
    except Exception:
        try:
            return str(ex)
        except Exception:
            return u"<未知错误>"


def _bic_int(name):
    """把 BuiltInCategory 名称安全地转成 int 值；不存在返回 None"""
    bc = getattr(BuiltInCategory, name, None)
    if bc is None:
        return None
    try:
        return int(bc)
    except Exception:
        try:
            return int(System.Convert.ToInt32(bc))
        except Exception:
            return None


_EXCLUDE_CAT_IDS = None


def exclude_cat_ids():
    global _EXCLUDE_CAT_IDS
    if _EXCLUDE_CAT_IDS is None:
        ids = set()
        for n in EXCLUDE_FI_CATEGORY_NAMES:
            v = _bic_int(n)
            if v is not None:
                ids.add(v)
        _EXCLUDE_CAT_IDS = ids
    return _EXCLUDE_CAT_IDS


def _norm(v):
    """归一化，零向量返回 None"""
    try:
        n = v.Normalize()
        if n is None or n.GetLength() < 0.5:
            return None
        return n
    except Exception:
        return None


# ============================== 链接与坐标变换 ==============================


def build_link_map():
    """收集当前文档里所有有效的链接实例"""
    links = []
    try:
        for li in FilteredElementCollector(active_doc).OfClass(RevitLinkInstance):
            try:
                ld = li.GetLinkDocument()
                if ld is not None:
                    links.append(li)
            except Exception:
                continue
    except Exception:
        pass
    return links


def find_link_instance(edoc):
    """根据元素所属 Document 反查 RevitLinkInstance"""
    if edoc is None:
        return None
    for li in _LINKS:
        try:
            ld = li.GetLinkDocument()
            if ld is not None and ld.Equals(edoc):
                return li
        except Exception:
            continue
    return None


def make_ref(elem, forced_link=None, forced_xform=None):
    """
    把元素包装成「带坐标变换的引用」。

    xform 定义：元素所在文档的本地坐标 -> 当前文档世界坐标
      · 当前文档元素：Transform.Identity
      · 链接元素    ：RevitLinkInstance.GetTotalTransform()
    """
    ref = {
        "elem": elem,
        "doc": None,
        "link": None,
        "xform": Transform.Identity,
        "linked": False,
        "source": u"当前模型",
    }

    try:
        ref["doc"] = elem.Document
    except Exception:
        ref["doc"] = None

    is_active = True
    try:
        if ref["doc"] is not None and active_doc is not None:
            is_active = ref["doc"].Equals(active_doc)
    except Exception:
        is_active = True

    if not is_active:
        li = forced_link if forced_link is not None else find_link_instance(ref["doc"])
        if li is not None:
            ref["link"] = li
            ref["linked"] = True
            try:
                if forced_xform is not None:
                    ref["xform"] = forced_xform
                else:
                    ref["xform"] = li.GetTotalTransform()
            except Exception:
                ref["xform"] = Transform.Identity
            try:
                nm = li.Name
                ref["source"] = u"链接: %s" % (nm if nm else u"未命名")
            except Exception:
                ref["source"] = u"链接"

    return ref


def transform_note(xform):
    """检查变换矩阵是否含镜像 / 缩放，返回提示文本或空串"""
    notes = []
    try:
        det = xform.Determinant
        if det is not None and det < 0.0:
            notes.append(u"镜像变换")
    except Exception:
        pass
    try:
        s = xform.Scale
        if s is not None and abs(s - 1.0) > 1e-3:
            notes.append(u"缩放%.3f" % s)
    except Exception:
        pass
    return u" / ".join(notes)


def ref_world_bbox(ref):
    """元素包围盒的八个角点变换到世界坐标后，取世界轴对齐包围盒"""
    try:
        bb = ref["elem"].get_BoundingBox(None)
    except Exception:
        return None
    if bb is None:
        return None

    mn = bb.Min
    mx = bb.Max
    xf = ref["xform"]

    pts = []
    for x in (mn.X, mx.X):
        for y in (mn.Y, mx.Y):
            for z in (mn.Z, mx.Z):
                try:
                    pts.append(xf.OfPoint(XYZ(x, y, z)))
                except Exception:
                    return None

    if not pts:
        return None

    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    zs = [p.Z for p in pts]
    return XYZ(min(xs), min(ys), min(zs)), XYZ(max(xs), max(ys), max(zs))


def point_in_world_bbox(pt, ref, margin=None):
    """点是否落在元素的世界包围盒（外扩 margin）内"""
    if margin is None:
        margin = HOST_BBOX_MARGIN
    bb = ref_world_bbox(ref)
    if bb is None:
        return True
    mn, mx = bb
    return (mn.X - margin <= pt.X <= mx.X + margin and
            mn.Y - margin <= pt.Y <= mx.Y + margin and
            mn.Z - margin <= pt.Z <= mx.Z + margin)


# ============================== 几何工具 ==============================


def bbox_center(elem):
    """元素本地坐标下的包围盒中心"""
    try:
        bb = elem.get_BoundingBox(None)
        if bb is None:
            return None
        return bb.Min.Add(bb.Max).Multiply(0.5)
    except Exception:
        return None


def element_center(elem):
    """
    取元素本地坐标下的几何中心：
      1. 包围盒中心（对 Opening 和族洞口都是最稳的"几何中心"）
      2. LocationPoint
      3. LocationCurve 中点
    """
    c = bbox_center(elem)
    if c is not None:
        return c

    loc = None
    try:
        loc = elem.Location
    except Exception:
        loc = None

    if isinstance(loc, LocationPoint):
        try:
            if loc.Point is not None:
                return loc.Point
        except Exception:
            pass

    if isinstance(loc, LocationCurve):
        try:
            curve = loc.Curve
            if curve is not None:
                return curve.Evaluate(0.5, True)
        except Exception:
            pass

    return None


def ref_center_world(ref):
    """元素几何中心的世界坐标"""
    c = element_center(ref["elem"])
    if c is None:
        return None
    try:
        return ref["xform"].OfPoint(c)
    except Exception:
        return None


def _line_closest_point(line, pt):
    """线段上距 pt 最近的点（严格夹在 [0,1] 区间内）"""
    p0 = line.GetEndPoint(0)
    p1 = line.GetEndPoint(1)
    v = p1.Subtract(p0)
    l2 = v.DotProduct(v)
    if l2 < 1e-12:
        return p0
    t = pt.Subtract(p0).DotProduct(v) / l2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return p0.Add(v.Multiply(t))


def _sample_closest_point(curve, pt, samples=48):
    """非直线（弧 / 样条）用采样 + 黄金分割细化求最近点"""
    best_t = 0.0
    best_d = None
    for i in range(samples + 1):
        t = float(i) / samples
        try:
            p = curve.Evaluate(t, True)
        except Exception:
            return None
        d = p.DistanceTo(pt)
        if best_d is None or d < best_d:
            best_d = d
            best_t = t

    step = 1.0 / samples
    lo = max(0.0, best_t - step)
    hi = min(1.0, best_t + step)
    for _ in range(40):
        m1 = lo + (hi - lo) * 0.382
        m2 = lo + (hi - lo) * 0.618
        try:
            d1 = curve.Evaluate(m1, True).DistanceTo(pt)
            d2 = curve.Evaluate(m2, True).DistanceTo(pt)
        except Exception:
            break
        if d1 < d2:
            hi = m2
        else:
            lo = m1

    try:
        return curve.Evaluate((lo + hi) * 0.5, True)
    except Exception:
        return None


def curve_closest_point(curve, pt):
    """曲线上距 pt 最近的点（限定在曲线范围之内）"""
    if isinstance(curve, Line):
        try:
            return _line_closest_point(curve, pt)
        except Exception:
            return None
    return _sample_closest_point(curve, pt)


def distance_to_curve(curve, pt):
    """点到曲线的最短距离，失败返回 None"""
    p = curve_closest_point(curve, pt)
    if p is None:
        return None
    try:
        return p.DistanceTo(pt)
    except Exception:
        return None


def curve_plane_hit(curve, plane_origin, plane_normal, overshoot=mm(50.0)):
    """
    曲线与平面的交点（限定在曲线范围内，允许 overshoot 长度的外延）。
    平行 / 共面时返回 None，由调用方回退到「最近点」策略。
    """
    if isinstance(curve, Line):
        try:
            p0 = curve.GetEndPoint(0)
            p1 = curve.GetEndPoint(1)
            d = p1.Subtract(p0)
            length = d.GetLength()
            if length < 1e-9:
                return None
            den = d.DotProduct(plane_normal)
            if abs(den) < 1e-12:
                return None
            t = plane_origin.Subtract(p0).DotProduct(plane_normal) / den
            tol = overshoot / length
            if t < -tol or t > 1.0 + tol:
                return None
            if t < 0.0:
                t = 0.0
            elif t > 1.0:
                t = 1.0
            return p0.Add(d.Multiply(t))
        except Exception:
            return None

    samples = 64
    prev_t = 0.0
    prev_s = None
    for i in range(samples + 1):
        t = float(i) / samples
        try:
            p = curve.Evaluate(t, True)
        except Exception:
            return None
        s = p.Subtract(plane_origin).DotProduct(plane_normal)
        if prev_s is not None:
            if abs(s) < 1e-9:
                return p
            if prev_s * s < 0.0:
                lo, hi = prev_t, t
                for _ in range(40):
                    mid = (lo + hi) * 0.5
                    try:
                        pm = curve.Evaluate(mid, True)
                    except Exception:
                        break
                    sm = pm.Subtract(plane_origin).DotProduct(plane_normal)
                    if prev_s * sm <= 0.0:
                        hi = mid
                    else:
                        lo = mid
                try:
                    return curve.Evaluate((lo + hi) * 0.5, True)
                except Exception:
                    return None
        prev_t = t
        prev_s = s
    return None


# ============================== 元素分类 ==============================


def _size_bip_names():
    return (
        "RBS_CURVE_WIDTH_PARAM",
        "RBS_CURVE_HEIGHT_PARAM",
        "RBS_CURVE_DIAMETER_PARAM",
        "RBS_PIPE_DIAMETER_PARAM",
        "RBS_PIPE_OUTER_DIAMETER",
        "RBS_CABLETRAY_WIDTH_PARAM",
        "RBS_CABLETRAY_HEIGHT_PARAM",
    )


def pipe_half_size(elem):
    """管线截面半尺寸（元素本地单位），用于放宽匹配容差"""
    vals = []
    for name in _size_bip_names():
        bip = getattr(BuiltInParameter, name, None)
        if bip is None:
            continue
        try:
            p = elem.get_Parameter(bip)
            if p is None:
                continue
            if p.StorageType != StorageType.Double:
                continue
            v = p.AsDouble()
            if v is not None and v > 0.0:
                vals.append(v)
        except Exception:
            continue
    if not vals:
        return mm(50.0)
    return max(vals) * 0.5


def get_host(elem):
    """取宿主（Opening.Host / FamilyInstance.Host），失败返回 None"""
    try:
        h = elem.Host
        if h is not None:
            return h
    except Exception:
        pass
    return None


def _name_has_opening_keyword(fi):
    """族名 / 类型名里是否带「预留洞」「套管」等关键字"""
    texts = []
    try:
        sym = fi.Symbol
        if sym is not None:
            try:
                texts.append(sym.Name)
            except Exception:
                pass
            try:
                if sym.Family is not None:
                    texts.append(sym.Family.Name)
            except Exception:
                pass
    except Exception:
        pass

    for t in texts:
        try:
            low = unicode(t).lower()
        except Exception:
            continue
        for kw in OPENING_NAME_KEYWORDS:
            if kw.lower() in low:
                return True
    return False


def _hosted_on_link(fi):
    """
    族是否贴在链接模型的面上（HostFace 指向链接元素）。
    FamilyInstance.Host 对链接宿主通常返回 null，只能靠这个判断。
    """
    try:
        hf = fi.HostFace
        if hf is None:
            return False
        try:
            lid = hf.LinkedElementId
        except Exception:
            lid = None
        if lid is not None and lid.IntegerValue >= 0:
            return True
    except Exception:
        pass
    return False


def is_opening_family_instance(fi):
    """
    判断一个 FamilyInstance 是否应被当成「洞口」：
      1. 族名 / 类型名带洞口关键字 -> 是（优先级最高）
      2. 类别在黑名单里 -> 否
      3. 宿主是墙 / 楼板 / 天花 -> 是
      4. 贴在链接模型的面上（HostFace 指向链接）-> 是
      5. 类别是常规模型 -> 是（后面再用坐标推断宿主）
    """
    try:
        if _name_has_opening_keyword(fi):
            return True

        cat = fi.Category
        if cat is None:
            return False
        cid = cat.Id.IntegerValue

        if cid in exclude_cat_ids():
            return False

        h = get_host(fi)
        if isinstance(h, (Wall, Floor, Ceiling)):
            return True

        if _hosted_on_link(fi):
            return True

        gm = _bic_int("OST_GenericModel")
        if gm is not None and gm == cid:
            return True

        return False
    except Exception:
        return False


def _refs_from_elements(elements):
    """把元素列表按“管线 / 宿主 / 洞口”分桶，并包装成 ref"""
    pipes = []
    hosts = []
    openings = []
    links = []

    for e in elements:
        try:
            if isinstance(e, RevitLinkInstance):
                links.append(e)
                continue

            if isinstance(e, MEPCurve):
                pipes.append(make_ref(e))
                continue
            if isinstance(e, (Wall, Floor, Ceiling)):
                hosts.append(make_ref(e))
                continue
            if isinstance(e, Opening):
                openings.append(make_ref(e))
                continue
            if isinstance(e, FamilyInstance):
                if is_opening_family_instance(e):
                    openings.append(make_ref(e))
                continue
        except Exception:
            continue

    return pipes, hosts, openings, links


# ============================== 链接模型扫描 ==============================


def _world_region(elements):
    """当前模型中所选元素的世界包围盒并集（外扩 LINK_SCAN_MARGIN）"""
    mins = []
    maxs = []
    for e in elements:
        try:
            bb = e.get_BoundingBox(None)
        except Exception:
            bb = None
        if bb is None:
            continue
        mins.append(bb.Min)
        maxs.append(bb.Max)

    if not mins:
        return None

    xs = [p.X for p in mins]
    ys = [p.Y for p in mins]
    zs = [p.Z for p in mins]
    Xs = [p.X for p in maxs]
    Ys = [p.Y for p in maxs]
    Zs = [p.Z for p in maxs]

    m = LINK_SCAN_MARGIN
    lo = XYZ(min(xs) - m, min(ys) - m, min(zs) - m)
    hi = XYZ(max(Xs) + m, max(Ys) + m, max(Zs) + m)
    return lo, hi


def _outline_in_link_local(region, xform):
    """把世界包围盒的 8 个角点逆变换到链接本地，构造 Outline"""
    try:
        inv = xform.Inverse
    except Exception:
        return None

    lo, hi = region
    pts = []
    for x in (lo.X, hi.X):
        for y in (lo.Y, hi.Y):
            for z in (lo.Z, hi.Z):
                try:
                    pts.append(inv.OfPoint(XYZ(x, y, z)))
                except Exception:
                    return None

    if not pts:
        return None

    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    zs = [p.Z for p in pts]
    try:
        return Outline(XYZ(min(xs), min(ys), min(zs)),
                       XYZ(max(xs), max(ys), max(zs)))
    except Exception:
        return None


def collect_linked_elements(link_instances, region_elements):
    """
    从选中的链接实例中，收集与「当前模型所选元素」包围盒相交范围内的：
      墙 / 楼板 / 天花 / 洞口 / 管线
    全部包装成带 GetTotalTransform() 的 ref。
    """
    hosts = []
    openings = []
    pipes = []

    if not link_instances:
        return hosts, openings, pipes

    region = _world_region(region_elements)
    if region is None:
        return hosts, openings, pipes

    for li in link_instances:
        try:
            ld = li.GetLinkDocument()
        except Exception:
            ld = None
        if ld is None:
            continue

        try:
            xf = li.GetTotalTransform()
        except Exception:
            xf = Transform.Identity

        outline = _outline_in_link_local(region, xf)
        if outline is None:
            continue

        try:
            col = FilteredElementCollector(ld)
            col = col.WherePasses(BoundingBoxIntersectsFilter(outline))
            col = col.WhereElementIsNotElementType()
        except Exception:
            continue

        for e in col:
            try:
                if isinstance(e, MEPCurve):
                    pipes.append(make_ref(e, forced_link=li, forced_xform=xf))
                elif isinstance(e, (Wall, Floor, Ceiling)):
                    hosts.append(make_ref(e, forced_link=li, forced_xform=xf))
                elif isinstance(e, Opening):
                    openings.append(make_ref(e, forced_link=li, forced_xform=xf))
                elif isinstance(e, FamilyInstance) and is_opening_family_instance(e):
                    openings.append(make_ref(e, forced_link=li, forced_xform=xf))
            except Exception:
                continue

    return hosts, openings, pipes


# ============================== 数据准备 ==============================


def collect_pipe_data(pipe_refs):
    """收集管线的世界坐标定位曲线与截面尺寸"""
    data = []
    for ref in pipe_refs:
        try:
            elem = ref["elem"]
            loc = elem.Location
            if not isinstance(loc, LocationCurve):
                continue
            curve = loc.Curve
            if curve is None:
                continue

            # 链接管线：把曲线整体变换到当前文档世界坐标
            curve_world = curve
            if ref["linked"]:
                try:
                    curve_world = curve.CreateTransformed(ref["xform"])
                except Exception:
                    continue
            if curve_world is None:
                continue

            half = pipe_half_size(elem)
            if ref["linked"]:
                try:
                    s = ref["xform"].Scale
                    if s is not None and s > 0.0:
                        half = half * s
                except Exception:
                    pass

            data.append({
                "ref": ref,
                "elem": elem,
                "curve": curve_world,
                "half": half,
            })
        except Exception:
            continue
    return data


def find_host_by_center_world(center_world, host_refs):
    """在候选宿主里，按世界包围盒包含关系 + 最近距离推断宿主"""
    best = None
    best_score = None

    for href in host_refs:
        if not point_in_world_bbox(center_world, href):
            continue
        c = ref_center_world(href)
        try:
            score = c.DistanceTo(center_world) if c is not None else 0.0
        except Exception:
            score = 0.0
        if best_score is None or score < best_score:
            best_score = score
            best = href

    return best


def resolve_host(opening_ref, host_refs, center_world):
    """解析洞口的宿主，返回 host_ref（世界坐标变换已就绪）"""
    h = get_host(opening_ref["elem"])
    if isinstance(h, (Wall, Floor, Ceiling)):
        # 宿主与洞口是否属于同一个文档（同一链接内 / 同一活动文档内）
        same_doc = False
        try:
            hdoc = h.Document
            odoc = opening_ref["doc"]
            if hdoc is not None and odoc is not None:
                same_doc = hdoc.Equals(odoc)
        except Exception:
            same_doc = False

        if same_doc:
            return make_ref(h,
                            forced_link=opening_ref["link"],
                            forced_xform=opening_ref["xform"])
        # 宿主在另一个文档（例如族洞口贴在链接墙面上）
        return make_ref(h)

    return find_host_by_center_world(center_world, host_refs)


def host_plane_world(host_ref, center_world):
    """
    返回宿主平面 (原点, 单位法向)，世界坐标：
      · 墙：定位线起点 + Orientation，均经 GetTotalTransform 变换
      · 楼板 / 天花：过洞口世界中心，法向 = 宿主本地 Z 经链接变换
    """
    xf = host_ref["xform"]
    elem = host_ref["elem"]

    if isinstance(elem, Wall):
        origin_local = None
        try:
            loc = elem.Location
            if isinstance(loc, LocationCurve) and loc.Curve is not None:
                origin_local = loc.Curve.GetEndPoint(0)
        except Exception:
            origin_local = None

        if origin_local is not None:
            try:
                origin = xf.OfPoint(origin_local)
            except Exception:
                origin = center_world
        else:
            origin = center_world

        n_local = None
        try:
            n_local = elem.Orientation
        except Exception:
            n_local = None

        if n_local is not None:
            try:
                n = _norm(xf.OfVector(n_local))
            except Exception:
                n = None
            if n is not None:
                return origin, n

        return origin, XYZ.BasisZ

    # 楼板 / 天花：平面过洞口世界中心，法向为宿主本地 Z 变换后的方向
    n = None
    try:
        n = _norm(xf.OfVector(XYZ.BasisZ))
    except Exception:
        n = None
    if n is None:
        n = XYZ.BasisZ
    return center_world, n


# ============================== 位移计算 ==============================


def _coord_note(opening_ref, host_ref, center_world):
    """坐标合理性校验（用于报告）"""
    issues = []

    tn = transform_note(opening_ref["xform"])
    if tn:
        issues.append(tn)

    if not point_in_world_bbox(center_world, host_ref):
        issues.append(u"洞口中心不在宿主世界包围盒内")

    # 洞口中心的世界坐标绝对值过大，通常意味着变换搞错了
    try:
        if center_world.GetLength() > mm(1000000.0):  # 1km
            issues.append(u"世界坐标异常偏大")
    except Exception:
        pass

    return u"; ".join(issues) if issues else u"OK"


def compute_move(opening_ref, pipe_data, host_refs):
    """
    计算某个洞口的位移。

    返回 (plan, reason)
      · 成功：plan 为 dict，reason 为 None
      · 跳过：plan 为 None，reason 为原因文本

    plan 字段：
      opening_ref / pipe_ref / host_ref
      delta_world   —— 当前文档世界坐标下的位移（用于移动当前模型元素）
      delta_local   —— 洞口所在文档本地坐标下的位移（链接模型时 = Inverse.OfVector）
      dist_world    —— 世界坐标下的位移长度
      center_local  —— 洞口中心（本地）
      center_world  —— 洞口中心（世界）
      sanity        —— 坐标合理性说明
    """
    elem = opening_ref["elem"]

    center_local = element_center(elem)
    if center_local is None:
        return None, u"无法获取洞口中心点"

    try:
        center_world = opening_ref["xform"].OfPoint(center_local)
    except Exception:
        return None, u"洞口坐标变换失败"

    host_ref = resolve_host(opening_ref, host_refs, center_world)
    if host_ref is None:
        return None, u"找不到宿主墙 / 楼板"

    origin_w, normal_w = host_plane_world(host_ref, center_world)
    if normal_w is None:
        return None, u"宿主平面法向异常"

    # ---- 1. 匹配管线（世界坐标）----
    # 排序优先级：是否穿过宿主 > 当前模型优先于链接 > 距离近
    candidates = []
    for idx, pd in enumerate(pipe_data):
        d = distance_to_curve(pd["curve"], center_world)
        if d is None:
            continue

        tol = MAX_MATCH_DIST
        if USE_SIZE_TOLERANCE:
            tol = MAX_MATCH_DIST + pd["half"]
        if d > tol:
            continue

        rank = 1
        try:
            hit0 = curve_plane_hit(pd["curve"], origin_w, normal_w)
            if hit0 is not None and point_in_world_bbox(hit0, host_ref):
                rank = 0
        except Exception:
            pass

        src_rank = 1 if pd["ref"]["linked"] else 0
        candidates.append((rank, src_rank, d, idx, pd))

    if not candidates:
        return None, u"附近没有匹配的管线"

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    best = candidates[0][4]

    # ---- 2. 目标点：管线轴线 ∩ 宿主平面（世界坐标）----
    hit = curve_plane_hit(best["curve"], origin_w, normal_w)
    if hit is None:
        hit = curve_closest_point(best["curve"], center_world)
    if hit is None:
        return None, u"无法计算管线与宿主平面的交点"

    # ---- 3. 位移只保留宿主平面内的分量 ----
    delta_world = hit.Subtract(center_world)
    dn = delta_world.DotProduct(normal_w)
    delta_world = delta_world.Subtract(normal_w.Multiply(dn))

    dist = delta_world.GetLength()
    if dist < MIN_MOVE:
        return None, u"已对齐"
    if dist > MAX_MOVE_DIST:
        return None, u"位移过大(%.0fmm)，已跳过" % to_mm(dist)

    # ---- 4. 链接模型：位移换算回链接本地坐标 ----
    delta_local = delta_world
    if opening_ref["linked"]:
        try:
            delta_local = opening_ref["xform"].Inverse.OfVector(delta_world)
        except Exception:
            delta_local = delta_world

    plan = {
        "opening_ref": opening_ref,
        "pipe_ref": best["ref"],
        "host_ref": host_ref,
        "delta_world": delta_world,
        "delta_local": delta_local,
        "dist_world": dist,
        "center_local": center_local,
        "center_world": center_world,
        "sanity": _coord_note(opening_ref, host_ref, center_world),
        "linked": opening_ref["linked"],
        "source": opening_ref["source"],
    }
    return plan, None


# ============================== 事务执行 ==============================


class _SwallowWarnings(IFailuresPreprocessor):
    """只删警告，不让弹窗打断；错误仍然照常抛出"""

    def PreprocessFailures(self, failuresAccessor):
        try:
            for msg in failuresAccessor.GetFailureMessages():
                try:
                    if msg.GetSeverity() == FailureSeverity.Warning:
                        failuresAccessor.DeleteWarning(msg)
                except Exception:
                    pass
        except Exception:
            pass
        return FailureProcessingResult.Continue


def _elem_is_active(opening_ref):
    """元素是否属于当前活动文档（只有它才能被 MoveElement 修改）"""
    try:
        d = opening_ref["doc"]
        if d is None:
            return False
        return d.Equals(active_doc)
    except Exception:
        return not opening_ref["linked"]


def apply_plan(plans):
    """
    在 Transaction + 每个洞口一个 SubTransaction 中执行移动。
    返回 (moved, results, linked_blocked, fatal)
    results: [dict(plan, status=..., applied=bool)]
    """
    results = []
    moved = 0
    linked_blocked = 0

    if not plans:
        return moved, results, linked_blocked, None

    if DRY_RUN:
        for plan in plans:
            results.append(dict(plan, status=u"试运行（未移动）", applied=False))
        return 0, results, 0, None

    t = Transaction(active_doc, u"同步机电洞口位置")
    t.Start()

    try:
        opts = t.GetFailureHandlingOptions()
        opts.SetFailuresPreprocessor(_SwallowWarnings())
        opts.SetClearAfterRollback(True)
        opts.SetForcedModalHandling(False)
        t.SetFailureHandlingOptions(opts)
    except Exception:
        pass

    fatal = None
    committed = False

    try:
        for plan in plans:
            ref = plan["opening_ref"]
            elem = ref["elem"]

            # ---- 链接模型中的洞口：API 不允许跨文档修改 ----
            if not _elem_is_active(ref):
                status = u"位于链接模型，无法由当前文档移动"
                applied = False

                if ALLOW_LINKED_MOVE_ATTEMPT:
                    st = SubTransaction(active_doc)
                    st.Start()
                    try:
                        # 按需求：用 Inverse.OfVector 算出的本地位移执行
                        ElementTransformUtils.MoveElement(
                            ref["doc"], elem.Id, plan["delta_local"])
                        st.Commit()
                        status = u"在链接文档中移动成功"
                        applied = True
                        moved += 1
                    except Exception as ex:
                        try:
                            st.RollBack()
                        except Exception:
                            pass
                        status = u"链接模型不可修改: %s" % _short(ex)

                if not applied:
                    linked_blocked += 1

                results.append(dict(plan, status=status, applied=applied))
                continue

            # ---- 当前模型中的洞口 ----
            if elem.Pinned:
                results.append(dict(plan, status=u"已锁定(Pinned)，跳过", applied=False))
                continue

            st = SubTransaction(active_doc)
            st.Start()
            try:
                ElementTransformUtils.MoveElement(
                    active_doc, elem.Id, plan["delta_world"])
                st.Commit()
                moved += 1
                results.append(dict(plan, status=u"成功", applied=True))
            except Exception as ex:
                try:
                    st.RollBack()
                except Exception:
                    pass
                if isinstance(elem, Opening):
                    results.append(dict(
                        plan,
                        status=u"失败（原生 Opening，可能是草图洞口）: %s" % _short(ex),
                        applied=False))
                else:
                    results.append(dict(
                        plan, status=u"失败: %s" % _short(ex), applied=False))

        try:
            active_doc.Regenerate()
        except Exception:
            pass

        t.Commit()
        committed = True

    except Exception as ex:
        fatal = _short(ex)
        try:
            if t.GetStatus() == TransactionStatus.Started:
                t.RollBack()
        except Exception:
            pass

    if not committed:
        moved = 0
        fixed = []
        for plan in results:
            if plan.get("status") == u"成功":
                plan = dict(plan, status=u"已回滚（事务提交失败）", applied=False)
            fixed.append(plan)
        results = fixed

    return moved, results, linked_blocked, fatal


# ============================== 主流程 ==============================


def get_selected_elements():
    """先取当前选择；没有选择则让用户框选"""
    elements = []

    try:
        ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        ids = []

    for eid in ids:
        try:
            e = active_doc.GetElement(eid)
            if e is not None:
                elements.append(e)
        except Exception:
            continue

    if elements:
        return elements

    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            u"请选择：机电管线 + 墙/楼板 + 洞口。"
            u"若墙/板/洞口在链接模型里，请再单击选中该「链接模型」实例。")
    except Exception:
        return []

    for r in refs:
        try:
            # 链接元素被 Pick 时，ElementId 通常是链接实例的 id
            e = active_doc.GetElement(r.ElementId)
            if e is not None:
                elements.append(e)
        except Exception:
            continue

    return elements


def _fmt_xyz(pt):
    try:
        return u"(%.0f, %.0f, %.0f)" % (to_mm(pt.X), to_mm(pt.Y), to_mm(pt.Z))
    except Exception:
        return u"-"


def main():
    global _LINKS

    if active_doc is None:
        forms.alert(u"当前没有打开的项目文档。", title=__title__)
        return

    _LINKS = build_link_map()

    elements = get_selected_elements()
    if not elements:
        forms.alert(u"未选择任何元素，脚本已退出。", title=__title__)
        return

    pipes, hosts, openings, link_instances = _refs_from_elements(elements)

    # ---------- 从链接模型补充元素 ----------
    if link_instances:
        region_elems = [r["elem"] for r in (pipes + hosts + openings)]
        if not region_elems:
            forms.alert(
                u"只选择了链接模型，没有选择当前模型里的任何元素。\n\n"
                u"请同时选中：当前模型中的管线 / 洞口，以及链接模型实例，\n"
                u"脚本才能确定要扫描的链接范围。",
                title=__title__)
            return

        linked_hosts, linked_openings, linked_pipes = collect_linked_elements(
            link_instances, region_elems)
        hosts.extend(linked_hosts)
        openings.extend(linked_openings)
        pipes.extend(linked_pipes)

    if not pipes:
        forms.alert(
            u"没有可用的管线（风管 / 水管 / 桥架）。\n\n"
            u"提示：管件、阀门属于 FamilyInstance，不会被识别为管线。\n"
            u"若管线在链接模型里，请把链接模型实例也框选进来。",
            title=__title__)
        return

    if not openings:
        forms.alert(
            u"没有识别到洞口。\n\n"
            u"可识别的洞口包括：\n"
            u"  · 原生 Opening（墙洞口 / 楼板洞口 / 竖井）\n"
            u"  · 宿主于墙、楼板、天花的 FamilyInstance（门窗等类别已排除）\n"
            u"  · 族名带「预留 / 洞口 / 开洞 / 套管」的族\n"
            u"若洞口在链接模型里，请把链接模型实例也框选进来。",
            title=__title__)
        return

    pipe_data = collect_pipe_data(pipes)
    if not pipe_data:
        forms.alert(u"管线没有可用的定位曲线（LocationCurve）。", title=__title__)
        return

    # ---------- 只读阶段：计算所有位移 ----------
    plans = []          # [plan]
    skipped = []        # [(opening_ref, reason)]
    for op in openings:
        plan, reason = compute_move(op, pipe_data, hosts)
        if plan is not None:
            plans.append(plan)
        else:
            skipped.append((op, reason))

    # ---------- 写入阶段 ----------
    moved, results, linked_blocked, fatal = apply_plan(plans)

    # ---------- 统计 ----------
    pipe_cur = len([r for r in pipes if not r["linked"]])
    pipe_lnk = len(pipes) - pipe_cur
    op_cur = len([r for r in openings if not r["linked"]])
    op_lnk = len(openings) - op_cur

    # ---------- 报告 ----------
    md = []
    md.append(u"### 同步结果")
    md.append(u"")
    md.append(u"- 选中管线：**{0}** 段（当前模型 {1} / 链接 {2}）".format(
        len(pipes), pipe_cur, pipe_lnk))
    md.append(u"- 识别洞口：**{0}** 个（当前模型 {1} / 链接 {2}）".format(
        len(openings), op_cur, op_lnk))
    md.append(u"- 计划移动：**{0}** 个".format(len(plans)))
    md.append(u"- 实际成功：**{0}** 个".format(moved))
    if linked_blocked:
        md.append(u"- 因位于链接模型而未移动：**{0}** 个".format(linked_blocked))
    md.append(u"- 跳过：**{0}** 个".format(len(skipped)))
    if fatal:
        md.append(u"- **事务提交失败：{0}**".format(fatal))
    md.append(u"")

    if results:
        md.append(u"| 洞口ID | 来源 | 类型 | 匹配管线 | 位移(mm) | 坐标校验 | 结果 |")
        md.append(u"|---|---|---|---|---|---|---|")
        for plan in results:
            opening = plan["opening_ref"]["elem"]
            dist_mm = to_mm(plan["dist_world"])
            flag = u" ⚠️" if dist_mm > WARN_DIST_MM else u""
            try:
                op_type = u"Opening" if isinstance(opening, Opening) else u"族洞口"
            except Exception:
                op_type = u"?"
            md.append(u"| {0} | {1} | {2} | {3} | {4:.1f}{5} | {6} | {7} |".format(
                opening.Id.IntegerValue,
                plan["source"],
                op_type,
                plan["pipe_ref"]["elem"].Id.IntegerValue,
                dist_mm, flag,
                plan["sanity"],
                plan["status"]))
        md.append(u"")

    # ---------- 链接模型坐标校验明细 ----------
    linked_plans = [p for p in results if p.get("linked")]
    if linked_plans:
        md.append(u"#### 链接模型坐标校验")
        md.append(u"")
        md.append(u"| 洞口ID | 链接 | 本地坐标(mm) | 世界坐标(mm) | 本地位移(mm) | 校验 |")
        md.append(u"|---|---|---|---|---|---|")
        for plan in linked_plans:
            opening = plan["opening_ref"]["elem"]
            md.append(u"| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                opening.Id.IntegerValue,
                plan["source"],
                _fmt_xyz(plan["center_local"]),
                _fmt_xyz(plan["center_world"]),
                _fmt_xyz(plan["delta_local"]),
                plan["sanity"]))
        md.append(u"")
        md.append(u"> 本地位移 = `GetTotalTransform().Inverse.OfVector(世界位移)`，"
                  u"可直接在链接模型中按此值移动洞口。")
        md.append(u"")

    if skipped:
        md.append(u"#### 跳过的洞口")
        md.append(u"")
        md.append(u"| 洞口ID | 来源 | 原因 |")
        md.append(u"|---|---|---|")
        for opening_ref, reason in skipped:
            md.append(u"| {0} | {1} | {2} |".format(
                opening_ref["elem"].Id.IntegerValue,
                opening_ref["source"], reason))
        md.append(u"")

    output.print_md(u"\n".join(md))

    # ---------- 弹窗 ----------
    summary = (
        u"选中管线：{0} 段（当前 {1} / 链接 {2}）\n"
        u"识别洞口：{3} 个（当前 {4} / 链接 {5}）\n"
        u"计划移动：{6} 个\n"
        u"成功对齐：{7} 个\n"
        u"跳过：{8} 个"
    ).format(
        len(pipes), pipe_cur, pipe_lnk,
        len(openings), op_cur, op_lnk,
        len(plans), moved, len(skipped))

    if linked_blocked:
        summary += (
            u"\n\n⚠️ 有 {0} 个洞口位于链接模型。\n"
            u"Revit API 不允许从当前文档修改链接模型中的元素，\n"
            u"因此这些洞口只计算了位移，没有移动。\n"
            u"可在输出窗口的「链接模型坐标校验」表中查看对应的本地位移，\n"
            u"或改用以下方式：在土建模型中运行本脚本（把机电模型作为链接载入）。"
        ).format(linked_blocked)

    if fatal:
        summary += u"\n\n⚠️ 事务提交失败，所有修改已回滚。"

    if DRY_RUN:
        summary += u"\n\n（当前为试运行，未实际移动任何洞口）"

    if moved > 0:
        summary += u"\n\n提示：位移超过 {0:.0f}mm 的洞口已标 ⚠️，建议人工复核。".format(
            WARN_DIST_MM)

    forms.alert(summary, title=u"同步洞口 - 完成",
                warn_icon=bool(fatal or skipped or linked_blocked))


# pyRevit 直接 exec 本文件，这里直接执行；不依赖 __name__ == "__main__"
try:
    main()
except Exception:
    err = traceback.format_exc()
    try:
        output.print_md(u"```\n{0}\n```".format(err))
    except Exception:
        pass
    last = err.strip().splitlines()[-1] if err.strip() else u"未知错误"
    forms.alert(u"脚本执行出错，详情见输出窗口。\n\n{0}".format(last),
                title=u"同步洞口 - 错误", warn_icon=True)

