# Revit pyRevit Tools

A collection of pyRevit tools for Autodesk Revit, built to handle repetitive modeling tasks.

**Other languages / 其他语言:** [中文](./README.md)

---

## Tools

| Tool | Description |
|:---|:---|
| [Extend Walls and Beams](./01-延伸墙梁.pushbutton/) | Box-select walls or beams and batch-extend them to a target |
| [Column Top Elevation Align](./02-柱顶标高对齐.pushbutton/) | Input an absolute elevation (e.g., 10.32m) to instantly align the tops of selected columns. Perfect for irregular roofs. |

> More tools are on the way. Star this repo to stay updated.

---

## Requirements

- **Autodesk Revit** 2020
- **pyRevit** 6.5.3+
- **Windows**

> Other Revit versions are untested; 2020-2024 should work in theory.

---

## Installation

### Prerequisite: Install pyRevit

If you don't have pyRevit yet, download it from:
https://www.pyrevitlabs.io/

Choose the **Admin / All Users** version during installation.

### Install a Tool

1. **Download the tool folder**
   In this repo, click the tool folder (e.g. `延伸墙梁.pushbutton`), then `Code` → `Download ZIP`.

2. **Copy to pyRevit extensions folder**
   Copy the extracted `.pushbutton` folder to:
   %APPDATA%\pyRevit\Extensions\
   Create the `Extensions` folder if it doesn't exist.

3. **Reload in Revit**
Open Revit, go to the `pyRevit` tab → click **`Reload`**.

4. **Find your tool**
The tool will appear in the `pyRevit` tab. If you don't have a custom panel, it will be in the default panel.

### Troubleshooting

- Button not showing: Check the `.pushbutton` folder location and Reload again.
- Error on click: Open `Output Window` in the `pyRevit` tab and post the red error in Issues.

---

## About

- **Bilibili**: https://space.bilibili.com/1562942194
- **GitHub**: https://github.com/xiaotang669

---

## License

MIT
