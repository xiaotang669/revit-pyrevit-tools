# -*- coding: utf-8 -*-
# ============================================================
#  Author  : xiaotang669
#  GitHub  : https://github.com/xiaotang669/revit-pyrevit-tools
#  Bilibili: https://space.bilibili.com/1562942194
#  本项目开源免费。如果你是在其他地方付费购买的，说明你被骗了。
# ============================================================
"""
===============================================================================
 梁净高查看 / 标注工具
 Revit 2020 + pyRevit + IronPython 2.7
-------------------------------------------------------------------------------
 使用方式：
   1. 在【平面视图】中选中若干结构梁（StructuralType.Beam）；
   2. 运行本按钮；
   3. 弹窗选择：
        A. 仅弹窗查看结果     —— 只输出汇总表，不改动模型
        B. 生成文字标注并查看 —— 生成 / 更新当前平面视图中的净高文字

 计算优先级（严格按需求实现）：
   ┌ 1. 梁底绝对 Z = 梁的 BoundingBox.Min.Z
   │     倾斜梁时包围盒 Min.Z 即"正下方最低点"，可直接用于净高判断。
   │
   ├ 2. 参考面（地面）标高，兜底链：
   │     a) 梁正下方的楼板 —— XY 包含梁中心点、或与梁投影重叠，
   │        且在梁底以下（容差内）的楼板中，取板顶 Z 最高的那块
   │        （即离梁底最近的楼板，取 BoundingBox.Max.Z）；
   │     b) 找不到楼板 → 取 doc.ActiveView.GenLevel.Elevation（视图关联标高）；
   │     c) GenLevel 为空（非楼层面视图）→ 取梁底以下最近的 Level。
   │
   ├ 3. 吊顶：在梁下方找 Ceiling，取 BoundingBox.Min.Z（吊顶完成面）。
   │     找不到就直接忽略"距顶"，不参与兜底。
   │
   └ 4. 距地净高 = 梁底 Z - 参考面 Z
         距顶净高 = 梁底 Z - 吊顶底面 Z

 防重复 / 更新已有标注（模式 B）：
   开启事务后先收集当前视图里所有 TextNote（只挑内容含"净高"或"距地"的，
   避免误改用户自己的文字），把每根梁的文字插入点与这些标注按距离做
   全局就近配对（阈值 MATCH_DIST_MM，默认 1000mm）：
     · 配对成功 → 直接修改 TextNote.Text，不新建
     · 配对失败 → 按原有逻辑新建
   这样反复运行脚本不会在同一位置堆叠多层文字，梁高改了再跑一次即可刷新。

 典型场景校验：
     楼层标高 4.0m，梁顶 8.0m，梁高 800mm
     → 梁底 = 8.0 - 0.8 = 7.2m
     → 距地净高 = 7.2 - 4.0 = 3.2m = 3200mm   ✔ 输出"净高: 3200"

 文字 Z 的两种策略（见 TEXT_Z_MODE）：
     VIEW_LEVEL  —— 直接放当前视图标高（推荐，等同于放在楼面上）
     BELOW_BEAM  —— 梁底再往下 100mm
===============================================================================
"""

__title__ = u"梁净高"
__doc__ = u"查看选中结构梁的净高（距楼板 / 距吊顶），可生成/更新文字标注。"
__author__ = u"Cherry Assistant"
# 只有选中元素后按钮才可用；若希望任何时候都能点，删掉下面这一行
__context__ = "Selection"

import math
import traceback

from pyrevit import forms, script, revit, DB


# =============================================================================
# 零、StructuralType 枚举寻址
# -----------------------------------------------------------------------------
# Revit 2020 里 StructuralType 位于 Autodesk.Revit.DB.Structure 命名空间，
# 直接写 DB.StructuralType 在部分 IronPython 环境下会报 AttributeError。
# 这里做一次兼容解析，并在完全取不到时退化为字符串比较。
# =============================================================================

try:
    from Autodesk.Revit.DB.Structure import StructuralType as StEnum
except Exception:
    try:
        StEnum = DB.Structure.StructuralType
    except Exception:
        StEnum = None


# =============================================================================
# 一、可调参数
# =============================================================================

# Z 方向容差（mm）：楼板/吊顶标高只要不高于"梁底 + 容差"，就认为在梁下方。
Z_TOL_MM = 20.0

# 连参考面都找不到时（既无楼板、又无视图标高）是否跳过该梁。
SKIP_IF_NO_REFERENCE = False

# ---------------- 防重复 / 更新已有标注 ----------------
# 是否在模式 B 中更新已有标注而不是重复新建（强烈建议保持 True）
UPDATE_EXISTING_TEXT = True

# 判定"同一个标注"的最大平面距离（mm）。旧文字包围盒中心/插入点与
# 新计算的插入点距离小于该值，就认为它们指向同一根梁。
MATCH_DIST_MM = 1000.0

# 只更新内容包含以下关键字的文字，避免误改用户的其它注记。
NOTE_KEYWORDS = (u"净高", u"距地")

# ---------------- 文字 Z 位置 ----------------
# "VIEW_LEVEL" —— Z 取当前视图关联标高（放在楼面上，平面图里最自然）
# "BELOW_BEAM" —— Z 取梁底再往下 TEXT_Z_BELOW_BEAM_MM
TEXT_Z_MODE = "VIEW_LEVEL"
TEXT_Z_BELOW_BEAM_MM = 100.0

# 文字在平面上的额外偏移（正数 = 往平面下方挪，避免压住梁）
TEXT_OFFSET_DOWN_MM = 0.0

# 文本框宽度（mm）。给足宽度可避免文字被自动换行。
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
# 三、标高相关
# =============================================================================

def get_view_level(view):
    """
    取当前视图的关联标高（ViewPlan.GenLevel）。
    非楼层平面视图（三维、剖面等）可能返回 None。
    """
    try:
        level = view.GenLevel
        if level is not None:
            return level
    except Exception:
        pass
    return None


def collect_levels(doc):
    """收集项目里所有标高，按 Elevation 升序排列"""
    levels = []
    try:
        for level in DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements():
            levels.append(level)
    except Exception:
        return []
    levels.sort(key=lambda l: l.Elevation)
    return levels


def nearest_level_below(levels, z):
    """
    仅当视图没有 GenLevel 时使用：
    取梁底以下、离梁底最近的那个标高。
    """
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
# 四、收集楼板 / 吊顶
# =============================================================================

def collect_surfaces(doc, builtin_category, use_top_z):
    """
    一次性收集全模型的楼板或吊顶，缓存包围盒与参考标高：
      楼板 use_top_z=True  -> BoundingBox.Max.Z（板顶）
      吊顶 use_top_z=False -> BoundingBox.Min.Z（吊顶完成面 / 底面）

    注意：只搜当前文档，不处理链接模型里的楼板/吊顶。
    """
    surfaces = []
    try:
        elements = (DB.FilteredElementCollector(doc)
                    .OfCategory(builtin_category)
                    .WhereElementIsNotElementType()
                    .ToElements())
    except Exception:
        return surfaces

    for elem in elements:
        try:
            bbox = elem.get_BoundingBox(None)   # None = 模型范围，不受视图裁剪影响
            if bbox is None:
                continue
            surfaces.append({
                "id": elem.Id.IntegerValue,
                "name": safe_name(elem),
                "bbox": bbox,
                "z": bbox.Max.Z if use_top_z else bbox.Min.Z,
            })
        except Exception:
            continue
    return surfaces


def find_nearest_support(beam_bbox, beam_bottom_z, surfaces):
    """
    在候选楼板/吊顶中找出梁下方"最近"的一块：
      1) 只要标高 <= 梁底 + 容差的（真的在梁下方）；
      2) 优先 XY 包住梁中心的，其次与梁投影有重叠的；
      3) 再取标高最高（离梁底最近）的那块，同标高取重叠面积大的。
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
# 五、文字类型
# =============================================================================

def resolve_text_type(doc, view):
    """取当前视图的默认文字类型；取不到则退回项目里第一个 TextNoteType"""
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
# 六、文字标注：新建 / 查找旧标注 / 更新
# =============================================================================

def create_text_note(doc, view, position, text, text_type_id):
    """
    创建文字标注，依次尝试多种 API 重载：
      1) TextNote.Create(doc, viewId, position, text, typeId)
      2) doc.Create.NewTextNote(...)                      —— 旧版重载
      3) doc.Create.NewTextNote(..., TextNoteOptions)     —— 带对齐选项

    另外：若第一次失败，会把 Z 换成视图标高再试一次
    （平面视图里 Z 无视觉意义，但个别情况下传任意 Z 会报"点不在视图平面内"）。
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
    TextNote 是视图专属元素，用带 viewId 的收集器最直接；
    万一该重载不可用，再退回全模型收集并按 OwnerViewId 过滤。
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
    """
    取文字标注的位置点：
      优先用插入点 Coord；取不到时退回包围盒中心。
    """
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
    """文字内容是否包含"净高"或"距地"，用于避免误改用户自己的注记"""
    text = note_text_safe(note)
    for keyword in NOTE_KEYWORDS:
        if keyword in text:
            return True
    return False


def match_notes_to_results(results, notes):
    """
    把"每根梁的期望插入点"和"视图里已有的标注"做全局就近配对。

    做法：先生成所有 (距离, 梁序号, 文字) 候选对，按距离从近到远排序，
    再依次认领——每根梁最多领一条文字、每条文字最多被一根梁领走。
    这比"逐根梁各自找最近"更稳：密集梁区不会出现两根梁抢同一条文字。

    返回 {梁序号: TextNote}
    """
    matches = {}
    if not notes:
        return matches

    max_dist = mm_to_ft(MATCH_DIST_MM)

    # 预先算好每条文字的位置，避免重复取
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

    # 按距离升序认领（key 只用距离，避免比较 TextNote 对象）
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

    返回值：
      u"已更新" / u"无变化"
    以下情况会抛异常，由调用方捕获：
      · 标注被锁定 / 编组 / 只读
      · 标注所在视图被样板锁定
    """
    old_text = note.Text
    if old_text == new_text:
        return u"无变化"
    note.Text = new_text
    return u"已更新"


# =============================================================================
# 七、选择集过滤
# =============================================================================

def is_structural_beam(element):
    """
    判断元素是否为需要处理的结构梁。

    只认 StructuralType.Beam：
      Revit 里主梁、次梁在 API 中统一表现为 Beam；
      不再判断 Girder，也不做任何"主梁/次梁"的额外分支。
    """
    if element is None:
        return False

    # 1) 必须是族实例
    if not isinstance(element, DB.FamilyInstance):
        return False

    # 2) 类别必须是结构框架
    try:
        if element.Category is None:
            return False
        if element.Category.Id.IntegerValue != int(DB.BuiltInCategory.OST_StructuralFraming):
            return False
    except Exception:
        return False

    # 3) 取出 StructuralType
    try:
        structural_type = element.StructuralType
    except Exception:
        return False

    # 4) 与 Beam 比较（优先用枚举，取不到枚举时用名称比较兜底）
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
# 八、单根梁计算
# =============================================================================

def compute_beam_result(index, beam, doc, view, floors, ceilings, levels):
    """
    计算单根梁的净高，返回结果字典。

    参数：
      index    —— 显示用的序号（从 1 开始）
      beam     —— FamilyInstance（结构梁）
      doc      —— 当前文档
      view     —— 当前平面视图
      floors   —— collect_surfaces(OST_Floors, use_top_z=True) 的结果
      ceilings —— collect_surfaces(OST_Ceilings, use_top_z=False) 的结果
      levels   —— collect_levels(doc) 的结果

    任何异常都记录在 "error" 里，不向上抛，保证一根失败不影响其它梁。
    """
    result = {
        "index": index,
        "beam": beam,
        "beam_id": -1,
        "name": u"",
        "bottom_z": None,     # 内部单位（英尺）
        "ref_z": None,        # 参考面 Z（楼板顶 / 视图标高）
        "ref_source": None,   # u"楼板" / u"视图标高" / u"最近标高"
        "floor_name": None,
        "ceiling_z": None,
        "ceiling_name": None,
        "hh_floor": None,     # 距地净高（mm）
        "hh_ceiling": None,   # 距顶净高（mm）
        "text_pos": None,
        "note_id": None,
        "note_state": None,   # u"已生成" / u"已更新" / u"无变化"
        "warning": None,
        "error": None,
    }

    try:
        result["beam_id"] = beam.Id.IntegerValue
    except Exception:
        pass
    result["name"] = safe_name(beam)

    try:
        # ---------------- 1. 梁底绝对 Z ----------------
        # 需求指定：直接用 BoundingBox.Min.Z。
        # 对倾斜梁，包围盒 Min.Z 就是"正下方的最低点"，正是净高校核需要的值。
        bbox = beam.get_BoundingBox(None)
        if bbox is None:
            raise Exception(u"无法获取梁的包围盒")
        bottom_z = bbox.Min.Z
        result["bottom_z"] = bottom_z

        # ---------------- 2. 参考面（地面） ----------------
        floor = find_nearest_support(bbox, bottom_z, floors)
        if floor is not None:
            result["ref_z"] = floor["z"]
            result["ref_source"] = u"楼板"
            result["floor_name"] = floor["name"]
        else:
            # 兜底 a：视图关联标高
            view_level = get_view_level(view)
            if view_level is not None:
                result["ref_z"] = view_level.Elevation
                result["ref_source"] = u"视图标高"
                result["floor_name"] = safe_name(view_level)
                result["warning"] = u"未找到楼板，已用视图标高兜底"
            else:
                # 兜底 b：梁底以下最近的标高（三维/剖面视图等）
                fallback_level = nearest_level_below(levels, bottom_z)
                if fallback_level is not None:
                    result["ref_z"] = fallback_level.Elevation
                    result["ref_source"] = u"最近标高"
                    result["floor_name"] = safe_name(fallback_level)
                    result["warning"] = u"未找到楼板且视图无关联标高，已用最近标高兜底"

        if result["ref_z"] is not None:
            result["hh_floor"] = ft_to_mm(bottom_z - result["ref_z"])

        # ---------------- 3. 吊顶（可选，找不到就忽略） ----------------
        ceiling = find_nearest_support(bbox, bottom_z, ceilings)
        if ceiling is not None:
            result["ceiling_z"] = ceiling["z"]
            result["ceiling_name"] = ceiling["name"]
            result["hh_ceiling"] = ft_to_mm(bottom_z - ceiling["z"])

        # ---------------- 4. 文字插入点 ----------------
        center_x, center_y = bbox_center_xy(bbox)
        if TEXT_OFFSET_DOWN_MM:
            center_y -= mm_to_ft(TEXT_OFFSET_DOWN_MM)

        if TEXT_Z_MODE == "BELOW_BEAM":
            text_z = bottom_z - mm_to_ft(TEXT_Z_BELOW_BEAM_MM)
        else:
            # 默认：放在当前视图标高上（"平地上"）
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
# 九、输出
# =============================================================================

def print_summary(results):
    """在 pyRevit 输出窗口打印结果表"""
    output = script.get_output()

    rows = []
    for r in results:
        # 连梁底都算不出来 -> 整行标记为失败
        if r["bottom_z"] is None:
            rows.append([r["index"], r["beam_id"], r["name"],
                         u"-", u"-", u"-", u"-", u"-", u"-",
                         u"失败：{0}".format(r["error"] or u"未知错误")])
            continue

        # 文字状态
        if r["note_state"]:
            note_state = r["note_state"]
        elif r["error"]:
            note_state = u"失败"
        else:
            note_state = u"-"

        rows.append([
            r["index"],
            r["beam_id"],
            r["name"],
            fmt_mm(ft_to_mm(r["bottom_z"])),
            fmt_mm(ft_to_mm(r["ref_z"])) if r["ref_z"] is not None else u"未找到",
            r["ref_source"] or u"-",
            fmt_mm(r["hh_floor"]),
            fmt_mm(ft_to_mm(r["ceiling_z"])) if r["ceiling_z"] is not None else u"无",
            fmt_mm(r["hh_ceiling"]),
            note_state,
        ])

    output.print_md(u"### 梁净高计算结果")
    output.print_table(
        table_data=rows,
        columns=[u"#", u"梁 ID", u"族/类型", u"梁底 (mm)", u"参考面 (mm)",
                 u"参考来源", u"距地净高 (mm)", u"吊顶底 (mm)",
                 u"距顶净高 (mm)", u"文字"],
    )
    output.print_md(
        u"**说明**：距地净高 = 梁底(BoundingBox.Min.Z) − 参考面；"
        u"参考面优先取梁下方楼板顶，无楼板时取视图关联标高。"
        u"距顶净高为负表示梁底已低于吊顶。")

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
# 十、主流程
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

    # ---------- 2. 取选择集中的结构梁 ----------
    try:
        selected_ids = list(uidoc.Selection.GetElementIds())
    except Exception:
        selected_ids = []

    beams = []
    for element_id in selected_ids:
        try:
            element = doc.GetElement(element_id)
        except Exception:
            element = None
        if is_structural_beam(element):
            beams.append(element)

    if not beams:
        forms.alert(
            u"未选中可处理的结构梁。\n\n"
            u"请先选中结构框架中的梁（StructuralType.Beam）再运行。",
            title=u"梁净高")
        return

    # ---------- 3. 预收集（全模型只做一次，避免逐梁重复遍历） ----------
    floors = collect_surfaces(doc, DB.BuiltInCategory.OST_Floors, use_top_z=True)
    ceilings = collect_surfaces(doc, DB.BuiltInCategory.OST_Ceilings, use_top_z=False)
    levels = collect_levels(doc)

    view_level = get_view_level(view)
    if view_level is None:
        # 提前提示，避免用户看到一堆"最近标高"却不知道原因
        script.get_output().print_md(
            u"⚠️ 当前视图没有关联标高（GenLevel），楼板缺失时将改用最近标高兜底。")

    # ---------- 4. 逐根计算 ----------
    results = []
    for i, beam in enumerate(beams):
        results.append(
            compute_beam_result(i + 1, beam, doc, view, floors, ceilings, levels))

    # ---------- 5. 选择模式 ----------
    choice = forms.CommandSwitchWindow.show(
        [MODE_VIEW_ONLY, MODE_CREATE_TEXT],
        message=u"已选中 {0} 根结构梁，请选择后续操作：".format(len(beams)),
        title=u"梁净高")

    if choice is None:
        return    # 用户关闭窗口

    # ---------- 6. 模式 A：只查看 ----------
    if choice == MODE_VIEW_ONLY:
        print_summary(results)
        return

    # ---------- 7. 模式 B：生成 / 更新文字标注 ----------
    created = 0
    updated = 0
    unchanged = 0
    failed = 0
    skipped = 0

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

        # ---- 7.1 收集当前视图已有标注，做全局就近配对 ----
        # 只把"内容含 净高/距地"的文字纳入候选，避免误改用户自己的注记。
        matches = {}
        if UPDATE_EXISTING_TEXT:
            view_notes = collect_view_text_notes(doc, view)
            candidate_notes = [n for n in view_notes if looks_like_clearance_note(n)]
            matches = match_notes_to_results(results, candidate_notes)

        # ---- 7.2 逐根处理：能更新就更新，否则新建 ----
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
                    # 被锁定 / 编组 / 只读：不新建，避免又叠出一层重复文字
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
        # 整体失败 -> 回滚，模型保持干净
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

    # ---------- 8. 汇总 ----------
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
    if TEXT_Z_MODE == "VIEW_LEVEL":
        summary += u"；文字放置于视图标高"
    else:
        summary += u"；文字放置于梁底下方 {0}mm".format(int(TEXT_Z_BELOW_BEAM_MM))
    output.print_md(summary + u"。")


# =============================================================================
# 入口：任何未预期异常都弹出可读信息，而不是静默失败
# =============================================================================
if __name__ == "__main__":
    try:
        main()
    except Exception:
        forms.alert(
            u"脚本运行出错：\n\n{0}".format(traceback.format_exc()),
            title=u"梁净高",
            warn_icon=True)

