# Revit pyRevit Tools

一套基于 pyRevit 的 Revit 工具集，用于处理日常建模中的重复性操作。

**其他语言 / Other languages:** [English](./README.en.md)

---

## 工具列表

| 工具 | 说明 |
|:---|:---|
| [延伸墙梁 / Extend Walls and Beams](./延伸墙梁.pushbutton/) | 框选一批墙或梁，点选目标构件，自动批量延伸 |
| [柱顶标高对齐/ Column Top Elevation Align] (./柱顶标高对齐.pushbutton/) | 输入绝对标高（如 10.32m），一键将选中的结构柱顶部齐平，专治异形屋面。 |

> 更多工具正在开发中，欢迎 Star 关注更新。

---

## 环境要求

- **Autodesk Revit** 2020
- **pyRevit** 6.5.3 或更高版本
- **Windows**

> 其他 Revit 版本未测试，理论上 2020-2024 都可用。

---

## 安装方法

### 前置：安装 pyRevit

如果你还没有 pyRevit，先去官网下载安装：
https://www.pyrevitlabs.io/

安装时选择 **Admin / All Users** 版本，一路 Next 即可。

### 安装工具

1. **下载工具文件夹**
   在本仓库里，点击你要用的工具文件夹（比如 `延伸墙梁.pushbutton`），点右上角绿色 `Code` 按钮 → `Download ZIP`。

2. **放到 pyRevit 扩展目录**
   把解压出来的 `.pushbutton` 文件夹，复制到：
   %APPDATA%\pyRevit\Extensions\
   如果 `Extensions` 文件夹不存在，手动新建一个。

4. **在 Revit 里加载**
打开 Revit，点击顶部 `pyRevit` 选项卡 → 点击 **`Reload`** 按钮。

5. **找到你的工具**
工具会出现在 `pyRevit` 选项卡下的自定义面板里。
如果你没有自定义面板，工具会出现在 `pyRevit` 的默认面板中。

### 遇到问题？

- 按钮没出现：检查 `.pushbutton` 文件夹是否放对了位置，然后重新 Reload。
- 点击报错：打开 `pyRevit` 选项卡下的 `Output Window`，把红色报错信息截图发到 Issues。

---

## 关于作者

- **B站主页**：https://space.bilibili.com/1562942194
- **GitHub**：https://github.com/xiaotang669
- 视频教程和开发记录都会发布在 B站，欢迎关注。

---

## 开源协议

MIT
