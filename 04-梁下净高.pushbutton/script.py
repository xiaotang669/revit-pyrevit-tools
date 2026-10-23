# -*- coding: utf-8 -*-
# ============================================================
#  Author  : xiaotang669
#  GitHub  : https://github.com/xiaotang669/revit-pyrevit-tools
#  Bilibili: https://space.bilibili.com/1562942194
#  本项目开源免费。如果你是在其他地方付费购买的，说明你被骗了。
# ============================================================
"""
===============================================================================
 梁下净高查看 / 标注工具（支持链接模型）
 Revit 2020 + pyRevit + IronPython 2.7
-------------------------------------------------------------------------------
 适用场景：
   机电模型（当前文档）里工作，梁和楼板都在「土建链接模型」中。
   本工具会读取链接模型里的梁，把它们的包围盒通过 RevitLinkInstance 的
   总变换矩阵换算到当前机电模型的世界坐标，再计算净高；文字始终生成在
   当前机电模型的当前视图里。

 使用流程：
   1. 在【平面视图】中运行本按钮（可以先选中当前模型里的梁）；
   2. 如果当前选择集中没有梁，会提示你到【链接模型】里点选结构梁，
      可连续点选，按 Enter 结束；
   3. 弹窗选择：
        A. 仅弹窗查看结果
        B. 生成/更新文字标注

 计算优先级（在"当前模型世界坐标"下进行）：
   ┌ 1. 梁底绝对 Z = 梁包围盒（已变换到宿主坐标）的 Min.Z
   │
   ├ 2. 参考面（地面）标高，兜底链：
   │     a) 梁正下方的楼板 —— 当前模型 + 所有链接模型的楼板统一参与匹配，
   │        取板顶 Z 最高（离梁底最近）的那块；
   │     b) 找不到楼板 → doc.ActiveView.GenLevel.Elevation；
   │     c) GenLevel 为空 → 梁底以下最近的 Level。
   │
   ├ 3. 吊顶：同样跨模型搜索，取 BoundingBox.Min.Z；找不到就忽略"距顶"。
   │
   └ 4. 距地净高 = 梁底 Z - 参考面 Z
         距顶净高 = 梁底 Z - 吊顶底面 Z

 坐标转换：
   链接图元的包围盒在链接文档坐标系里，使用
       transform = link_instance.GetTotalTransform()
   把包围盒 8 个角点逐个变换到宿主坐标后再重建轴对齐包围盒
   （比直接改 BoundingBoxXYZ.Transform 更稳，不受变换顺序影响）。

 防重复 / 更新已有标注（模式 B）：
   收集当前视图里内容含"净高"或"距地"的 TextNote，与每根梁的期望插入点
   做全局就近配对（MATCH_DIST_MM），配对成功就改 Text，否则新建。
   反复运行不会堆叠重复文字，梁高变了重跑即可刷新。

 典型场景校验：
     楼层标高 4.0m，梁顶 8.0m，梁高 800mm
     → 梁底 = 8.0 - 0.8 = 7.2m
     → 距地净高 = 7.2 - 4.0 = 3.2m = 3200mm   ✔ 输出"净高: 3200"
===============================================================================
"""

__title__ = u"梁净高"
__doc__ = u"查看梁净高（支持土建链接模型），可生成/更新文字标注。"
__author__ = u"Cherry Assistant"
# 只有选中元素后按钮才可用；若希望任何时候都能点，删掉下面这一行
__context__ = "Selection"

import math
import traceback

from pyrevit import forms, script, revit, DB


# =============================================================================
# 零、枚举寻址（Revit 2020 的命名空间兼容）
# -----------------------------------------------------------------------------
# StructuralType 位于 Autodesk.Revit.DB.Structure，
# ObjectType   位于 Autodesk.Revit.UI.Selection。
# 个别 IronPython 环境下属性直取会抛 AttributeError，这里统一做兼容解析。
# =============================================================================

try:
    from Autodesk.Revit.DB.Structure import StructuralType as StEnum
except Exception:
    try:
        StEnum = DB.Structure.StructuralType
    except Exception:
        StEnum = None

try:
    from Autodesk.Revit.UI.Selection import ObjectType as SelObjectType
except Exception:
    try:
        from pyrevit import UI
        SelObjectType = UI.Selection.ObjectType
    except Exception:
        SelObjectType = None


# =============================================================================
# 一、可调参数
# =============================================================================

# 是否搜索链接模型里的楼板 / 吊顶（机电模型必备，默认开启）
INCLUDE_LINKED_SURFACES = True

# Z 方向容差（mm）：楼板/吊顶标高只要不高于"梁底 + 容差"，就认为在梁下方。
Z_TOL_MM = 20.0

# 连参考面都找不到时（既无楼板、又无视图标高）是否跳过该梁。
SKIP_IF_NO_REFERENCE = False

# ---------------- 防重复 / 更新已有标注 ----------------
UPDATE_EXISTING_TEXT = True
MATCH_DIST_MM = 1000.0
NOTE_KEYWORDS = (u"净高", u"距地")

# ---------------- 文字 Z 位置 ----------------
TEXT_Z_MODE = "VIEW_LEVEL"          # "VIEW_LEVEL" | "BELOW_BEAM"
TEXT_Z_BELOW_BEAM_MM = 100.0
TEXT_OFFSET_DOWN_MM = 0.0
TEXT_MAX_WIDTH_MM = 2400.0

# 是否创建固定字高的文字类型（False = 用当前视图的默认文字类型）
USE_CUSTOM_TEXT_TYPE = False
CUSTOM_TEXT_TYPE_NAME = u"净高标注 3.0mm"
CUSTOM_TEXT_SIZE_MM = 3.0


# =============================================================================
# 二、单位换算 / 通用小工具
# =============================================================================

def mm_to_ft(value_mm):
    """毫米 -> 内部单位（英尺）"""
    return DB.UnitUtils.ConvertToInternalUnits(
        value_mm, DB.DisplayUnitType.DUT_MILLIMETERS)


def ft_to_mm(value_ft):
    """内部单位（英尺）-> 毫米"""
    if value_ft is None:
        return None
    return DB.UnitUtils.ConvertFromInternalUnits(
        value_ft, DB.DisplayUnitType.DUT_MILLIMETERS)


def fmt_mm(value_mm):
    """格式化毫米数值，保留 0 位小数"""
    if value_mm is None:
        return u"未知"
    return u"{0:.0f}".format(value_mm)


def safe_name(element):
    """尽量安全地取元素名称（个别元素取 Name 会抛异常）"""
    try:
        name = element.Name
        if name:
            return name
    except Exception:
        pass
    try:
        type_id = element.GetTypeId()
        if type_id and type_id != DB.ElementId.InvalidElementId:
            etype = element.Document.GetElement(type_id)
            if etype is not None and etype.Name:
                return etype.Name
    except Exception:
        pass
    try:
        return u"ID:{0}".format(element.Id.IntegerValue)
    except Exception:
        return u"未知元素"


def bbox_center_xy(bbox):
    """包围盒 XY 中心点"""
    return ((bbox.Min.X + bbox.Max.X) / 2.0,
            (bbox.Min.Y + bbox.Max.Y) / 2.0)


def xy_overlap_area(bb_a, bb_b):
    """两个包围盒在 XY 平面上的重叠面积（平方英尺）"""
    try:
        dx = min(bb_a.Max.X, bb_b.Max.X) - max(bb_a.Min.X, bb_b.Min.X)
        dy = min(bb_a.Max.Y, bb_b.Max.Y) - max(bb_a.Min.Y, bb_b.Min.Y)
        if dx <= 0 or dy <= 0:
            return 0.0
        return dx * dy
    except Exception:
        return 0.0


def xy_contains(bbox, x, y, eps=1e-6):
    """点 (x, y) 是否落在包围盒的 XY 范围内（含边界）"""
    try:
        return (bbox.Min.X - eps <= x <= bbox.Max.X + eps
                and bbox.Min.Y - eps <= y <= bbox.Max.Y + eps)
    except Exception:
        return False


def xy_distance(point_a, point_b):
    """两个点之间的水平距离（英尺，忽略 Z）"""
    dx = point_a.X - point_b.X
    dy = point_a.Y - point_b.Y
    return math.sqrt(dx * dx + dy * dy)


# =============================================================================
# 三、链接模型：坐标变换 / 包围盒换算
# =============================================================================

def transform_bbox(bbox, transform):
    """
    把包围盒变换到宿主坐标，并重建一个轴对齐包围盒。

    做法：取 8 个角点，先应用包围盒自身的 Transform（通常是单位阵），
    再应用链接的总变换，最后在宿主坐标里取 min/max。
    这样不会受 Transform 相乘顺序的影响，斜向/旋转链接也能正确处理。
    """
    if bbox is None or transform is None:
        return bbox

    corners = []
    for x in (bbox.Min.X, bbox.Max.X):
        for y in (bbox.Min.Y, bbox.Max.Y):
            for z in (bbox.Min.Z, bbox.Max.Z):
                point = DB.XYZ(x, y, z)
                # 包围盒自身变换（绝大多数情况是单位阵）
                try:
                    point = bbox.Transform.OfPoint(point)
                except Exception:
                    pass
                # 链接文档 -> 宿主文档
                try:
                    point = transform.OfPoint(point)
                except Exception:
                    pass
                corners.append(point)

    if not corners:
        return bbox

    min_x = min(p.X for p in corners)
    min_y = min(p.Y for p in corners)
    min_z = min(p.Z for p in corners)
    max_x = max(p.X for p in corners)
    max_y = max(p.Y for p in corners)
    max_z = max(p.Z for p in corners)

    new_bbox = DB.BoundingBoxXYZ()
    try:
        new_bbox.Transform = DB.Transform.Identity
    except Exception:
        pass
    new_bbox.Min = DB.XYZ(min_x, min_y, min_z)
    new_bbox.Max = DB.XYZ(max_x, max_y, max_z)
    return new_bbox


def make_beam_info(element, transform, is_linked, source_name, link_id):
    """
    把"一根梁"打包成统一的信息字典。
      transform  —— 链接的总变换；当前模型的梁传 None
      source_name—— u"本模型" 或链接名称（显示在结果表里）
      uid        —— 去重用的唯一键（同一个链接实例 + 同一个图元）
    """
    info = {
        "element": element,
        "transform": transform,
        "is_linked": is_linked,
        "source_name": source_name,
        "link_id": link_id,
        "uid": u"?",
        "element_id": -1,
    }
    try:
        info["element_id"] = element.Id.IntegerValue
    except Exception:
        pass
    try:
        prefix = u"H" if link_id is None else u"L{0}".format(link_id)
        info["uid"] = u"{0}:{1}".format(prefix, info["element_id"])
    except Exception:
        pass
    return info


def get_world_bbox(beam_info):
    """
    取梁在【宿主世界坐标】下的包围盒。
    当前模型的梁直接用；链接里的梁要过一遍 GetTotalTransform()。
    """
    try:
        bbox = beam_info["element"].get_BoundingBox(None)
    except Exception:
        return None
    if bbox is None:
        return None

    transform = beam_info.get("transform")
    if transform is None:
        return bbox

    try:
        if transform.IsIdentity:
            return bbox
    except Exception:
        pass

    return transform_bbox(bbox, transform)


def collect_link_sources(doc):
    """
    收集当前文档里所有"已载入"的链接模型。
    未载入的链接 GetLinkDocument() 返回 None，直接跳过。
    """
    links = []
    try:
        instances = (DB.FilteredElementCollector(doc)
                     .OfClass(DB.RevitLinkInstance)
                     .ToElements())
    except Exception:
        return links

    for instance in instances:
        try:
            link_doc = instance.GetLinkDocument()
            if link_doc is None:
                continue

            # GetTotalTransform 包含链接自身的变换与共享坐标偏移，
            # 比 GetTransform 更适合跨模型取几何。
            try:
                transform = instance.GetTotalTransform()
            except Exception:
                transform = instance.GetTransform()

            try:
                name = link_doc.Title
            except Exception:
                name = safe_name(instance)

            links.append({
                "instance": instance,
                "id": instance.Id.IntegerValue,
                "doc": link_doc,
                "transform": transform,
                "name": name,
            })
        except Exception:
            continue
    return links


# =============================================================================
# 四、交互拾取链接模型中的梁
# =============================================================================

def is_cancel_exception(ex):
    """判断是不是用户按 Esc 取消（避免把取消当成错误）"""
    try:
        return type(ex).__name__ == "OperationCanceledException"
    except Exception:
        return False


def pick_linked_refs(uidoc):
    """
    让用户在链接模型里点选图元。

    说明：Revit 不支持对链接模型内部图元做窗口框选（框选只会选中整个链接
    实例），所以这里用 PickObjects / PickObject 的 LinkedElement 模式多选。

    返回 Reference 列表；用户取消时返回空列表。
    """
    if SelObjectType is None:
        return []

    message = (u"请在【链接模型】中点选结构梁（可连续点选，按 Enter 结束）。\n"
               u"若要同时处理当前模型中的梁，请退出后先选中它们再运行。")

    # ---- 方式 1：多选（ObjectType, message）----
    try:
        refs = uidoc.Selection.PickObjects(SelObjectType.LinkedElement, message)
        return list(refs)
    except Exception as ex:
        if is_cancel_exception(ex):
            return []

    # ---- 方式 2：多选（ObjectType, filter, message），过滤器传 None ----
    try:
        refs = uidoc.Selection.PickObjects(SelObjectType.LinkedElement, None, message)
        return list(refs)
    except Exception as ex:
        if is_cancel_exception(ex):
            return []

    # ---- 方式 3：单选循环，直到用户按 Esc ----
    refs = []
    while True:
        try:
            ref = uidoc.Selection.PickObject(SelObjectType.LinkedElement, message)
            if ref is not None:
                refs.append(ref)
        except Exception:
            break
    return refs


def beam_info_from_reference(reference, link_cache):
    """
    把"链接里拾取到的 Reference"解析成 beam_info。
    Reference.ElementId 是宿主中的链接实例 ID，
    Reference.LinkedElementId 才是链接文档内部的图元 ID。
    """
    try:
        link_id = reference.ElementId.IntegerValue
    except Exception:
        return None

    link = link_cache.get(link_id)
    if link is None:
        return None     # 链接未载入，或不是链接图元

    try:
        linked_id = reference.LinkedElementId
    except Exception:
        return None

    if linked_id is None or linked_id == DB.ElementId.InvalidElementId:
        return None

    try:
        element = link["doc"].GetElement(linked_id)
    except Exception:
        return None

    if not is_structural_beam(element):
        return None

    return make_beam_info(element, link["transform"], True, link["name"], link_id)


# =============================================================================
# 五、标高相关
# =============================================================================

def get_view_level(view):
    """取当前视图的关联标高（ViewPlan.GenLevel）；非楼层平面可能返回 None"""
    try:
        level = view.GenLevel
        if level is not None:
            return level
    except Exception:
        pass
    return None


def collect_levels(doc):
    """收集宿主文档里所有标高，按 Elevation 升序排列"""
    levels = []
    try:
        for level in DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements():
            levels.append(level)
    except Exception:
        return []
    levels.sort(key=lambda l: l.Elevation)
    return levels


def nearest_level_below(levels, z):
    """仅当视图没有 GenLevel 时使用：取梁底以下、离梁底最近的标高"""
    best = None
    for level in levels:
        try:
            if level.Elevation <= z + mm_to_ft(Z_TOL_MM):
                if best is None or level.Elevation > best.Elevation:
                    best = level
        except Exception:
            continue
    return best


# =============================================================================
# 六、收集楼板 / 吊顶（当前模型 + 所有链接模型）
# =============================================================================

def _collect_surfaces_in_doc(source_doc, transform, builtin_category, use_top_z,
                             source_name):
    """
    在一个文档里收集指定类别的包围盒。
      transform 为 None 表示当前模型；否则把包围盒换算到宿主坐标。
      use_top_z=True  -> 取 Max.Z（楼板顶）
      use_top_z=False -> 取 Min.Z（吊顶完成面）
    """
    surfaces = []
    try:
        elements = (DB.FilteredElementCollector(source_doc)
                    .OfCategory(builtin_category)
                    .WhereElementIsNotElementType()
                    .ToElements())
    except Exception:
        return surfaces

    for elem in elements:
        try:
            bbox = elem.get_BoundingBox(None)
            if bbox is None:
                continue
            if transform is not None:
                bbox = transform_bbox(bbox, transform)
            surfaces.append({
                "id": elem.Id.IntegerValue,
                "name": safe_name(elem),
                "bbox": bbox,
                "z": bbox.Max.Z if use_top_z else bbox.Min.Z,
                "source": source_name,
            })
        except Exception:
            continue
    return surfaces


def collect_surfaces(doc, links, builtin_category, use_top_z):
    """
    收集楼板或吊顶：当前模型 + 所有链接模型，全部换算到宿主坐标后放在一起。
    链接里的楼板同样取变换后包围盒的 Max.Z / Min.Z。
    """
    surfaces = _collect_surfaces_in_doc(doc, None, builtin_category, use_top_z,
                                        u"本模型")

    if INCLUDE_LINKED_SURFACES:
        for link in links:
            surfaces.extend(
                _collect_surfaces_in_doc(link["doc"], link["transform"],
                                         builtin_category, use_top_z,
                                         link["name"]))
    return surfaces


def find_nearest_support(beam_bbox, beam_bottom_z, surfaces):
    """
    在候选楼板/吊顶（已全部换算到宿主坐标）中找出梁下方"最近"的一块：
      1) 标高 <= 梁底 + 容差的；
      2) 优先 XY 包住梁中心的，其次与梁投影重叠的；
      3) 再取标高最高（离梁底最近）的，同标高取重叠面积大的。
    """
    if not surfaces or beam_bbox is None or beam_bottom_z is None:
        return None

    center_x, center_y = bbox_center_xy(beam_bbox)
    tol_ft = mm_to_ft(Z_TOL_MM)

    containing = []
    overlapping = []

    for surface in surfaces:
        if surface["z"] > beam_bottom_z + tol_ft:
            continue
        if xy_contains(surface["bbox"], center_x, center_y):
            containing.append(surface)
        elif xy_overlap_area(beam_bbox, surface["bbox"]) > 0.0:
            overlapping.append(surface)

    candidates = containing if containing else overlapping
    if not candidates:
        return None

    candidates.sort(key=lambda s: (-s["z"],
                                   -xy_overlap_area(beam_bbox, s["bbox"])))
    return candidates[0]


# =============================================================================
# 七、文字类型
# =============================================================================

def resolve_text_type(doc, view):
    """取当前视图的默认文字类型；取不到则退回文档里第一个 TextNoteType"""
    text_type = None

    try:
        type_id = view.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)
        if type_id and type_id != DB.ElementId.InvalidElementId:
            text_type = doc.GetElement(type_id)
    except Exception:
        text_type = None

    if text_type is None:
        try:
            found = (DB.FilteredElementCollector(doc)
                     .OfClass(DB.TextNoteType)
                     .ToElements())
            if found:
                text_type = found[0]
        except Exception:
            text_type = None

    return text_type


def get_or_create_custom_text_type(doc, base_type):
    """查找或创建固定字高的文字类型（同名已存在则复用）。必须在事务内调用。"""
    try:
        for ttype in DB.FilteredElementCollector(doc).OfClass(DB.TextNoteType):
            if ttype.Name == CUSTOM_TEXT_TYPE_NAME:
                return ttype
    except Exception:
        pass

    if base_type is None:
        return None

    try:
        new_type = base_type.Duplicate(CUSTOM_TEXT_TYPE_NAME)
    except Exception:
        return base_type

    try:
        param = new_type.get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
        if param is not None and not param.IsReadOnly:
            param.Set(mm_to_ft(CUSTOM_TEXT_SIZE_MM))
    except Exception:
        pass

    return new_type


# =============================================================================
# 八、文字标注：新建 / 查找旧标注 / 更新
# =============================================================================

def create_text_note(doc, view, position, text, text_type_id):
    """
    在【宿主文档的当前视图】创建文字标注。
    依次尝试多种 API 重载，并按需把 Z 换成视图标高重试。
    """
    positions = [position]

    try:
        level = get_view_level(view)
        alt_z = level.Elevation if level is not None else view.Origin.Z
        if abs(position.Z - alt_z) > 1e-6:
            positions.append(DB.XYZ(position.X, position.Y, alt_z))
    except Exception:
        pass

    for pos in positions:
        # ---- 方式 1 ----
        try:
            note = DB.TextNote.Create(doc, view.Id, pos, text, text_type_id)
            if note is not None:
                return note
        except Exception:
            pass

        # ---- 方式 2 ----
        try:
            note = doc.Create.NewTextNote(
                view,
                pos,
                view.ViewDirection,
                view.RightDirection,
                view.UpDirection,
                mm_to_ft(TEXT_MAX_WIDTH_MM),
                DB.TextAlignFlags.TEF_ALIGN_CENTER,
                text,
                text_type_id)
            if note is not None:
                return note
        except Exception:
            pass

        # ---- 方式 3 ----
        try:
            options = DB.TextNoteOptions(text_type_id)
            options.HorizontalAlignment = DB.HorizontalTextAlignment.Center
            note = doc.Create.NewTextNote(
                view,
                pos,
                view.ViewDirection,
                view.RightDirection,
                view.UpDirection,
                mm_to_ft(TEXT_MAX_WIDTH_MM),
                DB.TextAlignFlags.TEF_ALIGN_CENTER,
                text,
                text_type_id,
                options)
            if note is not None:
                return note
        except Exception:
            pass

    return None


def collect_view_text_notes(doc, view):
    """
    收集当前视图中的全部 TextNote。
    TextNote 是视图专属元素；文字永远生成在宿主文档，所以只查宿主视图。
    """
    notes = []
    try:
        notes = list(DB.FilteredElementCollector(doc, view.Id)
                     .OfClass(DB.TextNote)
                     .ToElements())
        return notes
    except Exception:
        pass

    try:
        for note in DB.FilteredElementCollector(doc).OfClass(DB.TextNote).ToElements():
            try:
                if note.OwnerViewId == view.Id:
                    notes.append(note)
            except Exception:
                continue
    except Exception:
        pass
    return notes


def text_note_position(note):
    """取文字标注的位置点：优先插入点 Coord，取不到时退回包围盒中心"""
    try:
        coord = note.Coord
        if coord is not None:
            return coord
    except Exception:
        pass

    try:
        bbox = note.get_BoundingBox(None)
        if bbox is not None:
            return DB.XYZ((bbox.Min.X + bbox.Max.X) / 2.0,
                          (bbox.Min.Y + bbox.Max.Y) / 2.0,
                          (bbox.Min.Z + bbox.Max.Z) / 2.0)
    except Exception:
        pass
    return None


def note_text_safe(note):
    """安全读取 TextNote.Text；读不到（编组/异常）返回空字符串"""
    try:
        return note.Text or u""
    except Exception:
        return u""


def looks_like_clearance_note(note):
    """内容是否包含"净高"或"距地"，避免误改用户自己的注记"""
    text = note_text_safe(note)
    for keyword in NOTE_KEYWORDS:
        if keyword in text:
            return True
    return False


def match_notes_to_results(results, notes):
    """
    把"每根梁的期望插入点"和"视图里已有的标注"做全局就近配对。
    先生成所有 (距离, 梁序号, 文字) 候选对，按距离从近到远依次认领，
    保证每根梁最多领一条、每条文字最多被领一次。
    """
    matches = {}
    if not notes:
        return matches

    max_dist = mm_to_ft(MATCH_DIST_MM)

    note_positions = []
    for note in notes:
        pos = text_note_position(note)
        if pos is not None:
            note_positions.append((note, pos))

    pairs = []
    for r in results:
        target = r.get("text_pos")
        if target is None or r.get("error"):
            continue
        for note, pos in note_positions:
            distance = xy_distance(target, pos)
            if distance <= max_dist:
                pairs.append((distance, r["index"], note))

    pairs.sort(key=lambda item: item[0])

    used_results = set()
    used_notes = set()
    for distance, index, note in pairs:
        note_id = note.Id.IntegerValue
        if index in used_results or note_id in used_notes:
            continue
        used_results.add(index)
        used_notes.add(note_id)
        matches[index] = note

    return matches


def update_text_note(note, new_text):
    """
    更新已有标注的文字内容。
    返回 u"已更新" / u"无变化"；锁定、编组、只读等情况会抛异常，由调用方捕获。
    """
    old_text = note.Text
    if old_text == new_text:
        return u"无变化"
    note.Text = new_text
    return u"已更新"


# =============================================================================
# 九、选择集过滤
# =============================================================================

def is_structural_beam(element):
    """
    判断元素是否为结构梁：族实例 + 结构框架类别 + StructuralType.Beam。
    对链接文档里的元素同样成立（Category / StructuralType 与文档无关）。
    """
    if element is None:
        return False

    if not isinstance(element, DB.FamilyInstance):
        return False

    try:
        if element.Category is None:
            return False
        if element.Category.Id.IntegerValue != int(DB.BuiltInCategory.OST_StructuralFraming):
            return False
    except Exception:
        return False

    try:
        structural_type = element.StructuralType
    except Exception:
        return False

    if StEnum is not None:
        try:
            return structural_type == StEnum.Beam
        except Exception:
            pass

    try:
        return str(structural_type) == "Beam"
    except Exception:
        return False


# =============================================================================
# 十、单根梁计算
# =============================================================================

def compute_beam_result(index, beam_info, view, floors, ceilings, levels):
    """
    计算单根梁的净高，返回结果字典。

    所有几何量都已换算到宿主（机电模型）世界坐标：
      · 链接梁的包围盒经过 GetTotalTransform() 变换
      · 楼板 / 吊顶的包围盒同样经过各自链接的变换
    因此这里的减法不受两个模型坐标系差异的影响。

    任何异常都记录在 "error" 里，不向上抛。
    """
    result = {
        "index": index,
        "beam": beam_info["element"],
        "beam_id": beam_info.get("element_id", -1),
        "name": u"",
        "source_name": beam_info.get("source_name") or u"本模型",
        "is_linked": beam_info.get("is_linked", False),
        "bottom_z": None,
        "ref_z": None,
        "ref_source": None,
        "floor_name": None,
        "floor_source": None,
        "ceiling_z": None,
        "ceiling_name": None,
        "ceiling_source": None,
        "hh_floor": None,
        "hh_ceiling": None,
        "text_pos": None,
        "note_id": None,
        "note_state": None,
        "warning": None,
        "error": None,
    }

    result["name"] = safe_name(beam_info["element"])

    try:
        # ---------------- 1. 梁底绝对 Z（宿主坐标） ----------------
        bbox = get_world_bbox(beam_info)
        if bbox is None:
            raise Exception(u"无法获取梁的包围盒（可能是链接未载入）")
        bottom_z = bbox.Min.Z
        result["bottom_z"] = bottom_z

        # ---------------- 2. 参考面（地面） ----------------
        floor = find_nearest_support(bbox, bottom_z, floors)
        if floor is not None:
            result["ref_z"] = floor["z"]
            result["ref_source"] = u"楼板"
            result["floor_name"] = floor["name"]
            result["floor_source"] = floor.get("source")
        else:
            view_level = get_view_level(view)
            if view_level is not None:
                result["ref_z"] = view_level.Elevation
                result["ref_source"] = u"视图标高"
                result["floor_name"] = safe_name(view_level)
                result["warning"] = u"未找到楼板，已用视图标高兜底"
            else:
                fallback_level = nearest_level_below(levels, bottom_z)
                if fallback_level is not None:
                    result["ref_z"] = fallback_level.Elevation
                    result["ref_source"] = u"最近标高"
                    result["floor_name"] = safe_name(fallback_level)
                    result["warning"] = u"未找到楼板且视图无关联标高，已用最近标高兜底"

        if result["ref_z"] is not None:
            result["hh_floor"] = ft_to_mm(bottom_z - result["ref_z"])

        # ---------------- 3. 吊顶（可选） ----------------
        ceiling = find_nearest_support(bbox, bottom_z, ceilings)
        if ceiling is not None:
            result["ceiling_z"] = ceiling["z"]
            result["ceiling_name"] = ceiling["name"]
            result["ceiling_source"] = ceiling.get("source")
            result["hh_ceiling"] = ft_to_mm(bottom_z - ceiling["z"])

        # ---------------- 4. 文字插入点（宿主坐标，用于宿主视图） ----------------
        center_x, center_y = bbox_center_xy(bbox)
        if TEXT_OFFSET_DOWN_MM:
            center_y -= mm_to_ft(TEXT_OFFSET_DOWN_MM)

        if TEXT_Z_MODE == "BELOW_BEAM":
            text_z = bottom_z - mm_to_ft(TEXT_Z_BELOW_BEAM_MM)
        else:
            view_level = get_view_level(view)
            text_z = view_level.Elevation if view_level is not None else bottom_z

        result["text_pos"] = DB.XYZ(center_x, center_y, text_z)

    except Exception as ex:
        result["error"] = u"{0}".format(ex)

    return result


def build_note_text(result):
    """
    组装文字内容：
      有参考面 -> "净高: 3200"
      无参考面 -> "距地: 未知"
      有吊顶   -> 追加 " | 距顶: 2600"
    """
    if result["hh_floor"] is None:
        parts = [u"距地: 未知"]
    else:
        parts = [u"净高: {0}".format(fmt_mm(result["hh_floor"]))]

    if result["hh_ceiling"] is not None:
        parts.append(u"距顶: {0}".format(fmt_mm(result["hh_ceiling"])))

    return u" | ".join(parts)


# =============================================================================
# 十一、输出
# =============================================================================

def print_summary(results):
    """在 pyRevit 输出窗口打印结果表"""
    output = script.get_output()

    rows = []
    for r in results:
        if r["bottom_z"] is None:
            rows.append([r["index"], r["beam_id"], r["source_name"], r["name"],
                         u"-", u"-", u"-", u"-", u"-", u"-",
                         u"失败：{0}".format(r["error"] or u"未知错误")])
            continue

        if r["note_state"]:
            note_state = r["note_state"]
        elif r["error"]:
            note_state = u"失败"
        else:
            note_state = u"-"

        ref_cell = fmt_mm(ft_to_mm(r["ref_z"])) if r["ref_z"] is not None else u"未找到"
        if r["ref_source"]:
            ref_cell += u" ({0})".format(r["ref_source"])

        rows.append([
            r["index"],
            r["beam_id"],
            r["source_name"],
            r["name"],
            fmt_mm(ft_to_mm(r["bottom_z"])),
            ref_cell,
            fmt_mm(r["hh_floor"]),
            fmt_mm(ft_to_mm(r["ceiling_z"])) if r["ceiling_z"] is not None else u"无",
            fmt_mm(r["hh_ceiling"]),
            note_state,
        ])

    output.print_md(u"### 梁净高计算结果（支持链接模型）")
    output.print_table(
        table_data=rows,
        columns=[u"#", u"梁 ID", u"来源模型", u"族/类型", u"梁底 (mm)",
                 u"参考面 (mm)", u"距地净高 (mm)", u"吊顶底 (mm)",
                 u"距顶净高 (mm)", u"文字"],
    )
    output.print_md(
        u"**说明**：所有坐标已通过链接变换换算到当前模型世界坐标；"
        u"距地净高 = 梁底 − 参考面，参考面优先取梁下方楼板顶（含链接模型），"
        u"无楼板时取视图关联标高。")

    fallback_count = sum(1 for r in results if r["warning"])
    if fallback_count:
        output.print_md(
            u"⚠️ 有 **{0}** 根梁未找到下方楼板，已使用标高兜底，请人工复核。"
            .format(fallback_count))

    errors = [r for r in results if r["error"]]
    if errors:
        output.print_md(u"**错误明细**")
        for r in errors:
            output.print_md(
                u"- 第 {0} 根（ID {1} / {2}）：{3}"
                .format(r["index"], r["beam_id"], r["name"], r["error"]))


# =============================================================================
# 十二、主流程
# =============================================================================

MODE_VIEW_ONLY = u"仅弹窗查看结果"
MODE_CREATE_TEXT = u"生成文字标注并查看"


def main():
    doc = revit.doc
    uidoc = revit.uidoc
    view = doc.ActiveView

    # ---------- 1. 视图校验 ----------
    if not isinstance(view, DB.ViewPlan):
        forms.alert(
            u"请在【平面视图】中运行本工具。\n\n当前视图：{0}".format(safe_name(view)),
            title=u"梁净高")
        return

    if view.IsTemplate:
        forms.alert(u"当前视图是视图样板，无法进行标注。\n请切换到普通平面视图。",
                    title=u"梁净高")
        return

    # ---------- 2. 链接模型清单 ----------
    links = collect_link_sources(doc)
    link_cache = {}
    for link in links:
        link_cache[link["id"]] = link

    if not links:
        script.get_output().print_md(
            u"提示：当前文档中没有已载入的链接模型，只处理本模型的梁/楼板。")

    # ---------- 3. 收集梁：当前模型 + 链接模型 ----------
    beams = []
    seen = set()

    def add_beam(info):
        if info is None:
            return
        if info["uid"] in seen:
            return
        seen.add(info["uid"])
        beams.append(info)

    # 3.1 当前选择集（当前模型中的梁）
    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        selected_ids = []

    selected_link_instances = []
    for element_id in selected_ids:
        try:
            element = doc.GetElement(element_id)
        except Exception:
            element = None

        if isinstance(element, DB.RevitLinkInstance):
            selected_link_instances.append(element)
            continue

        if is_structural_beam(element):
            add_beam(make_beam_info(element, None, False, u"本模型", None))

    # 3.2 选择集里没有梁 -> 提示到链接里点选
    picked_count = 0
    if not beams:
        refs = pick_linked_refs(uidoc)
        picked_count = len(refs)
        for reference in refs:
            add_beam(beam_info_from_reference(reference, link_cache))

    if not beams:
        if picked_count:
            forms.alert(
                u"在链接模型里选中的 {0} 个图元中，没有找到结构梁。\n\n"
                u"请确认：\n"
                u"· 选中的是结构框架类别、且 StructuralType 为 Beam；\n"
                u"· 链接模型已经载入（不是未载入状态）。".format(picked_count),
                title=u"梁净高")
        else:
            forms.alert(
                u"未选中可处理的结构梁。\n\n"
                u"请先选中当前模型中的梁，或者在提示时到【链接模型】里点选梁。",
                title=u"梁净高")
        return

    # ---------- 4. 预收集楼板 / 吊顶（当前模型 + 所有链接） ----------
    floors = collect_surfaces(doc, links, DB.BuiltInCategory.OST_Floors, use_top_z=True)
    ceilings = collect_surfaces(doc, links, DB.BuiltInCategory.OST_Ceilings, use_top_z=False)
    levels = collect_levels(doc)

    view_level = get_view_level(view)
    if view_level is None:
        script.get_output().print_md(
            u"⚠️ 当前视图没有关联标高（GenLevel），楼板缺失时将改用最近标高兜底。")

    # ---------- 5. 逐根计算 ----------
    results = []
    for i, beam_info in enumerate(beams):
        results.append(
            compute_beam_result(i + 1, beam_info, view, floors, ceilings, levels))

    # ---------- 6. 选择模式 ----------
    linked_count = sum(1 for b in beams if b["is_linked"])
    message = u"共 {0} 根结构梁（本模型 {1} 根，链接模型 {2} 根），请选择后续操作：" \
        .format(len(beams), len(beams) - linked_count, linked_count)

    choice = forms.CommandSwitchWindow.show(
        [MODE_VIEW_ONLY, MODE_CREATE_TEXT],
        message=message,
        title=u"梁净高")

    if choice is None:
        return    # 用户关闭窗口

    # ---------- 7. 模式 A：只查看 ----------
    if choice == MODE_VIEW_ONLY:
        print_summary(results)
        return

    # ---------- 8. 模式 B：生成 / 更新文字标注 ----------
    created = 0
    updated = 0
    unchanged = 0
    failed = 0
    skipped = 0

    # 文字始终写入宿主（机电）文档
    transaction = DB.Transaction(doc, u"生成梁净高标注")
    transaction.Start()
    try:
        text_type = resolve_text_type(doc, view)
        if USE_CUSTOM_TEXT_TYPE:
            custom = get_or_create_custom_text_type(doc, text_type)
            if custom is not None:
                text_type = custom

        if text_type is None:
            raise Exception(u"当前视图/项目中没有可用的文字类型（TextNoteType）")

        # ---- 8.1 已有标注配对 ----
        matches = {}
        if UPDATE_EXISTING_TEXT:
            view_notes = collect_view_text_notes(doc, view)
            candidate_notes = [n for n in view_notes if looks_like_clearance_note(n)]
            matches = match_notes_to_results(results, candidate_notes)

        # ---- 8.2 逐根处理 ----
        for r in results:
            if r["error"]:
                continue
            if SKIP_IF_NO_REFERENCE and r["hh_floor"] is None:
                skipped += 1
                continue

            note_text = build_note_text(r)
            old_note = matches.get(r["index"])

            # ---------- 更新已有标注 ----------
            if old_note is not None:
                try:
                    change = update_text_note(old_note, note_text)
                    r["note_id"] = old_note.Id.IntegerValue
                    r["note_state"] = change
                    if change == u"无变化":
                        unchanged += 1
                    else:
                        updated += 1
                    continue
                except Exception as ex:
                    r["error"] = (u"已有标注更新失败（可能被锁定或位于编组中），"
                                  u"已跳过新建以避免重复：{0}").format(ex)
                    failed += 1
                    continue

            # ---------- 新建标注 ----------
            try:
                note = create_text_note(doc, view, r["text_pos"], note_text,
                                        text_type.Id)
                if note is None:
                    r["error"] = u"文字标注创建失败"
                    failed += 1
                else:
                    r["note_id"] = note.Id.IntegerValue
                    r["note_state"] = u"已生成"
                    created += 1
            except Exception as ex:
                r["error"] = u"文字标注失败：{0}".format(ex)
                failed += 1

        transaction.Commit()

    except Exception as ex:
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                transaction.RollBack()
        except Exception:
            pass
        forms.alert(u"生成文字标注时发生错误：\n\n{0}".format(ex),
                    title=u"梁净高",
                    warn_icon=True)
        print_summary(results)
        return

    # ---------- 9. 汇总 ----------
    output = script.get_output()
    print_summary(results)

    summary = u"新建 **{0}** 处".format(created)
    if UPDATE_EXISTING_TEXT:
        summary += u"，更新 **{0}** 处".format(updated)
        if unchanged:
            summary += u"，内容未变 **{0}** 处".format(unchanged)
    if failed:
        summary += u"，失败 **{0}** 处".format(failed)
    if skipped:
        summary += u"，跳过 **{0}** 根".format(skipped)
    summary += u"；文字已生成在当前模型（{0}）".format(safe_name(doc))
    output.print_md(summary + u"。")


# =============================================================================
# 入口
# =============================================================================
if __name__ == "__main__":
    try:
        main()
    except Exception:
        forms.alert(
            u"脚本运行出错：\n\n{0}".format(traceback.format_exc()),
            title=u"梁净高",
            warn_icon=True)
