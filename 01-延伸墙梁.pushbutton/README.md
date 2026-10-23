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

---

## 已知限制 / Known Limitations

- 机电类构件不支持 / MEP elements are not supported
- 梁的 LocationCurve setter 在部分环境下可能不可用 / The LocationCurve setter for beams may be read-only in some environments
- 缩短构件的操作默认被拒绝（保护门窗） / Shortening operations are refused by default (to protect openings)

---

## 开源协议 / License

MIT
