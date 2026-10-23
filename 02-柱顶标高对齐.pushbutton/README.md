# 柱顶标高批量驱动 / Column Top Elevation Align

输入绝对标高，一键将选中的结构柱顶部精准对齐到该高度（专治异形屋面，告别手动计算偏移量）。

Input an absolute elevation to precisely align the tops of selected structural columns (perfect for irregular roofs, no more manual offset calculations).

---

## 核心特性 / Features

- **专治异形屋面**：无论柱底在哪个标高，直接输入绝对高度（如 10.32m），柱顶瞬间齐平。
  Designed for irregular roofs: Regardless of the base level, input the absolute height (e.g., 10.32m) and all selected column tops will align instantly.
- **智能双重驱动**：自动识别柱子是“顶部约束”还是“无连接高度”，采用不同的参数（顶部偏移/实例长度）进行计算，防止标高算错。
  Smart dual-driving: Automatically detects whether the column uses a top constraint or unconnected height, applying the correct parameter calculation to prevent elevation errors.
- **毫米级复核**：修改后自动回读实际标高，偏差超过 0.1mm 自动报告失败，绝不“假成功”。
  Millimeter-level verification: Automatically reads back the actual elevation after modification. If the deviation exceeds 0.1mm, it reports a failure, ensuring no "false successes."
- **强制几何刷新**：内置 `doc.Regenerate()`，解决 API 修改参数后模型不更新的痛点。
  Forces geometry regeneration: Solves the issue of models not updating after API parameter changes.

---

## 使用方法 / Usage

1. 在 pyRevit 面板中点击本工具按钮。
   Click the tool button in the pyRevit panel.
2. 在弹出的对话框中输入目标顶标高（单位：米，例如 `10.32` 或 `10.32m`）。
   Enter the target top elevation (e.g., `10.32` or `10.32m`).
3. 框选需要处理的**结构柱**，按回车确认。
   Box-select the structural columns to process and press Enter.
4. 脚本运行完毕后，会弹窗报告成功和跳过的数量。
   A dialog will show the results after the script finishes.

---

## 已知限制 / Known Limitations

- **仅支持结构柱**：选中的图元如果是建筑柱或其他类别，会自动跳过。
  Only supports structural columns. Architectural columns or other categories will be skipped.
- **跳过锁定构件**：如果柱子被锁定（Pinned），脚本会跳过并报告。
  Pinned columns are skipped and reported.
- **基于绝对标高**：此脚本不负责修改柱子的“底部约束”，仅通过调整顶部偏移或长度来改变物理高度。
  Based on absolute elevations: This script does not modify the base level constraint, only adjusts top offsets or lengths to change physical heights.

---

## 开源协议 / License

MIT
