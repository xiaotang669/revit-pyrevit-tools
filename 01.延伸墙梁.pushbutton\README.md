# 延伸墙梁 / Extend Walls and Beams

框选一批墙或梁，点选目标构件，自动批量延伸。

Box-select multiple walls or beams, pick a target, and batch-extend them.

---

## 功能 / Features

- 框选一批墙或梁 / Box-select multiple walls or beams
- 点选一个目标构件 / Pick one target element
- 自动延伸，无需逐个操作 / Batch extend, no one-by-one picking
- 自动保护墙上的门窗 / Automatically protects doors and windows on walls
- 自动跳过机电类构件 / Skips MEP elements (pipes, ducts, etc.)

---

## 使用方法 / Usage

1. 点击按钮 / Click the button
2. 框选要延伸的墙或梁，按 **Enter** 确认 / Box-select walls or beams, press **Enter**
3. 点选一个目标构件 / Pick a target element
4. 脚本自动处理，弹窗显示结果 / The script runs and shows a result dialog

   弹窗内容示例 /
Example output:
Extended : 4
onto the target : 3
onto its ext. line: 1
Openings moved back : 0
Skipped : 0
Failed : 0

---

## 已知限制 / Known Limitations

### 1. 墙连接导致偶发失败

如果要延伸的墙，**要移动的那一端和相邻墙有连接**，Revit 可能会拒绝修改定位线，报「不能生成墙」的错误。

- **表现**：脚本处理到某面墙时报错，那面墙保持不变，其他墙正常处理。
- **临时解决**：保存文件，重启 Revit，再跑一次。多数情况下能成功。
- **根因**：Revit 内部的墙连接约束，与脚本无关。目前无法稳定复现和修复。
- **相关**：如果你的墙上有门窗，请同时阅读第 10 条。

### 2. 机电类构件不支持

管道、风管、电缆桥架、线管等 MEP 构件一律跳过，不参与延伸。脚本会以 `[skipped] 构件ID: unsupported category` 的形式报告。

延伸机电构件涉及管件生成、系统拓扑、连接关系重建等复杂逻辑，超出本脚本的设计范围。如有此类需求，建议使用专业的 MEP 插件（如品茗、橄榄山等）处理。

### 3. 只处理直线构件

弧形墙、弧形梁、曲线定位的构件全部跳过。脚本只处理定位线为 `Line` 的构件。

### 4. 组内构件跳过

位于 Revit 组（Group）内的墙或梁，脚本会跳过并报告 `[skipped] 构件ID: inside a group`。因为组内构件不能单独修改。

### 5. 锁定构件跳过

被锁定（Pinned）的构件跳过，报告 `[skipped] 构件ID: pinned`。

### 6. 非基本墙跳过

`WallType.Kind` 不是 `Basic` 的墙（如叠层墙、幕墙）跳过，报告 `[skipped] 构件ID: not a basic wall`。

### 7. 默认拒绝缩短操作

如果延伸后新长度比原长度短，脚本会默认拒绝并报告 `[skipped] would shorten the element`。这是为了保护墙上的门窗不被删除。如需允许缩短，把脚本开头的 `REFUSE_TO_SHORTEN` 改成 `False`。

### 8. 梁的定位线写入可能失败

`LocationCurve` 的 setter 只在墙构件上验证过。部分环境下，梁的定位线可能是只读的。如果 Revit 拒绝写入，每根梁会以 `[failed] 构件ID: 具体错误` 出现，不影响墙的处理。

### 9. Output Window 标签堆积

每次运行脚本，pyRevit 的 Output Window 都会**新开一个标签**。跑多次后会堆成一排。

- **不影响功能**，只是视觉上碍眼。
- **临时解决**：右键标签栏 → `Close All`，或直接关掉整个 Output Window。
- **根因**：这是 pyRevit 自身的窗口行为，脚本层面无法修改。

### 10. 门窗会移动回原位，但可能被删

延伸墙体时，脚本会记录墙上门窗的世界坐标，延伸后把它们搬回原位。但如果延伸导致门窗**超出墙的新范围**，Revit 会删除它。脚本检测到门窗消失后会回滚这面墙，并报告 `[failed]`。

---

## 遇到问题怎么办

- **按钮没出现**：检查 `.pushbutton` 文件夹是否放对了位置，然后在 pyRevit 选项卡点击 `Reload`。
- **点击报错**：打开 pyRevit 选项卡下的 `Output Window`，把红色报错信息截图发到 [Issues](https://github.com/xiaotang669/revit-pyrevit-tools/issues)。
- **偶发失败**：先尝试保存文件 → 重启 Revit → 重新运行。很多问题重启就能解决。

---

## 开源协议 / License

MIT
